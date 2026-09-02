"""Unified heading-hierarchy repair for the Constellation pipeline.

Single source of truth for level repair.  Before 2026-06-11 the same
font-size-to-level logic lived in three places with subtly different
strategies (``parser._normalize_hierarchy``: first-match without median
smoothing; ``resolver._validate_hierarchy``: first-match with sliding
median; ``resolver._infer_orphan_level``: best-diff) and the pipeline
ran the repair twice (post-merge and post-correction), making level
bugs impossible to attribute.  This module consolidates all of it:

- **Authoritative hints first** — visible numbering > explicit provider
  style > LLM level (fixed priority for paper reproducibility).
- **Jump repair** — levels may increase by at most one; illegal jumps
  resolve via font-size match (best-diff) before clamping.
- **Sibling promotion** — a legal but suspicious nesting is promoted
  when the block's font size matches a higher-priority level.
- **Sliding-median font tracking** — per-level font sizes use a
  10-sample sliding median, robust to outlier blocks.
- **Orphan inference** — swallowed headings get the closest font-size
  level (best-diff), falling back to ``parent_level + 1``.

The pipeline calls this exactly once, inside the resolver (after fuzzy
anchor correction, when block_ids are final).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from infrastructure.models import Block
from modules.parser.config import ResolverConfig
from modules.parser.heading_candidates import (
    infer_numbering_level,
    infer_style_level,
)
from modules.parser.schemas import ChapterNode, HeadingCandidate

logger = logging.getLogger(__name__)

_MAX_LEVEL = 6
_FONT_SAMPLE_WINDOW = 10


def is_legal_level_transition(previous_level: int, next_level: int) -> bool:
    """Return whether a selected heading may follow the previous heading.

    ``previous_level == 0`` denotes that no heading has been selected yet, so
    the first physical root must be L1.  Afterwards a sequence may close any
    number of scopes but may open at most one deeper scope at a time.
    """
    if not 1 <= next_level <= _MAX_LEVEL:
        return False
    if previous_level == 0:
        return next_level == 1
    return next_level <= previous_level + 1


def hierarchy_is_legal(chapters: List[ChapterNode]) -> bool:
    """Validate the exact transition constraint used by global inference."""
    previous = 0
    for chapter in chapters:
        if not is_legal_level_transition(previous, chapter.level):
            return False
        previous = chapter.level
    return True


class HierarchyRepairer:
    """Repair heading levels in document order, maintaining font state.

    One instance per resolved document.  ``level_font`` maps each level
    to the sliding median of observed font sizes and doubles as the
    evidence base for orphan-level inference.
    """

    def __init__(
        self,
        blocks: List[Block],
        candidates: List[HeadingCandidate] | None = None,
        config: ResolverConfig | None = None,
    ):
        cfg = config or ResolverConfig()
        self.block_map = {b.id: b for b in blocks}
        self.font_tolerance = cfg.level_jump_font_size_tolerance
        self._candidate_by_id = {
            c.block_id: c for c in (candidates or [])
        }
        # level -> sliding window of font samples
        self._font_samples: dict[int, list[float]] = {}
        # level -> current median font size (exposed for inspection)
        self.level_font: dict[int, float] = {}

    # ── Authoritative level hints ──────────────────────────────

    def authoritative_level(self, chapter: ChapterNode) -> Optional[int]:
        """Resolve numbering/style hints that outrank the LLM level.

        Priority is intentionally fixed: visible numbering > explicit
        provider style > (LLM level, returned as ``None`` here).
        """
        candidate = self._candidate_by_id.get(chapter.start_block_id)
        if candidate and candidate.numbering_level is not None:
            return max(1, min(candidate.numbering_level, _MAX_LEVEL))

        block = self.block_map.get(chapter.start_block_id)
        if block:
            numbering_level = infer_numbering_level(block.text)
            if numbering_level is not None:
                return max(1, min(numbering_level, _MAX_LEVEL))
            metadata = block.metadata or {}
            metadata_numbering = metadata.get("numbering_level")
            try:
                if metadata_numbering is not None:
                    return max(1, min(int(metadata_numbering), _MAX_LEVEL))
            except (TypeError, ValueError):
                pass

        if candidate and candidate.style_level is not None:
            return max(1, min(candidate.style_level, _MAX_LEVEL))

        if block:
            style_level = infer_style_level(block)
            if style_level is not None:
                return max(1, min(style_level, _MAX_LEVEL))

        return None

    # ── Font evidence ──────────────────────────────────────────

    def _best_font_level(self, font_size: float) -> Optional[int]:
        """Closest tracked level by font size within tolerance (best-diff).

        Best-diff beats first-match when adjacent levels have close
        medians: a 12.2pt block next to L1=12.5 / L2=12.0 belongs to L2
        (diff 0.2) even though L1 also falls inside the tolerance.
        Ties resolve to the higher-priority (numerically smaller) level
        via the sorted iteration order.
        """
        best_level: Optional[int] = None
        best_diff = float("inf")
        for lv in sorted(self.level_font.keys()):
            diff = abs(font_size - self.level_font[lv])
            if diff <= self.font_tolerance and diff < best_diff:
                best_diff = diff
                best_level = lv
        return best_level

    def _record_font(self, chapter: ChapterNode) -> None:
        """Add the chapter block's font size to its level's sliding median."""
        block = self.block_map.get(chapter.start_block_id)
        if not block or not block.font_size:
            return
        samples = self._font_samples.setdefault(chapter.level, [])
        samples.append(block.font_size)
        if len(samples) > _FONT_SAMPLE_WINDOW:
            del samples[:-_FONT_SAMPLE_WINDOW]
        ordered = sorted(samples)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 0:
            self.level_font[chapter.level] = (ordered[mid - 1] + ordered[mid]) / 2
        else:
            self.level_font[chapter.level] = ordered[mid]

    # ── Main repair pass ───────────────────────────────────────

    def repair(self, chapters: List[ChapterNode]) -> List[ChapterNode]:
        """Repair *chapters* in place and return them.

        Single forward pass: authoritative hints, first-chapter
        anchoring, jump repair, sibling promotion, font tracking,
        and level-stack maintenance.
        """
        if not chapters:
            return chapters

        fix_count = 0

        first = chapters[0]
        preferred = self.authoritative_level(first)
        target_first = preferred or 1
        if first.level != target_first:
            logger.warning(
                "[HierarchyRepair] First chapter '%s' level=%d -> %d",
                first.title, first.level, target_first,
            )
            first.level = target_first
            fix_count += 1
        self._record_font(first)

        level_stack = [first.level]

        for ch in chapters[1:]:
            preferred = self.authoritative_level(ch)
            # A chapter whose level comes from visible numbering or an
            # explicit style is *pinned*: font cross-validation must
            # never overrule it (fixed priority: numbering > style >
            # LLM > font).  Jump repair still applies — tree legality
            # outranks everything.
            pinned = preferred is not None
            if pinned and preferred != ch.level:
                logger.warning(
                    "[HierarchyRepair] '%s' level=%d -> %d (numbering/style priority)",
                    ch.title, ch.level, preferred,
                )
                ch.level = preferred
                fix_count += 1

            max_allowed = level_stack[-1] + 1
            block = self.block_map.get(ch.start_block_id)
            font_size = block.font_size if block and block.font_size else None

            if ch.level > max_allowed:
                # Jump repair: illegal level jump.  Font evidence first,
                # clamp as the fallback.  Font evidence is only usable
                # when it does not itself violate tree legality: a font
                # matching a historical L3 cannot justify nesting L3
                # directly under L1 (tree legality outranks everything).
                old_level = ch.level
                resolved = None
                if font_size is not None:
                    resolved = self._best_font_level(font_size)
                if resolved is not None and resolved <= max_allowed:
                    ch.level = resolved
                    font_assisted = True
                else:
                    ch.level = max_allowed
                    font_assisted = False
                logger.warning(
                    "[HierarchyRepair] '%s' level=%d -> %d (jump repair%s)",
                    ch.title[:30], old_level, ch.level,
                    ", font-assisted" if font_assisted else "",
                )
                fix_count += 1

            elif (
                not pinned
                and ch.level > 1
                and font_size is not None
                and self.level_font
            ):
                # Sibling promotion: legal nesting, but the font matches
                # a higher-priority level — the LLM nested what should
                # be siblings.  Skipped for pinned chapters: identical
                # fonts across heading levels are common, and numbering
                # evidence outranks font evidence.
                match = self._best_font_level(font_size)
                if match is not None and match < ch.level:
                    logger.warning(
                        "[HierarchyRepair] '%s' level=%d -> %d "
                        "(font cross-validation: %.1fpt matches L%d)",
                        ch.title[:30], ch.level, match, font_size, match,
                    )
                    ch.level = match
                    fix_count += 1

            self._record_font(ch)

            if ch.level > level_stack[-1]:
                level_stack.append(ch.level)
            else:
                while level_stack and level_stack[-1] >= ch.level:
                    level_stack.pop()
                level_stack.append(ch.level)

        if fix_count:
            logger.info("[HierarchyRepair] Fixed %d level issue(s)", fix_count)
        else:
            logger.info("[HierarchyRepair] Hierarchy compliant; no fixes needed")

        return chapters

    # ── Orphan inference ───────────────────────────────────────

    def infer_orphan_level(self, block: Block, parent_level: int) -> int:
        """Level for a swallowed orphan block promoted by inverse audit.

        Uses the font evidence accumulated during :meth:`repair`;
        falls back to ``parent_level + 1`` (child) when no level
        matches — assigning the parent's own level would corrupt
        the tree.
        """
        if block.font_size:
            match = self._best_font_level(block.font_size)
            if match is not None:
                return match
        return min(parent_level + 1, _MAX_LEVEL)
