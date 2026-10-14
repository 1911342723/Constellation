"""Document-level typography analysis: style classes, relative numbering, trust.

The evidence model assigns each physical signal a fixed reliability -- an
explicit heading style is worth the same 0.99 whether the author used styles
correctly or applied ``Heading 3`` to body paragraphs.  That assumption holds
for well-produced documents and fails exactly where users need help.

This module measures the typography instead of assuming it, and produces three
things the rest of the pipeline consumes:

- **Relative numbering depth** -- ``1.1`` is level 2 only if the document also
  contains a level-1 number.  A document whose sections all start at ``1.1`` is
  numbered from a different root, and depth must be measured relative to it.
- **Style classes and their normalised levels** -- headings that share a
  signature (style, size, weight, numbering depth) are the same *kind* of
  heading and must land on the same level.  The nesting order between kinds is
  recovered from the document's own repetition pattern, so a document that
  starts at ``Heading 2`` keeps its shape while its levels are renumbered from
  one.
- **Trust** -- how self-consistent the typography is.  Low trust does not by
  itself change any decision; it lowers the *level* authority of style and
  numbering evidence and raises the authority of the optional LLM review.

Nothing here selects headings.  It produces priors and reliabilities; the
document-wide decoder remains the single decision point.
"""
from __future__ import annotations

from modules.parser.analysis_context import block_index, measured_once

import logging
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from statistics import median
from typing import Sequence

from infrastructure.models import Block
from modules.parser.titles import SECTION_NUMBERING_RE, structural_text

logger = logging.getLogger(__name__)

_MAX_LEVEL = 6
_SENTENCE_FINAL = (".", "\u3002", "!", "?", "\uff01", "\uff1f")
# A period *between two digits* is a decimal point or a section-number
# separator, never a sentence terminator: "1.1.1 Frontend Runtime" carries no
# sentence, and "Version 1.2.3 notes" carries one at most.  Counting notation
# as punctuation is the same class of error as reading shape off the transport
# form (see titles.structural_text) -- it measures the writing system rather
# than the writing.
_SENTENCE_ENDER_RE = re.compile(r"(?<!\d)\.|\.(?!\d)|[\u3002!\uff01?\uff1f]")
# Shape thresholds for "this reads as prose, not as a name".  Length alone is a
# weak signal -- some real headings are long -- so the punctuation tests carry the
# decision and the length bound is set where no heading survives it.
_PROSE_MAX_CHARS = 200
_PROSE_SENTENCE_WORDS = 8
_PROSE_SENTENCE_CJK_CHARS = 20
# A heading site must be at least this short to be considered at all.
_SITE_MAX_CHARS = _PROSE_MAX_CHARS
# Above this share of bold text blocks, boldness carries no information.
_BOLD_SATURATION_RATIO = 0.60
# Minimum evidence before class-level normalisation is allowed to act.
_MIN_CLASSES = 2
_MIN_SITES = 4
# Share of a class's incoming transitions that a single predecessor must carry
# before it may set that class's depth.  A one-off level skip among many
# consistent observations is noise; the only predecessor a class ever has is
# evidence however rare it is.
_MIN_TRANSITION_SHARE = 0.25

# ── A03：区域画像与类层级可信度 ────────────────────────────────
# 最小区域规模：一个区域至少要有这么多标题站点才允许拥有独立画像，否则
# 回退到上层区域。防止一两个异常块（版式突变的孤块）频繁触发换模板。
_MIN_REGION_SITES = 4
# 等距抽样的初始分段数上限：先粗采，再在变化点附近补样。
_REGION_SAMPLE_SEGMENTS = 8
# 相邻分段正文字号中位数相对变化超过此比例，判定为分布变化点。字号是
# 比例量纲，用相对差而非绝对差，避免大字号文档过度敏感。
_REGION_BODY_SHIFT_RATIO = 0.12
# 类层级“唯一可解”判定：一个类若其最强入边占比不足此值即视为层级歧义，
# 拉低该类层级可信度（A B A B 交替式同级标题正落在这一分支）。
_CLASS_LEVEL_DOMINANCE = 0.60

_NUMBERING_RE = SECTION_NUMBERING_RE


def is_prose_shaped(text: str | None) -> bool:
    """Whether *text* reads as a sentence rather than as a name.

    Operates on the structural form, so a bold CJK sentence is judged by its content
    rather than by its transport markers.  Terminal punctuation plus length is
    what separates a sentence from a name; a long heading that simply never
    ends is intentionally *not* prose, because real documents do have those.
    """
    shape = structural_text(text)
    if not shape:
        return False
    if len(shape) > _PROSE_MAX_CHARS:
        return True
    if len(_SENTENCE_ENDER_RE.findall(shape)) >= 2:
        return True
    if shape.endswith(_SENTENCE_FINAL):
        words = shape.split()
        if len(words) > _PROSE_SENTENCE_WORDS:
            return True
        # CJK text has no spaces: fall back to a character budget.
        if len(words) <= 1 and len(shape) > _PROSE_SENTENCE_CJK_CHARS:
            return True
    return False


def numbering_depth(text: str | None) -> int | None:
    """Absolute depth of a visible section number (``1.2.3`` -> 3)."""
    value = structural_text(text)
    if not value:
        return None
    match = _NUMBERING_RE.match(value)
    if not match:
        return None
    num = match.group("num")
    if "." not in num and int(num) > 99:
        return None
    return min(num.count(".") + 1, _MAX_LEVEL)


