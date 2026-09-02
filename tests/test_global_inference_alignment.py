from __future__ import annotations

import asyncio

import pytest

from infrastructure.models import Block
from modules.parser.anchor_alignment import MonotonicAnchorAligner
from modules.parser.heading_candidates import generate_heading_candidates
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
