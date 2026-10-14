from __future__ import annotations

import asyncio

import pytest

from infrastructure.models import Block
from modules.parser.anchor_alignment import MonotonicAnchorAligner, _similarity
from modules.parser.heading_candidates import generate_heading_candidates
from modules.parser.hierarchy import (
    VirtualContainer,
    evaluate_soft_constraints,
    virtual_containers_for,
)
from modules.parser.parser import CaliperParser
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode, LLMRouterOutput


def _body(block_id: int, text: str | None = None, size: float = 12.0) -> Block:
    return Block(
        id=block_id,
        type="text",
        text=text or f"Body paragraph {block_id}.",
        font_size=size,
    )


def _fail_legacy(*_args, **_kwargs):
    raise AssertionError("legacy pre-alignment selection must not run")


def _overlap_fixture() -> tuple[list[Block], list]:
    blocks = [_body(index) for index in range(11)]
    blocks[5] = Block(
        id=5,
        type="text",
        text="Shared Physical Heading",
        font_size=16.0,
        is_bold=True,
    )
    return blocks, generate_heading_candidates(blocks)


def _window_output(raw_block_id: int) -> LLMRouterOutput:
    return LLMRouterOutput(
        doc_title="Document",
        doc_authors="",
        chapters=[ChapterNode(
            block_id=raw_block_id,
            title="Shared Physical Heading",
            level=1,
            snippet="Shared Physical Heading",
            confidence=0.9,
            out_of_candidate=True,
        )],
    )


def test_sync_reduce_aligns_raw_windows_before_legacy_selection():
    blocks, candidates = _overlap_fixture()
    parser = CaliperParser()
    parser._deduplicate_overlap_anchors = _fail_legacy
    parser._verify_downgraded_anchors = _fail_legacy

    def route_window(_chunk, index, _total, _tail, **_kwargs):
        return _window_output(4 if index == 0 else 6)

    parser.router.route_chunk = route_window
    output = parser._map_reduce_route(
        ["[0] first window\n", "[5] overlap window\n"],
        blocks=blocks,
        candidates=candidates,
    )

    assert [chapter.start_block_id for chapter in output.chapters] == [5]
    votes = output.chapters[0].anchor_votes
    assert {vote.raw_block_id for vote in votes} == {4, 6}
    assert {vote.window_index for vote in votes} == {0, 1}
    assert output.chapters[0].source_windows == [0, 1]
    assert all(vote.out_of_candidate for vote in votes)


def test_async_reduce_preserves_each_window_vote_index():
    blocks, candidates = _overlap_fixture()
    parser = CaliperParser()
    parser._deduplicate_overlap_anchors = _fail_legacy
    parser._verify_downgraded_anchors = _fail_legacy

    async def route_window(_chunk, index, _total, _tail, **_kwargs):
        return _window_output(4 if index == 0 else 6)

    parser.router.async_route_chunk = route_window
    output = asyncio.run(parser._async_serial_route(
        ["[0] first window\n", "[5] overlap window\n"],
        blocks=blocks,
        candidates=candidates,
    ))

    assert [chapter.start_block_id for chapter in output.chapters] == [5]
    assert [vote.window_index for vote in output.chapters[0].anchor_votes] == [0, 1]


def test_far_offset_anchor_rescued_by_global_candidate_scan():
    """路由器 block id 偏移远超模糊窗口时，全局候选救援仍能对齐真实标题。

    场景：真实标题在 block 100（带编号「4.」），LLM 报 block 8（偏移 92，
    远超 radius×2 的候选扫描范围），且回传标题去掉了编号——全文精确匹配
    与本地窗口双双失效。救援层放开距离限制、在确定性候选里按高相似度
    （containment ≥ 0.92）找回真实位置。
    """
    blocks = [_body(index) for index in range(120)]
    blocks[100] = Block(
        id=100,
        type="text",
        text="4. Experimental Results Analysis",
        font_size=16.0,
        is_bold=True,
    )
    candidates = generate_heading_candidates(blocks)
    assert any(candidate.block_id == 100 for candidate in candidates)

    raw = [ChapterNode(
        block_id=8,
        title="Experimental Results Analysis",
        snippet="Experimental Results Analysis",
        level=1,
        confidence=0.9,
    )]

    aligned = MonotonicAnchorAligner(blocks).align_windows([raw], candidates=candidates)

    assert [chapter.start_block_id for chapter in aligned] == [100]
    assert aligned[0].anchor_votes[0].raw_block_id == 8