def _numbering_tuple(text: str | None) -> tuple[int, ...] | None:
    value = structural_text(text)
    match = _NUMBERING_RE.match(value) if value else None
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group("num").split("."))
    except ValueError:
        return None


@dataclass(frozen=True)
class StyleClass:
    """A kind of heading: everything sharing one typographic signature."""

    class_id: str
    style_level: int | None
    font_size: float | None
    is_bold: bool
    numbering_depth: int | None
    block_ids: tuple[int, ...]

    @property
    def occurrences(self) -> int:
        return len(self.block_ids)


@dataclass(frozen=True)
class ClassLevelLabel:
    """One possible level for a style class, with how strongly it is supported.

    A03 的核心表示：类层级不再是单值，而是“多个可能标签 + 可信度”。
    ``confidence`` 是本标签相对其它候选标签的相对支持度（同一类的所有标签
    confidence 之和为 1）；``basis`` 说明这个层级来自哪种证据，供解释与
    下游按证据种类调节权重（纯 BFS 先验 vs 编号父前缀等语义证据）。
    """

    level: int
    confidence: float
    basis: str  # "bfs" | "numbering_prefix" | "sequence_ambiguous" | ...


@dataclass(frozen=True)
class RegionProfile:
    """Typography statistics scoped to one contiguous block range.

    A03：全篇单一统计扩展为分层区域统计（document → section range）。每个
    区域记录自己的正文字号中位数与站点数；``parent`` 指向回退画像，站点
    不足 ``_MIN_REGION_SITES`` 时本区域的 ``body_size`` 取自父区域，避免个别
    异常块把整段拖成一个不具代表性的模板。
    """

    region_id: str
    start_block_id: int
    end_block_id: int
    body_size: float
    site_count: int
    from_parent: bool = False  # 数据不足、body_size 借自上层画像
    parent_id: str | None = None


@dataclass
class TypographyProfile:
    """Measured typography of one document."""

    numbering_root_depth: int = 1
    numbering_trust: float = 1.0
    style_trust: float = 1.0
    trust: float = 1.0
    bold_ratio: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    relative_numbering: dict[int, int] = field(default_factory=dict)
    class_of: dict[int, str] = field(default_factory=dict)
    classes: dict[str, StyleClass] = field(default_factory=dict)
    class_level: dict[str, int] = field(default_factory=dict)
    # A03：类层级的多标签视图（class_id -> 按可信度降序的候选层级）。
    # ``class_level`` 仍是最强标签的单值投影，既有调用零改动；需要“同样
    # 合理的多棵树”信息的调用方读 ``class_level_labels``。
    class_level_labels: dict[str, tuple[ClassLevelLabel, ...]] = field(default_factory=dict)
    # 每个类最强标签的可信度（class_id -> [0,1]）。零语义证据且序列本身
    # 歧义时该值偏低，是 ``flat_view`` 的判定输入之一。
    class_level_confidence: dict[str, float] = field(default_factory=dict)
    # A03：区域画像（region_id -> RegionProfile），以及 block -> region_id。
    regions: dict[str, RegionProfile] = field(default_factory=dict)
    region_of: dict[int, str] = field(default_factory=dict)
    # 零语义证据且多棵树同样合理：输出低置信 / 扁平视图，不把合法路径当
    # 已证实结构。调用方据此决定是否呈现层级还是压平。
    flat_view: bool = False
    normalisation_skipped: str = ""
    body_signature: tuple[float | None, bool] | None = None
    body_signature_blocks: frozenset[int] = frozenset()

    def wears_body_signature(self, block_id: int) -> bool:
        """Whether the block is visually indistinguishable from running text."""
        return block_id in self.body_signature_blocks

    def numbering_level_for(self, block_id: int) -> int | None:
        """Relative numbering level of a block, or ``None`` when unnumbered."""
        return self.relative_numbering.get(block_id)

    def class_level_for(self, block_id: int) -> int | None:
        """Normalised level of the block's style class, when one was derived."""
        class_id = self.class_of.get(block_id)
        if class_id is None:
            return None
        return self.class_level.get(class_id)

    def class_level_confidence_for(self, block_id: int) -> float:
        """How strongly this block's class level is supported (0 when none).

        A03：层级可信度随块暴露，供全局解码/前端把“合法路径”与“已证实
        结构”区分开——低可信时应显示低置信/扁平视图而非当作确定层级。
        """
        class_id = self.class_of.get(block_id)
        if class_id is None:
            return 0.0
        return self.class_level_confidence.get(class_id, 0.0)

    def region_body_size_for(self, block_id: int) -> float | None:
        """Regional body font size covering this block, if a region profile exists."""
        region_id = self.region_of.get(block_id)
        if region_id is None:
            return None
        region = self.regions.get(region_id)
        return region.body_size if region else None

    @property
    def bold_is_uninformative(self) -> bool:
        return self.bold_ratio > _BOLD_SATURATION_RATIO


def _body_size(blocks: Sequence[Block]) -> float:
    from modules.parser.heading_candidates import _body_font_size

    return _body_font_size(blocks)


