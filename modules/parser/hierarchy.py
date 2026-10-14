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
import re
from dataclasses import dataclass, field
from typing import List, Optional

from infrastructure.models import Block
from modules.parser.analysis_context import block_index
from modules.parser.config import ResolverConfig
from modules.parser.heading_candidates import (
    infer_numbering_level,
    infer_style_level,
)
from modules.parser.schemas import ChapterNode, HeadingCandidate
from modules.parser.titles import SECTION_NUMBERING_RE, structural_text

logger = logging.getLogger(__name__)

_MAX_LEVEL = 6
_FONT_SAMPLE_WINDOW = 10

# ── A06：软约束参数 ────────────────────────────────────────────
# 编号元组解析（与 typography 同源规则，此处独立一份避免跨模块循环依赖）。
_NUMBERING_RE = SECTION_NUMBERING_RE
# 附录标题前缀（重编号重置的锚点）。命中即允许其后的编号系列独立重排。
_APPENDIX_RE = re.compile(r"^\s*(?:appendix|附录|附\s*录)\b", re.IGNORECASE)
# “异常短的连续标题串”阈值：连续这么多个标题之间没有任何正文块，视为
# 可疑（往往是把列表项/元数据误当标题）。仅告警，不删。
_HEADING_RUN_SUSPICIOUS = 4


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


# ══════════════════════════════════════════════════════════════════════
# A06 · 完整约束树关系：区分「层级合法」与「层级正确」
# ══════════════════════════════════════════════════════════════════════
#
# `is_legal_level_transition` 只保证「合法」（不非法跳级）；下面的软约束
# 度量「正确」——章节编号语义、真实父节点、主题归属是否自洽。全部是
# 低成本、可解释的检查：只报告 + 打分，绝不阻断、不删节点、不凭空造标题。
# 只有软约束都解决不了关键样本时才该上 beam search（见报告判断依据）。


def _numbering_series(text: str | None) -> tuple[int, ...] | None:
    """Parse a visible section number into its integer tuple (``1.2.3`` -> (1,2,3))."""
    if not text:
        return None
    match = _NUMBERING_RE.match(structural_text(text).strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group("num").split("."))
    except ValueError:
        return None


@dataclass(frozen=True)
class SoftConstraintViolation:
    """One explainable soft-constraint finding.  Advisory, never blocking."""

    kind: str                    # 约束种类标识（见 evaluate_soft_constraints 文档）
    block_ids: tuple[int, ...]   # 相关标题块
    penalty: float               # [0,1] 惩罚强度，供打分聚合
    explanation: str             # 面向人的中文说明


@dataclass(frozen=True)
class VirtualContainer:
    """A declared-only placeholder for a missing intermediate parent (A06).

    缺失 ``2.1`` 的 ``2.1.1`` 不能凭空生成原文不存在的标题：改为在结构表示
    里声明一个虚拟容器，``is_virtual=True`` 且标题带显式标记，与真实来源
    标题可区分。它只表达真实深度，不进入正文、不参与召回计分。
    """

    number: tuple[int, ...]      # 被补的中间编号，如 (2, 1)
    level: int                   # 其应处的深度
    child_block_id: int          # 触发补偿的子标题块
    is_virtual: bool = True

    @property
    def title(self) -> str:
        dotted = ".".join(str(part) for part in self.number)
        return f"[virtual {dotted}]"


@dataclass(frozen=True)
class SoftConstraintReport:
    """Aggregated soft-constraint findings for one decoded hierarchy."""

    violations: tuple[SoftConstraintViolation, ...] = ()
    virtual_containers: tuple[VirtualContainer, ...] = ()

    @property
    def total_penalty(self) -> float:
        return sum(v.penalty for v in self.violations)

    def by_kind(self, kind: str) -> list[SoftConstraintViolation]:
        return [v for v in self.violations if v.kind == kind]


