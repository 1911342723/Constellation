"""LLM Client — unified large-model calling interface.

Uses the OpenAI SDK as a universal transport layer, compatible with
DeepSeek / OpenAI / Claude and any OpenAI-protocol provider.

The module exposes a **singleton** accessor :func:`get_llm_client` so
that all consumers share a single ``httpx`` connection pool, avoiding
the per-request TCP handshake overhead that would otherwise occur
under concurrent load.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import asyncio
from collections import OrderedDict
from functools import lru_cache
from typing import Type, TypeVar

import httpx
import tiktoken
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

from app.core.exceptions import LLMRouterError
from infrastructure.ai.usage import record_llm_usage
from modules.parser.config import LLMClientConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Use a pinned, deterministic tokenizer for the complete outbound payload.
# OpenAI-compatible gateways can expose different models, so we also retain a
# conservative UTF-8 heuristic and take the larger value.  The extra configured
# safety margin covers residual differences from a provider-specific tokenizer.
_TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens using cl100k and a conservative UTF-8 fallback."""
    if not text:
        return 0

    word_chars = 0
    horizontal_space = 0
    newlines = 0
    punctuation = 0
    non_ascii_tokens = 0
    for char in text:
        if not char.isascii():
            # UTF-8 byte count is an upper bound for byte-fallback tokenizers,
            # including rare four-byte code points that may become 4 tokens.
            non_ascii_tokens += len(char.encode("utf-8"))
        elif char.isalnum() or char == "_":
            word_chars += 1
        elif char == "\n":
            newlines += 1
        elif char.isspace():
            horizontal_space += 1
        else:
            punctuation += 1

    heuristic = (
        math.ceil(word_chars / 3)
        + math.ceil(horizontal_space / 4)
        + newlines
        + math.ceil(punctuation * 2 / 3)
        + non_ascii_tokens
    )
    tokenized = len(_TOKEN_ENCODING.encode(text, disallowed_special=()))
    return max(tokenized, heuristic)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate chat input tokens, including a small per-message envelope."""
    total = 2  # assistant priming / request envelope
    for message in messages:
        total += 4
        total += estimate_text_tokens(str(message.get("role", "")))
        total += estimate_text_tokens(str(message.get("content", "")))
    return max(total, 1)


def _validate_input_token_limit(messages: list[dict], max_input_tokens: int) -> int:
    """Reject an oversized final payload locally before it reaches the API."""
    estimated = estimate_messages_tokens(messages)
    if estimated > max_input_tokens:
        raise LLMRouterError(
            "LLM input is too long before request: estimated "
            f"{estimated} tokens exceeds configured limit {max_input_tokens}. "
            "Split or shorten the prompt before calling the model."
        )
    return estimated

# ── Client pool (keyed by resolved config fingerprint) ───────
# Previously a single global singleton; BYOK needs per-(key, model, base_url)
# clients, so we cache one client per distinct config — keeping connection-pool
# reuse while supporting user-supplied credentials.  httpx pools are thread-safe.
#
# O01：池条目带使用计数。淘汰只关「使用计数为零」的连接；在途请求持有租约，
# 不会因为 LRU 表面过期而被关掉。容量打满且全部在用时等待或拒绝，不关在途连接。
from contextlib import contextmanager
from dataclasses import dataclass, field

_MAX_POOLED_CLIENTS = 32
_IDLE_TTL_SECONDS = 1800.0
_EVICT_WAIT_SECONDS = 30.0
_clients_lock = threading.Lock()
_clients_released = threading.Condition(_clients_lock)
_pool_metrics = {
    "sync_evictions": 0,
    "sync_rejects": 0,
    "async_evictions": 0,
    "async_rejects": 0,
    "async_close_tasks": 0,
}


@dataclass
class _PooledEntry:
    """One pooled client plus the lease / idle / close bookkeeping O01 requires."""

    client: object
    uses: int = 0
    last_idle: float = field(default_factory=time.monotonic)
    closed: bool = False
    retiring: bool = False
    loop: object | None = None
    close_task: object | None = None

    def acquire(self) -> None:
        self.uses += 1

    def release(self) -> None:
        self.uses = max(0, self.uses - 1)
        if self.uses == 0:
            self.last_idle = time.monotonic()


_clients: "OrderedDict[str, _PooledEntry]" = OrderedDict()

# ── JSON-schema instruction appended to every system prompt ──
_SCHEMA_INSTRUCTION = (
    "\n\nYou MUST return a valid JSON object directly. "
    "Do NOT output any markdown formatting or extra text."
    "\nExpected JSON Schema:\n{schema}"
)
_THINK_FENCE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


@lru_cache(maxsize=64)
def _schema_instruction_for(response_model: Type[BaseModel]) -> str:
    """Serialize an immutable response schema once per model class."""
    schema_json = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
    return _SCHEMA_INSTRUCTION.format(schema=schema_json)


def _client_cache_key(cfg: dict) -> str:
    """Fingerprint a resolved config so identical configs share one client."""
    return "|".join(
        str(cfg.get(k, ""))
        for k in (
            "api_key", "base_url", "model", "temperature",
            "max_tokens", "max_input_tokens", "input_token_safety_margin",
            "timeout", "connect_timeout", "max_retries",
        )
    )


def build_llm_config(
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> "LLMClientConfig | None":
    """Build a per-request LLM config from BYOK overrides.

    Non-empty overrides win; the rest falls back to ``settings``.  Returns
    ``None`` when no override is given (caller then uses the default client).
    """
    ak = (api_key or "").strip()
    md = (model or "").strip()
    bu = (base_url or "").strip()
    from app.core.config import settings
    configured_base = str(settings.llm_base_url or "").strip().rstrip("/")
    requested_base = bu.rstrip("/")
    if requested_base and requested_base != configured_base:
        raise LLMRouterError("LLM Base URL override is disabled by service security policy.")
    if not ak and not md and not bu:
        return None
    return LLMClientConfig(
        api_key=ak or settings.llm_api_key,
        base_url=bu or settings.llm_base_url,
        model=md or settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        max_input_tokens=getattr(settings, "llm_max_input_tokens", 8192),
        input_token_safety_margin=getattr(
            settings, "llm_input_token_safety_margin", 512,
        ),
        timeout=getattr(settings, "llm_timeout", 120.0),
        connect_timeout=getattr(settings, "llm_connect_timeout", 10.0),
        max_retries=getattr(settings, "llm_max_retries", 3),
    )


def _sync_transport_closed(client: object) -> bool:
    inner = getattr(client, "client", None)
    checker = getattr(inner, "is_closed", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(client, "closed", False) or getattr(client, "_closed", False))


def _close_sync_entry(entry: _PooledEntry) -> None:
    if entry.closed:
        return
    entry.closed = True
    closer = getattr(entry.client, "close", None)
    if callable(closer):
        closer()
    _pool_metrics["sync_evictions"] += 1


def _evict_idle_sync_unlocked(now: float, *, force_capacity: bool) -> list[_PooledEntry]:
    """Drop idle / TTL-expired entries. Never closes an entry with uses > 0."""
    stale: list[_PooledEntry] = []
    over = len(_clients) > _MAX_POOLED_CLIENTS
    if not over and not force_capacity:
        # TTL pass even when under capacity.
        expired = [
            key for key, entry in _clients.items()
            if entry.uses == 0 and (now - entry.last_idle) >= _IDLE_TTL_SECONDS
        ]
        for key in expired:
            stale.append(_clients.pop(key))
        return stale
    # Prefer the least-recently-used idle entry.
    idle_keys = [
        key for key, entry in _clients.items()
        if entry.uses == 0 and (
            over or (now - entry.last_idle) >= _IDLE_TTL_SECONDS
        )
    ]
    while idle_keys and len(_clients) > _MAX_POOLED_CLIENTS:
        key = idle_keys.pop(0)
        if key in _clients and _clients[key].uses == 0:
            stale.append(_clients.pop(key))
    for key in list(_clients):
        entry = _clients[key]
        if entry.uses == 0 and (now - entry.last_idle) >= _IDLE_TTL_SECONDS:
            stale.append(_clients.pop(key))
    return stale


def get_llm_client(config: "LLMClientConfig | None" = None, *, _reserve: bool = False) -> "LLMClient":
    stale: list[_PooledEntry] = []
    try:
        return _get_llm_client(config, _reserve=_reserve, stale=stale)
    finally:
        for item in stale:
            _close_sync_entry(item)


def _get_llm_client(config, *, _reserve: bool, stale: list[_PooledEntry]):
    """Return a pooled :class:`LLMClient` for *config* (or the default).

    One client is cached per distinct resolved config so connection pools are
    reused; BYOK configs transparently get their own pooled client.
    Acquisition here is **not** a use-lease: parsers may keep the config and
    call :func:`llm_client_lease` (or ``structured_completion``) around a real
    request. Idle entries may be closed; in-use entries are not.
    """
    cfg = _resolve_config(config)
    cache_key = _client_cache_key(cfg)
    deadline = time.monotonic() + _EVICT_WAIT_SECONDS
    with _clients_released:
        while True:
            now = time.monotonic()
            stale.extend(_evict_idle_sync_unlocked(now, force_capacity=False))
            entry = _clients.get(cache_key)
            if entry is not None and entry.retiring:
                raise LLMRouterError("LLM client is shutting down; retry after its active requests finish.")
            if entry is not None and not entry.closed and not _sync_transport_closed(entry.client):
                _clients.move_to_end(cache_key)
                client = entry.client
                break
            if entry is not None:
                _clients.pop(cache_key, None)
                stale.append(entry)
                entry = None
            if len(_clients) < _MAX_POOLED_CLIENTS:
                client = LLMClient(config=config)
                _clients[cache_key] = _PooledEntry(client=client)
                break
            idle = next((key for key, item in _clients.items() if item.uses == 0), None)
            if idle is not None:
                stale.append(_clients.pop(idle))
                client = LLMClient(config=config)
                _clients[cache_key] = _PooledEntry(client=client)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _pool_metrics["sync_rejects"] += 1
                raise LLMRouterError(
                    "LLM client pool is at capacity and every connection is in use; "
                    "wait for an in-flight request to finish instead of closing it."
                )
            _clients_released.wait(timeout=remaining)
        client._pool_managed = True
        client._pool_entry = _clients[cache_key]
        if _reserve:
            _clients[cache_key].acquire()
    return client


@contextmanager
def llm_client_lease(client: object):
    """Hold a use-lease on a pooled client for the duration of a real call."""
    entry: _PooledEntry | None = None
    with _clients_lock:
        for item in _clients.values():
            if item.client is client:
                if item.retiring:
                    raise LLMRouterError("LLM client is shutting down.")
                entry = item
                item.acquire()
                break
    if entry is None and getattr(client, "_pool_managed", False):
        client = get_llm_client(LLMClientConfig(**client._cfg), _reserve=True)
        entry = client._pool_entry
    try:
        yield client
    finally:
        if entry is not None:
            retire = False
            with _clients_released:
                entry.release()
                if entry.retiring and entry.uses == 0:
                    for key, item in list(_clients.items()):
                        if item is entry:
                            _clients.pop(key)
                    retire = True
                _clients_released.notify_all()
            if retire:
                _close_sync_entry(entry)


def close_llm_clients() -> None:
    """Close and forget all synchronous connection pools."""
    with _clients_lock:
        entries = []
        for key, entry in list(_clients.items()):
            if entry.uses:
                entry.retiring = True
            else:
                entries.append(_clients.pop(key))
    for entry in entries:
        _close_sync_entry(entry)


def _is_internal_llm_host(base_url: str) -> bool:
    """Whether this host is reached directly and rejects ``json_object`` mode.

    Enterprise gateways deployed inside a network perimeter typically have
    to bypass the ambient HTTP proxy, and many of them do not implement
    ``response_format={"type": "json_object"}``.  Declare them as a
    comma-separated list of host suffixes in the ``LLM_DIRECT_HOSTS``
    environment variable; unset (the default) means neither special case
    applies.
    """
    suffixes = [
        item.strip().lower()
        for item in os.environ.get("LLM_DIRECT_HOSTS", "").split(",")
        if item.strip()
    ]
    if not suffixes:
        return False
    try:
        host = (httpx.URL(base_url).host or "").lower()
    except Exception:
        return False
    return any(host.endswith(suffix) for suffix in suffixes)


def _is_deepseek_model(model: str) -> bool:
    """DeepSeek v3.1+/v4 turn thinking on unless the request disables it."""
    return "deepseek" in (model or "").lower()


def _resolve_proxy_url(base_url: str) -> str | None:
    """Read the ambient HTTP proxy; direct-connect gateways bypass it."""
    if _is_internal_llm_host(base_url):
        return None
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )


def _httpx_client_kwargs(
    timeout: float,
    connect_timeout: float,
    base_url: str,
) -> dict:
    """Build shared HTTP options without constructing an unused sibling pool."""
    timeout_cfg = httpx.Timeout(timeout, connect=connect_timeout)
    client_kwargs: dict = {"timeout": timeout_cfg, "trust_env": True}
    proxy = _resolve_proxy_url(base_url)
    if proxy:
        client_kwargs["proxy"] = proxy
        logger.info("[LLM] HTTP proxy enabled: %s", proxy)
    elif _is_internal_llm_host(base_url):
        logger.info("[LLM] Internal gateway %s — direct connection (no proxy)", base_url)
    return client_kwargs


def _build_sync_httpx_client(
    timeout: float, connect_timeout: float, base_url: str,
) -> httpx.Client:
    return httpx.Client(**_httpx_client_kwargs(timeout, connect_timeout, base_url))


def _build_async_httpx_client(
    timeout: float, connect_timeout: float, base_url: str,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(**_httpx_client_kwargs(timeout, connect_timeout, base_url))


def _completion_kwargs(
    base_url: str,
    model: str,
    messages: list,
    temperature: float,
    max_tokens: int,
) -> dict:
    """Build chat.completions kwargs.

    ``response_format`` is omitted for direct-connect gateways because some
    of them reject ``json_object`` mode outright.
    """
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if not _is_internal_llm_host(base_url):
        kwargs["response_format"] = {"type": "json_object"}
    if _is_deepseek_model(model):
        # DeepSeek endpoints: thinking is ON by default.
        # Reasoning tokens burn max_tokens first; Stage 3 then sees
        # content=None or finish_reason=length and the parse dies.
        # Some gateways ignore chat_template_kwargs / enable_thinking — only
        # thinking.type=disabled actually turns it off (probed 2026-09-20).
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return kwargs


def _record_completion_usage(model: str, completion, estimated_input: int, raw_content: str) -> None:
    """把一次 completion 的 token 用量记入当前请求累加器。

    网关未返回 usage 时退化为本地确定性估算（输入用请求前的 estimate，
    输出按返回文本估算），保证计费侧永远拿得到非零口径。
    """
    usage = getattr(completion, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    if prompt_tokens <= 0:
        prompt_tokens = estimated_input
    if completion_tokens <= 0:
        completion_tokens = estimate_text_tokens(raw_content)
    record_llm_usage(model, prompt_tokens, completion_tokens)


def _format_llm_error(exc: Exception) -> str:
    """Surface OpenAI / gateway error bodies in logs and API responses."""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if status is not None:
        detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False) if body else str(exc)
        return f"LLM HTTP {status}: {detail}"
    return str(exc)


def _resolve_config(config: "LLMClientConfig | None") -> dict:
    """Resolve LLM parameters from *config* or application settings."""
    if config is not None:
        return {
            "api_key": config.api_key,
            "base_url": config.base_url,
            "model": config.model,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "max_input_tokens": config.max_input_tokens,
            "input_token_safety_margin": config.input_token_safety_margin,
            "timeout": config.timeout,
            "connect_timeout": config.connect_timeout,
            "max_retries": config.max_retries,
        }
    from app.core.config import settings
    return {
        "api_key": settings.llm_api_key,
        "base_url": settings.llm_base_url,
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "max_input_tokens": getattr(settings, "llm_max_input_tokens", 8192),
        "input_token_safety_margin": getattr(
            settings, "llm_input_token_safety_margin", 512,
        ),
        "timeout": getattr(settings, "llm_timeout", 120.0),
        "connect_timeout": getattr(settings, "llm_connect_timeout", 10.0),
        "max_retries": getattr(settings, "llm_max_retries", 3),
    }


class LLMClient:
    """Stateless LLM caller wrapping the OpenAI SDK.

    Accepts an optional :class:`LLMClientConfig` to decouple from the
    global ``settings`` singleton.  When *config* is ``None``, values
    are read from ``app.core.config.settings`` at construction time.

    Prefer :func:`get_llm_client` over direct instantiation to
    benefit from connection-pool reuse.
    """

    def __init__(self, config: "LLMClientConfig | None" = None):
        cfg = _resolve_config(config)
        self._cfg = cfg
        self._missing_api_key = not bool(str(cfg["api_key"]).strip())
        sync_http = _build_sync_httpx_client(
            cfg["timeout"], cfg["connect_timeout"], cfg["base_url"],
        )
        self.client = OpenAI(
            # The SDK rejects an empty key during construction. Keep startup
            # and liveness endpoints available, then fail clearly on use.
            api_key=cfg["api_key"] or "missing-llm-api-key",
            base_url=cfg["base_url"],
            max_retries=cfg["max_retries"],
            http_client=sync_http,
        )
        self.model: str = cfg["model"]
        self.temperature: float = cfg["temperature"]
        self.max_tokens: int = cfg["max_tokens"]
        self.max_input_tokens: int = cfg["max_input_tokens"]
        self.input_token_safety_margin: int = cfg["input_token_safety_margin"]

    def close(self) -> None:
        """Release this client's underlying HTTP connection pool."""
        self.client.close()

    # ── Sync API ───────────────────────────────────────────────

    def structured_completion(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str | None = None,
    ) -> T:
        """Call the LLM and return a validated Pydantic model.

        Uses the shared ``_build_messages`` / ``_parse_response``
        helpers so that sync and async paths stay in lock-step.
        """
        try:
            if self._missing_api_key:
                raise LLMRouterError(
                    "LLM_API_KEY is not configured. Inject LLM_API_KEY as a deployment secret "
                    "or provide llm_api_key in the request."
                )
            messages = self._build_messages(prompt, response_model, system_prompt)
            estimated_tokens = _validate_input_token_limit(
                messages, self.max_input_tokens,
            )
            logger.info(
                "[LLM] Calling model: %s (estimated input=%d/%d tokens)",
                self.model, estimated_tokens, self.max_input_tokens,
            )

            with llm_client_lease(self) as active:
                completion = active.client.chat.completions.create(
                    **_completion_kwargs(
                        self._cfg["base_url"], self.model, messages, self.temperature, self.max_tokens,
                    )
                )

            if not completion.choices:
                raise LLMRouterError(
                    "LLM returned empty choices list (possibly blocked by content filter)"
                )
            raw_content = completion.choices[0].message.content
            if raw_content is None:
                raise LLMRouterError(
                    "LLM returned content=None (model refused to generate)"
                )

            _record_completion_usage(self.model, completion, estimated_tokens, raw_content)
            if getattr(completion.choices[0], "finish_reason", None) == "length":
                raise LLMRouterError("LLM output was truncated by its output-token limit; split the window or increase the limit.")
            return self._parse_response(raw_content.strip(), response_model)

        except LLMRouterError:
            raise
        except Exception as e:
            logger.error("[LLM] %s", _format_llm_error(e))
            raise LLMRouterError(f"LLM call failed: {_format_llm_error(e)}") from e

    # ── Shared helpers ─────────────────────────────────────────

    @staticmethod
    def _build_messages(
        prompt: str,
        response_model: Type[T],
        system_prompt: str | None,
    ) -> list:
        """Build the messages list shared by sync and async paths."""
        schema_instruction = _schema_instruction_for(response_model)
        instruction_content = (
            system_prompt or "You are a helpful assistant."
        ) + schema_instruction
        return [
            {
                "role": "user",
                "content": (
                    "Instructions:\n"
                    f"{instruction_content}\n\n"
                    "User request:\n"
                    f"{prompt}"
                ),
            },
        ]

    @staticmethod
    def _parse_response(content: str, response_model: Type[T]) -> T:
        """Parse raw LLM response text into a validated Pydantic model."""
        # Strip markdown code fences if present
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = _THINK_FENCE.sub("", content).strip()

        logger.debug("[LLM] Raw response: %s...", content[:200])

        # Parse JSON, with fallback regex extraction
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r"\{[\s\S]*\}", content)
            if json_match:
                parsed = json.loads(json_match.group())
            else:
                raise LLMRouterError(
                    f"Cannot extract valid JSON from LLM output: {content[:200]}"
                ) from None

        # Backward-compat: rename block_id -> start_block_id
        if "chapters" in parsed:
            for ch in parsed["chapters"]:
                if "block_id" in ch and "start_block_id" not in ch:
                    ch["start_block_id"] = ch["block_id"]
                if "snippet" not in ch or not ch.get("snippet"):
                    ch["snippet"] = ch.get("title", "")

        return response_model.model_validate(parsed)