def _looks_like_heading_site(block: Block, body_size: float) -> bool:
    """Cheap, candidate-layer-independent test for "this could be a heading".

    Deliberately does not consult :mod:`heading_candidates`: the candidate layer
    consumes this profile, so depending on it would be circular.
    """
    if block.type != "text" or not block.text:
        return False
    shape = structural_text(block.text)
    if not shape or len(shape) > _SITE_MAX_CHARS:
        return False
    if is_prose_shaped(shape):
        return False
    if block.is_heading_style:
        return True
    if numbering_depth(shape) is not None:
        return True
    if block.font_size and body_size > 0 and block.font_size >= body_size * 1.05:
        return True
    if block.is_bold and len(shape) <= 80:
        return True
    return False


def _visual_signature(block: Block) -> tuple[float | None, bool]:
    """What the reader actually sees: size and weight, nothing else."""
    return (
        round(block.font_size, 1) if block.font_size else None,
        bool(block.is_bold),
    )


def _body_signature(
    text_blocks: Sequence[Block],
    site_ids: set[int],
) -> tuple[float | None, bool] | None:
    """The signature the document's own running text wears."""
    counts: Counter[tuple[float | None, bool]] = Counter()
    for block in text_blocks:
        if block.id in site_ids:
            continue
        counts[_visual_signature(block)] += max(
            int((block.metadata or {}).get("char_count", len(structural_text(block.text)))), 1,
        )
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _sites_wearing_body_signature(
    sites: Sequence[Block],
    body_signature: tuple[float | None, bool] | None,
) -> frozenset[int]:
    """Heading sites a reader could not tell apart from running text.

    A short line carrying a heading *style* but the body's own size and weight
    is the failure that shape analysis cannot reach: "Status: In review" is not
    a sentence, so nothing about its wording betrays it.  What betrays it is
    that the author's own document draws a visible line between headings and
    text, and this line sits on the wrong side of it.

    The test only means something when the document draws that line somewhere.
    Where every heading wears the body's signature -- a document set entirely
    in one size and weight -- it separates nothing and must stay silent, for
    the same reason boldness carries nothing in a document that is bold
    throughout.
    """
    if body_signature is None:
        return frozenset()
    # A centred line is set apart from running text by its placement even in
    # the body's size and weight (AMS/small-caps section titles such as a
    # centred "1. Introduction"); a reader can tell it apart, so it is not
    # wearing the body's signature.
    matching = {
        block.id for block in sites
        if _visual_signature(block) == body_signature
        and (block.alignment or "").lower() != "center"
    }
    if len(matching) == len(sites):
        return frozenset()
    return frozenset(matching)


def _analyse_numbering(
    sites: Sequence[Block],
) -> tuple[int, float, dict[int, int]]:
    """Return ``(root_depth, trust, block_id -> relative level)``.

    A document numbered ``1.1 / 1.2 / 1.3`` at the top level is shifted back to
    ``1 / 2 / 3``, but only when the numbering is internally consistent: the
    shift must not silently repair numbers that were never a hierarchy.
    """
    numbered = [
        (block.id, _numbering_tuple(block.text))
        for block in sites
    ]
    numbered = [(bid, num) for bid, num in numbered if num]
    if not numbered:
        return 1, 1.0, {}

    depths = [len(num) for _bid, num in numbered]
    root_depth = min(depths)

    # Consistency: within one parent prefix, the last component must not go
    # backwards, and every depth deeper than the root must have a parent.
    last_by_prefix: dict[tuple[int, ...], int] = {}
    violations = 0
    seen_prefixes: set[tuple[int, ...]] = set()
    for _bid, num in numbered:
        prefix = num[:-1]
        previous = last_by_prefix.get(prefix)
        if previous is not None and num[-1] <= previous:
            violations += 1
        last_by_prefix[prefix] = num[-1]
        if len(num) > root_depth and prefix not in seen_prefixes:
            violations += 1
        seen_prefixes.add(num)

    trust = max(0.0, 1.0 - violations / max(len(numbered), 1))
    if root_depth > 1 and trust < 0.75:
        # Numbering that is neither rooted nor self-consistent is not a
        # hierarchy; leave depths alone and let other evidence decide.
        logger.info(
            "[Typography] Numbering root depth %d but consistency %.2f; "
            "not normalising", root_depth, trust,
        )
        return 1, trust, {bid: len(num) for bid, num in numbered}

    # 锚点不能硬钉成 1。此前 ``shift = root_depth - 1`` 把「最浅编号深度」无条件当成
    # L1，于是真实样本里一篇「``供应商基本信息`` 是 L2、其下 ``3.1``/``3.2`` 是 L3」的
    # PRD（全篇没有裸 ``3`` 这个标题，故 root_depth=2）被判成 ``3.1`` = L1，后面整棵
    # 子树跟着错位一层。编号只说「在序列里深几层」，它落在文档的哪一层要由结构证据说。
    #
    # 锚点取 root_depth 那一层里带明确样式的标题的样式级别（众数）：样式是文件里写下的
    # 结构信号。整篇在该层没有任何明确样式时才退回 1——那时它确实是唯一可用证据，
    # 也正是原注释想保护的「顶层全是 1.1 的文档不是 L2 文档」那种情形。
    anchor = _numbering_anchor_level(sites, numbered, root_depth)
    shift = root_depth - anchor
    relative = {
        bid: max(1, min(len(num) - shift, _MAX_LEVEL))
        for bid, num in numbered
    }
    if shift:
        logger.info(
            "[Typography] Numbering rooted at depth %d, anchored at level %d; "
            "shifting %d heading number(s) by %d", root_depth, anchor, len(relative), shift,
        )
    return root_depth, trust, relative


