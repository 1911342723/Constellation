"""LLM Router — Cursor-Caliper Stage 3: Cursor Pointer Routing.

Routes the compressed virtual skeleton through an LLM to obtain
section boundary anchors.  The LLM acts exclusively as a *pointer
annotator*: it outputs ``(block_id, level, title, snippet)`` tuples
and is strictly forbidden from generating any body content.

Prompt templates are loaded from ``modules/parser/prompts/`` at
initialisation time so they can be edited without touching code.
"""

from __future__ import annotations

import logging
from typing import List

from infrastructure.ai.llm_client import get_async_llm_client, get_llm_client
from modules.parser.prompts import load_prompt
from modules.parser.schemas import ChapterNode, LLMRouterOutput
from app.core.exceptions import LLMRouterError

logger = logging.getLogger(__name__)


class LLMRouter:
    """Annotate section boundaries on a virtual skeleton via LLM.

    The router sends the skeleton text together with a structured
    system prompt to the configured LLM and parses the response into
    a flat list of :class:`ChapterNode` anchors.
    """

    def __init__(self) -> None:
        self._client = get_llm_client()
        self._system_prompt = load_prompt("router_system")
        self._user_template = load_prompt("router_user")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, skeleton_text: str) -> LLMRouterOutput:
        """Identify section headings in *skeleton_text*.

        Args:
            skeleton_text: Compressed skeleton produced by
                :class:`SkeletonCompressor`.

        Returns:
            Structured output containing ``doc_title``, ``doc_authors``
            and a flat ``chapters`` anchor list.

        Raises:
            LLMRouterError: On empty input or LLM call failure.
        """
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton text; cannot route.")

        user_prompt = self._user_template.format(skeleton_text=skeleton_text)

        try:
            logger.info(
                "[LLMRouter] Sending skeleton (%d chars) to LLM …",
                len(skeleton_text),
            )

            result: LLMRouterOutput = self._client.structured_completion(
                prompt=user_prompt,
                response_model=LLMRouterOutput,
                system_prompt=self._system_prompt,
            )

            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(f"LLM routing failed: {exc}") from exc

    def route_chunk(
        self,
        skeleton_text: str,
        chunk_index: int,
        total_chunks: int,
        previous_tail_context: str = "",
    ) -> LLMRouterOutput:
        """Route a single skeleton chunk (Map phase of Map-Reduce).

        For the first chunk (``chunk_index == 0``), the LLM is asked
        to extract ``doc_title`` and ``doc_authors`` as usual.  For
        subsequent chunks, those fields are explicitly set to empty
        strings so the LLM focuses solely on heading detection.
        Tail context is provided to prevent level jumps.

        Args:
            skeleton_text: One window's compressed skeleton.
            chunk_index: Zero-based window index.
            total_chunks: Total number of windows.
            previous_tail_context: Formatted string of latest headings from previous chunk.

        Returns:
            :class:`LLMRouterOutput` for this chunk.

        Raises:
            LLMRouterError: On empty input or LLM call failure.
        """
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton chunk; cannot route.")

        is_first = chunk_index == 0

        # Build a window-aware user prompt
        window_hint = (
            f"\n\n[窗口信息] 这是文档的第 {chunk_index + 1}/{total_chunks} 个分片。"
        )
        if is_first:
            window_hint += "\n请正常提取 doc_title 和 doc_authors。"
        else:
            window_hint += (
                "\n这不是文档的开头，请将 doc_title 和 doc_authors 设为空字符串，"
                "只关注章节标题识别。"
            )
            
        if previous_tail_context:
            window_hint += (
                f"\n[前序状态继承] 紧接上文，本文档在进入本窗口前，最后的子层级结构如下：\n"
                f"{previous_tail_context}\n"
                f"请参照此层级关系，继续判别本窗口内的后续章节层级（Level 1-6），防止发生层级断层与错乱。"
            )

        user_prompt = self._user_template.format(skeleton_text=skeleton_text) + window_hint

        try:
            logger.info(
                "[LLMRouter] Sending chunk %d/%d (%d chars) to LLM …",
                chunk_index + 1,
                total_chunks,
                len(skeleton_text),
            )

            result: LLMRouterOutput = self._client.structured_completion(
                prompt=user_prompt,
                response_model=LLMRouterOutput,
                system_prompt=self._system_prompt,
            )

            # Force empty metadata for non-first chunks
            if not is_first:
                result.doc_title = ""
                result.doc_authors = ""

            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(
                f"LLM routing failed on chunk {chunk_index + 1}/{total_chunks}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Async variants (for FastAPI / asyncio contexts)
    # ------------------------------------------------------------------

    async def async_route(self, skeleton_text: str) -> LLMRouterOutput:
        """Async version of :meth:`route`."""
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton text; cannot route.")

        user_prompt = self._user_template.format(skeleton_text=skeleton_text)
        async_client = get_async_llm_client()

        try:
            logger.info("[LLMRouter] Async sending skeleton (%d chars) …", len(skeleton_text))
            result: LLMRouterOutput = await async_client.structured_completion(
                prompt=user_prompt,
                response_model=LLMRouterOutput,
                system_prompt=self._system_prompt,
            )
            self._log_result(result)
            return result
        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(f"Async LLM routing failed: {exc}") from exc

    async def async_route_chunk(
        self,
        skeleton_text: str,
        chunk_index: int,
        total_chunks: int,
        previous_tail_context: str = "",
    ) -> LLMRouterOutput:
        """Async version of :meth:`route_chunk`."""
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton chunk; cannot route.")

        is_first = chunk_index == 0
        window_hint = f"\n\n[窗口信息] 这是文档的第 {chunk_index + 1}/{total_chunks} 个分片。"
        if is_first:
            window_hint += "\n请正常提取 doc_title 和 doc_authors。"
        else:
            window_hint += (
                "\n这不是文档的开头，请将 doc_title 和 doc_authors 设为空字符串，"
                "只关注章节标题识别。"
            )
        if previous_tail_context:
            window_hint += (
                f"\n[前序状态继承] 紧接上文，本文档在进入本窗口前，最后的子层级结构如下：\n"
                f"{previous_tail_context}\n"
                f"请参照此层级关系，继续判别本窗口内的后续章节层级（Level 1-6），防止发生层级断层与错乱。"
            )

        user_prompt = self._user_template.format(skeleton_text=skeleton_text) + window_hint
        async_client = get_async_llm_client()

        try:
            logger.info("[LLMRouter] Async sending chunk %d/%d (%d chars) …",
                        chunk_index + 1, total_chunks, len(skeleton_text))
            result: LLMRouterOutput = await async_client.structured_completion(
                prompt=user_prompt,
                response_model=LLMRouterOutput,
                system_prompt=self._system_prompt,
            )
            if not is_first:
                result.doc_title = ""
                result.doc_authors = ""
            self._log_result(result)
            return result
        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(
                f"Async LLM routing failed on chunk {chunk_index + 1}/{total_chunks}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _log_result(result: LLMRouterOutput) -> None:
        """Emit a human-readable summary of the routing result."""
        logger.info(
            "[LLMRouter] Done — title='%s', authors='%s', chapters=%d",
            result.doc_title,
            result.doc_authors,
            len(result.chapters),
        )
        for ch in result.chapters:
            indent = "  " * (ch.level - 1)
            snippet_hint = (
                f" [snippet: {ch.snippet[:20]}…]" if ch.snippet else ""
            )
            logger.info(
                "  %s[%d] L%d: %s%s",
                indent,
                ch.start_block_id,
                ch.level,
                ch.title,
                snippet_hint,
            )
