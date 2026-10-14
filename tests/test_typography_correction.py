"""Regression tests for typography-driven heading correction (D layer).

Four typographies that real authors produce, plus the control.  Every variant
renders the *same* logical structure, so ``level`` is always the truth and the
typography is the only variable — the same discipline as
``tests/data/mis_styled/``, built in memory so the tests carry no fixtures.

Blocks mirror the DOCX provider's transport form: bold runs arrive wrapped in
``**``.  That is deliberate — the shape gates must read through the transport
markers (see ``titles.structural_text``), and a test that fed clean text would
not exercise them.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser.global_inference import GlobalHeadingInference
from modules.parser.heading_candidates import generate_heading_candidate_set
from modules.parser.schemas import ChapterNode
from modules.parser.titles import normalize_title
from modules.parser.typography import (
    ClassLevelLabel,
    RegionProfile,
    analyze_typography,
    is_prose_shaped,
)

BODY_SIZE = 11.0
LEVEL_SIZE = {1: 16.0, 2: 14.0, 3: 12.5, 4: 11.5}
BODY = (
    "This section records the operational context in enough detail that a "
    "reader can reconstruct the decision without consulting the appendix."
)
BODY2 = (
    "Measurements were repeated three times and the median is reported; the "
    "spread never exceeded four percent across the runs."
)
SECTIONS: tuple[tuple[int, str], ...] = (
    (1, "Programme Overview"),
    (2, "Scope and Boundaries"),
    (2, "Known Risks"),
    (1, "Technical Design"),
    (2, "Storage Layer"),
    (2, "Serving Layer"),
    (1, "Rollout Plan"),
    (2, "Staged Release"),
    (2, "Rollback Criteria"),
)
# Four levels, so that a depth signal has something to get wrong.
DEEP_SECTIONS: tuple[tuple[int, str], ...] = (
    (1, "System Overview"),
    (2, "Architecture"),
    (3, "Frontend Runtime"),
    (3, "Backend Runtime"),
    (4, "Request Pipeline"),
    (4, "Persistence Adapters"),
    (2, "Deployment Topology"),
    (1, "Data Pipeline"),
    (2, "Ingestion"),
    (2, "Transformation"),
    (3, "Validation Rules"),
    (3, "Normalisation"),
    (1, "Observed Results"),
    (2, "Throughput"),
    (2, "Accuracy"),
)


def _numbers(sections: tuple[tuple[int, str], ...] = SECTIONS) -> list[str]:
    """Depth-correct section numbers: 1, 1.1, 1.2, 2, ..."""
    counters = [0] * 6
    out: list[str] = []
    for level, _title in sections:
        counters[level - 1] += 1
        for deeper in range(level, 6):
            counters[deeper] = 0
        out.append(".".join(str(counters[i]) for i in range(level)))
    return out


class _Builder:
    """Accumulates blocks in the provider's transport form."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []

    def add(
        self,
        text: str,
        *,
        size: float = BODY_SIZE,
        bold: bool = False,
        style_level: int | None = None,
    ) -> int:
        block_id = len(self.blocks)
        self.blocks.append(Block(
            id=block_id,
            type="text",
            text=f"**{text}**" if bold else text,
            font_size=size,
            is_bold=bold,
            is_heading_style=style_level is not None,
            heading_level=style_level,
            metadata={"source": "docx"},
        ))
        return block_id


def _decode(blocks: list[Block], votes: list[ChapterNode] | None = None) -> list[tuple[int, str]]:
    """Run the deterministic core and return its (level, title) outline."""
    candidate_set = generate_heading_candidate_set(blocks)
    decoded = GlobalHeadingInference(blocks, candidate_set).decode(votes or [])
    return [(chapter.level, normalize_title(chapter.title)) for chapter in decoded]


