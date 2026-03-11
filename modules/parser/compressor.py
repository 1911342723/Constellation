"""Skeleton Compressor — Constellation Stage 2.

Compresses a full Block sequence into a compact *virtual skeleton*
that maximises the structural signal-to-noise ratio while minimising
token consumption.

Compression strategies:

1. **I-frame / P-frame classification** — short/formatted blocks are
   kept in full (I-frames); long body paragraphs are head/tail
   truncated (P-frames).
2. **RLE folding with degraded visibility** — consecutive P-frames are
   merged, but each retains a one-line summary so the LLM can still
   see every paragraph's opening text (prevents hidden-heading loss).
3. **High-pass Meta-Tag injection** — physical formatting spikes
   (bold, large font, centred) are surfaced as explicit tags.
4. **Sliding-window sharding** — documents exceeding a configurable
   block threshold are split into overlapping windows.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

from infrastructure.models import Block
from modules.parser.config import CompressorConfig
from app.core.exceptions import CompressorError

logger = logging.getLogger(__name__)

_GRAMMAR_PREFIX_RE = re.compile(r'^(\[[\d,\s-]+\]|\([\d,\s-]+\))\s*')


class SkeletonCompressor:
    """Compress a Block list into a minimal virtual skeleton.

    Achieves 90–95 % token reduction while preserving 100 % of the
    structural signal.  P-frame folds retain per-paragraph summaries
    so the LLM always has minimum visibility into every block.
    """

    def __init__(self, config: CompressorConfig | None = None):
        cfg = config or CompressorConfig()
        self.head_chars = cfg.head_chars
        self.tail_chars = cfg.tail_chars
        self.enable_rle = cfg.enable_rle
        self.rle_threshold = cfg.rle_threshold
        self.max_rle_group = cfg.max_rle_group
        self.sliding_window_threshold = cfg.sliding_window_threshold
        self.window_size = cfg.window_size
        self.window_overlap = cfg.window_overlap
        self.rle_dynamic_prefix_min_length = cfg.rle_dynamic_prefix_min_length
        self.rle_dynamic_prefix_extra = cfg.rle_dynamic_prefix_extra
    
    def compress(self, blocks: List[Block]) -> List[str]:
        """Compress *blocks* into one or more virtual skeleton strings.

        Automatically selects single-pass or sliding-window mode
        depending on the block count.

        Args:
            blocks: Ordered list of :class:`Block` objects.

        Returns:
            A list of skeleton text chunks.  Normal documents yield a
            single-element list; oversized documents yield one chunk
            per sliding window, each independently consumable by the LLM.

        Raises:
            CompressorError: If the block list is empty or compression fails.
        """
        if not blocks:
            raise CompressorError("Block 列表为空，无法压缩")
        
        try:
            # 超长文档：使用滑动窗口分片，返回多个独立骨架
            if len(blocks) > self.sliding_window_threshold:
                return self._compress_with_sliding_window(blocks)
            
            return [self._compress_single(blocks)]
            
        except CompressorError:
            raise
        except Exception as e:
            raise CompressorError(f"骨架压缩失败: {str(e)}")
    
    def _compress_single(self, blocks: List[Block]) -> str:
        """Single-pass compression for normal-length documents."""
        # 第一步：为每个 Block 生成骨架行，并标记是否为 I帧
        skeleton_items = self._classify_and_compress(blocks)
        
        # 第二步：游程折叠 v2（如果启用）
        if self.enable_rle:
            skeleton_items = self._run_length_fold_v2(skeleton_items)
        
        # 第三步：拼接为最终骨架文本
        skeleton_text = self._build_skeleton_text(skeleton_items, blocks)
        
        # 计算压缩统计
        original_chars = sum(len(b.text or "") for b in blocks)
        compressed_chars = len(skeleton_text)
        ratio = (1 - compressed_chars / max(original_chars, 1)) * 100
        logger.info(
            f"[虚拟空间压缩] 原文: {original_chars} 字符 → 骨架: {compressed_chars} 字符 "
            f"(压缩率: {ratio:.1f}%, Blocks: {len(blocks)})"
        )
        
        return skeleton_text
    
    def _compress_with_sliding_window(self, blocks: List[Block]) -> List[str]:
        """Sliding-window compression for oversized documents.

        Splits the block sequence into overlapping windows and
        compresses each independently using a thread pool.  Each
        window's compression is pure CPU work with no shared mutable
        state, making it safe to parallelise.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(blocks)
        step = self.window_size - self.window_overlap
        windows: List[Tuple[int, int]] = []
        
        start = 0
        while start < total:
            end = min(start + self.window_size, total)
            windows.append((start, end))
            if end >= total:
                break
            start += step
        
        num_windows = len(windows)
        logger.info(
            "[滑动窗口] 超长文档 (%d Blocks)，分为 %d 个窗口 (size=%d, overlap=%d)",
            total, num_windows, self.window_size, self.window_overlap,
        )
        
        original_chars = sum(len(b.text or "") for b in blocks)

        max_workers = min(num_windows, 4)
        ordered_chunks: List[Optional[str]] = [None] * num_windows

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._compress_window, blocks[ws:we], i, num_windows, total, ws, we,
                ): i
                for i, (ws, we) in enumerate(windows)
            }
            for future in as_completed(futures):
                idx = futures[future]
                ordered_chunks[idx] = future.result()

        chunks = [c for c in ordered_chunks if c is not None]

        total_skeleton_chars = sum(len(c) for c in chunks)
        ratio = (1 - total_skeleton_chars / max(original_chars, 1)) * 100
        logger.info(
            "[滑动窗口压缩] 原文: %d 字符 → 骨架总计: %d 字符 (%d 个分片, 压缩率: %.1f%%)",
            original_chars, total_skeleton_chars, len(chunks), ratio,
        )
        
        return chunks

    def _compress_window(
        self,
        window_blocks: List[Block],
        window_index: int,
        total_windows: int,
        total_blocks: int,
        ws: int,
        we: int,
    ) -> str:
        """Compress a single sliding window into a skeleton string.

        This method is stateless and safe to call from a thread pool.
        """
        items = self._classify_and_compress(window_blocks)
        if self.enable_rle:
            items = self._run_length_fold_v2(items)

        lines = [
            "=" * 60,
            f"Constellation 文档虚拟骨架 — 窗口 {window_index + 1}/{total_windows}",
            f"原始 Block 总数: {total_blocks}",
            f"本窗口 Block 范围: {ws}~{we - 1} ({we - ws} Blocks)",
            "标记说明: <Bold>=加粗, <Size:N>=字号, <Center>=居中, <Heading N>=标题样式",
            "注意: 折叠区域内每行 [id] 开头的是段落首句摘要，请仔细检查是否有遗漏的标题",
            "=" * 60,
            "",
        ]
        for item in items:
            lines.append(item["text"])
        lines.extend([
            "",
            "=" * 60,
            f"窗口 {window_index + 1} 骨架结束 (Block {ws}~{we - 1})",
            "=" * 60,
        ])

        chunk_text = "\n".join(lines)
        logger.info(
            "[滑动窗口] 窗口 %d/%d: Block %d~%d, 骨架 %d 字符",
            window_index + 1, total_windows, ws, we - 1, len(chunk_text),
        )
        return chunk_text
    
    def _classify_and_compress(self, blocks: List[Block]) -> List[dict]:
        """Phase 1: classify each block as I-frame or P-frame and generate its skeleton line."""
        items = []
        
        for block in blocks:
            is_iframe = (
                block.type in ("image", "table", "formula", "code")
                or (block.type == "text" and block.is_potential_title())
            )
            skeleton_text = block.get_skeleton_text(
                head_chars=self.head_chars,
                tail_chars=self.tail_chars,
                preserve_full_text=is_iframe,
            )
            
            if block.type in ("image", "table", "formula", "code"):
                items.append({
                    "type": "iframe",
                    "block": block,
                    "text": skeleton_text,
                })
            elif block.type == "text":
                if block.is_potential_title():
                    items.append({
                        "type": "iframe",
                        "block": block,
                        "text": skeleton_text,
                    })
                else:
                    items.append({
                        "type": "pframe",
                        "block": block,
                        "text": skeleton_text,
                    })
            else:
                items.append({
                    "type": "pframe",
                    "block": block,
                    "text": skeleton_text,
                })
        
        return items
    
    def _run_length_fold_v2(self, items: List[dict]) -> List[dict]:
        """Phase 2: RLE folding with degraded visibility.

        Consecutive P-frames exceeding ``rle_threshold`` are merged
        into a single summary record.  Each folded paragraph retains a
        one-line snippet (``[id] first_25_chars…``) so the LLM can
        still detect headings that lack formatting cues.

        An I-frame always interrupts the fold buffer.
        """
        if not items:
            return items
        
        result = []
        pframe_buffer: List[dict] = []
        
        def flush_buffer():
            nonlocal pframe_buffer
            if not pframe_buffer:
                return
            
            if len(pframe_buffer) >= self.rle_threshold:
                # 触发折叠：生成摘要头 + 每个段落的极简行
                first_block = pframe_buffer[0]["block"]
                last_block = pframe_buffer[-1]["block"]
                count = len(pframe_buffer)
                total_chars = sum(
                    len(item["block"].text or "") 
                    for item in pframe_buffer
                )
                
                # 摘要头行
                summary_lines = [
                    f"[{first_block.id} to {last_block.id}] "
                    f"<Text: {count} Paras, {total_chars} chars>"
                ]
                
                for item in pframe_buffer:
                    b = item["block"]
                    full_text = (b.text or "").strip()
                    
                    match = _GRAMMAR_PREFIX_RE.search(full_text)
                    prefix_len = match.end() if match else 0
                    
                    snippet_len = max(self.rle_dynamic_prefix_min_length, prefix_len + self.rle_dynamic_prefix_extra)
                    snippet = full_text[:snippet_len]
                    
                    # Try to preserve the last complete word (space-delimited).
                    # For CJK text (no spaces), skip this heuristic entirely.
                    if len(full_text) > snippet_len and " " in full_text[:snippet_len]:
                        next_space = full_text.find(" ", snippet_len)
                        if next_space != -1 and next_space - snippet_len < 10:
                            snippet = full_text[:next_space]
                    
                    meta = b._build_meta_tags()
                    meta_str = f" {meta}" if meta else ""
                    summary_lines.append(f"  [{b.id}]{meta_str} {snippet}...")
                
                merged_text = "\n".join(summary_lines)
                
                result.append({
                    "type": "rle_merged",
                    "blocks": [item["block"] for item in pframe_buffer],
                    "text": merged_text,
                    "start_id": first_block.id,
                    "end_id": last_block.id,
                })
            else:
                result.extend(pframe_buffer)
            
            pframe_buffer = []
        
        for item in items:
            if item["type"] == "pframe":
                pframe_buffer.append(item)
                if len(pframe_buffer) >= self.max_rle_group:
                    flush_buffer()
            else:
                flush_buffer()
                result.append(item)
        
        flush_buffer()
        
        return result
    
    def _build_skeleton_text(self, items: List[dict], blocks: List[Block]) -> str:
        """Phase 3: assemble the final skeleton text with header/footer."""
        lines = []
        
        lines.append("=" * 60)
        lines.append("Constellation 文档虚拟骨架")
        lines.append(f"原始 Block 总数: {len(blocks)}")
        lines.append(f"骨架项数量: {len(items)}")
        lines.append("标记说明: <Bold>=加粗, <Size:N>=字号, <Center>=居中, <Heading N>=标题样式")
        lines.append("注意: 折叠区域内每行 [id] 开头的是段落首句摘要，请仔细检查是否有遗漏的标题")
        lines.append("=" * 60)
        lines.append("")
        
        for item in items:
            lines.append(item["text"])
        
        lines.append("")
        lines.append("=" * 60)
        lines.append(f"骨架结束 (共 {len(blocks)} 个 Block, ID 范围: 0~{len(blocks)-1})")
        lines.append("=" * 60)
        
        return "\n".join(lines)