def _numbering_anchor_level(
    sites: Sequence[Block],
    numbered: Sequence[tuple[int, tuple[int, ...]]],
    root_depth: int,
) -> int:
    """``root_depth`` 那一层编号标题的绝对层级锚点（无证据时为 1）。

    ``root_depth == 1`` 时**一律返回 1**：编号自带根（文档里真有裸 ``1``/``2``/``3``
    标题），它的深度本身就是绝对层级。此时绝不能拿样式当锚点——「作者从 Heading 2 开始
    写、整篇样式统一偏深一级」正是编号该纠正样式的经典场景，反过来用样式锚定会把偏移
    照抄一遍（tests::test_style_shift_recovers_levels_from_numbering 钉着这条）。

    只有 ``root_depth > 1``（文档里没有裸 ``3``，最浅编号是 ``3.1`` 这类）才需要锚点，
    因为此时编号序列可能整体嵌在若干个**无编号**的外层标题之下。有明确样式就用它的众数，
    没有才退回 1（顶层全是 ``1.1`` 且无样式的文档仍按原行为归到 L1）。
    """
    if root_depth <= 1:
        return 1
    root_ids = {bid for bid, num in numbered if len(num) == root_depth}
    counts: dict[int, int] = {}
    for block in sites:
        if block.id not in root_ids:
            continue
        if not block.is_heading_style or not block.heading_level:
            continue
        level = max(1, min(int(block.heading_level), _MAX_LEVEL))
        counts[level] = counts.get(level, 0) + 1
    if not counts:
        return 1
    # 众数；并列时取更浅的一级，避免把整篇结构无依据地压深。
    return max(sorted(counts), key=lambda lv: (counts[lv], -lv))


def _class_signature(
    block: Block,
    relative_numbering: dict[int, int],
    bold_informative: bool,
) -> tuple[int | None, float | None, bool, int | None]:
    style_level = block.heading_level if block.is_heading_style else None
    size = round(block.font_size, 1) if block.font_size else None
    bold = bool(block.is_bold) if bold_informative else False
    return (style_level, size, bold, relative_numbering.get(block.id))


def _build_classes(
    sites: Sequence[Block],
    relative_numbering: dict[int, int],
    bold_informative: bool,
) -> tuple[dict[str, StyleClass], dict[int, str]]:
    grouped: dict[tuple, list[int]] = {}
    for block in sites:
        signature = _class_signature(block, relative_numbering, bold_informative)
        grouped.setdefault(signature, []).append(block.id)

    classes: dict[str, StyleClass] = {}
    class_of: dict[int, str] = {}
    for signature, block_ids in grouped.items():
        style_level, size, bold, depth = signature
        class_id = (
            f"s{style_level if style_level is not None else '-'}"
            f":f{size if size is not None else '-'}"
            f":b{int(bold)}:n{depth if depth is not None else '-'}"
        )
        classes[class_id] = StyleClass(
            class_id=class_id,
            style_level=style_level,
            font_size=size,
            is_bold=bold,
            numbering_depth=depth,
            block_ids=tuple(sorted(block_ids)),
        )
        for block_id in block_ids:
            class_of[block_id] = class_id
    return classes, class_of


def _class_transitions(sequence: Sequence[str]) -> dict[tuple[str, str], int]:
    """Count how often one class is immediately followed by another."""
    counts: dict[tuple[str, str], int] = {}
    for left, right in zip(sequence, sequence[1:], strict=False):
        if left == right:
            continue
        counts[(left, right)] = counts.get((left, right), 0) + 1
    return counts


def _class_levels(
    sequence: Sequence[str],
    classes: Sequence[str],
) -> dict[str, int]:
    """Assign each class the deepest level its own document permits.

    The document already states its hierarchy through the one constraint the
    decoder enforces: a heading may open at most one scope deeper than the
    heading before it, so ``L(next) <= L(prev) + 1``.  Reading the observed
    class sequence as that system of difference constraints, the deepest level
    a class may legally hold is one more than the fewest transitions needed to
    reach it from the class that opens the document -- a breadth-first distance.

    Counting *containment* instead (how often class A's span encloses a B) is
    the tempting formulation and it does not work: in a balanced document
    ``A B B A B B A B B`` the two consecutive B's that straddle an A produce a
    backwards edge exactly as often as A's own span produces the forward one,
    so the two directions tie and the hierarchy vanishes.

    Because the levels come from the sequence alone, this is immune to every
    perturbation of the typography itself -- a document whose deeper headings
    are rendered *larger* is levelled correctly with no font signal at all.
    """
    transitions = _class_transitions(sequence)
    incoming_total: dict[str, int] = {}
    for (_left, right), weight in transitions.items():
        incoming_total[right] = incoming_total.get(right, 0) + weight

    outgoing: dict[str, set[str]] = {class_id: set() for class_id in classes}
    for (left, right), weight in transitions.items():
        if left not in outgoing or right not in outgoing:
            continue
        if weight < _MIN_TRANSITION_SHARE * incoming_total[right]:
            continue
        outgoing[left].add(right)

    root = sequence[0]
    level = {root: 1}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for child in sorted(outgoing.get(node, ())):
            if child in level:
                continue
            level[child] = min(level[node] + 1, _MAX_LEVEL)
            queue.append(child)

    # A class the root cannot reach has no stated relationship to the rest of
    # the document; leaving it out is what lets the caller fall back to other
    # evidence for it rather than inventing a depth.
    return level