# ── Async LLM Client ─────────────────────────────────────────

_async_clients: "OrderedDict[tuple[object, str], _PooledEntry]" = OrderedDict()
_async_clients_lock = threading.Lock()
_async_clients_released = threading.Condition(_async_clients_lock)
_async_close_tasks: set = set()


def _track_close_task(task: "asyncio.Task") -> None:
    _async_close_tasks.add(task)
    _pool_metrics["async_close_tasks"] = len(_async_close_tasks)

    def _done(_task: "asyncio.Task") -> None:
        _async_close_tasks.discard(_task)
        _pool_metrics["async_close_tasks"] = len(_async_close_tasks)
        try:
            exc = _task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return
        if exc is not None:
            logger.warning("[AsyncLLM] client close failed: %s", exc)

    task.add_done_callback(_done)


def _schedule_async_close(entry: _PooledEntry, loop: object | None) -> None:
    if entry.closed:
        return
    entry.closed = True
    closer = getattr(entry.client, "aclose", None)
    if not callable(closer):
        return
    _pool_metrics["async_evictions"] += 1
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is None:
        if running is None:
            asyncio.run(closer())
            return
        loop = running
    if running is loop:
        task = running.create_task(closer())
        entry.close_task = task
        _track_close_task(task)
        return
    # Close on the loop that owns the httpx client; never close from a stranger.
    def schedule():
        task = loop.create_task(closer())
        entry.close_task = task
        _track_close_task(task)
    try:
        loop.call_soon_threadsafe(schedule)
    except RuntimeError:
        logger.warning("[AsyncLLM] could not schedule close on owning loop")