def _rescue_fixture() -> tuple[list[Block], list]:
    """真实标题在 block 100（「4.2 Ablation Studies and Discussion」）。"""
    blocks = [_body(index) for index in range(120)]
    blocks[100] = Block(
        id=100,
        type="text",
        text="4.2 Ablation Studies and Discussion",
        font_size=16.0,
        is_bold=True,
    )
    candidates = generate_heading_candidates(blocks)
    assert any(candidate.block_id == 100 for candidate in candidates)
    return blocks, candidates


def test_far_offset_anchor_rescued_below_legacy_containment_threshold():
    """单复数 + 编号差异（相似度 ≈ 0.875 < 旧 0.92 硬门槛）也要能救回。

    路由器回传「Ablation Study and Discussion」（单数、无编号），真实标题
    是「4.2 Ablation Studies and Discussion」——非包含关系，早期 0.92 硬
    门槛必漏；双门槛（绝对下限 0.6 + 相对优势 +0.2）应召回。
    """
    blocks, candidates = _rescue_fixture()
    raw = [ChapterNode(
        block_id=8,
        title="Ablation Study and Discussion",
        snippet="Ablation Study and Discussion",
        level=1,
        confidence=0.9,
    )]

    aligned = MonotonicAnchorAligner(blocks).align_windows([raw], candidates=candidates)

    assert [chapter.start_block_id for chapter in aligned] == [100]


def test_weak_far_candidate_does_not_hijack_local_anchor():
    """弱相似（≈ 0.32 < 绝对下限）的远端候选不得劫持锚点。

    查询标题与远端候选完全不相关时，即便本地窗口全是低分正文，
    救援也不应把锚点拖到 block 100——对齐结果必须留在本地窗口附近。
    """
    blocks, candidates = _rescue_fixture()
    raw = [ChapterNode(
        block_id=8,
        title="Completely Unrelated Heading",
        snippet="Completely Unrelated Heading",
        level=1,
        confidence=0.9,
    )]

    aligned = MonotonicAnchorAligner(blocks).align_windows([raw], candidates=candidates)

    assert len(aligned) == 1
    assert aligned[0].start_block_id != 100
    assert abs(aligned[0].start_block_id - 8) <= 16  # 留在本地模糊窗口内


def test_rewritten_nearby_candidate_beats_plausible_local_text():
    """radius×2 内的结构候选不能被远距离救援触发门槛挡掉。

    本地文本已有 0.8 相似度，因此不会触发全局救援；但稍远处的真实标题
    有更强的文本和结构证据，即使相似度低于旧的 0.92 门槛也应参与 DP。
    """
    blocks = [_body(index) for index in range(130)]
    blocks[100] = _body(100, "Methods Result Draft")
    blocks[112] = Block(
        id=112,
        type="text",
        text="Methods and Results",
        font_size=16.0,
        is_bold=True,
    )
    candidates = generate_heading_candidates(blocks)
    assert any(candidate.block_id == 112 for candidate in candidates)

    raw = [ChapterNode(
        block_id=100,
        title="Methods Results",
        snippet="Methods Results",
        level=1,
        confidence=0.9,
    )]

    aligned = MonotonicAnchorAligner(blocks).align_windows([raw], candidates=candidates)

    assert [chapter.start_block_id for chapter in aligned] == [112]