def virtual_containers_for(
    chapters: List[ChapterNode],
) -> list[VirtualContainer]:
    """Declare virtual parents for numbered children missing an intermediate.

    仅依据可见编号：当一个编号标题的父前缀在文档里没有对应的真实标题时，
    补一个虚拟容器声明其父节点存在于结构中——但绝不生成原文没有的标题
    文本。返回的每个容器都 ``is_virtual=True`` 且标题形如 ``[virtual 2.1]``，
    与真实标题一望可辨。多级缺失（``2.1.1`` 同时缺 ``2.1`` 和 ``2``）会为每一
    个缺失层各补一个。
    """
    present: set[tuple[int, ...]] = set()
    numbered: list[tuple[tuple[int, ...], ChapterNode]] = []
    for chapter in chapters:
        number = _numbering_series(chapter.title)
        if number is None:
            continue
        present.add(number)
        numbered.append((number, chapter))

    containers: list[VirtualContainer] = []
    declared: set[tuple[int, ...]] = set()
    for number, chapter in numbered:
        for depth in range(1, len(number)):
            prefix = number[:depth]
            if prefix in present or prefix in declared:
                continue
            declared.add(prefix)
            containers.append(VirtualContainer(
                number=prefix,
                level=min(len(prefix), _MAX_LEVEL),
                child_block_id=chapter.start_block_id,
            ))
    if containers:
        logger.info(
            "[SoftConstraint] Declared %d virtual container(s) for missing "
            "intermediate numbering (never fabricated as real headings)",
            len(containers),
        )
    return containers