def _evict_idle_async_unlocked(loop: object | None, now: float) -> list[_PooledEntry]:
    stale: list[_PooledEntry] = []
    loop_keys = [key for key in _async_clients if key[0] is loop]
    over = len(loop_keys) > _MAX_POOLED_CLIENTS
    for key in list(_async_clients):
        if key[0] is not loop:
            continue
        entry = _async_clients[key]
        idle_expired = entry.uses == 0 and (now - entry.last_idle) >= _IDLE_TTL_SECONDS
        if idle_expired or (over and entry.uses == 0 and len(
            [k for k in _async_clients if k[0] is loop]
        ) > _MAX_POOLED_CLIENTS):
            stale.append(_async_clients.pop(key))
            over = len([k for k in _async_clients if k[0] is loop]) > _MAX_POOLED_CLIENTS
    return stale


def get_async_llm_client(
    config: "LLMClientConfig | None" = None,
    *, _reserve: bool = False,
) -> "AsyncLLMClient":
    stale: list[_PooledEntry] = []
    try:
        return _get_async_llm_client(config, _reserve=_reserve, stale=stale)
    finally:
        for item in stale:
            _schedule_async_close(item, item.loop)


def _get_async_llm_client(config, *, _reserve: bool, stale: list[_PooledEntry]):
    """Return a pooled :class:`AsyncLLMClient` for *config* (or the default).

    Mirrors :func:`get_llm_client` but uses ``AsyncOpenAI`` so callers can
    ``await`` completions without blocking the event loop; BYOK configs each
    get their own pooled client. Close always happens on the owning loop.
    """
    cfg = _resolve_config(config)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # Compatibility for callers constructing clients before running a loop.
    cache_key = (loop, _client_cache_key(cfg))
    with _async_clients_released:
        while True:
            now = time.monotonic()
            stale.extend(_evict_idle_async_unlocked(loop, now))
            entry = _async_clients.get(cache_key)
            if entry is not None and entry.retiring:
                raise LLMRouterError("Async LLM client is shutting down.")
            if entry is not None and not entry.closed and not _sync_transport_closed(entry.client):
                _async_clients.move_to_end(cache_key)
                client = entry.client
                break
            if entry is not None:
                stale.append(_async_clients.pop(cache_key))
            loop_count = len([key for key in _async_clients if key[0] is loop])
            if loop_count < _MAX_POOLED_CLIENTS:
                client = AsyncLLMClient(config=config)
                _async_clients[cache_key] = _PooledEntry(client=client, loop=loop)
                break
            idle = next(
                (key for key, item in _async_clients.items()
                 if key[0] is loop and item.uses == 0),
                None,
            )
            if idle is not None:
                stale.append(_async_clients.pop(idle))
                client = AsyncLLMClient(config=config)
                _async_clients[cache_key] = _PooledEntry(client=client, loop=loop)
                break
            # This accessor is synchronous: waiting here would stop the very
            # event loop that must finish in-flight requests and release leases.
            _pool_metrics["async_rejects"] += 1
            raise LLMRouterError(
                "Async LLM client pool is at capacity and every connection is in use; retry later."
            )
        client._pool_managed = True
        client._pool_entry = _async_clients[cache_key]
        if _reserve:
            _async_clients[cache_key].acquire()
    return client