def _votes_at(blocks: list[Block], levels: list[int]) -> list[ChapterNode]:
    """One perfectly aligned vote per heading block, at the levels given.

    Builders alternate heading and body, so the headings are the even ids.
    """
    heading_blocks = [block for block in blocks if block.id % 2 == 0]
    return [
        ChapterNode(
            block_id=block.id,
            title=normalize_title(block.text),
            snippet=normalize_title(block.text)[:40],
            level=level,
            confidence=0.95,
        )
        for block, level in zip(heading_blocks, levels, strict=True)
    ]


def _votes_resolve_depth(blocks: list[Block], votes: list[ChapterNode]) -> bool:
    inference = GlobalHeadingInference(blocks, generate_heading_candidate_set(blocks))
    return inference._compute_votes_resolve_depth(inference._build_sites(votes))


# ── Variants: each returns (blocks, expected outline) ─────────────────

def _build_clean() -> tuple[list[Block], list[tuple[int, str]]]:
    """Control: correct Heading N styles, depth-correct numbering."""
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    for (level, title), number in zip(SECTIONS, _numbers(), strict=True):
        builder.add(
            f"{number} {title}", size=LEVEL_SIZE[level], bold=True, style_level=level,
        )
        expected.append((level, f"{number} {title}"))
        builder.add(BODY)
    return builder.blocks, expected


def _build_style_shift() -> tuple[list[Block], list[tuple[int, str]]]:
    """Author started at Heading 2: every style is one level too deep."""
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    for (level, title), number in zip(SECTIONS, _numbers(), strict=True):
        builder.add(
            f"{number} {title}",
            size=LEVEL_SIZE[level],
            bold=True,
            style_level=min(level + 1, 6),
        )
        expected.append((level, f"{number} {title}"))
        builder.add(BODY)
    return builder.blocks, expected


def test_numbered_series_nested_under_unnumbered_sections_keeps_its_depth():
    """真实样本（Windycar PRD）缺陷：编号序列整体嵌在无编号章节之下时不得被拉到 L1。

    文档里没有裸 ``3`` / ``4`` 标题，最浅编号是 ``3.1``（深度 2），而它们的父级
    ``供应商基本信息`` / ``需求内容`` 是无编号的 L2。此前 ``_analyse_numbering`` 把
    「最浅编号深度」无条件归一到 L1，于是 ``3.1`` 被判成 L1、后面整棵子树错位一层。
    锚点必须由该层的明确样式给出。
    """
    builder = _Builder()
    builder.add("Windycar 非标对接需求 PRD", size=20.0, bold=True, style_level=1)
    builder.add(BODY)
    builder.add("版本信息", size=16.0, bold=True, style_level=2)
    builder.add(BODY)
    builder.add("供应商基本信息", size=16.0, bold=True, style_level=2)
    builder.add(BODY)
    builder.add("3.1 供应商接口账号信息", size=15.0, bold=True, style_level=3)
    builder.add(BODY)
    builder.add("3.2 对接项和对接方式", size=15.0, bold=True, style_level=3)
    builder.add(BODY)
    builder.add("需求内容", size=16.0, bold=True, style_level=2)
    builder.add(BODY)
    builder.add("4.1 整体逻辑", size=15.0, bold=True, style_level=3)
    builder.add(BODY)
    builder.add("4.2 静态信息", size=15.0, bold=True, style_level=3)
    builder.add(BODY)

    outline = dict((title, level) for level, title in _decode(builder.blocks))
    unnumbered = {outline["版本信息"], outline["供应商基本信息"], outline["需求内容"]}
    assert len(unnumbered) == 1, f"三个无编号章节应同级，实际 {unnumbered}"
    parent_level = unnumbered.pop()
    for numbered in ("3.1 供应商接口账号信息", "3.2 对接项和对接方式",
                     "4.1 整体逻辑", "4.2 静态信息"):
        assert outline[numbered] == parent_level + 1, (
            f"{numbered} 应恰好比无编号父级深一级（L{parent_level + 1}），"
            f"实际 L{outline[numbered]}——编号序列被无条件拉到顶层就是原缺陷"
        )