def _numbering_prefix_parents(
    sites: Sequence[Block],
    class_of: dict[int, str],
) -> dict[str, str]:
    """Semantic parent evidence from section numbering, per style class.

    A03：编号父前缀是比“重复模式先验”更强的父子证据。若类 C 的成员编号
    多为 ``x.y.…``，其父前缀 ``x.…`` 的所属类若一致，则 C 的真实父类就是
    那个类——这是语义证据，不是先验。返回 ``child_class -> parent_class``；
    仅当某类内“父前缀落在同一类”的成员占多数时才登记，个别噪声不成立。
    """
    # 每个块的编号元组与所属类。
    numbered: list[tuple[int, tuple[int, ...], str]] = []
    number_of_class: dict[str, set[tuple[int, ...]]] = {}
    for block in sites:
        class_id = class_of.get(block.id)
        if class_id is None:
            continue
        num = _numbering_tuple(block.text)
        if not num:
            continue
        numbered.append((block.id, num, class_id))
        number_of_class.setdefault(class_id, set()).add(num)

    # 编号元组 -> 类（用于按父前缀反查父类）。
    class_by_number: dict[tuple[int, ...], str] = {}
    for class_id, numbers in number_of_class.items():
        for num in numbers:
            class_by_number[num] = class_id

    # 对每个子类，统计其成员父前缀落在哪个类，取多数。
    votes: dict[str, Counter[str]] = {}
    for _bid, num, class_id in numbered:
        if len(num) < 2:
            continue
        parent_class = class_by_number.get(num[:-1])
        if parent_class is None or parent_class == class_id:
            continue
        votes.setdefault(class_id, Counter())[parent_class] += 1

    parents: dict[str, str] = {}
    for class_id, counter in votes.items():
        parent_class, count = counter.most_common(1)[0]
        member_total = len(number_of_class.get(class_id, ()))
        if member_total and count >= max(1, member_total // 2):
            parents[class_id] = parent_class
    return parents


def _numbering_depth_confirmed_classes(
    sites: Sequence[Block],
    class_of: dict[int, str],
    class_level: dict[str, int],
) -> set[str]:
    """Classes whose members' own numbering depth agrees with the derived level.

    A03：一个类若其成员编号深度大多等于其派生层级（如全为 ``1 / 2 / 3`` 的
    depth-1 类落在 L1），编号本身就证实了这个深度——不依赖父前缀，因此也
    覆盖编号根类（无父可比）。这是语义证据，用于把该类层级判为高可信。
    """
    depth_hits: dict[str, list[bool]] = {}
    for block in sites:
        class_id = class_of.get(block.id)
        if class_id is None or class_id not in class_level:
            continue
        num = _numbering_tuple(block.text)
        if not num:
            continue
        depth_hits.setdefault(class_id, []).append(len(num) == class_level[class_id])
    confirmed: set[str] = set()
    for class_id, hits in depth_hits.items():
        if hits and sum(hits) >= max(1, len(hits) // 2 + len(hits) % 2):
            confirmed.add(class_id)
    return confirmed


def _class_levels_with_confidence(
    sequence: Sequence[str],
    classes: Sequence[str],
    numbering_parents: dict[str, str],
    depth_confirmed: set[str] | None = None,
) -> tuple[dict[str, int], dict[str, tuple[ClassLevelLabel, ...]], dict[str, float]]:
    """Class levels *plus* per-class confidence and alternative labels.

    A03 的落地要求：类层级保留多个可能标签 + 可信度，不把合法路径当已
    证实结构。基础层级仍是 :func:`_class_levels` 的 BFS 值（唯一权威投影，
    既有调用零改动）；本函数在其之上判定每个类层级有多可信：

    * **BFS 的入边是否唯一主导**——若某类的最强前驱占其全部入边不足
      ``_CLASS_LEVEL_DOMINANCE``，说明多条同样合理的父路径并存（``A B A B``
      交替式同级标题正落这里），层级歧义，可信度按主导度线性下调，并额外
      产出一个“与最强前驱同级”的候选标签（承认它可能只是交替样式）。
    * **编号父前缀是否印证**——若该类的真实父类由编号父前缀给出且与 BFS
      父路径一致，则是语义证据，可信度拉满并把标签基底记为
      ``numbering_prefix``；不一致则维持 BFS 值但可信度不因编号而升高。

    返回 ``(class_level, class_level_labels, class_level_confidence)``：第一个
    与旧 ``_class_levels`` 完全一致；后两者是新增的多标签视图。
    """
    level = _class_levels(sequence, classes)
    depth_confirmed = depth_confirmed or set()

    transitions = _class_transitions(sequence)
    incoming: dict[str, Counter[str]] = {}
    for (left, right), weight in transitions.items():
        incoming.setdefault(right, Counter())[left] += weight

    labels: dict[str, tuple[ClassLevelLabel, ...]] = {}
    confidence: dict[str, float] = {}
    for class_id in classes:
        if class_id not in level:
            continue
        base_level = level[class_id]
        counter = incoming.get(class_id)

        # 根类（无入边）层级为 1，先验充分，给中等偏上的基线可信度。
        if not counter:
            base_conf = 0.75
            candidates = [ClassLevelLabel(base_level, 1.0, "bfs_root")]
        else:
            total = sum(counter.values())
            top_pred, top_weight = counter.most_common(1)[0]
            dominance = top_weight / total if total else 1.0

            # 双向对称歧义：若最强前驱 P 同时也是本类的强后继（P→C 与
            # C→P 边权相当），父/子方向未定——这正是 `A B A B` 交替式同级
            # 标题：A、B 互为对方的唯一前驱与唯一后继。此时入边主导度再高
            # 也不能证明层级，可信度按对称度压低，并给出「同级」备选。
            back_weight = transitions.get((class_id, top_pred), 0)
            forward_weight = transitions.get((top_pred, class_id), 0)
            symmetry = (
                min(forward_weight, back_weight) / max(forward_weight, back_weight)
                if max(forward_weight, back_weight) > 0 else 0.0
            )
            base_conf = min(1.0, dominance) * (1.0 - symmetry)

            if symmetry >= _CLASS_LEVEL_DOMINANCE:
                # 高度对称：BFS 值与「与前驱同级」并列同样合理。
                candidates = [
                    ClassLevelLabel(base_level, 1.0 - symmetry / 2, "sequence_ambiguous"),
                    ClassLevelLabel(
                        max(1, base_level - 1), symmetry / 2, "sibling_alt",
                    ),
                ]
            elif dominance >= _CLASS_LEVEL_DOMINANCE:
                candidates = [ClassLevelLabel(base_level, dominance, "bfs")]
                remainder = 1.0 - dominance
                if remainder > 1e-6:
                    alt_level = max(1, base_level - 1)
                    candidates.append(
                        ClassLevelLabel(alt_level, remainder, "sequence_alt")
                    )
            else:
                candidates = [
                    ClassLevelLabel(base_level, dominance, "sequence_ambiguous"),
                    ClassLevelLabel(
                        max(1, base_level - 1), 1.0 - dominance, "sibling_alt",
                    ),
                ]

        # 编号父前缀印证 BFS：语义证据压过先验歧义。
        parent_class = numbering_parents.get(class_id)
        if parent_class is not None and level.get(parent_class) is not None:
            implied = min(level[parent_class] + 1, _MAX_LEVEL)
            if implied == base_level:
                base_conf = max(base_conf, 0.97)
                candidates = [ClassLevelLabel(base_level, 1.0, "numbering_prefix")]

        # 编号自身深度印证该类层级（覆盖无父可比的编号根类，如 depth-1 -> L1）。
        if class_id in depth_confirmed:
            base_conf = max(base_conf, 0.9)
            if candidates[0].basis not in ("numbering_prefix",):
                candidates = [ClassLevelLabel(base_level, 1.0, "numbering_depth")]

        # 归一化候选标签的 confidence，保证同类各标签之和为 1。
        weight_sum = sum(c.confidence for c in candidates) or 1.0
        normalised = tuple(
            ClassLevelLabel(c.level, c.confidence / weight_sum, c.basis)
            for c in sorted(candidates, key=lambda c: -c.confidence)
        )
        labels[class_id] = normalised
        confidence[class_id] = round(base_conf, 4)

    return level, labels, confidence


def _profile_regions(
    blocks: Sequence[Block],
    sites: Sequence[Block],
    document_body_size: float,
) -> tuple[dict[str, RegionProfile], dict[int, str]]:
    """Layer typography statistics document -> section range (A03).

    先对文档按块序等距切成至多 ``_REGION_SAMPLE_SEGMENTS`` 段并算各段正文
    字号中位数（粗采样），再在相邻段中位数相对变化超过 ``_REGION_BODY_SHIFT_RATIO``
    的地方判定为分布变化点、在其附近细分（补样），把文档划成若干连续区域。
    每个区域算自己的正文字号中位数；站点数不足 ``_MIN_REGION_SITES`` 的区域
    ``from_parent=True``、``body_size`` 回退到文档级画像，防止一两个异常块把
    整段拖成不具代表性的模板、导致频繁换模板。

    这是画像的“区域视图”，document 级统计仍是回退父层。返回
    ``(regions, block_id -> region_id)``。
    """
    text_blocks = [b for b in blocks if b.type == "text" and b.text and b.font_size]
    document_region = RegionProfile(
        region_id="region:document",
        start_block_id=blocks[0].id if blocks else 0,
        end_block_id=blocks[-1].id if blocks else 0,
        body_size=document_body_size,
        site_count=len(sites),
        parent_id=None,
    )
    regions: dict[str, RegionProfile] = {document_region.region_id: document_region}
    region_of: dict[int, str] = {}

    if len(text_blocks) < 2 * _MIN_REGION_SITES:
        # 文档太短，单一模板即可代表；只保留 document 区域。
        for block in blocks:
            region_of[block.id] = document_region.region_id
        return regions, region_of

    # 粗采样：等距分段的正文字号中位数。
    segment_count = min(_REGION_SAMPLE_SEGMENTS, len(text_blocks))
    span = len(text_blocks) / segment_count
    segments: list[tuple[int, int, float]] = []  # (start_idx, end_idx, median_size)
    for seg in range(segment_count):
        lo = int(seg * span)
        hi = int((seg + 1) * span) if seg < segment_count - 1 else len(text_blocks)
        sizes = [round(b.font_size, 1) for b in text_blocks[lo:hi]]
        if not sizes:
            continue
        segments.append((lo, hi, median(sizes)))

    # 变化点检测 + 补样：相对变化超阈值处切一刀，合并同质相邻段。
    boundaries: list[int] = [segments[0][0]] if segments else [0]
    for prev, cur in zip(segments, segments[1:]):
        prev_size, cur_size = prev[2], cur[2]
        if prev_size <= 0:
            continue
        if abs(cur_size - prev_size) / prev_size >= _REGION_BODY_SHIFT_RATIO:
            boundaries.append(cur[0])
    boundaries.append(len(text_blocks))
    boundaries = sorted(set(boundaries))

    site_ids = {b.id for b in sites}
    for index in range(len(boundaries) - 1):
        lo, hi = boundaries[index], boundaries[index + 1]
        members = text_blocks[lo:hi]
        if not members:
            continue
        start_id, end_id = members[0].id, members[-1].id
        region_id = f"region:{start_id}-{end_id}"
        local_site_count = sum(1 for b in members if b.id in site_ids)
        local_sizes = [round(b.font_size, 1) for b in members]
        if local_site_count >= _MIN_REGION_SITES and local_sizes:
            body_size = median(local_sizes)
            from_parent = False
        else:
            # 局部数据不足：回退上层（文档）画像，避免频繁换模板。
            body_size = document_body_size
            from_parent = True
        regions[region_id] = RegionProfile(
            region_id=region_id,
            start_block_id=start_id,
            end_block_id=end_id,
            body_size=body_size,
            site_count=local_site_count,
            from_parent=from_parent,
            parent_id=document_region.region_id,
        )
        for block in members:
            region_of[block.id] = region_id

    # 任何未落入细分区域的块（区间外/无字号）挂到 document 区域。
    for block in blocks:
        region_of.setdefault(block.id, document_region.region_id)
    return regions, region_of


def _style_metrics(
    blocks: Sequence[Block],
    sites: Sequence[Block],
    classes: dict[str, StyleClass],
    relative_numbering: dict[int, int],
    class_level: dict[str, int],
) -> dict[str, float]:
    # 正文块子集取自本次解析共享的 BlockIndex（L01）：此前本模块三处各全篇扫一遍。
    text_blocks = list(block_index(blocks).text_blocks())
    styled = [b for b in text_blocks if b.is_heading_style]
    metrics: dict[str, float] = {
        "heading_density": len(sites) / max(len(text_blocks), 1),
        "styled_density": len(styled) / max(len(text_blocks), 1),
        "prose_styled_ratio": (
            sum(1 for b in styled if is_prose_shaped(b.text)) / len(styled)
            if styled else 0.0
        ),
    }

    # Disagreement is measured against the *relative* depth, because that is
    # the level the evidence layer will actually assert.  A document that is
    # uniformly shifted in both style and numbering agrees with itself.
    disagreements = total = 0
    style_levels: set[int] = set()
    numbering_levels: set[int] = set()
    for block in sites:
        style_level = block.heading_level if block.is_heading_style else None
        depth = relative_numbering.get(block.id)
        if style_level is not None:
            style_levels.add(style_level)
        if depth is not None:
            numbering_levels.add(depth)
        if style_level is None or depth is None:
            continue
        total += 1
        if style_level != depth:
            disagreements += 1
    metrics["style_numbering_disagreement"] = disagreements / max(total, 1)

    # Resolving power, not agreement: a document whose headings all carry one
    # style while it demonstrably has four depths has a style that cannot
    # express depth at all.  Disagreement alone understates this -- the style is
    # not wrong about any particular level, it is silent about every level.
    # The reference is how many depths the document itself distinguishes, which
    # its style classes state even when nothing is numbered.
    observed_depths = max(len(set(class_level.values())), len(numbering_levels))
    if style_levels and observed_depths > 1:
        metrics["style_level_underresolution"] = max(
            0.0, 1.0 - len(style_levels) / observed_depths,
        )
    else:
        metrics["style_level_underresolution"] = 0.0

    # Font size should not grow with depth.  Compare the median size of each
    # class ordered by its style level, when styles are present.
    sizes_by_level: dict[int, list[float]] = {}
    for style_class in classes.values():
        level = style_class.style_level or style_class.numbering_depth
        if level is None or style_class.font_size is None:
            continue
        sizes_by_level.setdefault(level, []).append(style_class.font_size)
    ordered = sorted(sizes_by_level)
    inversions = pairs = 0
    for shallow, deep in zip(ordered, ordered[1:], strict=False):
        pairs += 1
        if median(sizes_by_level[deep]) > median(sizes_by_level[shallow]):
            inversions += 1
    metrics["font_level_inversion"] = inversions / max(pairs, 1)
    return metrics




@measured_once
def analyze_typography(blocks: Sequence[Block]) -> TypographyProfile:
    """Measure one document's typography.  Pure function of the block list."""
    # 正文块子集取自本次解析共享的 BlockIndex（L01）：此前本模块三处各全篇扫一遍。
    text_blocks = list(block_index(blocks).text_blocks())
    if not text_blocks:
        return TypographyProfile()

    body_size = _body_size(blocks)
    bold_ratio = sum(1 for b in text_blocks if b.is_bold) / len(text_blocks)
    sites = [b for b in text_blocks if _looks_like_heading_site(b, body_size)]

    # Sites that wear the body's own signature are dropped before anything is
    # measured: leaving them in would let mis-styled label lines take part in
    # the class hierarchy and push the real headings a level deeper.
    body_signature = _body_signature(text_blocks, {b.id for b in sites})
    body_signature_blocks = _sites_wearing_body_signature(sites, body_signature)
    if body_signature_blocks:
        logger.info(
            "[Typography] %d site(s) wear the body signature %s; "
            "excluded from the heading typography",
            len(body_signature_blocks), body_signature,
        )
        sites = [b for b in sites if b.id not in body_signature_blocks]

    root_depth, numbering_trust, relative = _analyse_numbering(sites)
    classes, class_of = _build_classes(
        sites, relative, bold_informative=bold_ratio <= _BOLD_SATURATION_RATIO,
    )
    class_level: dict[str, int] = {}
    class_level_labels: dict[str, tuple[ClassLevelLabel, ...]] = {}
    class_level_confidence: dict[str, float] = {}
    skipped = ""
    if len(classes) < _MIN_CLASSES or len(sites) < _MIN_SITES:
        skipped = "insufficient evidence"
    else:
        sequence = [class_of[b.id] for b in sites]
        # A03：编号父前缀是语义父子证据，先于层级可信度计算求出。
        numbering_parents = _numbering_prefix_parents(sites, class_of)
        base_level = _class_levels(sequence, list(classes))
        depth_confirmed = _numbering_depth_confirmed_classes(
            sites, class_of, base_level,
        )
        class_level, class_level_labels, class_level_confidence = (
            _class_levels_with_confidence(
                sequence, list(classes), numbering_parents, depth_confirmed,
            )
        )
        unreached = len(classes) - len(class_level)
        if unreached:
            logger.info(
                "[Typography] %d class(es) unreachable from the opening class; "
                "no class level derived for them", unreached,
            )

    # A03：分层区域画像（document -> section range）。始终产出至少 document
    # 区域，作为其它区域的回退父层。
    regions, region_of = _profile_regions(blocks, sites, body_size)

    metrics = _style_metrics(blocks, sites, classes, relative, class_level)

    # Style evidence loses *level* authority when styles are demonstrably
    # misused; it keeps its authority over heading existence, which is judged
    # per block by shape evidence rather than by a document-wide average.
    #
    # Every term below is a measure of style *misuse*.  Style density is not:
    # a short memo with four sections and a textbook of two hundred one-line
    # sections are both heading-dense and both correctly typeset, so density
    # cannot separate them from an over-styled document.  It stays in
    # ``metrics`` as a reported diagnostic, and the over-styling it was meant
    # to proxy for is measured directly by ``prose_styled_ratio``.
    style_trust = 1.0
    style_trust -= min(metrics["prose_styled_ratio"], 1.0) * 0.60
    style_trust -= min(metrics["style_level_underresolution"], 1.0) * 0.60
    style_trust -= min(metrics["font_level_inversion"], 1.0) * 0.30
    style_trust -= min(metrics["style_numbering_disagreement"], 1.0) * 0.30
    style_trust = max(0.0, min(style_trust, 1.0))

    trust = min(style_trust, numbering_trust)
    if bold_ratio > _BOLD_SATURATION_RATIO:
        trust = min(trust, 0.85)

    # A03：零语义证据（既无编号、又无显式样式层级）且类层级本身歧义
    # （多标签且最强标签可信度普遍偏低）时，输出低置信/扁平视图——承认
    # 多棵树同样合理，不把某条合法路径当作已证实结构。有编号或显式样式
    # 层级、或类层级可信度高，都不判扁平。
    has_semantic_level = bool(relative) or any(
        style_class.style_level is not None for style_class in classes.values()
    )
    multi_depth = len(set(class_level.values())) > 1
    ambiguous = bool(class_level_confidence) and all(
        conf < _CLASS_LEVEL_DOMINANCE for conf in class_level_confidence.values()
    )
    flat_view = multi_depth and not has_semantic_level and ambiguous
    if flat_view:
        logger.info(
            "[Typography] No semantic depth evidence and class levels are "
            "ambiguous; emitting a low-confidence flat view",
        )

    profile = TypographyProfile(
        numbering_root_depth=root_depth,
        numbering_trust=numbering_trust,
        style_trust=style_trust,
        trust=trust,
        bold_ratio=bold_ratio,
        metrics=metrics,
        relative_numbering=relative,
        class_of=class_of,
        classes=classes,
        class_level=class_level,
        class_level_labels=class_level_labels,
        class_level_confidence=class_level_confidence,
        regions=regions,
        region_of=region_of,
        flat_view=flat_view,
        normalisation_skipped=skipped,
        body_signature=body_signature,
        body_signature_blocks=body_signature_blocks,
    )
    logger.info(
        "[Typography] %d site(s), %d class(es), trust=%.2f "
        "(style=%.2f, numbering=%.2f), numbering root depth=%d%s",
        len(sites), len(classes), trust, style_trust, numbering_trust,
        root_depth, f", normalisation skipped: {skipped}" if skipped else "",
    )
    return profile


__all__ = [
    "StyleClass",
    "ClassLevelLabel",
    "RegionProfile",
    "TypographyProfile",
    "analyze_typography",
    "is_prose_shaped",
    "numbering_depth",
]
