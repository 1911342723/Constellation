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
_clients: dict[str, "LLMClient"] = {}
_clients_lock = threading.Lock()

# ── JSON-schema instruction appended to every system prompt ──
_SCHEMA_INSTRUCTION = (
    "\n\nYou MUST return a valid JSON object directly. "
    "Do NOT output any markdown formatting or extra text."
    "\nExpected JSON Schema:\n{schema}"
)


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


def get_llm_client(config: "LLMClientConfig | None" = None) -> "LLMClient":
    """Return a pooled :class:`LLMClient` for *config* (or the default).

    One client is cached per distinct resolved config so connection pools are
    reused; BYOK configs transparently get their own pooled client.
    """
    cfg = _resolve_config(config)
    cache_key = _client_cache_key(cfg)
    client = _clients.get(cache_key)
    if client is None:
        with _clients_lock:
            client = _clients.get(cache_key)
            if client is None:
                client = LLMClient(config=config)
                _clients[cache_key] = client
    return client


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


def _build_httpx_clients(
    timeout: float,
    connect_timeout: float,
    base_url: str,
) -> tuple[httpx.Client, httpx.AsyncClient]:
    """Build sync/async httpx clients with proxy + no_proxy support."""
    timeout_cfg = httpx.Timeout(timeout, connect=connect_timeout)
    client_kwargs: dict = {"timeout": timeout_cfg, "trust_env": True}
    proxy = _resolve_proxy_url(base_url)
    if proxy:
        client_kwargs["proxy"] = proxy
        logger.info("[LLM] HTTP proxy enabled: %s", proxy)
    elif _is_internal_llm_host(base_url):
        logger.info("[LLM] Internal gateway %s — direct connection (no proxy)", base_url)
    return httpx.Client(**client_kwargs), httpx.AsyncClient(**client_kwargs)


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
        sync_http, _ = _build_httpx_clients(cfg["timeout"], cfg["connect_timeout"], cfg["base_url"])
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

            completion = self.client.chat.completions.create(
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
        schema_json = json.dumps(
            response_model.model_json_schema(), ensure_ascii=False,
        )
        schema_instruction = _SCHEMA_INSTRUCTION.format(schema=schema_json)
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
        content = content.strip()

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

_async_clients: dict[str, "AsyncLLMClient"] = {}
_async_clients_lock = threading.Lock()


def get_async_llm_client(
    config: "LLMClientConfig | None" = None,
) -> "AsyncLLMClient":
    """Return a pooled :class:`AsyncLLMClient` for *config* (or the default).

    Mirrors :func:`get_llm_client` but uses ``AsyncOpenAI`` so callers can
    ``await`` completions without blocking the event loop; BYOK configs each
    get their own pooled client.
    """
    cfg = _resolve_config(config)
    cache_key = _client_cache_key(cfg)
    client = _async_clients.get(cache_key)
    if client is None:
        with _async_clients_lock:
            client = _async_clients.get(cache_key)
            if client is None:
                client = AsyncLLMClient(config=config)
                _async_clients[cache_key] = client
    return client


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
        _, async_http = _build_httpx_clients(cfg["timeout"], cfg["connect_timeout"], cfg["base_url"])
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

            completion = await self.client.chat.completions.create(
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
            return LLMClient._parse_response(raw_content.strip(), response_model)

        except LLMRouterError:
            raise
        except Exception as e:
            logger.error("[AsyncLLM] %s", _format_llm_error(e))
            raise LLMRouterError(f"Async LLM call failed: {_format_llm_error(e)}") from e