def _build_numbering_shift() -> tuple[list[Block], list[tuple[int, str]]]:
    """No styles; visible numbering one level too deep (top sections are 1.1)."""
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    for (level, title), number in zip(SECTIONS, _numbers(), strict=True):
        rendered = f"1.{number} {title}"
        builder.add(rendered, size=LEVEL_SIZE[level], bold=True)
        expected.append((level, rendered))
        builder.add(BODY)
    return builder.blocks, expected


def _build_over_styled() -> tuple[list[Block], list[tuple[int, str]], list[int]]:
    """Correct headings, but every second body paragraph is styled Heading 3."""
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    distractors: list[int] = []
    for index, ((level, title), number) in enumerate(
        zip(SECTIONS, _numbers(), strict=True),
    ):
        builder.add(
            f"{number} {title}", size=LEVEL_SIZE[level], bold=True, style_level=level,
        )
        expected.append((level, f"{number} {title}"))
        if index % 2 == 0:
            distractors.append(builder.add(BODY, style_level=3))
        else:
            builder.add(BODY2)
    return builder.blocks, expected, distractors


def _build_styled_labels() -> tuple[list[Block], list[tuple[int, str]], list[int]]:
    """Correct headings, plus short metadata lines wearing a heading style.

    Unlike ``over_styled`` these distractors are **not** prose: short, no
    sentence punctuation, nothing in the wording gives them away.  What does is
    that they wear the body's own size and weight while the real headings are
    all larger and bold.
    """
    labels = (
        "Owner: Platform Team",
        "Status: In review",
        "Last updated: 2026-03-11",
    )
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    distractors: list[int] = []
    for index, ((level, title), number) in enumerate(
        zip(SECTIONS, _numbers(), strict=True),
    ):
        builder.add(
            f"{number} {title}", size=LEVEL_SIZE[level], bold=True, style_level=level,
        )
        expected.append((level, f"{number} {title}"))
        distractors.append(
            builder.add(labels[index % len(labels)], style_level=3),
        )
        builder.add(BODY)
    return builder.blocks, expected, distractors


def _build_font_inverted(
    sections: tuple[tuple[int, str], ...] = DEEP_SECTIONS,
) -> tuple[list[Block], list[tuple[int, str]]]:
    """No styles, no numbering, and deeper headings rendered *larger*.

    Every physical depth cue is either absent or actively misleading, so the
    only thing left that states the hierarchy is the order the headings
    themselves repeat in.
    """
    inverted = {1: 12.5, 2: 14.0, 3: 16.0, 4: 17.5}
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    for level, title in sections:
        builder.add(title, size=inverted[level], bold=True)
        expected.append((level, title))
        builder.add(BODY)
    return builder.blocks, expected


def _build_single_level() -> tuple[list[Block], list[tuple[int, str]]]:
    """A document that really is flat: one rank of sections, one style class."""
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    for title in ("Purpose", "Method", "Findings", "Appendix"):
        builder.add(title, size=14.0, bold=True)
        expected.append((1, title))
        builder.add(BODY)
    return builder.blocks, expected


def _build_bold_everything() -> tuple[list[Block], list[tuple[int, str]]]:
    """Whole document bold at body size: emphasis carries no information."""
    builder = _Builder()
    expected: list[tuple[int, str]] = []
    for (level, title), number in zip(SECTIONS, _numbers(), strict=True):
        builder.add(f"{number} {title}", size=BODY_SIZE, bold=True)
        expected.append((level, f"{number} {title}"))
        builder.add(BODY, size=BODY_SIZE, bold=True)
    return builder.blocks, expected


# ── Shape gate: notation is not punctuation ───────────────────────────

