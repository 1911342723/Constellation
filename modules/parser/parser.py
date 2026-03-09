"""
Constellation 主解析器 (CaliperParser)

游标卡尺算法的总调度器，协调四个阶段的流水线处理：

Stage 1: 物理降维层 (DocxProvider)
  → 原始 .docx → List[Block]（带物理特征标注）

Stage 2: 虚拟骨架压缩 (SkeletonCompressor)
  → List[Block] → 极简骨架文本（I帧/P帧 + RLE + Meta-Tag）

Stage 3: AI 游标漫游 (LLMRouter)
  → 骨架文本 → 章节锚点列表 [{block_id, title, level}]

Stage 4: 游标闭合与组装 (IntervalResolver + DocumentTree)
  → 锚点 + 原始 Blocks → 完整文档树 → 多个结构化 Markdown 文档
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import List, Optional

from infrastructure.models import Block
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig, ParserConfig, ResolverConfig
from modules.parser.router import LLMRouter
from modules.parser.resolver import IntervalResolver, _levenshtein_ratio
from modules.parser.document_tree import DocumentTree
from modules.parser.schemas import LLMRouterOutput
from app.core.exceptions import ParserError

logger = logging.getLogger(__name__)


# ── Document-level result cache ──────────────────────────────
# Keyed by SHA-256 of the concatenated block texts so that
# identical documents parsed twice skip the expensive LLM calls.

class _LRUCache:
    """Thread-safe LRU cache with a configurable max size."""

    def __init__(self, max_size: int = 32):
        self._store: OrderedDict[str, DocumentTree] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[DocumentTree]:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                return self._store[key]
        return None

    def put(self, key: str, value: DocumentTree) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            else:
                if len(self._store) >= self._max_size:
                    self._store.popitem(last=False)
            self._store[key] = value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_doc_cache = _LRUCache(max_size=32)


def _compute_blocks_hash(blocks: List[Block]) -> str:
    """Derive a deterministic cache key from block contents.

    Includes physical formatting features (bold, font_size, heading style)
    so that structurally different documents never share a cache entry.
    """
    h = hashlib.sha256()
    for b in blocks:
        h.update(
            f"{b.id}|{b.type}|{b.text or ''}|"
            f"{b.is_bold}|{b.font_size or 0}|{b.is_heading_style}"
            .encode("utf-8", errors="replace")
        )
    return h.hexdigest()


class CaliperParser:
    """Constellation main parser — orchestrates the four-stage pipeline.

    Accepts optional :class:`CompressorConfig` and :class:`ResolverConfig`
    so that the pipeline can be driven without a global ``settings``
    singleton (useful for testing and library-mode usage).
    """

    def __init__(
        self,
        compressor_config: CompressorConfig | None = None,
        resolver_config: ResolverConfig | None = None,
        parser_config: ParserConfig | None = None,
    ):
        self._compressor_config = compressor_config
        self._resolver_config = resolver_config
        self._parser_config = parser_config or ParserConfig()
        self.compressor = SkeletonCompressor(config=compressor_config)
        self.router = LLMRouter()

    @staticmethod
    def clear_cache() -> None:
        """Clear the document-level result cache."""
        _doc_cache.clear()
    
    def parse(self, blocks: List[Block]) -> DocumentTree:
        """
        核心解析入口：将 Block 列表解析为文档树
        
        四阶段流水线：
        1. 骨架压缩 → 虚拟空间
        2. LLM 路由 → 章节锚点
        3. 强制闭合 → 区间分割
        4. 无损组装 → 文档树
        
        Args:
            blocks: Block 列表（由 Provider 提供）
            
        Returns:
            DocumentTree 对象
            
        Raises:
            ParserError: 解析失败时抛出
        """
        if not blocks:
            raise ParserError("Block 列表为空，无法解析")
        
        # Cache lookup
        cache_key = _compute_blocks_hash(blocks)
        cached = _doc_cache.get(cache_key)
        if cached is not None:
            logger.info("[游标卡尺] 命中缓存，跳过 LLM 调用 (hash=%s…)", cache_key[:12])
            return cached
        
        try:
            logger.info("[游标卡尺] ===== 开始解析 (共 %d 个 Block) =====", len(blocks))
            
            # ===== 阶段二：虚拟骨架压缩 =====
            logger.info("[游标卡尺] 阶段 2/4：虚拟骨架压缩...")
            skeleton_chunks = self.compressor.compress(blocks)
            total_chars = sum(len(c) for c in skeleton_chunks)
            logger.info(
                "[游标卡尺] 骨架生成完成 (%d 字符, %d 个分片)",
                total_chars, len(skeleton_chunks),
            )
            
            # ===== 阶段三：AI 游标漫游 (Map-Reduce) =====
            logger.info("[游标卡尺] 阶段 3/4：AI 游标漫游...")
            llm_output = self._map_reduce_route(skeleton_chunks)
            logger.info(
                "[游标卡尺] LLM 识别完成: 标题='%s', 章节数=%d",
                llm_output.doc_title, len(llm_output.chapters),
            )
            
            # ===== 阶段四：游标闭合与组装 =====
            logger.info("[游标卡尺] 阶段 4/4：游标闭合与组装...")
            resolver = IntervalResolver(blocks, config=self._resolver_config)
            
            # 处理前置冗余段（标题/作者等元信息）
            preamble_content = ""
            if llm_output.chapters:
                first_start_id = llm_output.chapters[0].start_block_id
                preamble_blocks = resolver.get_preamble_blocks(first_start_id)
                if preamble_blocks:
                    preamble_content = "\n\n".join(
                        b.to_markdown() for b in preamble_blocks if b.to_markdown()
                    )
                    logger.info("[游标卡尺] 前置冗余段: %d 个 Block", len(preamble_blocks))
            
            # 强制闭合 + 组装
            document_nodes = resolver.resolve(llm_output.chapters)
            
            # 构建文档树
            tree = DocumentTree(
                nodes=document_nodes,
                doc_title=llm_output.doc_title,
                doc_authors=llm_output.doc_authors,
                preamble_content=preamble_content,
            )
            
            # Store in cache
            _doc_cache.put(cache_key, tree)

            stats = tree.get_stats()
            logger.info(
                f"[游标卡尺] ===== 解析完成 =====\n"
                f"  文档标题: {stats['doc_title']}\n"
                f"  文档作者: {stats['doc_authors']}\n"
                f"  顶级章节: {stats['top_level_sections']} 个\n"
                f"  总章节数: {stats['total_sections']} 个\n"
                f"  内容总量: {stats['total_content_chars']} 字符\n"
                f"  最大深度: {stats['max_depth']} 层"
            )
            
            return tree
            
        except ParserError:
            raise
        except Exception as e:
            raise ParserError(f"文档解析失败: {str(e)}")
    
    # ------------------------------------------------------------------
    # Map-Reduce LLM routing
    # ------------------------------------------------------------------

    def _map_reduce_route(self, skeleton_chunks: List[str]) -> LLMRouterOutput:
        """Route skeleton chunks through the LLM using Map-Reduce.

        For single-chunk documents, falls back to the original
        ``router.route()`` path.  For multi-chunk documents, each
        chunk is sent as an independent LLM request via
        ``router.route_chunk()``, and the results are merged into a
        single :class:`LLMRouterOutput`.

        Args:
            skeleton_chunks: List of skeleton text strings from the
                compressor (one per window).

        Returns:
            Merged :class:`LLMRouterOutput` with deduplicated chapters.
        """
        total = len(skeleton_chunks)

        # Fast path: single chunk — no Map-Reduce overhead
        if total == 1:
            logger.info("[Map-Reduce] 单分片模式，直接路由")
            return self.router.route(skeleton_chunks[0])

        # Sequential phase: route each chunk in order to preserve hierarchy state
        logger.info(
            f"[Map-Reduce] 多分片串行模式 (带状态幽灵传递): {total} 个窗口"
        )

        # results indexed by chunk_index
        results: dict[int, LLMRouterOutput] = {}
        previous_tail_context = ""

        for idx, chunk in enumerate(skeleton_chunks):
            output = self.router.route_chunk(chunk, idx, total, previous_tail_context)
            results[idx] = output
            
            logger.info(
                f"[Map-Reduce] 窗口 {idx+1}/{total} 完成: "
                f"{len(output.chapters)} 个锚点"
            )
            
            # Extract last 2 anchors to form tail context for the next chunk
            if output.chapters:
                tail = output.chapters[-2:]
                context_lines = []
                for ch in tail:
                    context_lines.append(f"- Level {ch.level}: {ch.title}")
                previous_tail_context = "\n".join(context_lines)

        # Merge in original window order
        doc_title = results[0].doc_title
        doc_authors = results[0].doc_authors
        all_chapters = []
        for i in range(total):
            all_chapters.extend(results[i].chapters)

        logger.info(
            f"[Map-Reduce] Map 阶段完成: 合计 {len(all_chapters)} 个原始锚点"
        )

        # Reduce phase: sort by block_id, then deduplicate overlapping anchors
        all_chapters.sort(key=lambda ch: ch.start_block_id)
        deduped = self._deduplicate_overlap_anchors(all_chapters)
        
        logger.info(
            f"[Map-Reduce] Reduce 去重完成: "
            f"{len(all_chapters)} → {len(deduped)} 个锚点"
        )

        return LLMRouterOutput(
            doc_title=doc_title,
            doc_authors=doc_authors,
            chapters=deduped,
        )

    def _deduplicate_overlap_anchors(self, chapters: list) -> list:
        """Remove duplicate anchors produced by overlapping windows."""
        if not chapters:
            return chapters
            
        cfg = self._resolver_config if self._resolver_config else ResolverConfig()
        id_diff_threshold = cfg.dedup_id_diff
        sim_threshold = cfg.dedup_sim_threshold

        result = [chapters[0]]

        for ch in chapters[1:]:
            is_dup = False
            for prev in result[-5:]:
                id_diff = abs(ch.start_block_id - prev.start_block_id)
                if id_diff <= id_diff_threshold:
                    sim = _levenshtein_ratio(
                        ch.title.strip().lower(),
                        prev.title.strip().lower(),
                    )
                    if sim >= sim_threshold:
                        is_dup = True
                        break
            if not is_dup:
                result.append(ch)

        return result

    # ------------------------------------------------------------------
    # Async pipeline (non-blocking for FastAPI / asyncio)
    # ------------------------------------------------------------------

    async def async_parse(self, blocks: List[Block]) -> DocumentTree:
        """Async version of :meth:`parse`.

        Uses :class:`AsyncLLMClient` for LLM calls so the event loop
        is never blocked.  CPU-bound Stage 4 (resolution) is offloaded
        to a thread via ``asyncio.to_thread`` for pipeline overlap.
        """
        import asyncio

        if not blocks:
            raise ParserError("Block 列表为空，无法解析")

        # Cache lookup
        cache_key = _compute_blocks_hash(blocks)
        cached = _doc_cache.get(cache_key)
        if cached is not None:
            logger.info("[游标卡尺] 命中缓存，跳过 LLM 调用 (hash=%s…)", cache_key[:12])
            return cached

        try:
            logger.info("[游标卡尺] ===== 异步解析开始 (共 %d 个 Block) =====", len(blocks))

            skeleton_chunks = self.compressor.compress(blocks)
            total_chars = sum(len(c) for c in skeleton_chunks)
            logger.info("[游标卡尺] 骨架生成完成 (%d 字符, %d 个分片)", total_chars, len(skeleton_chunks))

            llm_output = await self._async_map_reduce_route(skeleton_chunks)
            logger.info("[游标卡尺] LLM 识别完成: 标题='%s', 章节数=%d",
                        llm_output.doc_title, len(llm_output.chapters))

            tree = await asyncio.to_thread(
                self._resolve_and_build_tree, blocks, llm_output,
            )

            _doc_cache.put(cache_key, tree)

            stats = tree.get_stats()
            logger.info(
                "[游标卡尺] ===== 异步解析完成 =====\n"
                "  文档标题: %s\n  总章节数: %s 个\n  最大深度: %s 层",
                stats["doc_title"], stats["total_sections"], stats["max_depth"],
            )
            return tree

        except ParserError:
            raise
        except Exception as e:
            raise ParserError(f"异步文档解析失败: {str(e)}")

    def _resolve_and_build_tree(
        self, blocks: List[Block], llm_output: LLMRouterOutput,
    ) -> DocumentTree:
        """Stage 4: resolve anchors and build the document tree.

        Extracted as a standalone method so it can be submitted to
        ``asyncio.to_thread`` or a ``ThreadPoolExecutor``.
        """
        resolver = IntervalResolver(blocks, config=self._resolver_config)

        preamble_content = ""
        if llm_output.chapters:
            first_start_id = llm_output.chapters[0].start_block_id
            preamble_blocks = resolver.get_preamble_blocks(first_start_id)
            if preamble_blocks:
                preamble_content = "\n\n".join(
                    b.to_markdown() for b in preamble_blocks if b.to_markdown()
                )

        document_nodes = resolver.resolve(llm_output.chapters)

        return DocumentTree(
            nodes=document_nodes,
            doc_title=llm_output.doc_title,
            doc_authors=llm_output.doc_authors,
            preamble_content=preamble_content,
        )

    async def _async_map_reduce_route(self, skeleton_chunks: List[str]) -> LLMRouterOutput:
        """Async Map-Reduce routing — dispatches to serial or speculative strategy."""
        total = len(skeleton_chunks)

        if total == 1:
            logger.info("[Map-Reduce] 异步单分片模式")
            return await self.router.async_route(skeleton_chunks[0])

        if self._parser_config.enable_speculative_execution and total > 2:
            return await self._async_speculative_route(skeleton_chunks)

        return await self._async_serial_route(skeleton_chunks)

    async def _async_serial_route(self, skeleton_chunks: List[str]) -> LLMRouterOutput:
        """Serial routing with state phantom projection (default)."""
        total = len(skeleton_chunks)
        logger.info("[Map-Reduce] 异步串行模式 (带状态幽灵传递): %d 个窗口", total)

        results: dict[int, LLMRouterOutput] = {}
        previous_tail_context = ""

        for idx, chunk in enumerate(skeleton_chunks):
            output = await self.router.async_route_chunk(chunk, idx, total, previous_tail_context)
            results[idx] = output

            logger.info("[Map-Reduce] 窗口 %d/%d 完成: %d 个锚点",
                        idx + 1, total, len(output.chapters))

            if output.chapters:
                tail = output.chapters[-2:]
                previous_tail_context = "\n".join(
                    f"- Level {ch.level}: {ch.title}" for ch in tail
                )

        return self._merge_route_results(results, total)

    async def _async_speculative_route(self, skeleton_chunks: List[str]) -> LLMRouterOutput:
        """Speculative parallel execution with boundary verification.

        Strategy:
        1. Fire all window LLM requests in parallel (no state projection).
        2. Verify that each window boundary is level-consistent with
           the previous window's tail context.
        3. Re-request only the inconsistent windows serially with
           proper state projection.

        This yields parallel speed in the common case (most boundaries
        are consistent) and falls back to serial only for mismatched
        windows.
        """
        import asyncio

        total = len(skeleton_chunks)
        tolerance = self._parser_config.speculative_boundary_tolerance

        logger.info("[Speculative] 推测并行模式: %d 个窗口 (boundary_tolerance=%d)",
                    total, tolerance)

        # Phase 1: parallel fire (no state projection)
        tasks = [
            self.router.async_route_chunk(chunk, idx, total, "")
            for idx, chunk in enumerate(skeleton_chunks)
        ]
        speculative_results: List[LLMRouterOutput] = await asyncio.gather(*tasks)
        results: dict[int, LLMRouterOutput] = dict(enumerate(speculative_results))

        logger.info("[Speculative] 并行阶段完成, 开始边界验证")

        # Phase 2: boundary verification + selective re-request
        rerun_count = 0
        for idx in range(1, total):
            prev_output = results[idx - 1]
            curr_output = results[idx]

            if not prev_output.chapters or not curr_output.chapters:
                continue

            prev_tail_level = prev_output.chapters[-1].level
            curr_head_level = curr_output.chapters[0].level

            # A boundary is inconsistent if the first heading of the
            # current window jumps more than `tolerance` levels relative
            # to the last heading of the previous window.
            if curr_head_level > prev_tail_level + tolerance:
                tail = prev_output.chapters[-2:]
                tail_ctx = "\n".join(f"- Level {ch.level}: {ch.title}" for ch in tail)

                logger.info(
                    "[Speculative] 窗口 %d 边界不一致 (prev_tail=%d, curr_head=%d), 串行重请求",
                    idx + 1, prev_tail_level, curr_head_level,
                )
                results[idx] = await self.router.async_route_chunk(
                    skeleton_chunks[idx], idx, total, tail_ctx,
                )
                rerun_count += 1

        logger.info("[Speculative] 边界验证完成: %d/%d 个窗口需要重请求", rerun_count, total - 1)

        return self._merge_route_results(results, total)

    def _merge_route_results(
        self, results: dict[int, LLMRouterOutput], total: int,
    ) -> LLMRouterOutput:
        """Merge per-window LLM results into a single output."""
        doc_title = results[0].doc_title
        doc_authors = results[0].doc_authors
        all_chapters = []
        for i in range(total):
            all_chapters.extend(results[i].chapters)

        all_chapters.sort(key=lambda ch: ch.start_block_id)
        deduped = self._deduplicate_overlap_anchors(all_chapters)

        logger.info("[Map-Reduce] Reduce 完成: %d → %d 个锚点",
                    len(all_chapters), len(deduped))

        return LLMRouterOutput(
            doc_title=doc_title,
            doc_authors=doc_authors,
            chapters=deduped,
        )