def test_monotonic_alignment_uses_physical_order_not_crossed_claims():
    blocks = [_body(index) for index in range(11)]
    blocks[2] = _body(2, "Alpha Heading", 16.0)
    blocks[8] = _body(8, "Beta Heading", 15.0)
    raw = [
        ChapterNode(
            block_id=1,
            title="Beta Heading",
            snippet="Beta Heading",
            level=1,
        ),
        ChapterNode(
            block_id=9,
            title="Alpha Heading",
            snippet="Alpha Heading",
            level=1,
        ),
    ]

    aligned = MonotonicAnchorAligner(blocks).align_windows([raw])

    assert [chapter.start_block_id for chapter in aligned] == [2, 8]
    assert [chapter.anchor_votes[0].raw_block_id for chapter in aligned] == [9, 1]
    assert all(chapter.anchor_votes[0].window_index == 0 for chapter in aligned)


@pytest.mark.parametrize(
    ("blocks", "chapters", "expected_promoted"),
    [
        (
            [
                Block(id=0, type="text", text="Chapter 1 Introduction", font_size=16.0, is_bold=True),
                _body(1),
                _body(2),
                Block(id=3, type="text", text="Hidden Heading", font_size=18.0, is_bold=True),
                _body(4),
                Block(id=5, type="text", text="Chapter 2 Methods", font_size=16.0, is_bold=True),
                _body(6),
            ],
            [
                ChapterNode(block_id=0, title="Chapter 1 Introduction", level=1, snippet="Chapter 1"),
                ChapterNode(block_id=5, title="Chapter 2 Methods", level=1, snippet="Chapter 2"),
            ],
            3,
        ),
        (
            [
                Block(id=0, type="text", text="Chapter One Title", font_size=14.0),
                _body(1),
                Block(id=2, type="text", text="Missed Heading", font_size=14.0, is_bold=True),
                _body(3),
            ],
            [
                ChapterNode(block_id=0, title="Chapter One Title", level=1, snippet="Chapter One"),
            ],
            2,
        ),
    ],
    ids=["genuinely_large_font", "same_size_sibling_bold"],
)
def test_inverse_proposals_are_selected_inside_global_dp_only(
    blocks: list[Block],
    chapters: list[ChapterNode],
    expected_promoted: int,
):
    resolver = IntervalResolver(blocks)
    # Any use of the former fuzzy/repair/insertion stages is a regression.
    resolver._fuzzy_anchor_correction = _fail_legacy
    resolver._validate_hierarchy = _fail_legacy
    resolver._inverse_audit_and_repair = _fail_legacy

    nodes = resolver.resolve(chapters)
    starts: list[int] = []

    def walk(items) -> None:
        for item in items:
            starts.append(item.start_block_id)
            walk(item.children)

    walk(nodes)
    assert expected_promoted in starts


def test_same_size_bold_sibling_rescued_by_confirmed_style_signature():
    """与正文同字号、仅加粗的标题款式：LLM 漏标的兄弟由样式签名放大救回。

    字号比 = 1.0 时既有结构地板（需 ratio≥1.1）不生效，漏标即永久丢失；
    但 LLM 已确认 ≥2 个同签名（同字号 + 加粗）标题时，同款未确认候选
    应获得激活地板、由全局 DP 决定是否选中。
    """
    blocks = [
        Block(id=0, type="text", text="First Bold Heading", font_size=12.0, is_bold=True),
        _body(1),
        _body(2),
        Block(id=3, type="text", text="Missed Bold Heading", font_size=12.0, is_bold=True),
        _body(4),
        Block(id=5, type="text", text="Third Bold Heading", font_size=12.0, is_bold=True),
        _body(6),
    ]
    chapters = [
        ChapterNode(block_id=0, title="First Bold Heading", level=1, snippet="First Bold Heading"),
        ChapterNode(block_id=5, title="Third Bold Heading", level=1, snippet="Third Bold Heading"),
    ]

    resolver = IntervalResolver(blocks)
    nodes = resolver.resolve(chapters)

    starts: list[int] = []

    def walk(items) -> None:
        for item in items:
            starts.append(item.start_block_id)
            walk(item.children)

    walk(nodes)
    assert 3 in starts


