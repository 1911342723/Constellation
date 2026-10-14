"""Interval Resolver — Constellation Stage 4.

Converts LLM-produced heading anchors back into a lossless document
tree by performing three sequential operations:

1. **Fuzzy anchor correction** — cross-validates each ``block_id``
   against its ``snippet`` using Levenshtein distance and auto-corrects
   off-by-one (or larger) errors within a configurable search radius.
2. **Hierarchy validation** — detects and repairs level jumps (e.g.
   L1 -> L3) to guarantee a well-formed tree with no gaps.
3. **Forced-closure interval slicing** — computes non-overlapping,
   exhaustive ``[start, end]`` intervals and assembles lossless
   Markdown content for each section.
"""
from __future__ import annotations

import copy
import bisect
import re
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

from infrastructure.models import Block
from modules.parser.analysis_context import block_index, current_block_store

from modules.parser.anchor_alignment import MonotonicAnchorAligner
from modules.parser.global_inference import GlobalHeadingInference
from modules.parser.heading_candidates import generate_heading_candidate_set
from modules.parser.hierarchy import HierarchyRepairer, hierarchy_is_legal
from modules.parser.schemas import (
    ChapterNode,
    DocumentNode,
    HeadingCandidate,
    HeadingCandidateSet,
)
from modules.parser.config import ParserConfig, ResolverConfig
from modules.parser.titles import PSEUDO_ROOT_TITLE, strip_title_emphasis
from app.core.exceptions import AssemblerError

import logging

if TYPE_CHECKING:  # 仅类型标注需要，避免运行时把仓库层拉进解析层的导入图
    from infrastructure.blocks import BlockStore

logger = logging.getLogger(__name__)

_TRUNCATION_MARKER_RE = re.compile(r"\[(?:省略|omitted)[^\]]*\]", re.IGNORECASE)


# ── Levenshtein Implementation Resolution ──

def _pure_python_levenshtein(s1: str, s2: str) -> float:
    """Pure-Python two-row DP Levenshtein similarity ratio."""
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0
    prev = list(range(len2 + 1))
    curr = [0] * (len2 + 1)
    for i in range(1, len1 + 1):
        curr[0] = i
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    distance = prev[len2]
    return 1.0 - (distance / max(len1, len2))


def _resolve_levenshtein_impl():
    """Resolve the fastest available Levenshtein backend once."""
    try:
        from rapidfuzz.distance import Levenshtein as _RL
        return _RL.normalized_similarity
    except ImportError:
        pass
    try:
        from Levenshtein import ratio
        return ratio
    except ImportError:
        pass
    return _pure_python_levenshtein


_lev_impl = _resolve_levenshtein_impl()


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """Compute Levenshtein similarity ratio (0.0-1.0, 1.0 = identical).

    The underlying C-extension backend is resolved once at module load
    to avoid repeated try/except import overhead on the hot path.
    """
    if not s1 and not s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    return _lev_impl(s1, s2)


