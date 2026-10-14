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
from typing import List, Optional, Tuple

from infrastructure.models import Block
from modules.parser.config import CompressorConfig
from modules.parser.heading_candidates import _body_font_size, analyze_candidate_regions
from modules.parser.schemas import HeadingCandidate, HeadingCandidateSet, RegionRisk
from modules.parser.prefix_detector import PrefixDetector
from app.core.exceptions import CompressorError

logger = logging.getLogger(__name__)

# ── Skeleton header / footer constants ───────────────────────
_SEPARATOR = "=" * 60
_HEADER_TAG = "Constellation Virtual Skeleton"
_WINDOW_TAG = "Constellation Virtual Skeleton — Window {idx}/{total}"
_META_LEGEND = (
    "Legend: <Bold>=bold, <Size:N>=font larger than body text, "
    "<Center>=centred, <Heading N>=heading-style"
)
_FOLD_NOTICE = (
    "Note: lines starting with [id] inside folded regions are "
    "first-sentence summaries.  Check for missed headings."
)
_FOOTER_TAG = "Skeleton end ({count} Blocks, ID range: 0~{last})"
_WINDOW_FOOTER_TAG = "Window {idx} end (Block {ws}~{we})"


class SkeletonCompressor:
    """Compress a Block list into a minimal virtual skeleton.

    Achieves 90-95% token reduction while preserving 100% of the
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
        self.compress_max_workers = cfg.compress_max_workers
        self.enable_candidate_sparse = cfg.enable_candidate_sparse
        self.candidate_context_blocks = cfg.candidate_context_blocks
        self.sparse_preamble_blocks = cfg.sparse_preamble_blocks
        self._prefix_detector = PrefixDetector()

    # ── Public API ─────────────────────────────────────────────

    def compress(
        self,
        blocks: List[Block],
        candidates: HeadingCandidateSet | List[HeadingCandidate] | None = None,
        region_risks: dict[str, RegionRisk] | None = None,
    ) -> List[str]:
        """Compress blocks, using sparse candidate-aware routing when possible.

        ``candidates is not None`` is semantically significant: it means the
        deterministic proposal pass has already run, even when it produced an
        empty list.  In that path the skeleton contains only document preamble,
        every routed candidate with local context, and complete escape-risk
        regions.  All other ranges are represented by explicit block-ID gaps;
        body paragraphs are no longer expanded into one summary line each.
        """
        if not blocks:
            raise CompressorError("Block list is empty, cannot compress.")

        try:
            body_font_size = _body_font_size(blocks)
            if self.enable_candidate_sparse and candidates is not None:
                if isinstance(candidates, HeadingCandidateSet):
                    candidate_list = list(candidates.candidates)
                    risks = region_risks or candidates.region_risks
                else:
                    candidate_list = list(candidates)
                    risks = region_risks or {}
                return [self._compress_sparse(
                    blocks,
                    candidate_list,
                    risks,
                    body_font_size,
                )]

            # Library callers that do not run Stage 2.5 retain the standalone
            # full-skeleton API.  CaliperParser never takes this branch.
            if len(blocks) > self.sliding_window_threshold:
                return self._compress_with_sliding_window(blocks, body_font_size)
            return [self._compress_single(blocks, body_font_size)]

        except CompressorError:
            raise
        except Exception as e:
            raise CompressorError(f"Skeleton compression failed: {e}") from e

    def _compress_sparse(
        self,
        blocks: List[Block],
        candidates: List[HeadingCandidate],
        region_risks: dict[str, RegionRisk],
        body_font_size: float,
    ) -> str:
        """Render candidate neighbourhoods plus complete escape regions.

        Selection is made by list position, never by assuming contiguous block
        IDs.  This keeps the control plane valid for custom providers while gap
        records preserve the physical ID range needed by router sharding.
        """
        index_by_id = {block.id: index for index, block in enumerate(blocks)}
        candidate_by_id: dict[int, HeadingCandidate] = {}
        for candidate in candidates:
            previous = candidate_by_id.get(candidate.block_id)
            if previous is None or candidate.heading_probability > previous.heading_probability:
                candidate_by_id[candidate.block_id] = candidate

        protected: set[int] = set(range(min(self.sparse_preamble_blocks, len(blocks))))
        for block_id in candidate_by_id:
            index = index_by_id.get(block_id)
            if index is None:
                continue
            start = max(0, index - self.candidate_context_blocks)
            end = min(len(blocks), index + self.candidate_context_blocks + 1)
            protected.update(range(start, end))

        # Region membership comes from the same analyser used to produce risk
        # evidence.  Provider page/column IDs therefore cannot drift from the
        # candidate model.  Escape means broad scan: no arbitrary truncation.
        assessments = analyze_candidate_regions(blocks)
        for region_id, assessment in assessments.items():
            risk = region_risks.get(region_id, assessment.risk)
            if risk.band != "escape":
                continue
            for block_id in assessment.block_ids:
                index = index_by_id.get(block_id)
                if index is not None:
                    protected.add(index)

        # Preserve at least the first physical block for metadata extraction.
        if blocks:
            protected.add(0)

        lines = self._build_header(
            "Constellation Candidate-Aware Sparse Skeleton",
            extra_lines=[
                f"Total blocks: {len(blocks)}",
                f"Routed candidates: {len(candidate_by_id)}",
                "Only candidate neighbourhoods and escape-risk regions are expanded; "
                "<Gap> ranges contain no routed candidate.",
            ],
        )

        ordered = sorted(protected)
        cursor = 0
        for index in ordered:
            if index < cursor or index >= len(blocks):
                continue
            if index > cursor:
                omitted = blocks[cursor:index]
                first_id, last_id = omitted[0].id, omitted[-1].id
                lines.append(
                    f"[{first_id} to {last_id}] <Gap: {len(omitted)} body blocks; "
                    "no routed heading candidate>"
                )
            block = blocks[index]
            preserve_full = block.id in candidate_by_id
            lines.append(block.get_skeleton_text(
                head_chars=self.head_chars,
                tail_chars=self.tail_chars,
                preserve_full_text=preserve_full,
                body_font_size=body_font_size if body_font_size > 0 else None,
            ))
            cursor = index + 1

        if cursor < len(blocks):
            omitted = blocks[cursor:]
            lines.append(
                f"[{omitted[0].id} to {omitted[-1].id}] "
                f"<Gap: {len(omitted)} body blocks; no routed heading candidate>"
            )

        last_id = max((block.id for block in blocks), default=0)
        lines.extend([
            "",
            _SEPARATOR,
            _FOOTER_TAG.format(count=len(blocks), last=last_id),
            _SEPARATOR,
        ])
        return "\n".join(lines)

    # ── Single-pass compression ────────────────────────────────

    def _compress_single(self, blocks: List[Block], body_font_size: float = 0.0) -> str:
        """Single-pass compression for normal-length documents.

        Pipeline:
            1. Classify each block as I-frame / P-frame.
            2. Apply RLE folding (if enabled).
            3. Assemble the final skeleton text with header / footer.
        """
        skeleton_items = self._classify_and_compress(blocks, body_font_size)

        if self.enable_rle:
            skeleton_items = self._run_length_fold_v2(skeleton_items, body_font_size)

        skeleton_text = self._build_skeleton_text(skeleton_items, blocks)

        # Compression ratio logging (debug-level to avoid noise)
        original_chars = sum(len(b.text or "") for b in blocks)
        compressed_chars = len(skeleton_text)
        ratio = (1 - compressed_chars / max(original_chars, 1)) * 100
        logger.debug(
            "[Compressor] %d chars -> %d chars (%.1f%% reduction, %d blocks)",
            original_chars, compressed_chars, ratio, len(blocks),
        )

        return skeleton_text

    # ── Sliding-window compression ─────────────────────────────

    def _compress_with_sliding_window(
        self,
        blocks: List[Block],
        body_font_size: float = 0.0,
    ) -> List[str]:
        """Sliding-window compression for oversized documents.

        Splits the block sequence into overlapping windows and
        compresses each independently using a thread pool.  Each
        window's compression is pure CPU work with no shared mutable
        state, making it safe to parallelise.
        """
        import os
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(blocks)
        step = self.window_size - self.window_overlap
        windows: List[Tuple[int, int]] = []

        # Build window ranges
        start = 0
        while start < total:
            end = min(start + self.window_size, total)
            windows.append((start, end))
            if end >= total:
                break
            start += step

        num_windows = len(windows)
        logger.debug(
            "[Compressor] Sliding-window: %d blocks -> %d windows "
            "(size=%d, overlap=%d)",
            total, num_windows, self.window_size, self.window_overlap,
        )

        original_chars = sum(len(b.text or "") for b in blocks)
        # Resolve the thread-pool cap: explicit override when configured,
        # otherwise auto-scale to the host CPU count (fallback 4 when the
        # platform does not report it).  Never exceed the window count.
        if self.compress_max_workers > 0:
            worker_cap = self.compress_max_workers
        else:
            worker_cap = os.cpu_count() or 4
        max_workers = max(1, min(num_windows, worker_cap))
        logger.debug(
            "[Compressor] Parallel compression with %d workers "
            "(windows=%d, cap=%d)",
            max_workers, num_windows, worker_cap,
        )
        ordered_chunks: List[Optional[str]] = [None] * num_windows

        # Parallel compression — each window is independent
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._compress_window,
                    blocks[ws:we], i, num_windows, total, ws, we,
                    body_font_size,
                ): i
                for i, (ws, we) in enumerate(windows)
            }
            for future in as_completed(futures):
                idx = futures[future]
                ordered_chunks[idx] = future.result()

        chunks = [c for c in ordered_chunks if c is not None]

        total_skeleton_chars = sum(len(c) for c in chunks)
        ratio = (1 - total_skeleton_chars / max(original_chars, 1)) * 100
        logger.debug(
            "[Compressor] Sliding-window done: %d chars -> %d chars "
            "(%d chunks, %.1f%% reduction)",
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
        body_font_size: float = 0.0,
    ) -> str:
        """Compress a single sliding window into a skeleton string.

        This method is stateless and safe to call from a thread pool.
        """
        items = self._classify_and_compress(window_blocks, body_font_size)
        if self.enable_rle:
            items = self._run_length_fold_v2(items, body_font_size)

        # Assemble window skeleton with metadata header
        lines = self._build_header(
            _WINDOW_TAG.format(idx=window_index + 1, total=total_windows),
            extra_lines=[
                f"Total blocks: {total_blocks}",
                f"This window: blocks {ws}~{we - 1} ({we - ws} blocks)",
            ],
        )
        for item in items:
            lines.append(item["text"])
        lines.extend([
            "",
            _SEPARATOR,
            _WINDOW_FOOTER_TAG.format(idx=window_index + 1, ws=ws, we=we - 1),
            _SEPARATOR,
        ])

        chunk_text = "\n".join(lines)
        logger.debug(
            "[Compressor] Window %d/%d: blocks %d~%d, %d chars",
            window_index + 1, total_windows, ws, we - 1, len(chunk_text),
        )
        return chunk_text

    # ── Phase 1: I-frame / P-frame classification ──────────────

    def _classify_and_compress(
        self,
        blocks: List[Block],
        body_font_size: float = 0.0,
    ) -> List[dict]:
        """Classify each block as I-frame or P-frame.

        I-frames (kept in full):
            - Multimedia blocks (image, table, formula, code).
            - Text blocks that pass ``is_potential_title()``.

        P-frames (truncated):
            - All remaining text blocks.

        ``body_font_size`` anchors both the title heuristic and the
        relative ``Size:`` meta-tag to the document's actual body font
        instead of an absolute 12pt assumption.
        """
        items: List[dict] = []
        min_body = body_font_size if body_font_size > 0 else 12.0

        for block in blocks:
            is_iframe = (
                block.type in ("image", "table", "formula", "code")
                or (
                    block.type == "text"
                    and block.is_potential_title(min_body_size=min_body)
                )
            )
            skeleton_text = block.get_skeleton_text(
                head_chars=self.head_chars,
                tail_chars=self.tail_chars,
                preserve_full_text=is_iframe,
                body_font_size=body_font_size if body_font_size > 0 else None,
            )

            frame_type = "iframe" if is_iframe else "pframe"
            items.append({
                "type": frame_type,
                "block": block,
                "text": skeleton_text,
            })

        return items

    # ── Phase 2: RLE folding with degraded visibility ──────────

    def _run_length_fold_v2(
        self,
        items: List[dict],
        body_font_size: float = 0.0,
    ) -> List[dict]:
        """Fold consecutive P-frames into a single summary record.

        When ``rle_threshold`` or more consecutive P-frames are
        buffered, they are merged into one summary block.  Each folded
        paragraph retains a one-line snippet (``[id] prefix…``) so the
        LLM can still detect headings that lack formatting cues.

        An I-frame always interrupts the fold buffer.  A P-frame whose
        block passes ``is_potential_title()`` also triggers a flush so
        that the title is not swallowed into the fold.
        """
        if not items:
            return items

        min_body = body_font_size if body_font_size > 0 else 12.0
        meta_baseline = body_font_size if body_font_size > 0 else None

        result: List[dict] = []
        pframe_buffer: List[dict] = []

        def flush_buffer() -> None:
            """Flush the P-frame buffer, folding if threshold is met."""
            nonlocal pframe_buffer
            if not pframe_buffer:
                return

            if len(pframe_buffer) >= self.rle_threshold:
                # RLE fold: emit summary header + per-paragraph snippet
                first_block = pframe_buffer[0]["block"]
                last_block = pframe_buffer[-1]["block"]
                count = len(pframe_buffer)
                total_chars = sum(
                    len(item["block"].text or "")
                    for item in pframe_buffer
                )

                summary_lines = [
                    f"[{first_block.id} to {last_block.id}] "
                    f"<Text: {count} Paras, {total_chars} chars>"
                ]

                for item in pframe_buffer:
                    b = item["block"]
                    full_text = (b.text or "").strip()

                    # Compute snippet length: at least
                    # rle_dynamic_prefix_min_length chars, or enough
                    # to cover any detected structural prefix.
                    prefix_len = self._prefix_detector.detect_length(full_text)
                    snippet_len = max(
                        self.rle_dynamic_prefix_min_length,
                        prefix_len + self.rle_dynamic_prefix_extra,
                    )
                    snippet = full_text[:snippet_len]

                    # Preserve the last complete word for
                    # space-delimited text.  CJK has no spaces, so
                    # this heuristic is skipped automatically.
                    if len(full_text) > snippet_len and " " in full_text[:snippet_len]:
                        next_space = full_text.find(" ", snippet_len)
                        if next_space != -1 and next_space - snippet_len < 10:
                            snippet = full_text[:next_space]

                    meta = b._build_meta_tags(body_font_size=meta_baseline)
                    meta_str = f" {meta}" if meta else ""
                    summary_lines.append(f"  [{b.id}]{meta_str} {snippet}...")

                result.append({
                    "type": "rle_merged",
                    "blocks": [item["block"] for item in pframe_buffer],
                    "text": "\n".join(summary_lines),
                    "start_id": first_block.id,
                    "end_id": last_block.id,
                })
            else:
                # Below threshold — emit each P-frame individually
                result.extend(pframe_buffer)

            pframe_buffer = []

        for item in items:
            if item["type"] == "pframe":
                # Flush before a potential title to prevent it from
                # being absorbed into an RLE fold.
                block = item["block"]
                if block.type == "text" and block.is_potential_title(min_body_size=min_body):
                    flush_buffer()
                pframe_buffer.append(item)
                if len(pframe_buffer) >= self.max_rle_group:
                    flush_buffer()
            else:
                # I-frame always breaks the fold
                flush_buffer()
                result.append(item)

        flush_buffer()
        return result

    # ── Phase 3: Skeleton text assembly ────────────────────────

    @staticmethod
    def _build_header(title: str, extra_lines: list[str] | None = None) -> list[str]:
        """Build a standard skeleton header block.

        Args:
            title: Header title (e.g. ``_HEADER_TAG`` or a window tag).
            extra_lines: Optional additional lines to insert after
                the title (e.g. window-specific metadata).

        Returns:
            List of header lines (including separators).
        """
        lines = [_SEPARATOR, title]
        if extra_lines:
            lines.extend(extra_lines)
        lines.extend([_META_LEGEND, _FOLD_NOTICE, _SEPARATOR, ""])
        return lines

    def _build_skeleton_text(self, items: List[dict], blocks: List[Block]) -> str:
        """Assemble the final skeleton text with header and footer."""
        lines = self._build_header(
            _HEADER_TAG,
            extra_lines=[
                f"Total blocks: {len(blocks)}",
                f"Skeleton items: {len(items)}",
            ],
        )

        for item in items:
            lines.append(item["text"])

        # Use the actual max block id: ids are 0-based and contiguous
        # today, but the footer should not silently assume that.
        last_id = max((b.id for b in blocks), default=0)
        lines.extend([
            "",
            _SEPARATOR,
            _FOOTER_TAG.format(count=len(blocks), last=last_id),
            _SEPARATOR,
        ])

        return "\n".join(lines)