def test_section_number_dots_are_not_sentence_punctuation():
    """A section number must never make its own heading look like prose.

    ``1.1.1 Frontend Runtime`` contains two periods and no sentence.  Counting
    them excluded every heading numbered at depth >= 3 from the typography
    layer *and* fired the styled-prose veto on it.
    """
    for name in (
        "1.1.1 Frontend Runtime",
        "1.1.2.1 Request Pipeline",
        "2.2.1 Validation Rules",
        "Version 1.2.3 notes",
        "3. Results",
    ):
        assert not is_prose_shaped(name), name


def test_real_prose_is_still_prose():
    for prose in (
        BODY,
        BODY2,
        "本节说明该阶段的输入、输出与失败模式，并给出可复现实验的参数取值。",
        # A numbered paragraph is still a paragraph.
        f"1.2.3 {BODY}",
        "Done. Next.",
    ):
        assert is_prose_shaped(prose), prose


# ── Hard gate: a clean document must be untouched ─────────────────────

def test_clean_document_decodes_exactly_and_measures_full_trust():
    """The control must survive every correction mechanism unchanged.

    Trust is asserted alongside the outline on purpose: a clean document that
    still decodes correctly while *measuring* as untrustworthy is a latent
    failure, because the trust value is what governs how far the corrections
    may go on the next document.
    """
    blocks, expected = _build_clean()

    assert _decode(blocks) == expected

    profile = analyze_typography(blocks)
    assert profile.trust == 1.0
    assert profile.style_trust == 1.0
    assert profile.numbering_trust == 1.0
    assert profile.metrics["prose_styled_ratio"] == 0.0
    assert profile.metrics["style_level_underresolution"] == 0.0


# ── The four author mistakes ──────────────────────────────────────────

def test_style_shift_recovers_levels_from_numbering():
    """Styles start at Heading 2; numbering still carries the true depth."""
    blocks, expected = _build_style_shift()
    assert _decode(blocks) == expected

    profile = analyze_typography(blocks)
    assert profile.metrics["style_numbering_disagreement"] == 1.0
    assert profile.style_trust < profile.numbering_trust


def test_numbering_shift_measures_depth_relative_to_the_document_root():
    """Top-level sections numbered ``1.1`` are level 1, not level 2."""
    blocks, expected = _build_numbering_shift()
    assert _decode(blocks) == expected

    profile = analyze_typography(blocks)
    assert profile.numbering_root_depth == 2
    assert profile.numbering_trust == 1.0
    # Every heading keeps a numbering level, shifted up by the root depth.
    assert sorted(set(profile.relative_numbering.values())) == [1, 2]


def test_over_styled_prose_never_becomes_a_heading():
    """A heading style applied to a paragraph loses to the paragraph's shape."""
    blocks, expected, distractors = _build_over_styled()
    outline = _decode(blocks)

    assert outline == expected
    titles = {title for _level, title in outline}
    assert normalize_title(BODY) not in titles

    candidate_set = generate_heading_candidate_set(blocks)
    by_id = {c.block_id: c for c in candidate_set.candidates}
    for block_id in distractors:
        assert "styled_prose_negative" in by_id[block_id].evidence_kinds(polarity=-1)


def test_short_labels_wearing_the_body_signature_are_not_headings():
    """The failure shape analysis cannot reach.

    ``Status: In review`` is short, carries no sentence punctuation and reads
    like a name, so the styled-prose veto has nothing to bite on.  It is
    rejected because the document's own typography puts it on the body side of
    a line the author drew.
    """
    blocks, expected, distractors = _build_styled_labels()
    outline = _decode(blocks)

    assert outline == expected

    profile = analyze_typography(blocks)
    assert profile.body_signature == (BODY_SIZE, False)
    assert set(profile.body_signature_blocks) == set(distractors)

    by_id = {c.block_id: c for c in generate_heading_candidate_set(blocks).candidates}
    for block_id in distractors:
        candidate = by_id[block_id]
        # Shape alone would have let these through.
        assert "styled_prose_negative" not in candidate.evidence_kinds(polarity=-1)
        assert "body_signature_negative" in candidate.evidence_kinds(polarity=-1)


