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
import re
import threading
from typing import Optional, Type, TypeVar

import httpx
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

from app.core.exceptions import LLMRouterError
from modules.parser.config import LLMClientConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ── Thread-safe singleton bookkeeping ────────────────────────
_instance: Optional["LLMClient"] = None
_lock = threading.Lock()


def get_llm_client(config: "LLMClientConfig | None" = None) -> "LLMClient":
    """Return the module-level :class:`LLMClient` singleton.

    The underlying ``httpx.Client`` connection pool is thread-safe,
    so sharing a single instance across concurrent requests is both
    safe and desirable (avoids port exhaustion under load).

    Pass *config* on the first call to override ``settings`` defaults.
    Subsequent calls ignore *config* and return the cached instance.

    Uses double-checked locking to avoid race conditions under
    multi-threaded access.
    """
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = LLMClient(config=config)
    return _instance


class LLMClient:
    """Stateless LLM caller wrapping the OpenAI SDK.

    Accepts an optional :class:`LLMClientConfig` to decouple from the
    global ``settings`` singleton.  When *config* is ``None``, values
    are read from ``app.core.config.settings`` at construction time.

    Prefer :func:`get_llm_client` over direct instantiation to
    benefit from connection-pool reuse.
    """

    def __init__(self, config: "LLMClientConfig | None" = None):
        if config is not None:
            api_key = config.api_key
            base_url = config.base_url
            model = config.model
            temperature = config.temperature
            max_tokens = config.max_tokens
            timeout = config.timeout
            connect_timeout = config.connect_timeout
            max_retries = config.max_retries
        else:
            # Lazy import keeps the module importable without .env
            from app.core.config import settings
            api_key = settings.llm_api_key
            base_url = settings.llm_base_url
            model = settings.llm_model
            temperature = settings.llm_temperature
            max_tokens = settings.llm_max_tokens
            timeout = getattr(settings, "llm_timeout", 120.0)
            connect_timeout = getattr(settings, "llm_connect_timeout", 10.0)
            max_retries = getattr(settings, "llm_max_retries", 3)

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            max_retries=max_retries,
        )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
    
    def structured_completion(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str | None = None,
    ) -> T:
        """Call the LLM and return a validated Pydantic model.

        Uses the shared ``_build_messages`` / ``_parse_response`` helpers
        so that sync and async paths stay in lock-step.
        """
        try:
            messages = self._build_messages(prompt, response_model, system_prompt)

            logger.info("[LLM Client] 调用模型: %s", self.model)

            completion = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if not completion.choices:
                raise LLMRouterError("LLM 返回空 choices 列表（可能被内容审查拒绝）")
            raw_content = completion.choices[0].message.content
            if raw_content is None:
                raise LLMRouterError("LLM 返回 content=None（模型拒绝生成）")
            content = raw_content.strip()

            return self._parse_response(content, response_model)

        except LLMRouterError:
            raise
        except Exception as e:
            raise LLMRouterError(f"LLM 调用失败: {str(e)}")

    @staticmethod
    def _build_messages(prompt: str, response_model: Type[T], system_prompt: str | None) -> list:
        """Build the messages list shared by sync and async paths."""
        schema_instruction = (
            "\n\n你必须直接返回一个合法的 JSON 对象，不要输出任何 markdown 格式标记与额外文字。"
            f"\n预期的 JSON Schema:\n{json.dumps(response_model.model_json_schema(), ensure_ascii=False)}"
        )
        sys_content = (system_prompt or "You are a helpful assistant.") + schema_instruction
        return [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": prompt},
        ]

    @staticmethod
    def _parse_response(content: str, response_model: Type[T]) -> T:
        """Parse raw LLM response text into a validated Pydantic model."""
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        logger.debug("[LLM Client] 原始响应: %s...", content[:200])

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                parsed = json.loads(json_match.group())
            else:
                raise LLMRouterError(f"无法从 LLM 输出中提取有效 JSON: {content[:200]}")

        if "chapters" in parsed:
            for ch in parsed["chapters"]:
                if "block_id" in ch and "start_block_id" not in ch:
                    ch["start_block_id"] = ch["block_id"]
                if "snippet" not in ch or not ch.get("snippet"):
                    ch["snippet"] = ch.get("title", "")

        return response_model.model_validate(parsed)


# ── Async LLM Client ─────────────────────────────────────────

_async_instance: Optional["AsyncLLMClient"] = None
_async_lock = threading.Lock()


def get_async_llm_client(config: "LLMClientConfig | None" = None) -> "AsyncLLMClient":
    """Return the module-level :class:`AsyncLLMClient` singleton.

    Mirrors :func:`get_llm_client` but uses ``AsyncOpenAI`` under the
    hood so callers can ``await`` completions without blocking the
    event loop.
    """
    global _async_instance
    if _async_instance is None:
        with _async_lock:
            if _async_instance is None:
                _async_instance = AsyncLLMClient(config=config)
    return _async_instance


class AsyncLLMClient:
    """Async counterpart of :class:`LLMClient`.

    Uses ``AsyncOpenAI`` to avoid blocking the event loop in
    FastAPI / asyncio contexts.  Shares the same config schema and
    response-parsing logic as the sync variant.
    """

    def __init__(self, config: "LLMClientConfig | None" = None):
        if config is not None:
            api_key = config.api_key
            base_url = config.base_url
            model = config.model
            temperature = config.temperature
            max_tokens = config.max_tokens
            timeout = config.timeout
            connect_timeout = config.connect_timeout
            max_retries = config.max_retries
        else:
            from app.core.config import settings
            api_key = settings.llm_api_key
            base_url = settings.llm_base_url
            model = settings.llm_model
            temperature = settings.llm_temperature
            max_tokens = settings.llm_max_tokens
            timeout = getattr(settings, "llm_timeout", 120.0)
            connect_timeout = getattr(settings, "llm_connect_timeout", 10.0)
            max_retries = getattr(settings, "llm_max_retries", 3)

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            max_retries=max_retries,
        )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def structured_completion(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str | None = None,
    ) -> T:
        """Async version of :meth:`LLMClient.structured_completion`."""
        try:
            messages = LLMClient._build_messages(prompt, response_model, system_prompt)

            logger.info("[AsyncLLM] 调用模型: %s", self.model)

            completion = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if not completion.choices:
                raise LLMRouterError("LLM 返回空 choices 列表（可能被内容审查拒绝）")
            raw_content = completion.choices[0].message.content
            if raw_content is None:
                raise LLMRouterError("LLM 返回 content=None（模型拒绝生成）")
            content = raw_content.strip()
            return LLMClient._parse_response(content, response_model)

        except LLMRouterError:
            raise
        except Exception as e:
            raise LLMRouterError(f"异步 LLM 调用失败: {str(e)}")