def test_style_signature_boost_skips_emphasis_prose():
    """同签名但句末标点结尾的粗体强调行不得被签名放大误促。"""
    blocks = [
        Block(id=0, type="text", text="First Bold Heading", font_size=12.0, is_bold=True),
        _body(1),
        Block(id=2, type="text", text="注意：这个操作不可逆，执行前必须先备份数据。", font_size=12.0, is_bold=True),
        _body(3),
        Block(id=4, type="text", text="Third Bold Heading", font_size=12.0, is_bold=True),
        _body(5),
    ]
    chapters = [
        ChapterNode(block_id=0, title="First Bold Heading", level=1, snippet="First Bold Heading"),
        ChapterNode(block_id=4, title="Third Bold Heading", level=1, snippet="Third Bold Heading"),
    ]

    resolver = IntervalResolver(blocks)
    nodes = resolver.resolve(chapters)

    starts: list[int] = []

    def walk(items) -> None:
        for item in items:
            starts.append(item.start_block_id)
            walk(item.children)

    walk(nodes)
    assert 2 not in starts


def test_out_of_candidate_vote_is_physically_checked_after_alignment():
    blocks = [_body(index) for index in range(7)]
    blocks[4] = Block(
        id=4,
        type="text",
        text="Recovered After Alignment",
        font_size=17.0,
        is_bold=True,
    )
    resolver = IntervalResolver(blocks)
    raw = [ChapterNode(
        block_id=1,
        title="Recovered After Alignment",
        snippet="Recovered After Alignment",
        level=1,
        confidence=0.4,
        out_of_candidate=True,
    )]

    aligned = resolver._aligner.align(raw, candidates=resolver.heading_candidates)
    assert aligned[0].start_block_id == 4
    assert aligned[0].anchor_votes[0].raw_block_id == 1
    assert aligned[0].anchor_votes[0].out_of_candidate

    decoded = resolver._global_inference.decode(aligned)
    assert [chapter.start_block_id for chapter in decoded] == [4]



def test_single_window_sync_and_async_also_use_physical_alignment():
    blocks, candidates = _overlap_fixture()

    sync_parser = CaliperParser()
    sync_parser._verify_downgraded_anchors = _fail_legacy
    sync_parser.router.route = lambda *_args, **_kwargs: _window_output(4)
    sync_output = sync_parser._map_reduce_route(
        ["[0] only window\n"], blocks=blocks, candidates=candidates,
    )

    async_parser = CaliperParser()
    async_parser._verify_downgraded_anchors = _fail_legacy

    async def route(*_args, **_kwargs):
        return _window_output(6)

    async_parser.router.async_route = route
    async_output = asyncio.run(async_parser._async_map_reduce_route(
        ["[0] only window\n"], blocks=blocks, candidates=candidates,
    ))

    for output, raw_id in ((sync_output, 4), (async_output, 6)):
        assert [chapter.start_block_id for chapter in output.chapters] == [5]
        assert [vote.raw_block_id for vote in output.chapters[0].anchor_votes] == [raw_id]
        assert output.chapters[0].anchor_votes[0].window_index == 0


def test_candidate_only_global_heading_keeps_pre_heading_blocks_as_preamble():
    blocks = [
        _body(0, "Document preface line."),
        _body(1, "More preface context."),
        Block(
            id=2,
            type="text",
            text="1. Introduction",
            font_size=16.0,
            is_bold=True,
        ),
        _body(3, "Section content."),
    ]
    resolver = IntervalResolver(blocks)

    nodes = resolver.resolve([])
    preamble = CaliperParser._extract_preamble(
        resolver, nodes, had_anchors=False,
    )

    assert not resolver._pseudo_root
    assert nodes[0].start_block_id == 2
    assert "Document preface line." in preamble
    assert "More preface context." in preamble


