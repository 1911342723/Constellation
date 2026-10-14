"""L01 之二：两遍处理——正文落盘、决策吃有界特征、按最终区间回读。

验收口径按方案原文：
  * 页数/块数翻倍不导致正文副本数跟着增加（这里用"决策期常驻字符数"量，它是可数事实，
    比"峰值内存"这种随机器波动的量更能进 CI）；
  * 小文档不为了流式付出磁盘 I/O；
  * 装配出来的正文必须逐字完整——**截断却声称完整**是本仓反复出现的缺陷类型，
    所以这里额外钉一条：拿有界预览去装配正文时必须显式失败，不许静默输出短正文。
"""
from __future__ import annotations

import pytest

from app.core.exceptions import AssemblerError
from infrastructure.blocks import BlockStore, FeatureView, feature_of
from infrastructure.models import Block
from modules.parser.parser import (
    TWO_PASS_MIN_BLOCKS,
    TWO_PASS_MIN_CHARS,
    CaliperParser,
)
from modules.parser.resolver import IntervalResolver

BODY = "正文内容" * 120


def _doc(n_blocks: int, *, heading_every: int = 40) -> list[Block]:
    blocks: list[Block] = []
    for i in range(n_blocks):
        if i % heading_every == 0:
            blocks.append(Block(
                id=i, type="text", text=f"**{1 + i // heading_every} 章节 {i}**",
                font_size=16.0, is_bold=True, is_heading_style=True, heading_level=1,
            ))
        else:
            blocks.append(Block(id=i, type="text", text=f"段{i}：{BODY}", font_size=11.0))
    return blocks


# ── 落盘仓库本身 ──────────────────────────────────────────────────────────


def test_store_roundtrip_is_byte_exact():
    blocks = _doc(30)
    with BlockStore() as store:
        store.write(blocks)
        for original in blocks:
            loaded = store.load(original.id)
            assert loaded is not None
            assert loaded.text == original.text
            assert loaded.type == original.type
            assert loaded.font_size == original.font_size


def test_store_load_many_preserves_requested_order():
    blocks = _doc(20)
    with BlockStore() as store:
        store.write(blocks)
        wanted = [11, 3, 19, 0]
        assert [b.id for b in store.load_many(wanted)] == wanted


def test_store_random_read_does_not_need_full_scan():
    """读一个块只 seek 一行，不解析整份产物——这是随机读而非单向流的意义。"""
    blocks = _doc(200)
    with BlockStore() as store:
        store.write(blocks)
        one = store.load(blocks[137].id)
        assert one is not None and one.text == blocks[137].text
        # 顺序读回全部仍然可用（导出/装配用），但不是决策阶段的路径。
        assert sum(1 for _ in store.iter_blocks()) == len(blocks)


def test_feature_keeps_real_length_while_bounding_resident_text():
    """截断了必须如实记真实长度——截断却声称完整是本仓的经典缺陷。"""
    long_block = Block(id=1, type="text", text="字" * 5000, font_size=11.0)
    feature = feature_of(long_block)
    assert feature.truncated is True
    assert feature.char_count == 5000
    assert len(feature.text_head) + len(feature.text_tail) < 5000
    assert "…" in feature.text_preview  # 预览显式带省略标记，不假装是全文


def test_feature_view_resident_chars_far_below_total():
    blocks = _doc(300)
    with BlockStore() as store:
        store.write(blocks)
        view = FeatureView.build(blocks, store)
        assert view.total_chars() > 100_000
        assert view.resident_chars() < view.total_chars() * 0.5


# ── 规模门槛：小文档不落盘 ────────────────────────────────────────────────


def test_small_document_is_not_spilled():
    parser = CaliperParser()
    blocks = _doc(20)
    lean, store = parser._spill_for_two_pass(blocks)
    assert store is None, "小文档不应为了流式多付一趟磁盘 I/O"
    assert lean is blocks, "未落盘时应原样返回，行为与从前逐字一致"