def test_body_signature_veto_stays_silent_when_nothing_distinguishes():
    """A document set entirely in one size and weight must not veto itself.

    In ``bold_everything`` every heading wears the body's signature, so the
    test separates nothing — exactly the situation where it has to abstain
    rather than reject the whole document.
    """
    blocks, expected = _build_bold_everything()
    profile = analyze_typography(blocks)

    assert profile.body_signature == (BODY_SIZE, True)
    assert profile.body_signature_blocks == frozenset()
    assert _decode(blocks) == expected


def test_font_inverted_levels_come_from_the_repetition_pattern():
    """Depth survives a font scale that runs backwards.

    There is no style, no numbering, and the font size says the opposite of
    the truth, so the style-class level is the only remaining depth signal.
    """
    blocks, expected = _build_font_inverted()
    assert _decode(blocks) == expected

    profile = analyze_typography(blocks)
    # Nothing else could have supplied a level here.
    assert profile.relative_numbering == {}
    assert all(style_class.style_level is None for style_class in profile.classes.values())
    # Larger font, deeper level: the class levels invert the size order.
    by_size = {
        style_class.font_size: profile.class_level[class_id]
        for class_id, style_class in profile.classes.items()
    }
    assert by_size == {12.5: 1, 14.0: 2, 16.0: 3, 17.5: 4}


def test_class_level_prior_yields_to_trusted_numbering():
    """The measured class level must not overrule evidence that is trusted.

    On a clean document every family agrees, so agreement alone proves
    nothing; what must hold is that the prior's *weight* collapses when the
    numbering it would compete with is fully trusted.
    """
    blocks, _expected = _build_clean()
    candidate_set = generate_heading_candidate_set(blocks)
    inference = GlobalHeadingInference(blocks, candidate_set)
    sites = inference._build_sites([])

    assert inference.profile.numbering_trust == 1.0
    for site in sites:
        prior = inference._class_level_prior(site)
        assert prior is not None
        _level, weight = prior
        assert weight == 0.0


# ── A vote that separates nothing does not get to decide ──────────────

def test_flat_votes_do_not_erase_a_hierarchy_the_document_states():
    """Voting ``L1`` for everything is abstaining on depth, not settling it.

    Observed live on 2026-09-03: the deployed model found every heading in a
    font-inverted document and called them all level 1.  The vote's level
    weight (3.0) then outranked the class-level prior (2.8) — the *only* depth
    signal such a document has — and level accuracy fell back to 29.4%, the
    number this whole layer was built to fix.
    """
    blocks, expected = _build_font_inverted()
    flat = _votes_at(blocks, [1] * len(expected))

    assert _votes_resolve_depth(blocks, flat) is False
    assert _decode(blocks, flat) == expected
    # The vote keeps its full say over heading existence; only its level
    # claim abstains, so nothing is lost from the outline itself.
    assert _decode(blocks, flat) == _decode(blocks)


def test_votes_that_state_depth_keep_their_level_authority():
    """The abstention keys on the votes being flat, not on votes existing."""
    blocks, expected = _build_font_inverted()
    truthful = _votes_at(blocks, [level for level, _title in expected])

    assert _votes_resolve_depth(blocks, truthful) is True
    assert _decode(blocks, truthful) == expected


def test_a_genuinely_flat_document_never_triggers_the_abstention():
    """Uniform votes are only uninformative next to a document that has depth.

    Here the author wrote one rank of sections, so the style classes are flat
    too: the votes agree with the document rather than failing to read it, and
    there is nothing for them to abstain from.
    """
    blocks, expected = _build_single_level()
    flat = _votes_at(blocks, [1] * len(expected))

    assert _votes_resolve_depth(blocks, flat) is True
    assert _decode(blocks, flat) == expected