# ── B07：短标题子串匹配绕过精确匹配保护（P0）──
#
# 复现：修复前 ``_similarity("AI", "Details of training") == 0.9263``——大小写
# 折叠后 ``ai`` 是 ``training`` 的子串,而"子串高分"分支跑在短文本保护之前。
# 修复：短文本保护先行,很短的编号 / 缩写 / 中文词必须精确匹配;较长标题的
# 包含匹配按覆盖比例加权,不再让一个短词落进大标题就拿近满分。


def test_b07_short_acronym_substring_does_not_score_high():
    """AI/Details、API/Capital:短缩写恰为正文词子串,必须判为不同标题。"""
    assert _similarity("AI", "Details of training") == 0.0
    assert _similarity("API", "Capital expenditure") == 0.0
    # 真正同名的短缩写仍精确命中满分。
    assert _similarity("AI", "AI") == 1.0


def test_b07_single_char_numbering_requires_exact_match():
    """单字符编号 ``1`` 不得因是 ``1 Introduction ...`` 的子串而高分错锚。"""
    assert _similarity("1", "1 Introduction and Overview") == 0.0
    assert _similarity("1", "1") == 1.0


def test_b07_short_cjk_word_requires_exact_match():
    """双字中文 ``结论`` 是 ``本文的结论与展望`` 的子串,但应判为不同标题。"""
    assert _similarity("结论", "本文的结论与展望") == 0.0
    assert _similarity("结论", "结论") == 1.0


def test_b07_短标题只差首尾标点仍应命中():
    """短文本保护不能顺手把「只差一个标点」的短标题也挡掉。

    中文文档里两字标题极常见(摘要/引言/结论/前言/致谢),标题原文常带尾随
    句号或冒号,而模型回抄 snippet 时通常不带。如果短文本一律判 0,B07 的
    修复就会换来一批新的漏锚 —— 这正是方案 B07 写的「完整匹配**或明确词
    边界**」的后半句。剥掉首尾标点后必须完全相等才放行,所以下面的
    ``AI``/``details of training`` 与 ``1``/``1 introduction`` 仍是 0。
    """
    assert _similarity("结论", "结论。") >= 0.9
    assert _similarity("引言", "引言：") >= 0.9
    assert _similarity("Intro", "Intro:") >= 0.9
    assert _similarity("1.2", "1.2.") >= 0.9
    # 剥标点后核心不相等的,一个都不许放行(保护未被削弱)。
    assert _similarity("AI", "Details of training") == 0.0
    assert _similarity("1", "1 Introduction and Overview") == 0.0
    assert _similarity("摘要", "摘要与关键词") == 0.0


def test_b07_long_title_truncated_by_model_keeps_recall():
    """保留召回:模型把长标题尾巴截掉,前缀包含仍应高分(非本次收紧的目标)。"""
    score = _similarity(
        "Introduction to Machine",
        "Introduction to Machine Learning Methods",
    )
    assert score >= 0.85


def test_b07_long_title_with_numbering_prefix_still_matches():
    """带编号前缀的真长标题(``4. Experimental...`` 含 ``Experimental...``)仍召回。"""
    score = _similarity(
        "Experimental Results Analysis",
        "4. Experimental Results Analysis",
    )
    assert score >= 0.85


def test_b07_short_query_scores_below_rescue_and_dedup_thresholds():
    """回归护栏:短查询落进长标题的分数必须低于救援 / 去重门槛(0.60 / 0.80)。

    修复前 0.9263 会越过 dedup_sim_threshold(0.8),把不同标题聚成一簇并
    错误消歧;现在归零,既进不了聚簇也触发不了远距离救援。
    """
    for query, title in [
        ("AI", "Details of training"),
        ("API", "Capital expenditure"),
        ("1", "1 Introduction and Overview"),
        ("结论", "本文的结论与展望"),
    ]:
        assert _similarity(query, title) < 0.60