def test_large_document_is_spilled_and_previews_are_flagged():
    parser = CaliperParser()
    blocks = _doc(TWO_PASS_MIN_BLOCKS + 100)
    lean, store = parser._spill_for_two_pass(blocks)
    try:
        assert store is not None
        assert store.bytes_on_disk() > 0
        previews = [b for b in lean if (b.metadata or {}).get("text_is_preview")]
        assert previews, "长块应被替换成有界预览"
        for block in previews:
            assert (block.metadata or {})["char_count"] > len(block.text or "")
    finally:
        if store is not None:
            store.dispose()


def test_resident_text_does_not_grow_with_document_size():
    """块数翻倍，决策期常驻字符数**不**跟着翻倍——正文副本没有留在内存里。"""
    parser = CaliperParser()
    small = _doc(TWO_PASS_MIN_BLOCKS + 100)
    large = _doc((TWO_PASS_MIN_BLOCKS + 100) * 2)

    lean_small, store_small = parser._spill_for_two_pass(small)
    lean_large, store_large = parser._spill_for_two_pass(large)
    try:
        per_block_small = sum(len(b.text or "") for b in lean_small) / len(lean_small)
        per_block_large = sum(len(b.text or "") for b in lean_large) / len(lean_large)
        # 每块常驻字符数是常量上界（head+tail），与文档规模无关。
        assert per_block_large <= per_block_small * 1.05
        original_ratio = (
            sum(len(b.text or "") for b in lean_large)
            / sum(len(b.text or "") for b in large)
        )
        assert original_ratio < 0.5, f"常驻/原文比 {original_ratio:.3f} 未显著下降"
    finally:
        for store in (store_small, store_large):
            if store is not None:
                store.dispose()


# ── 惰性装配：正文逐字完整，且静默截断不可能 ──────────────────────────────


def test_two_pass_parse_emits_full_text_not_previews():
    blocks = _doc(TWO_PASS_MIN_BLOCKS + 200)
    tree = CaliperParser().parse_fast(blocks)
    markdown = tree.to_markdown()
    assert BODY in markdown, "装配出来的正文必须是全文，不是预览"
    assert "…" not in markdown, "预览的省略标记不得泄漏进产物"
    original_chars = sum(len(b.text or "") for b in blocks)
    # 标题记号会让产物略长于原文；关键是不得**短于**原文。
    assert len(markdown) >= original_chars * 0.99


def test_preview_blocks_without_store_are_refused_not_truncated():
    """没有仓库却拿预览块装配正文时必须显式失败。

    这条是本条最重要的一条：宁可失败得响，也不交付一份被截断却声称完整的正文。
    """
    preview = Block(
        id=0, type="text", text="开头…结尾",
        metadata={"text_is_preview": True, "char_count": 9999},
    )
    resolver = IntervalResolver([preview])
    with pytest.raises(AssemblerError, match="bounded preview"):
        resolver._extract_content(0, 0)


def test_store_backed_extraction_returns_full_text():
    full = Block(id=0, type="text", text="完整正文" * 200)
    preview = full.model_copy(update={
        "text": "完整正文…完整正文",
        "metadata": {"text_is_preview": True, "char_count": len(full.text)},
    })
    with BlockStore() as store:
        store.write([full])
        resolver = IntervalResolver([preview], block_store=store)
        content = resolver._extract_content(0, 0)
    assert content == full.text


def test_preamble_also_reads_full_text_from_store():
    """前言同样是要交付的正文，不能把预览当前言输出。"""
    front = Block(id=0, type="text", text="前言" * 300)
    heading = Block(
        id=1, type="text", text="**第一章**", font_size=16.0,
        is_bold=True, is_heading_style=True, heading_level=1,
    )
    preview_front = front.model_copy(update={
        "text": "前言…前言",
        "metadata": {"text_is_preview": True, "char_count": len(front.text)},
    })
    with BlockStore() as store:
        store.write([front, heading])
        resolver = IntervalResolver([preview_front, heading], block_store=store)
        preamble = resolver.get_preamble_blocks(1)
    assert [b.text for b in preamble] == [front.text]


def test_thresholds_are_explainable():
    """阈值必须是可解释的配置量，而不是散落的魔法数。"""
    assert TWO_PASS_MIN_BLOCKS > 0 and TWO_PASS_MIN_CHARS > 0
    assert TWO_PASS_MIN_CHARS >= 100_000, "字符阈值过低会让普通文档白付 I/O"