def test_flat_votes_leave_trusted_numbering_in_charge():
    """Silencing a flat vote must not disturb evidence that did state a level.

    On ``numbering_shift`` the class prior is already weightless (numbering is
    fully trusted), so the recovered levels have to come from the numbering
    itself — with or without a model that abstained.
    """
    blocks, expected = _build_numbering_shift()
    flat = _votes_at(blocks, [1] * len(expected))

    assert _votes_resolve_depth(blocks, flat) is False
    assert _decode(blocks, flat) == expected


def test_bold_everything_does_not_promote_body_paragraphs():
    """Saturated bold carries no information, with or without model votes.

    The oracle path is the one that used to fail: once two same-signature
    headings were confirmed, the style-signature boost promoted every bold
    body paragraph in the document, because the signature was the body's own.
    """
    blocks, expected = _build_bold_everything()

    assert _decode(blocks) == expected

    votes = [
        ChapterNode(
            block_id=block.id,
            title=normalize_title(block.text),
            snippet=normalize_title(block.text)[:40],
            level=level,
            confidence=0.95,
        )
        for block, (level, _title) in zip(
            [b for b in blocks if b.id % 2 == 0], expected, strict=True,
        )
    ]
    assert _decode(blocks, votes) == expected

    profile = analyze_typography(blocks)
    assert profile.bold_is_uninformative


# ══════════════════════════════════════════════════════════════════
# A03 · 区域画像 + 类层级多标签可信度
# ══════════════════════════════════════════════════════════════════


def test_clean_document_class_levels_are_high_confidence():
    """干净文档：类层级唯一可解，可信度高，且不判扁平视图。"""
    blocks, _expected = _build_clean()
    profile = analyze_typography(blocks)

    assert profile.class_level  # 有派生
    assert not profile.flat_view
    # 每个有层级的类都带一个可信度，且干净文档下应偏高。
    for class_id in profile.class_level:
        assert class_id in profile.class_level_confidence
        assert profile.class_level_confidence[class_id] >= 0.6
        labels = profile.class_level_labels[class_id]
        assert labels  # 至少一个候选标签
        assert abs(sum(l.confidence for l in labels) - 1.0) < 1e-6
        # 最强标签的 level 与单值投影一致（既有权威不变）。
        assert labels[0].level == profile.class_level[class_id]


def test_numbering_prefix_pins_class_level_with_high_confidence():
    """编号父前缀印证 BFS 时，标签基底记为 numbering_prefix 且可信度拉满。"""
    blocks, _expected = _build_clean()  # 带 1 / 1.1 / 1.2 … 的深度正确编号
    profile = analyze_typography(blocks)

    bases = {
        label.basis
        for labels in profile.class_level_labels.values()
        for label in labels
    }
    # 至少有一个非根类的层级由编号父前缀语义证据钉住。
    assert "numbering_prefix" in bases
    for class_id, labels in profile.class_level_labels.items():
        if labels[0].basis == "numbering_prefix":
            assert profile.class_level_confidence[class_id] >= 0.9


def _build_alternating_same_level() -> list[Block]:
    """连续同级但两种交替样式的标题（A B A B）：应判为层级歧义。

    没有任何编号、没有显式样式；两种字号交替出现，每个后面跟一段正文。
    真值是「它们其实同级」，BFS 会给出 A=L1/B=L2 的合法先验，但这只是
    一种可能的树 —— 可信度必须低，并保留「同级」备选标签。
    """
    builder = _Builder()
    titles = ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta")
    for index, title in enumerate(titles):
        size = 15.0 if index % 2 == 0 else 13.0
        builder.add(title, size=size, bold=True)
        builder.add(BODY)
    return builder.blocks