def test_b07_acronym_collision_does_not_anchor_to_wrong_block():
    """端到端:标题 ``AI`` 不得被对齐到只是文本里含 ``ai`` 子串的正文块。

    真标题在 block 5;block 2 的正文 ``Details of training`` 含子串 ``ai``。
    修复前 ``ai`` in ``training`` 得 0.9263,可能把锚点错钉到 block 2;修复后
    该块相似度为 0,对齐落在真标题块 5。
    """
    blocks = [_body(index) for index in range(9)]
    blocks[2] = _body(2, "Details of training procedures and setup", 12.0)
    blocks[5] = Block(id=5, type="text", text="AI", font_size=18.0, is_bold=True)
    candidates = generate_heading_candidates(blocks)

    raw = [ChapterNode(
        block_id=5, title="AI", snippet="AI", level=1, confidence=0.9,
    )]
    aligned = MonotonicAnchorAligner(blocks).align_windows([raw], candidates=candidates)

    assert [chapter.start_block_id for chapter in aligned] == [5]


def test_b07_same_named_chapters_disambiguated_by_position():
    """同名章节(两个 ``Abstract``)按物理位置消歧,不塌成一个锚点。

    ``_similarity`` 对完全相同文本仍返回 1.0(它们确实同名),消歧交给
    对齐 / 去重按 id 跨度(dedup_id_diff)与窗口跨度处理:两个相距很远的
    同名标题应保留为两个锚点,而非被聚成一簇丢掉一个。
    """
    blocks = [_body(index) for index in range(40)]
    blocks[3] = Block(id=3, type="text", text="Abstract", font_size=16.0, is_bold=True)
    blocks[30] = Block(id=30, type="text", text="Abstract", font_size=16.0, is_bold=True)
    candidates = generate_heading_candidates(blocks)

    raw = [
        ChapterNode(block_id=3, title="Abstract", snippet="Abstract", level=1, confidence=0.9),
        ChapterNode(block_id=30, title="Abstract", snippet="Abstract", level=1, confidence=0.9),
    ]
    aligned = MonotonicAnchorAligner(blocks).align_windows([raw], candidates=candidates)

    starts = sorted(chapter.start_block_id for chapter in aligned)
    assert starts == [3, 30]


# ══════════════════════════════════════════════════════════════════
# A06 · 软约束树关系：区分「层级合法」与「层级正确」
# ══════════════════════════════════════════════════════════════════


def _chapter(block_id: int, title: str, level: int) -> ChapterNode:
    return ChapterNode(block_id=block_id, title=title, level=level, snippet=title[:40])


def test_soft_constraint_flags_same_series_depth_inconsistency():
    """同一编号系列成员被判到不同层级 -> numbering_series_depth 违约。

    ``1.1`` 与 ``1.2`` 同为深度 2 的兄弟，却落在 L2/L3，编号语义要求同级。
    这是「层级合法但不正确」：路径本身没有非法跳级，软约束才抓得到。
    """
    chapters = [
        _chapter(0, "1 Overview", 1),
        _chapter(1, "1.1 Scope", 2),
        _chapter(2, "1.2 Risks", 3),  # 与 1.1 同系列却更深
    ]
    report = evaluate_soft_constraints(chapters)
    kinds = {v.kind for v in report.violations}
    assert "numbering_series_depth" in kinds
    assert report.total_penalty > 0


def test_soft_constraint_flags_numbering_parent_prefix_break():
    """编号父前缀存在但层级不构成父子 -> numbering_parent_prefix 违约。"""
    chapters = [
        _chapter(0, "2 Design", 1),
        _chapter(1, "2.1 Storage", 3),  # 父 2 是 L1，子应为 L2 而非 L3
    ]
    report = evaluate_soft_constraints(chapters)
    assert any(v.kind == "numbering_parent_prefix" for v in report.violations)


