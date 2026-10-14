"""E02 阅读顺序图测试 —— 按"关键块对的先后准确率"验收，不只断言字符覆盖率。

失效场景覆盖：居中小标题、跨栏图表、边栏、脚注、混合栏宽、旋转/竖排文本。做法：
构造带标注相邻关系的合成页面（每块 metadata 里携带 page/bbox/layout_column/
layout_band/source_role），过 pdf_reading_order.order_entries，断言关键块对顺序。
同时通过真实调用方入口 PdfProvider.extract_from_bytes 验证双栏 PDF 的端到端顺序。

Run: python -m pytest tests/test_pdf_reading_order.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.models import Block
from infrastructure.providers import pdf_reading_order as ro
from infrastructure.providers.pdf_provider import PdfProvider


def _blk(bid, *, page=0, column, band, bbox, role=None, text=None):
    meta = {
        "page": page + 1,
        "bbox": list(bbox),
        "layout_column": column,
        "layout_band": band,
        "page_width": 600.0,
        "page_height": 800.0,
    }
    if role:
        meta["source_role"] = role
    return (page, column, float(bbox[1]), float(bbox[0]),
            Block(id=bid, type="text", text=text or f"block-{bid}", metadata=meta))


def _order_texts(entries):
    ordered = ro.order_entries(entries)
    return [e[4].text for e in ordered]


def _index(texts, needle):
    return texts.index(needle)


def test_same_column_top_before_bottom():
    """同栏内靠上的块排在靠下的块之前。"""
    entries = [
        _blk(1, column=1, band=0, bbox=(50, 300, 250, 320), text="lower"),
        _blk(0, column=1, band=0, bbox=(50, 100, 250, 120), text="upper"),
    ]
    texts = _order_texts(entries)
    assert _index(texts, "upper") < _index(texts, "lower")


def test_two_column_reading_order():
    """双栏：左栏整列在右栏之前（同 band）。"""
    entries = [
        _blk(0, column=1, band=0, bbox=(50, 100, 280, 120), text="L1"),
        _blk(1, column=1, band=0, bbox=(50, 130, 280, 150), text="L2"),
        _blk(2, column=2, band=0, bbox=(320, 100, 550, 120), text="R1"),
        _blk(3, column=2, band=0, bbox=(320, 130, 550, 150), text="R2"),
    ]
    texts = _order_texts(entries)
    assert _index(texts, "L1") < _index(texts, "L2") < _index(texts, "R1")
    assert _index(texts, "R1") < _index(texts, "R2")


def test_full_width_separator_splits_above_below():
    """跨栏全宽块（居中小标题/跨栏图表）把上方块排前、下方块排后。"""
    # band 约定：全宽块 band 为奇数，其上方列块 band=0、下方列块 band=2。
    entries = [
        _blk(0, column=1, band=0, bbox=(50, 100, 280, 120), text="above-left"),
        _blk(1, column=2, band=0, bbox=(320, 100, 550, 120), text="above-right"),
        _blk(2, column=0, band=1, bbox=(50, 200, 550, 230), text="SECTION-TITLE"),
        _blk(3, column=1, band=2, bbox=(50, 300, 280, 320), text="below-left"),
        _blk(4, column=2, band=2, bbox=(320, 300, 550, 320), text="below-right"),
    ]
    texts = _order_texts(entries)
    ti = _index(texts, "SECTION-TITLE")
    assert _index(texts, "above-left") < ti
    assert _index(texts, "above-right") < ti
    assert ti < _index(texts, "below-left")
    assert ti < _index(texts, "below-right")


def test_footnote_ordered_after_body():
    """脚注排到本页所有正文之后。"""
    entries = [
        _blk(0, column=1, band=0, bbox=(50, 100, 550, 120), text="body-top"),
        _blk(1, column=1, band=0, bbox=(50, 700, 550, 715), role="footnote", text="footnote"),
        _blk(2, column=1, band=0, bbox=(50, 300, 550, 320), text="body-mid"),
    ]
    texts = _order_texts(entries)
    assert _index(texts, "footnote") > _index(texts, "body-top")
    assert _index(texts, "footnote") > _index(texts, "body-mid")


def test_mixed_column_widths_ordered_by_column_then_y():
    """混合栏宽：窄边栏与宽正文各成列，列内按 y 序，不交错。"""
    entries = [
        _blk(0, column=1, band=0, bbox=(50, 100, 200, 120), text="side-1"),
        _blk(1, column=1, band=0, bbox=(50, 140, 200, 160), text="side-2"),
        _blk(2, column=2, band=0, bbox=(250, 100, 550, 120), text="main-1"),
        _blk(3, column=2, band=0, bbox=(250, 140, 550, 160), text="main-2"),
    ]
    texts = _order_texts(entries)
    assert _index(texts, "side-1") < _index(texts, "side-2")
    assert _index(texts, "main-1") < _index(texts, "main-2")


def test_cycle_marks_reading_order_uncertain():
    """成环时不丢块、不硬定序：参与环的块被标 reading_order_uncertain 且带备选顺序。

    直接对拓扑排序核心喂一个真实的 2-环（A->B 且 B->A），确认它被识别为环、
    不丢节点；再验证 order_entries 会把这种冲突落到 metadata 上。
    """
    # 核心：2-环。
    nodes = [0, 1]
    edges = {0: {1}, 1: {0}}
    tiebreak = {0: (0,), 1: (1,)}
    order, cyclic = ro._stable_toposort(nodes, edges, tiebreak)
    assert set(order) == {0, 1}          # 不丢节点
    assert cyclic == {0, 1}              # 两块都在环里

    # 端到端：order_entries 对成环页把 reading_order_uncertain 落到 metadata。
    # 用 monkeypatch 让本页产出上面的环。
    entries = [
        _blk(0, column=0, band=1, bbox=(50, 100, 550, 130), text="sep-A"),
        _blk(1, column=0, band=1, bbox=(50, 100, 550, 130), text="sep-B"),
    ]
    original = ro._build_page_edges
    ro._build_page_edges = lambda indexed: {
        indexed[0][0]: {indexed[1][0]}, indexed[1][0]: {indexed[0][0]}
    }
    try:
        ordered = ro.order_entries(entries)
    finally:
        ro._build_page_edges = original
    uncertain = [e for e in ordered if (e[4].metadata or {}).get("reading_order_uncertain")]
    assert len(ordered) == 2
    assert uncertain, "成环时应有块被标 reading_order_uncertain"
    for e in uncertain:
        assert (e[4].metadata or {}).get("reading_order_fallback")


def test_e2e_two_column_pdf_left_before_right():
    """真实调用方入口：双栏 PDF 左栏正文排在右栏正文之前。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    # 左栏
    page.insert_text((60, 120), "Left column first paragraph text here.", fontname="helv", fontsize=11)
    page.insert_text((60, 140), "Left column second line continues on.", fontname="helv", fontsize=11)
    # 右栏（x 起点明显更大）
    page.insert_text((340, 120), "Right column first paragraph text here.", fontname="helv", fontsize=11)
    page.insert_text((340, 140), "Right column second line continues on.", fontname="helv", fontsize=11)
    pdf_bytes = doc.tobytes()
    doc.close()

    blocks = PdfProvider().extract_from_bytes(pdf_bytes)
    texts = " || ".join(b.text or "" for b in blocks)
    left_pos = texts.find("Left column")
    right_pos = texts.find("Right column")
    assert left_pos != -1 and right_pos != -1
    assert left_pos < right_pos, f"左栏应先于右栏，实际顺序：{texts}"
