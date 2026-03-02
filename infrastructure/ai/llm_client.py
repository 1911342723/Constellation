"""LLM Client — unified large-model calling interface.

Uses the OpenAI SDK as a universal transport layer, compatible with
DeepSeek / OpenAI / Claude and any OpenAI-protocol provider.

The module exposes a **singleton** accessor :func:`get_llm_client` so
that all consumers share a single ``httpx`` connection pool, avoiding
the per-request TCP handshake overhead that would otherwise occur
under concurrent load.
"""
from __future__ import annotations

from typing import Optional, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from app.core.exceptions import LLMRouterError
from modules.parser.config import LLMClientConfig

import json
import logging
import re

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ── Singleton bookkeeping ────────────────────────────────────
_instance: Optional["LLMClient"] = None


def get_llm_client(config: "LLMClientConfig | None" = None) -> "LLMClient":
    """Return the module-level :class:`LLMClient` singleton.

    The underlying ``httpx.Client`` connection pool is thread-safe,
    so sharing a single instance across concurrent requests is both
    safe and desirable (avoids port exhaustion under load).

    Pass *config* on the first call to override ``settings`` defaults.
    Subsequent calls ignore *config* and return the cached instance.
    """
    global _instance
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
        else:
            # Lazy import keeps the module importable without .env
            from app.core.config import settings
            api_key = settings.llm_api_key
            base_url = settings.llm_base_url
            model = settings.llm_model
            temperature = settings.llm_temperature
            max_tokens = settings.llm_max_tokens

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
    
    def structured_completion(
        self,
        prompt: str,
        response_model: Type[T],
        system_prompt: str = None
    ) -> T:
        """
        调用 LLM 并返回结构化输出
        
        Args:
            prompt: 用户提示词
            response_model: Pydantic 响应模型
            system_prompt: 系统提示词
            
        Returns:
            结构化响应对象
            
        Raises:
            LLMRouterError: LLM 调用失败时抛出
        """
        try:
            messages = []
            
            # 手动拼接 JSON Schema 提示
            schema_instruction = (
                "\n\n你必须直接返回一个合法的 JSON 对象，不要输出任何 markdown 格式标记与额外文字。"
                f"\n预期的 JSON Schema:\n{json.dumps(response_model.model_json_schema(), ensure_ascii=False)}"
            )
            
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt + schema_instruction})
            else:
                messages.append({"role": "system", "content": "You are a helpful assistant." + schema_instruction})
                
            messages.append({"role": "user", "content": prompt})
            
            logger.info(f"[LLM Client] 调用模型: {self.model}")
            
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            
            content = completion.choices[0].message.content.strip()
            
            # 清理 markdown 代码块标记
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            logger.debug(f"[LLM Client] 原始响应: {content[:200]}...")
            
            # 尝试解析 JSON，处理可能的格式问题
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # 尝试从文本中提取 JSON
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    parsed = json.loads(json_match.group())
                else:
                    raise LLMRouterError(f"无法从 LLM 输出中提取有效 JSON: {content[:200]}")
            
            # 处理 block_id → start_block_id 的兼容性
            # LLM 可能输出 "block_id" 而 Pydantic 模型使用 "start_block_id"
            if "chapters" in parsed:
                for ch in parsed["chapters"]:
                    if "block_id" in ch and "start_block_id" not in ch:
                        ch["start_block_id"] = ch["block_id"]
                    # snippet 兼容：如果 LLM 没返回 snippet，用 title 兜底
                    if "snippet" not in ch or not ch.get("snippet"):
                        ch["snippet"] = ch.get("title", "")
            
            return response_model.model_validate(parsed)
            
        except LLMRouterError:
            raise
        except Exception as e:
            raise LLMRouterError(f"LLM 调用失败: {str(e)}")