class IntervalResolver:
    """Resolve LLM anchors into a lossless document tree.

    Pipeline: fuzzy anchor correction -> hierarchy validation ->
    forced-closure interval slicing -> Markdown assembly -> tree build.
    """

    # ── Initialisation ──

    def __init__(
        self,
        blocks: List[Block],
        config: ResolverConfig | None = None,
        parser_config: ParserConfig | None = None,
        heading_candidates: List[HeadingCandidate] | HeadingCandidateSet | None = None,
        block_store: Optional["BlockStore"] = None,
    ):
        cfg = config or ResolverConfig()
        parser_cfg = parser_config or ParserConfig()
        # L01：正文按最终区间从仓库回读的句柄。显式传入优先；否则取本次解析作用域里
        # 登记的那一份（两遍模式下由 parser 登记）。都没有就走内存块——小文档的正常路径。
        self._block_store = block_store if block_store is not None else current_block_store()
        self.blocks = blocks
        # 索引只建一次：id 唯一性校验、id→block 映射、有序 id 都由本次解析共享的
        # BlockIndex 提供（L01）。此前每个决策阶段各建一份 ``{b.id: b for b in blocks}``，
        # 同一份不变的 blocks 被重新物化七八次，且重复 id 只有本类校验、别处没有。
        try:
            self._index = block_index(blocks)
        except ValueError as exc:
            raise AssemblerError(str(exc)) from exc
        self.block_map = self._index.by_id
        self._ordered_ids = list(self._index.ordered_ids)
        self._relation_block_ids = [b.id for b in self._index.blocks
                                    if (b.metadata or {}).get("content_relations")]
        self.total_blocks = len(blocks)
        self.max_block_id = self._ordered_ids[-1] if self._ordered_ids else 0
        self.fuzzy_radius = cfg.fuzzy_anchor_radius
        self.fuzzy_min_similarity = cfg.fuzzy_min_similarity
        self.anchor_match_min_length = cfg.anchor_match_min_length
        self.anchor_match_levenshtein_threshold = cfg.anchor_match_levenshtein_threshold
        self.snippet_prefix_check_len = cfg.snippet_prefix_check_len
        self.snippet_exact_match_len = cfg.snippet_exact_match_len
        self.snippet_extra_chars = cfg.snippet_extra_chars
        self.level_jump_font_size_tolerance = cfg.level_jump_font_size_tolerance
        self.orphan_bold_max_text_len = cfg.orphan_bold_max_text_len
        self.max_orphan_promotions = cfg.max_orphan_promotions

        if isinstance(heading_candidates, HeadingCandidateSet):
            self.candidate_set = heading_candidates
        else:
            # Direct resolver users historically passed either None or the
            # capped v1 route list.  Generate the uncapped proposal set, then
            # let explicitly supplied candidates override the same block.
            generated = generate_heading_candidate_set(blocks)
            if heading_candidates is None:
                self.candidate_set = generated
            else:
                by_id = {candidate.block_id: candidate for candidate in generated.candidates}
                by_id.update({candidate.block_id: candidate for candidate in heading_candidates})
                self.candidate_set = generated.model_copy(
                    update={"candidates": [by_id[key] for key in sorted(by_id)]}
                )
        self.heading_candidates = list(self.candidate_set.candidates)
        self._candidate_by_id = {
            candidate.block_id: candidate for candidate in self.heading_candidates
        }
        self._aligner = MonotonicAnchorAligner(blocks, cfg)
        self._global_inference = GlobalHeadingInference(
            blocks, self.candidate_set, parser_config=parser_cfg,
        )
        # Kept as a legacy adapter for direct callers of hierarchy helpers.  The
        # main pipeline's levels are selected once by GlobalHeadingInference.
        self._hierarchy = HierarchyRepairer(
            blocks,
            candidates=self.heading_candidates,
            config=cfg,
        )
        # Exposed alias kept for backwards compatibility with callers
        # and tests that inspect the per-level font evidence.
        self._level_font = self._hierarchy.level_font
        # Body font size is an invariant of the block list; computed
        # lazily once instead of per-anchor (confidence scoring used to
        # recompute the median for every anchor — O(anchors x blocks)).
        self._body_font_size_cache: float | None = None
        # Set when resolve() falls back to a synthetic single-root
        # chapter for anchor-less documents.  Used instead of comparing
        # titles against the literal string "Document", which a real
        # document's first heading could legitimately match.
        self._pseudo_root: bool = False

    # id 唯一性 / 稀疏 / 乱序的契约现在由 BlockIndex 单点保证（L01）：
    # 重复 id 直接拒绝（本类转译为 AssemblerError，对外消息不变）；稀疏与乱序合法，
    # id 只作身份字段，区间遍历靠有序 id 二分，对二者一致成立。原先本类另有一份
    # _validate_block_index，与索引构建处各校验一遍，已删除。

    @property
    def lossless_fallback(self) -> bool:
        """resolve() 是否退到了「零特征文档」的伪根兜底（全篇装进一个合成节点）。

        输出层据此避免把内部哨兵标题当成文档里真实存在的标题渲染出去。
        """
        return self._pseudo_root

    # ── Main Entry Point ──

    def initial_review_plan(self, policy=None):
        """A01 初判：无投票解码 + 复审计划，与终判共用**同一个**解码器实例。

        为什么走 resolver 而不是让 parser 自己建一个 GlobalHeadingInference：解码器由
        resolver 持有，两处各建一个就会各自算一遍候选/区域风险，初判据以选区的权重与
        终判实际用的权重可能不同——那时选出来的「不确定区域」与最终决策无关。
        """
        return self._global_inference.initial_decode(policy)

    def record_routed_blocks(self, block_ids) -> None:
        """Known observations only; unknown legacy callers must not imply rejection."""
        if block_ids is not None:
            previous = self._global_inference._routed_block_ids or set()
            self._global_inference._routed_block_ids = previous | set(block_ids)

    def resolve(self, chapters: List[ChapterNode]) -> List[DocumentNode]:
        """Resolve LLM observations through alignment, global DP and closure.

        The order is an invariant: monotonic physical alignment first;
        physical/region-risk validation and ``NONE/L1..L6`` inference second;
        forced-closure slicing last.  Closure is not permitted to add anchors.
        """
        try:
            observations = copy.deepcopy(chapters)
            if observations and not all(chapter.anchor_votes for chapter in observations):
                observations = self._aligner.align(
                    observations,
                    candidates=self.heading_candidates,
                )
            else:
                observations.sort(key=lambda chapter: chapter.start_block_id)

            # Selection, physical/risk validation and level inference happen
            # exactly once in the document-wide DP.  Legacy fuzzy correction,
            # hierarchy repair and inverse insertion remain adapters only and
            # are deliberately absent from this path.
            chapters = self._global_inference.decode(observations)
            if not chapters:
                logger.warning(
                    "[IntervalResolver] Global inference selected no headings; "
                    "falling back to a single lossless root node",
                )
                first_text_block = next(
                    (
                        block for block in self.blocks
                        if block.type == "text" and block.text
                    ),
                    None,
                )
                snippet = (
                    first_text_block.text.strip()[:40]
                    if first_text_block else PSEUDO_ROOT_TITLE
                )
                self._pseudo_root = True
                chapters = [ChapterNode(
                    block_id=min(self.block_map, default=0),
                    title=PSEUDO_ROOT_TITLE,
                    level=1,
                    snippet=snippet,
                    globally_inferred=True,
                )]

            if not hierarchy_is_legal(chapters):
                raise AssemblerError("Global heading sequence violates hierarchy legality")

            intervals = self._compute_intervals(chapters)
            flat_nodes = self._build_flat_nodes(intervals)
            tree_nodes = self._build_tree(flat_nodes)
            # Keep physical ownership unchanged; expose the target's semantic
            # section separately (a page-bottom footnote may reference an
            # earlier section). Never duplicate source content to fix attachment.
            from bisect import bisect_right
            heading_ids = sorted(node.start_block_id for node in flat_nodes)
            for node in flat_nodes:
                for relation in node.content_relations:
                    target = relation.get("target_block_id")
                    pos = bisect_right(heading_ids, target) - 1 if target is not None else -1
                    relation["target_section_start_block_id"] = heading_ids[pos] if pos >= 0 else None

            logger.info(
                "[IntervalResolver] Done: %d globally inferred chapters -> "
                "%d top-level nodes",
                len(flat_nodes),
                len(tree_nodes),
            )
            return tree_nodes

        except AssemblerError:
            raise
        except Exception as e:
            raise AssemblerError(f"Interval resolution failed: {str(e)}") from e

    # ── Fuzzy Anchor Correction ──

    def _estimate_body_font_size(self) -> float:
        """Estimate body font size (char-weighted mode, shared estimator).

        Delegates to the same character-weighted estimator used by the
        candidate generator and the compressor, so the whole pipeline
        shares one notion of "body size".  This used to be a separate
        block-median implementation, which annotation-heavy PDFs drag
        toward caption/reference sizes (the exact failure the
        char-weighted mode was introduced to fix).

        Cached after the first call — the block list never changes for
        the lifetime of a resolver instance.
        """
        if self._body_font_size_cache is not None:
            return self._body_font_size_cache

        from modules.parser.heading_candidates import _body_font_size

        result = _body_font_size(self.blocks)
        self._body_font_size_cache = result
        return result

    def _compute_anchor_confidence(self, chapter: ChapterNode) -> float:
        """Compute confidence for an anchor based on physical features.

        Returns 0.0-1.0 where:
        - 1.0: heading style or font significantly larger than body
        - 0.8: bold text
        - 0.7: font slightly larger than body
        - 0.5: plain text block with no formatting cues

        The LLM's own confidence (if provided) is blended using
        ``max(physical, llm)`` — physical evidence is authoritative
        when strong, but LLM confidence can boost plain-text anchors.
        """
        block = self.block_map.get(chapter.start_block_id)
        if not block:
            return max(chapter.confidence * 0.5, 0.3)

        body_size = self._estimate_body_font_size()
        score = 0.5  # base: plain text

        if block.is_heading_style:
            score = 1.0
        elif block.font_size and body_size > 0:
            ratio = block.font_size / body_size
            if ratio >= 1.4:
                score = 1.0   # significantly larger
            elif ratio >= 1.15:
                score = 0.85  # moderately larger
            elif ratio >= 1.05:
                score = 0.7   # slightly larger
            elif block.is_bold:
                score = 0.75  # bold but same size
        elif block.is_bold:
            score = 0.75

        # Blend: take the *lower* of physical evidence and LLM/router
        # confidence — either dimension being suspicious should widen
        # the fuzzy search.  This is what makes the downgrade channel's
        # "reduced confidence => wider re-anchor search" contract real:
        # a downgraded anchor (confidence 0.4) widens the radius even
        # when the block looks physically fine.  (The earlier
        # ``max(score, confidence * score)`` was an identity for any
        # confidence <= 1 — the confidence channel was silently dead.)
        return min(score, max(chapter.confidence, 0.0))

    def _fuzzy_anchor_correction(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Cross-validate each anchor's ``block_id`` against its ``snippet``.

        Mismatches trigger a sliding-window search within ``+/-fuzzy_radius``
        blocks using Levenshtein distance to find the correct anchor.
        """
        corrected = []
        correction_count = 0

        for ch in chapters:
            original_id = ch.start_block_id
            snippet = ch.snippet.strip() if ch.snippet else ch.title.strip()

            if not snippet:
                corrected.append(ch)
                continue

            # Check whether current block_id matches the snippet
            if self._is_anchor_match(original_id, snippet):
                corrected.append(ch)
                continue

            # Low-confidence anchors get wider search radius
            confidence = self._compute_anchor_confidence(ch)
            effective_radius = int(self.fuzzy_radius * (2.0 - confidence))

            # Mismatch: search the sliding window for the best match
            best_id, best_score = self._search_best_match(
                original_id, snippet, radius=effective_radius,
            )

            if best_id is not None and best_id != original_id:
                logger.warning(
                    f"[FuzzyAnchor] Corrected block_id {original_id} -> {best_id} "
                    f"(similarity: {best_score:.2f}, snippet: '{snippet[:30]}...')"
                )
                ch.start_block_id = best_id
                correction_count += 1
            elif best_id is None:
                logger.warning(
                    f"[FuzzyAnchor] Unable to correct block_id={original_id}, "
                    f"snippet='{snippet[:30]}...', keeping original value"
                )

            corrected.append(ch)

        if correction_count > 0:
            logger.info("[FuzzyAnchor] Corrected %d anchors in total", correction_count)
        else:
            logger.info("[FuzzyAnchor] All anchors verified; no corrections needed")

        return corrected

    # ── Anchor Title Restoration ──

    def _restore_anchor_titles(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Recover full heading text when the LLM echoes a truncated skeleton line."""
        restored_count = 0

        for ch in chapters:
            block = self.block_map.get(ch.start_block_id)
            if not block or block.type != "text" or not block.text:
                continue

            full_title = block.text.strip()
            if not full_title:
                continue

            if not self._should_restore_title(ch.title, ch.snippet, full_title):
                continue

            logger.info(
                "[TitleRepair] Restored anchor title: '%s' -> '%s'",
                ch.title[:40],
                full_title[:60],
            )
            ch.title = full_title
            if not ch.snippet or self._contains_truncation_marker(ch.snippet):
                ch.snippet = full_title[:40]
            restored_count += 1

        if restored_count > 0:
            logger.info("[TitleRepair] Restored %d truncated anchor titles", restored_count)

        return chapters

    @staticmethod
    def _contains_truncation_marker(text: str) -> bool:
        if not text:
            return False
        return bool(_TRUNCATION_MARKER_RE.search(text))

    def _should_restore_title(self, title: str, snippet: str, full_title: str) -> bool:
        if self._contains_truncation_marker(title) or self._contains_truncation_marker(snippet):
            return True

        normalized_title = (title or "").strip()
        if "..." not in normalized_title and "?" not in normalized_title:
            return False

        normalized_full = full_title.strip()
        if len(normalized_full) <= len(normalized_title):
            return False

        title_prefix = normalized_title.replace("?", "").replace("...", "").strip()
        if len(title_prefix) < 8:
            return False

        return normalized_full.startswith(title_prefix)

    # ── Anchor Matching Utilities ──

    def _is_anchor_match(self, block_id: int, snippet: str) -> bool:
        """Return ``True`` if the block at *block_id* matches *snippet*.

        For ultra-short snippets (<= 5 chars), only exact substring
        matching is used because Levenshtein distance is unreliable
        on short strings (a single-char difference can swing the
        ratio from 1.0 to 0.5).
        """
        if block_id not in self.block_map:
            return False

        block = self.block_map[block_id]
        block_text = (block.text or "").strip()

        if not block_text:
            return False

        # Exact substring / prefix check (always performed)
        if (
            snippet in block_text
            or block_text.startswith(snippet[: self.snippet_prefix_check_len])
        ):
            return True

        # Ultra-short snippets: exact match only — Levenshtein is too noisy
        if len(snippet) <= self.anchor_match_min_length:
            return False

        # Levenshtein fuzzy check for longer snippets
        block_head = block_text[: len(snippet) + self.snippet_extra_chars]
        ratio = _levenshtein_ratio(snippet.lower(), block_head.lower())
        return ratio >= self.anchor_match_levenshtein_threshold

    def _search_best_match(
        self,
        center_id: int,
        snippet: str,
        radius: int | None = None,
    ) -> Tuple[Optional[int], float]:
        """Search ``[center_id +/- radius]`` for the best Levenshtein match.

        Args:
            center_id: The original block_id to search around.
            snippet: The title snippet to match against.
            radius: Override for ``self.fuzzy_radius``.  Used by
                confidence-aware search to widen the window for
                low-confidence anchors.

        Returns:
            ``(block_id, score)`` of the best match, or ``(None, 0.0)``.
        """
        if radius is None:
            radius = self.fuzzy_radius

        best_id = None
        best_score = 0.0
        snippet_len = len(snippet)
        snippet_lower = snippet.lower()
        snippet_prefix = snippet[: self.snippet_exact_match_len]

        # Length tolerance: skip blocks whose text length differs by more
        # than 50% of the snippet length (cheap pre-filter before O(n*m)
        # Levenshtein).
        len_tolerance = max(snippet_len * 0.5, 10)

        search_start = max(0, center_id - radius)
        search_end = min(self.max_block_id, center_id + radius)

        # ── Pass 1: nearest exact match, spiraling out from the center.
        # Fuzzy correction exists to fix off-by-a-few drift, so the
        # exact match *closest to the claimed id* is the right target.
        # A left-to-right scan used to return the leftmost match, which
        # let TOC echo lines (e.g. "3.2 Attention . . . . 12") dozens of
        # blocks away hijack the anchor from the true heading next door.
        for offset in range(0, radius + 1):
            candidates = (center_id,) if offset == 0 else (
                center_id - offset, center_id + offset,
            )
            for bid in candidates:
                if bid < search_start or bid > search_end:
                    continue
                block = self.block_map.get(bid)
                if block is None:
                    continue
                block_text = (block.text or "").strip()
                if not block_text:
                    continue
                if snippet in block_text or block_text.startswith(snippet_prefix):
                    return bid, 0.95

        # ── Pass 2: best Levenshtein match over the window, with a
        # position penalty for distant blocks.
        for bid in range(search_start, search_end + 1):
            if bid not in self.block_map:
                continue

            block = self.block_map[bid]
            block_text = (block.text or "").strip()

            if not block_text:
                continue

            # Length pre-filter: skip blocks whose text is wildly different
            if abs(len(block_text) - snippet_len) > len_tolerance:
                continue

            # Compare block text head against snippet
            block_head = block_text[: snippet_len + self.snippet_extra_chars]
            score = _levenshtein_ratio(snippet_lower, block_head.lower())

            # Position-relative span penalty
            offset = abs(bid - center_id)
            if offset > (self.fuzzy_radius * 0.3) and score < 1.0:
                penalty = (offset / self.fuzzy_radius) * 0.1
                score -= penalty

            if score > best_score:
                best_score = score
                best_id = bid

        if best_score >= self.fuzzy_min_similarity:
            return best_id, best_score

        return None, 0.0

    # ── Hierarchy Validation and Repair ──

    def _validate_hierarchy(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Legacy repair adapter; globally inferred levels are immutable."""
        if chapters and all(chapter.globally_inferred for chapter in chapters):
            if not hierarchy_is_legal(chapters):
                raise AssemblerError("Globally inferred hierarchy is illegal")
            return chapters
        repaired = self._hierarchy.repair(chapters)
        self._level_font = self._hierarchy.level_font
        return repaired

    # ── Anchor Validation and Deduplication ──

    def _validate_anchors(self, chapters: List[ChapterNode]):
        """Clamp out-of-range ``block_id`` values to ``[0, max_block_id]``."""
        for ch in chapters:
            if ch.start_block_id < 0 or ch.start_block_id > self.max_block_id:
                logger.warning(
                    f"[IntervalResolver] Anchor block_id={ch.start_block_id} is out of range "
                    f"(0-{self.max_block_id}); clamping to valid range"
                )
                ch.start_block_id = max(0, min(ch.start_block_id, self.max_block_id))

    def _deduplicate_anchors(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Remove duplicate ``block_id`` entries, keeping the first occurrence."""
        seen = set()
        result = []
        for ch in chapters:
            if ch.start_block_id not in seen:
                seen.add(ch.start_block_id)
                result.append(ch)
            else:
                logger.warning(
                    f"[Dedup] Removing duplicate anchor: block_id={ch.start_block_id}, title='{ch.title}'"
                )
        return result

    # ── Interval Computation ──

    def _compute_intervals(self, chapters: List[ChapterNode]) -> List[dict]:
        """Compute exhaustive intervals from a final global decision sequence.

        Legacy callers sometimes invoke this helper with raw LLM chapters.
        They are routed through the same alignment + global decoder *before*
        interval construction.  No inverse-audit mutation is allowed after
        boundaries exist.
        """
        if chapters and not all(chapter.globally_inferred for chapter in chapters):
            observations = self._aligner.align(
                copy.deepcopy(chapters),
                candidates=self.heading_candidates,
            )
            chapters = self._global_inference.decode(observations)

        intervals = []
        for index, chapter in enumerate(chapters):
            start_id = chapter.start_block_id
            end_id = (
                chapters[index + 1].start_block_id - 1
                if index + 1 < len(chapters)
                else self.max_block_id
            )
            if end_id < start_id:
                end_id = start_id
            intervals.append({
                "chapter": chapter,
                "start_id": start_id,
                "end_id": end_id,
            })
        return intervals

    # ── Inverse Audit and Orphan Promotion ──

    def _infer_orphan_level(self, block: Block, parent_level: int) -> int:
        """Infer the heading level for a swallowed orphan block.

        Delegates to the unified :class:`HierarchyRepairer`, reusing the
        font evidence accumulated during ``_validate_hierarchy``.
        """
        return self._hierarchy.infer_orphan_level(block, parent_level)

    def _candidate_promotion_level(
        self,
        block: Block,
        parent_level: int,
    ) -> Optional[int]:
        """Return a promotion level when Stage 2.5 candidate evidence is strong."""
        candidate = self._candidate_by_id.get(block.id)
        if candidate is None:
            return None

        strong_kinds = {
            "explicit_heading_style",
            "outline_level",
            "visible_numbering",
            "effective_numbering",
            "semantic_title",
            "toc_destination",
            "font_ratio",
        }
        positive_kinds = candidate.evidence_kinds(polarity=1)
        structural_kinds = positive_kinds & strong_kinds
        if candidate.promotion_probability < 0.45 or not structural_kinds:
            return None

        # Semantic level is an independent, lowest-priority observation.  It
        # may guide this legacy promotion adapter but never populates or
        # outranks numbering/style evidence.
        return (
            candidate.numbering_level
            or candidate.style_level
            or candidate.semantic_level
            or self._infer_orphan_level(block, parent_level)
        )

    def _inverse_audit_and_repair(self, intervals: List[dict]) -> List[dict]:
        """Deprecated no-op: inverse audit is now a pre-DP proposal channel."""
        logger.info(
            "[InverseAudit] No post-closure insertion; proposals were consumed by global inference"
        )
        return intervals

    # ── Content Extraction and Tree Building ──

    def _build_flat_nodes(self, intervals: List[dict]) -> List[DocumentNode]:
        """Extract content for each interval and build flat :class:`DocumentNode` list."""
        nodes = []

        for interval in intervals:
            chapter = interval["chapter"]
            start_id = interval["start_id"]
            end_id = interval["end_id"]

            section_type = self._infer_section_type(chapter.title)

            # For a virtual root node generated by zero-feature document
            # fallback, do not skip the original body content.  Detected
            # via the explicit pseudo-root flag — comparing the title
            # against the literal "Document" would also swallow a real
            # first heading that happens to be named "Document".
            is_pseudo_root = self._pseudo_root and chapter.title == PSEUDO_ROOT_TITLE
            skip_title_id = None if is_pseudo_root else start_id
            content = self._extract_content(
                start_id, end_id,
                skip_title_id=skip_title_id,
                skip_title_span=None if is_pseudo_root else self._title_span_of(start_id),
            )

            # 标题是结构字段（这一节叫什么名字），不是排版：DOCX 的加粗标题会被
            # provider 渲染成 ``**…**``，全局推断又会把带记号的 block.text 覆写回
            # chapter.title。在建节点这一处剥掉，章节大纲 / sections / paper_data /
            # full_markdown 全部出口一次到位；正文 content 不经过这里，仍逐字保真。
            node = DocumentNode(
                title=strip_title_emphasis(chapter.title),
                level=chapter.level,
                start_block_id=start_id,
                end_block_id=end_id,
                content=content,
                children=[],
                section_type=section_type,
                confidence_details=dict(chapter._confidence_details),
                content_relations=[
                    {"source_block_id": bid, **relation}
                    for bid in self._relation_block_ids if start_id <= bid <= end_id
                    for relation in (self.block_map[bid].metadata or {}).get("content_relations", [])
                ],
            )

            nodes.append(node)

        return nodes

    def _build_tree(self, flat_nodes: List[DocumentNode]) -> List[DocumentNode]:
        """Build a tree from flat nodes using a stack-based algorithm (O(n))."""
        root_nodes: List[DocumentNode] = []
        stack: List[Tuple[int, DocumentNode]] = []

        for node in flat_nodes:
            level = node.level

            while stack and stack[-1][0] >= level:
                stack.pop()

            if not stack:
                root_nodes.append(node)
            else:
                _, parent = stack[-1]
                parent.children.append(node)

            stack.append((level, node))

        return root_nodes

    def _extract_content(
        self,
        start_id: int,
        end_id: int,
        skip_title_id: Optional[int] = None,
        skip_title_span: Optional[tuple[int, int]] = None,
    ) -> str:
        """Losslessly render blocks ``[start_id, end_id]`` as Markdown.

        修复 B09:旧实现 ``for block_id in range(start_id, end_id + 1)`` 遍历整个
        整数区间,代价随 id 数值跨度线性增长——两个 id 为 0 和 1,000,000 的块
        要空转一百万次、约 40ms 才吐出 10 余字符;稀疏 id 越大越慢。

        改为在有序 id 数组上二分定位区间端点,只遍历落在 ``[start_id, end_id]``
        内的真实块。代价与真实块数量成正比,与 id 数值跨度无关:相同块数、
        不同跨度耗时接近(实测 0→1e6 与 0→1 均降到 ~0.01ms 量级)。
        """
        # bisect_left(start) .. bisect_right(end) 精确圈出闭区间 [start, end] 内
        # 的真实 id,不多不少——这保证了区间覆盖既无遗漏(端点含在内)也无重复
        # (相邻区间以 end=next_start-1 衔接,不会共享同一 id)。
        lo = bisect.bisect_left(self._ordered_ids, start_id)
        hi = bisect.bisect_right(self._ordered_ids, end_id)
        interval_ids = self._ordered_ids[lo:hi]

        # L01 第二遍：正文按**最终区间**从块仓库回读。决策阶段拿到的是有界预览
        # （text_is_preview），整篇正文不常驻内存；只有区间定下来之后才为这一段读全文。
        blocks_by_id = self._resolve_interval_blocks(interval_ids)

        content_parts = []
        for block_id in interval_ids:
            block = blocks_by_id[block_id]

            if block_id == skip_title_id and block.type == "text":
                # A02：只摘掉**标题跨度**那几个字符，块内其余字符仍归正文。
                # 旧行为是跳过整个起始块，于是 ``**Implementation.** We first…``
                # 这种 run-in 标题段落会连正文一起消失——标题找对了，正文没了。
                remainder = self._block_without_title_span(block, skip_title_span)
                if remainder is None:
                    continue
                block = remainder

            markdown = block.to_markdown()
            if markdown:
                content_parts.append(markdown)

        return "\n\n".join(content_parts)

    @staticmethod
    def _block_without_title_span(
        block: Block, span: Optional[tuple[int, int]]
    ) -> Optional[Block]:
        """把标题跨度从块里摘掉，返回只含正文的块；整块都是标题时返回 ``None``。

        半开区间 ``[start, end)``，所以「标题字符」与「正文字符」的去向可以逐一核对：
        两者并集恰好是原文、交集为空（A02 的验收口径是逐 atom 检查每个字符的去向）。
        """
        text = block.text or ""
        if not span:
            return None  # 没有跨度信息时按整块是标题处理（与旧行为一致）
        start, end = span
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        remainder = (text[:start] + text[end:]).strip()
        if not remainder:
            return None
        return block.model_copy(update={"text": remainder})

    def _title_span_of(self, block_id: int) -> Optional[tuple[int, int]]:
        """取该块被判定的标题跨度（来自候选集，与候选生成同一份判定）。"""
        candidate = self._candidate_by_id.get(block_id)
        span = getattr(candidate, "title_span", None) if candidate else None
        if span is None:
            return None
        return int(span.start), int(span.end)

    def _resolve_interval_blocks(self, interval_ids: Sequence[int]) -> dict[int, Block]:
        """取区间内块的**完整**正文：有仓库就回读，没仓库就用内存里的块。

        并发的一道硬约束：内存里的块若是有界预览（``text_is_preview``），而又没有仓库
        可回读，那么继续拼装就会产出一份**被截断却声称完整**的正文。本仓反复出现过这类
        缺陷（XLSX 声称 truncated=false、页眉被删无从恢复），所以这里直接抛错而不是
        悄悄输出短正文——宁可失败得响，也不交付静默残缺的产物。
        """
        if self._block_store is None:
            missing_full_text = [
                bid for bid in interval_ids
                if (self.block_map[bid].metadata or {}).get("text_is_preview")
            ]
            if missing_full_text:
                raise AssemblerError(
                    f"blocks {missing_full_text[:5]} only carry bounded previews and no "
                    f"block store was provided; assembling them would emit truncated "
                    f"content while claiming completeness"
                )
            return {bid: self.block_map[bid] for bid in interval_ids}

        loaded = {
            block.id: block
            for block in self._block_store.load_many(list(interval_ids))
        }
        # 仓库缺项（写盘时被跳过 / id 不在其中）退回内存块，但同样不允许预览块混进来。
        for bid in interval_ids:
            if bid in loaded:
                continue
            fallback = self.block_map[bid]
            if (fallback.metadata or {}).get("text_is_preview"):
                raise AssemblerError(
                    f"block {bid} is missing from the block store and only a bounded "
                    f"preview is in memory; refusing to emit truncated content"
                )
            loaded[bid] = fallback
        return loaded

    # ── Section Type Inference ──

    def _infer_section_type(self, title: str) -> str:
        """Infer semantic section type from the heading title."""
        lower_title = title.lower().strip()

        if any(kw in lower_title for kw in ["abstract", "摘要"]):
            return "abstract"
        elif any(kw in lower_title for kw in ["reference", "参考文献", "bibliography"]):
            return "reference"
        elif any(kw in lower_title for kw in ["appendix", "附录"]):
            return "appendix"
        elif any(kw in lower_title for kw in ["acknowledgment", "致谢"]):
            return "acknowledgment"

        return "section"

    # ── Preamble Extraction ──

    def get_preamble_blocks(self, first_chapter_start_id: int) -> List[Block]:
        """Return blocks before the first chapter (title page, metadata, etc.).

        与 :meth:`_extract_content` 同一条约束：前言也是要渲染进产物的正文，所以这里
        同样按仓库回读全文，不能把有界预览当前言交付出去。
        """
        preamble_ids = [
            block.id for block in self.blocks if block.id < first_chapter_start_id
        ]
        if not preamble_ids:
            return []
        resolved = self._resolve_interval_blocks(preamble_ids)
        return [resolved[bid] for bid in preamble_ids]