def test_soft_constraint_clean_hierarchy_has_no_violations():
    """编号自洽的干净层级不产生任何惩罚性违约。"""
    chapters = [
        _chapter(0, "1 Overview", 1),
        _chapter(1, "1.1 Scope", 2),
        _chapter(2, "1.2 Risks", 2),
        _chapter(3, "2 Design", 1),
        _chapter(4, "2.1 Storage", 2),
    ]
    report = evaluate_soft_constraints(chapters)
    assert report.total_penalty == 0.0


def test_soft_constraint_appendix_reset_is_informational_not_penalised():
    """附录标志登记为信息项（penalty=0），不因编号重置而误报违约。"""
    chapters = [
        _chapter(0, "1 Overview", 1),
        _chapter(1, "1.1 Scope", 2),
        _chapter(2, "Appendix A Data", 1),
        _chapter(3, "1 Tables", 2),  # 附录内重置编号
    ]
    report = evaluate_soft_constraints(chapters)
    appendix = report.by_kind("appendix_reset")
    assert appendix and all(v.penalty == 0.0 for v in appendix)


def test_missing_intermediate_is_a_virtual_container_not_a_fabricated_heading():
    """缺失 2.1 的 2.1.1：声明虚拟容器，绝不凭空生成原文不存在的标题。

    A06 硬约束原文：缺失 2.1 的 2.1.1 不能凭空生成原文不存在的标题；可保留
    真实深度并用虚拟容器，仅在结构表示中声明，且与真实来源标题可区分。
    """
    chapters = [
        _chapter(0, "2 Design", 1),
        _chapter(1, "2.1.1 Detail", 3),  # 缺 2.1
    ]
    containers = virtual_containers_for(chapters)
    numbers = {c.number for c in containers}
    assert (2, 1) in numbers  # 补了缺失的中间层
    for container in containers:
        assert isinstance(container, VirtualContainer)
        assert container.is_virtual is True
        # 标题带显式虚拟标记，与真实来源标题一望可辨。
        assert container.title.startswith("[virtual ")
    # 真实存在的编号绝不会被当作缺失补进来。
    assert (2,) not in numbers


def test_virtual_container_not_declared_when_intermediate_present():
    """中间层真实存在时不声明虚拟容器（不重复真实标题）。"""
    chapters = [
        _chapter(0, "2 Design", 1),
        _chapter(1, "2.1 Storage", 2),
        _chapter(2, "2.1.1 Detail", 3),
    ]
    assert virtual_containers_for(chapters) == []


def test_soft_constraint_penalises_suspicious_heading_run():
    """连续多个标题之间没有正文 -> heading_run 告警（疑似过度切分）。"""
    blocks = [
        Block(id=0, type="text", text="Alpha", font_size=16.0, is_bold=True),
        Block(id=1, type="text", text="Beta", font_size=16.0, is_bold=True),
        Block(id=2, type="text", text="Gamma", font_size=16.0, is_bold=True),
        Block(id=3, type="text", text="Delta", font_size=16.0, is_bold=True),
    ]
    chapters = [
        _chapter(0, "Alpha", 1),
        _chapter(1, "Beta", 1),
        _chapter(2, "Gamma", 1),
        _chapter(3, "Delta", 1),
    ]
    report = evaluate_soft_constraints(chapters, blocks=blocks)
    assert any(v.kind == "heading_run" for v in report.violations)


def test_soft_constraints_never_mutate_the_input_chapters():
    """软约束是纯度量：只报告，绝不改层级、不增删节点。"""
    chapters = [
        _chapter(0, "1 Overview", 1),
        _chapter(1, "1.1 Scope", 3),
    ]
    before = [(c.start_block_id, c.title, c.level) for c in chapters]
    evaluate_soft_constraints(chapters)
    after = [(c.start_block_id, c.title, c.level) for c in chapters]
    assert before == after