@contextmanager
def async_llm_client_lease(client: object):
    """Hold a use-lease on a pooled async client for the duration of a real call."""
    entry: _PooledEntry | None = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    with _async_clients_lock:
        for item in _async_clients.values():
            if item.client is client and item.loop is loop:
                if item.retiring:
                    raise LLMRouterError("Async LLM client is shutting down.")
                entry = item
                item.acquire()
                break
    if entry is None and getattr(client, "_pool_managed", False):
        client = get_async_llm_client(LLMClientConfig(**client._cfg), _reserve=True)
        entry = client._pool_entry
    try:
        yield client
    finally:
        if entry is not None:
            retire = False
            with _async_clients_released:
                entry.release()
                if entry.retiring and entry.uses == 0:
                    for key, item in list(_async_clients.items()):
                        if item is entry:
                            _async_clients.pop(key)
                    retire = True
                _async_clients_released.notify_all()
            if retire:
                _schedule_async_close(entry, entry.loop)


async def close_async_llm_clients(*, all_loops: bool = False) -> None:
    """Close clients owned by the running loop (plus unbound clients).

    Passing ``all_loops`` is reserved for process teardown where no other loop
    can keep using a client. Normal callers close only their own loop's pools.
    Close tasks are awaited so shutdown can record the result.
    """
    try:
        current_loop: object | None = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    with _async_clients_lock:
        keys = [
            key for key in _async_clients
            if all_loops or key[0] is current_loop or key[0] is None
        ]
        entries = []
        for key in keys:
            entry = _async_clients[key]
            if entry.uses:
                entry.retiring = True
            else:
                entries.append(_async_clients.pop(key))
    closers = []
    for entry in entries:
        if entry.closed:
            if entry.close_task is not None:
                closers.append(entry.close_task)
            continue
        entry.closed = True
        closer = getattr(entry.client, "aclose", None)
        if callable(closer):
            if entry.loop is not None and entry.loop is not current_loop:
                if entry.loop.is_running():
                    closers.append(asyncio.wrap_future(asyncio.run_coroutine_threadsafe(closer(), entry.loop)))
                else:
                    logger.warning("[AsyncLLM] owning loop stopped before client shutdown")
            else:
                closers.append(closer())
    if closers:
        await asyncio.gather(*closers, return_exceptions=True)
    pending = [task for task in list(_async_close_tasks)
               if not task.done() and task.get_loop() is current_loop]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class AsyncLLMClient:
    """Async counterpart of :class:`LLMClient`.

    Uses ``AsyncOpenAI`` to avoid blocking the event loop in
    FastAPI / asyncio contexts.  Shares the same config schema and
    response-parsing logic as the sync variant.
    """

    def __init__(self, config: "LLMClientConfig | None" = None):
        cfg = _resolve_config(config)
        self._cfg = cfg
        self._missing_api_key = not bool(str(cfg["api_key"]).strip())
        async_http = _build_async_httpx_client(
            cfg["timeout"], cfg["connect_timeout"], cfg["base_url"],
        )
        self.client = AsyncOpenAI(
            api_key=cfg["api_key"] or "missing-llm-api-key",
            base_url=cfg["base_url"],
            max_retries=cfg["max_retries"],
            http_client=async_http,
        )
        self.model: str = cfg["model"]
        self.temperature: float = cfg["temperature"]
        self.max_tokens: int = cfg["max_tokens"]
        self.max_input_tokens: int = cfg["max_input_tokens"]
        self.input_token_safety_margin: int = cfg["input_token_safety_margin"]

    async def aclose(self) -> None:
        """Release this client's underlying asynchronous connection pool."""
        await self.client.close()

    async def structured_completion(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str | None = None,
    ) -> T:
        """Async version of :meth:`LLMClient.structured_completion`."""
        try:
            if self._missing_api_key:
                raise LLMRouterError(
                    "LLM_API_KEY is not configured. Inject LLM_API_KEY as a deployment secret "
                    "or provide llm_api_key in the request."
                )
            messages = LLMClient._build_messages(
                prompt, response_model, system_prompt,
            )
            estimated_tokens = _validate_input_token_limit(
                messages, self.max_input_tokens,
            )
            logger.info(
                "[AsyncLLM] Calling model: %s (estimated input=%d/%d tokens)",
                self.model, estimated_tokens, self.max_input_tokens,
            )

            with async_llm_client_lease(self) as active:
                completion = await active.client.chat.completions.create(
                    **_completion_kwargs(
                        self._cfg["base_url"], self.model, messages, self.temperature, self.max_tokens,
                    )
                )

            if not completion.choices:
                raise LLMRouterError(
                    "LLM returned empty choices list (possibly blocked by content filter)"
                )
            raw_content = completion.choices[0].message.content
            if raw_content is None:
                raise LLMRouterError(
                    "LLM returned content=None (model refused to generate)"
                )

            _record_completion_usage(self.model, completion, estimated_tokens, raw_content)
            if getattr(completion.choices[0], "finish_reason", None) == "length":
                raise LLMRouterError("LLM output was truncated by its output-token limit; split the window or increase the limit.")
            return LLMClient._parse_response(raw_content.strip(), response_model)

        except LLMRouterError:
            raise
        except Exception as e:
            logger.error("[AsyncLLM] %s", _format_llm_error(e))
            raise LLMRouterError(f"Async LLM call failed: {_format_llm_error(e)}") from e