def evaluate_soft_constraints(
    chapters: List[ChapterNode],
    blocks: List[Block] | None = None,
    candidates: List[HeadingCandidate] | None = None,
) -> SoftConstraintReport:
    """Score a decoded hierarchy against low-cost, explainable soft constraints.

    约束种类（``kind``）：

    * ``numbering_series_depth`` —— 同一编号系列（共享父前缀）的成员深度应
      一致；``1.1`` 与 ``1.2`` 却落在不同 level 即违约。
    * ``numbering_parent_prefix`` —— 编号父前缀 ``x`` 存在时，``x.y`` 的 level
      应恰为父 level+1；不符即违约。
    * ``style_class_level`` —— 携带相同可信样式类的标题应同级（由候选的
      ``style_level`` 代理）。
    * ``appendix_reset`` —— 附录标志后编号可重置为 1，本身不算违约，仅用于
      抑制“编号回退”误报（在此登记为信息项，penalty=0）。
    * ``short_chapter`` / ``heading_run`` —— 异常短章节或连续标题串惩罚（可疑
      的过度切分），仅告警。

    纯度量：只读入已解码的 chapters（可选 blocks/candidates 作辅助证据），
    返回报告，绝不修改 chapters，也绝不新增/删除节点。虚拟容器由
    :func:`virtual_containers_for` 单独声明并附在报告里。
    """
    violations: list[SoftConstraintViolation] = []
    # Check the ACTUAL active ancestor, not merely a same-numbered heading
    # somewhere in the document (possibly later or in a different appendix).
    active: list[ChapterNode] = []
    for chapter in chapters:
        active = active[:max(chapter.level - 1, 0)]
        number = _numbering_series(chapter.title)
        parent = active[-1] if active else None
        parent_number = _numbering_series(parent.title) if parent else None
        if number and len(number) > 1 and parent_number and number[:-1] != parent_number:
            violations.append(SoftConstraintViolation(
                kind="numbering_actual_parent",
                block_ids=(parent.start_block_id, chapter.start_block_id), penalty=0.6,
                explanation="编号父前缀与当前实际祖先不符，需要确认归属；未改写原文。",
            ))
        active.append(chapter)
    # 索引由本次解析共享的 BlockIndex 提供（L01），不再自建。
    block_map = block_index(blocks or []).by_id
    candidate_by_id = {c.block_id: c for c in (candidates or [])}

    level_by_number: dict[tuple[int, ...], list[tuple[int, int]]] = {}
    for chapter in chapters:
        number = _numbering_series(chapter.title)
        if number is None:
            continue
        # 同系列（同父前缀 + 同深度）成员应同级。
        series_key = number[:-1] + (len(number),)
        level_by_number.setdefault(series_key, []).append(
            (chapter.start_block_id, chapter.level)
        )

    for series_key, members in level_by_number.items():
        levels = {lvl for _bid, lvl in members}
        if len(levels) > 1:
            violations.append(SoftConstraintViolation(
                kind="numbering_series_depth",
                block_ids=tuple(bid for bid, _lvl in members),
                penalty=0.5,
                explanation=(
                    f"同一编号系列（深度 {series_key[-1]}）的成员层级不一致："
                    f"{sorted(levels)}，编号语义要求同级。"
                ),
            ))

    # 编号父前缀：x.y 的 level 应为父 x 的 level+1。
    level_of_number: dict[tuple[int, ...], int] = {}
    for chapter in chapters:
        number = _numbering_series(chapter.title)
        if number is not None:
            level_of_number[number] = chapter.level
    for number, level in level_of_number.items():
        if len(number) < 2:
            continue
        parent_level = level_of_number.get(number[:-1])
        if parent_level is not None and level != parent_level + 1:
            dotted = ".".join(str(p) for p in number)
            violations.append(SoftConstraintViolation(
                kind="numbering_parent_prefix",
                block_ids=(),
                penalty=0.4,
                explanation=(
                    f"编号 {dotted} 的层级 {level} 与其父前缀层级 {parent_level} "
                    f"不构成父子（应为 {parent_level + 1}）。"
                ),
            ))

    # 同可信样式类应同级（用候选的显式 style_level 代理）。
    level_by_style: dict[int, set[int]] = {}
    style_blocks: dict[int, list[int]] = {}
    for chapter in chapters:
        candidate = candidate_by_id.get(chapter.start_block_id)
        style_level = candidate.style_level if candidate else None
        if style_level is None:
            continue
        level_by_style.setdefault(style_level, set()).add(chapter.level)
        style_blocks.setdefault(style_level, []).append(chapter.start_block_id)
    for style_level, seen in level_by_style.items():
        if len(seen) > 1:
            violations.append(SoftConstraintViolation(
                kind="style_class_level",
                block_ids=tuple(style_blocks[style_level]),
                penalty=0.3,
                explanation=(
                    f"同一显式样式（style_level={style_level}）的标题被判到不同"
                    f"层级 {sorted(seen)}，可信样式类应同级。"
                ),
            ))

    # 附录重编号重置：登记为信息项（penalty=0），用于解释编号回退。
    for chapter in chapters:
        if _APPENDIX_RE.match(chapter.title or ""):
            violations.append(SoftConstraintViolation(
                kind="appendix_reset",
                block_ids=(chapter.start_block_id,),
                penalty=0.0,
                explanation="附录标志：其后的编号系列可独立重置，非违约。",
            ))

    # 异常短的连续标题串：blocks 可用时，检测标题之间无正文块的连续段。
    if blocks:
        heading_ids = [c.start_block_id for c in chapters]
        heading_set = set(heading_ids)
        ordered = sorted(b.id for b in blocks)
        run: list[int] = []
        runs: list[list[int]] = []
        for bid in ordered:
            block = block_map.get(bid)
            if bid in heading_set:
                run.append(bid)
            elif block and block.type == "text" and (block.text or "").strip():
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)
        for group in runs:
            if len(group) >= _HEADING_RUN_SUSPICIOUS:
                violations.append(SoftConstraintViolation(
                    kind="heading_run",
                    block_ids=tuple(group),
                    penalty=0.2,
                    explanation=(
                        f"连续 {len(group)} 个标题之间没有正文，疑似过度切分"
                        f"（可能把列表项/元数据误判为标题）。"
                    ),
                ))

    report = SoftConstraintReport(
        violations=tuple(violations),
        virtual_containers=tuple(virtual_containers_for(chapters)),
    )
    if violations:
        logger.info(
            "[SoftConstraint] %d finding(s), total penalty %.2f",
            len([v for v in violations if v.penalty > 0]),
            report.total_penalty,
        )
    return report


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
        # 索引由本次解析共享的 BlockIndex 提供（L01），不再自建。
        self.block_map = block_index(blocks).by_id
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

        优先级仍是「可见编号 > 明确样式 > LLM 级别」。这里消费的
        ``HeadingCandidate.numbering_level`` 是 :func:`typography._analyse_numbering`
        产出的**已按样式证据锚定**的绝对层级——锚定只有那一处实现，本方法不再自己算一份
        （曾经两处各算导致 3.1 被判 L1，见该函数注释）。
        """
        candidate = self._candidate_by_id.get(chapter.start_block_id)
        if candidate and candidate.numbering_level is not None:
            return max(1, min(candidate.numbering_level, _MAX_LEVEL))

        block = self.block_map.get(chapter.start_block_id)
        if block:
            # 没有候选（也就没有 profile 锚定过的值）时，正文里的可见编号深度就是仅有的
            # 绝对信号：此时无任何锚点证据可用，句法深度是能说的全部，直接用它。
            numbering_level = infer_numbering_level(block.text)
            if numbering_level is not None:
                return max(1, min(numbering_level, _MAX_LEVEL))
            metadata = block.metadata or {}
            metadata_numbering = metadata.get("numbering_level")
            try:
                if metadata_numbering is not None:
                    # Provider 给的编号层级是文件里的真实列表层级，天然是绝对值。
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


__all__ = [
    "is_legal_level_transition",
    "hierarchy_is_legal",
    "HierarchyRepairer",
    # A06 soft constraints
    "SoftConstraintViolation",
    "VirtualContainer",
    "SoftConstraintReport",
    "virtual_containers_for",
    "evaluate_soft_constraints",
]