def test_alternating_styles_are_flagged_as_ambiguous_not_certain():
    """A B A B 交替样式：层级歧义，低可信度并给出「同级」备选标签。

    方案原文：`A B A B` 也可能是两种样式交替出现的同级标题；BFS 只是一种
    满足相邻限制的先验，不是唯一真实父子关系。这里断言可信度被压低、且
    产出了一个「与前驱同级」的备选标签，而不是把 A=L1/B=L2 当已证实结构。
    """
    blocks = _build_alternating_same_level()
    profile = analyze_typography(blocks)

    assert profile.class_level  # 仍派生了层级（合法路径）
    # 至少一个类被判为层级歧义（可信度低于主导阈值）。
    low_conf = [
        class_id for class_id, conf in profile.class_level_confidence.items()
        if conf < 0.6
    ]
    assert low_conf, profile.class_level_confidence
    # 歧义类保留多个候选标签（多棵树同样合理）。
    for class_id in low_conf:
        labels = profile.class_level_labels[class_id]
        assert len(labels) >= 2
        bases = {label.basis for label in labels}
        assert bases & {"sequence_ambiguous", "sibling_alt"}
    # 零语义证据 + 歧义 => 扁平视图。
    assert profile.flat_view is True


def test_flat_view_off_when_numbering_supplies_semantics():
    """有编号语义证据时，即使字号交替也不判扁平视图。"""
    blocks, _expected = _build_numbering_shift()
    profile = analyze_typography(blocks)
    assert profile.flat_view is False


def _build_spliced_templates() -> list[Block]:
    """拼接模板 + 章节中途字号变化：正文字号前后两段不同。

    前半段正文 11pt、标题 16pt；后半段（如附录/引用区）正文 13pt、标题
    18pt。区域画像应在字号变化点附近切出至少两个区域，各自的 body_size
    反映本区域而非全篇。
    """
    builder = _Builder()
    for _ in range(8):
        builder.add("Front Section", size=16.0, bold=True)
        builder.add(BODY, size=11.0)
        builder.add(BODY2, size=11.0)
    for _ in range(8):
        builder.add("Back Section", size=18.0, bold=True)
        builder.add(BODY, size=13.0)
        builder.add(BODY2, size=13.0)
    return builder.blocks


def test_region_profile_splits_on_body_size_change():
    """区域画像随正文字号分布变化切分，body_size 反映局部而非全篇。"""
    blocks = _build_spliced_templates()
    profile = analyze_typography(blocks)

    # 至少 document 区域 + 一个细分区域。
    assert "region:document" in profile.regions
    sub_regions = [
        r for rid, r in profile.regions.items() if rid != "region:document"
    ]
    assert len(sub_regions) >= 2
    # 每个块都被归入某个区域。
    assert all(bid in profile.region_of for bid in (b.id for b in blocks))
    # 存在 body_size 不同的区域，证明分层统计生效。
    body_sizes = {round(r.body_size, 1) for r in sub_regions}
    assert len(body_sizes) >= 2, body_sizes
    # region_body_size_for 能取到局部正文字号。
    first_front = blocks[1].id  # 前段正文块
    assert profile.region_body_size_for(first_front) is not None


def test_small_region_falls_back_to_document_profile():
    """站点不足的小区域回退上层画像（from_parent），不自立门户。

    构造一个几乎全是正文、只有极少数标题的文档：任何被切出的细分区域
    站点数都不足 _MIN_REGION_SITES，应回退到 document 的 body_size。
    """
    builder = _Builder()
    builder.add("Only Heading", size=16.0, bold=True)
    for _ in range(30):
        builder.add(BODY, size=11.0)
    blocks = builder.blocks
    profile = analyze_typography(blocks)

    sub_regions = [
        r for rid, r in profile.regions.items() if rid != "region:document"
    ]
    # 若产生了细分区域，站点不足者必须标记 from_parent 且 body_size 取父层。
    doc_body = profile.regions["region:document"].body_size
    for region in sub_regions:
        if region.site_count < 4:
            assert region.from_parent is True
            assert region.body_size == doc_body
