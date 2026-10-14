"""版面分区（pdf_layout）测试 —— 从真实 PDF 输入开始验收，不给排序器预标栏号。

覆盖用户实测到的三处缺口与不确定性漏报：

- 不等宽双栏（宽左栏 + 窄右栏）：旧实现按"宽度 ≥ 页宽 62% 即跨栏"把左栏每行当成
  分隔，读成 R1 → L1 → R2 → L2；
- 窄栏距：分栏前的行内合并把两栏同一行缝成一行；
- 同页栏数变化：上单栏下双栏；
- 不确定性：压在栏间空白上的块、旋转文字、图区标签簇造成的局部栏数变化，都要
  落到 ``layout_uncertain_reasons`` 并折成 ``reading_order_uncertain``。

几何层（analyze_page_layout）单测 + PdfProvider.extract_from_bytes 端到端两层。

Run: python -m pytest tests/test_pdf_layout.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import fitz

from infrastructure.providers import pdf_layout as pl
from infrastructure.providers.pdf_provider import PdfProvider


# ── 几何层 ─────────────────────────────────────────────────────────────

def _lines(specs):
    return [pl.LineBox(*spec[:4], **(spec[4] if len(spec) > 4 else {})) for spec in specs]


def _column_rows(x0, x1, ys, height=10.0):
    return [(x0, y, x1, y + height) for y in ys]


def test_unequal_columns_detect_gutter_not_width_ratio():
    """宽左栏（占页宽 65%）+ 窄右栏：左栏行不是跨栏块，栏间空白被正确找到。"""
    ys = [100, 112, 124, 136]
    boxes = _lines(_column_rows(40, 430, ys) + _column_rows(460, 560, ys))
    layout = pl.analyze_page_layout(boxes, 600.0, 800.0)
    zone = layout.zones[0]
    assert len(zone.columns) == 2
    assert len(zone.gutters) == 1
    gutter = zone.gutters[0]
    assert 429 <= gutter.x0 <= 431 and 459 <= gutter.x1 <= 461
    placement = layout.classify((40, 100, 430, 110))
    assert placement.column_index == 0 and not placement.spanning
    assert layout.classify((460, 100, 560, 110)).column_index == 1


def test_single_column_centered_title_is_not_a_second_column():
    """单栏页：居中标题 + 段末短行凑不出纵向共存，不得被当成第二栏。"""
    body = _column_rows(50, 550, [120, 132, 144, 156, 168, 180])
    short_last = [(50, 192, 170, 202)]
    title = [(180, 60, 420, 76)]
    layout = pl.analyze_page_layout(_lines(body + short_last + title), 600.0, 800.0)
    assert all(not zone.gutters for zone in layout.zones)
    assert len(layout.zones[0].columns) == 1


def test_abstract_above_two_columns_is_spanning_and_columns_stay_intact():
    """上单栏（摘要）下双栏：摘要行跨栏成分隔群，下方仍是两栏。"""
    abstract = _column_rows(50, 550, [100, 112, 124])
    left = _column_rows(50, 290, [300, 312, 324, 336])
    right = _column_rows(320, 550, [300, 312, 324, 336])
    layout = pl.analyze_page_layout(_lines(abstract + left + right), 600.0, 800.0)
    assert layout.separators, "摘要应形成一个跨栏分隔群"
    sep_y0, sep_y1 = layout.separators[0]
    assert sep_y0 <= 100 and sep_y1 >= 134
    assert layout.classify((50, 100, 550, 110)).spanning
    below = layout.classify((50, 300, 290, 310))
    assert below.column_index == 0 and below.column_count == 2
    assert layout.classify((320, 300, 550, 310)).column_index == 1


def test_three_columns_and_spanning_title_over_two_gutters():
    cols = (
        _column_rows(20, 150, [60, 72, 84])
        + _column_rows(220, 350, [60, 72, 84])
        + _column_rows(420, 550, [60, 72, 84])
    )
    layout = pl.analyze_page_layout(_lines(cols), 600.0, 800.0)
    assert len(layout.zones[0].columns) == 3
    assert layout.classify((100, 20, 500, 35)).spanning
    assert layout.classify((220, 60, 350, 70)).column_index == 1


def test_label_clusters_inside_body_column_do_not_create_fake_gutter():
    """图区里两簇短标签在正文栏内部"共存"，但正文一行行穿过它们之间的空白：
    这条空白不是 gutter。"""
    left_body = _column_rows(72, 290, [300 + 12 * i for i in range(20)])
    right_body = _column_rows(307, 526, [300 + 12 * i for i in range(20)])
    labels_a = [(350, 100 + 24 * i, 400, 110 + 24 * i) for i in range(5)]
    labels_b = [(414, 100 + 24 * i, 470, 110 + 24 * i) for i in range(5)]
    layout = pl.analyze_page_layout(
        _lines(left_body + right_body + labels_a + labels_b), 600.0, 800.0,
    )
    global_gutters = [
        (round(g.x0), round(g.x1)) for g in layout.zones[-1].gutters
    ]
    assert global_gutters == [(290, 307)]


def test_rotated_lines_are_excluded_and_flagged():
    body = _column_rows(50, 550, [100, 112, 124, 136])
    rotated = [(20, 100, 32, 400, {"horizontal": False})]
    layout = pl.analyze_page_layout(_lines(body + rotated), 600.0, 800.0)
    assert len(layout.zones[0].columns) == 1
    placement = layout.classify((20, 100, 32, 400), horizontal=False)
    assert pl.REASON_ROTATED in placement.reasons


def test_block_overlapping_gutter_is_flagged_not_promoted():
    """探进 gutter 一半宽度但中心仍在栏内的块：留在栏里，但标不确定。"""
    left = _column_rows(50, 290, [100, 112, 124, 136])
    right = _column_rows(320, 550, [100, 112, 124, 136])
    layout = pl.analyze_page_layout(_lines(left + right), 600.0, 800.0)
    placement = layout.classify((300, 150, 550, 160))
    assert placement.column_index == 1
    assert pl.REASON_OVERLAPS_GUTTER in placement.reasons


def test_block_centered_on_gutter_is_spanning():
    left = _column_rows(50, 290, [100, 112, 124, 136])
    right = _column_rows(320, 550, [100, 112, 124, 136])
    layout = pl.analyze_page_layout(_lines(left + right), 600.0, 800.0)
    assert layout.classify((250, 200, 360, 214)).spanning


def test_gap_covers_gutter_guard():
    left = _column_rows(40, 237, [90, 102, 114, 126])
    right = _column_rows(247, 347, [90, 102, 114, 126])
    layout = pl.analyze_page_layout(_lines(left + right), 600.0, 800.0)
    assert layout.gap_covers_gutter(237, 247, 90, 100)
    assert not layout.gap_covers_gutter(60, 72, 90, 100)


def test_grid_like_zone_detected_for_borderless_table_cells():
    """正文页中间一片 ≥ 4 列短单元格（无框线表，find_tables 漏检）判成网格区。

    "短行"是相对**页面正文栏**算的，所以表必须嵌在有正文的页里——整页只有一张表
    时正文栏就是表自己的列，这条判据会自咬尾巴（见 `_detect_grid_regions` 注释）。
    """
    body = _column_rows(50, 550, [60 + 12 * i for i in range(6)])
    cells = []
    for y in (200 + 14 * i for i in range(8)):
        cells += [(50, y, 110, y + 10), (200, y, 235, y + 10),
                  (350, y, 425, y + 10), (500, y, 530, y + 10)]
    tail = _column_rows(50, 550, [340 + 12 * i for i in range(6)])
    layout = pl.analyze_page_layout(_lines(body + cells + tail), 600.0, 800.0)

    grid_zones = [z for z in layout.zones if z.grid]
    assert len(grid_zones) == 1
    assert grid_zones[0].y0 <= 200 and grid_zones[0].y1 >= 308
    placement = layout.classify((200, 200, 235, 210))
    assert placement.grid and placement.spanning
    assert pl.REASON_GRID_LIKE in placement.reasons
    # 网格区外的正文不受影响
    assert not layout.classify((50, 60, 550, 70)).grid


def test_body_columns_are_not_grid_like():
    """双栏正文（行填满各自的栏）不是网格区——短行判据不能把正文误判成表。"""
    left = _column_rows(50, 290, [60 + 12 * i for i in range(12)])
    right = _column_rows(320, 550, [60 + 12 * i for i in range(12)])
    layout = pl.analyze_page_layout(_lines(left + right), 600.0, 800.0)
    assert all(not zone.grid for zone in layout.zones)
    assert layout.classify((320, 60, 550, 70)).column_index == 1


def test_figure_label_cluster_inside_drawings():
    """矢量图框里的短标签簇是图区域；同高度的邻栏正文不是。"""
    body = _column_rows(320, 550, [100 + 12 * i for i in range(8)])
    labels = [
        (90, 100, 140, 110), (180, 100, 240, 110),
        (90, 160, 105, 170), (140, 160, 155, 170),
        (190, 160, 205, 170), (130, 220, 190, 230),
    ]
    drawings = [(80, 80, 280, 250)]
    layout = pl.analyze_page_layout(
        _lines(body + labels), 600.0, 800.0, drawings=drawings,
    )
    assert layout.figures
    label = layout.classify((90, 100, 140, 110))
    assert label.figure and label.column_index == 0
    assert not layout.classify((320, 100, 550, 110)).figure


def test_short_labels_without_drawings_are_not_figures():
    """没有墨迹的短标签只是短标签——作者单位 / 关键词不能被认成图。"""
    body = _column_rows(50, 550, [300 + 12 * i for i in range(6)])
    labels = [
        (90, 100, 140, 110), (180, 100, 240, 110),
        (90, 160, 105, 170), (140, 160, 155, 170),
        (190, 160, 205, 170), (130, 220, 190, 230),
    ]
    layout = pl.analyze_page_layout(_lines(body + labels), 600.0, 800.0)
    assert not layout.figures
    assert not layout.classify((90, 100, 140, 110)).figure


def test_boxed_body_and_hairline_are_not_figures():
    """套了装饰框的正文、标题下划线，都不是图。"""
    body = _column_rows(50, 550, [100 + 12 * i for i in range(10)])
    boxed = pl.analyze_page_layout(
        _lines(body), 600.0, 800.0, drawings=[(40, 90, 560, 230)],
    )
    assert not boxed.figures
    heading = _lines([(50, 80, 220, 92)] + _column_rows(50, 550, [120 + 12 * i for i in range(6)]))
    underlined = pl.analyze_page_layout(
        heading, 600.0, 800.0, drawings=[(50, 94, 220, 95.5)],
    )
    assert not underlined.figures


def test_group_separators_splits_when_column_lines_interleave():
    spanning = _lines([(50, 100, 550, 110), (50, 112, 550, 122), (50, 300, 550, 310)])
    column = _lines([(50, 200, 290, 210)])
    groups = pl.group_separators(spanning, column, 10.0)
    assert len(groups) == 2
    assert groups[0] == (100, 122) and groups[1] == (300, 310)


# ── 端到端：真实 PDF 输入 → PdfProvider ────────────────────────────────

def _pdf(draw) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    draw(page)
    data = doc.tobytes()
    doc.close()
    return data


def _texts(blocks):
    return [b.text or "" for b in blocks if b.type == "text"]


def _pos(texts, prefix):
    for index, text in enumerate(texts):
        if prefix in text:
            return index
    raise AssertionError(f"{prefix!r} 不在输出里：{texts}")


def test_e2e_wide_left_narrow_right_reads_left_column_first():
    """用户复现的场景：宽左栏（63% 页宽）+ 窄右栏，期望 L1 → L2 → R1 → R2，
    旧实现输出 R1 → L1 → R2 → L2 且不标不确定。"""
    def draw(page):
        for i, y in enumerate((100, 116, 132, 148)):
            page.insert_text(
                (40, y),
                f"L{i+1} left very wide column body text continues here and fills the entire line width of the page",
                fontname="helv", fontsize=10,
            )
            page.insert_text((460, y), f"R{i+1} right narrow", fontname="helv", fontsize=10)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    joined = " || ".join(_texts(blocks))
    # 所有左栏行先于所有右栏行
    assert joined.rfind("L4") < joined.find("R1"), joined
    assert joined.find("L1") < joined.find("L2") and joined.find("R1") < joined.find("R2")
    regions = {(b.metadata or {}).get("layout_region") for b in blocks}
    assert "full-width" not in regions, "左栏行不应被当成跨栏块"
    assert not any((b.metadata or {}).get("reading_order_uncertain") for b in blocks)


def test_e2e_narrow_gutter_does_not_merge_columns_inline():
    """10pt 栏距：PyMuPDF 把左右两栏同一行放进同一原始块，旧实现的行内合并会把
    它们缝成一行；现在分栏在合并之前，左右各自成段。"""
    def draw(page):
        left_text = "L{} left column body text line that runs to the edge"
        width = fitz.get_text_length(left_text.format(1), fontname="helv", fontsize=9)
        right_x = 40 + width + 10
        for i, y in enumerate((100, 112, 124, 136, 148)):
            page.insert_text((40, y), left_text.format(i + 1), fontname="helv", fontsize=9)
            page.insert_text((right_x, y), f"R{i+1} right column text here", fontname="helv", fontsize=9)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    texts = _texts(blocks)
    for text in texts:
        assert not ("L1" in text and "R1" in text), f"左右栏被缝成一行：{text!r}"
    assert _pos(texts, "L5") <= _pos(texts, "R1")


def test_e2e_single_then_double_column_page():
    """上半页单栏摘要、下半页双栏、中间没有跨栏标题：摘要 → 左栏 → 右栏。"""
    def draw(page):
        for i, y in enumerate((100, 114, 128)):
            page.insert_text(
                (50, y),
                f"A{i+1} abstract single column line spanning the whole text width of the page here",
                fontname="helv", fontsize=9,
            )
        for i, y in enumerate((300, 314, 328)):
            page.insert_text((50, y), f"L{i+1} left column body text line here", fontname="helv", fontsize=9)
            page.insert_text((320, y), f"R{i+1} right column body text line here", fontname="helv", fontsize=9)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    texts = _texts(blocks)
    assert _pos(texts, "A1") < _pos(texts, "L1") < _pos(texts, "R1")
    # 摘要三行同属一个跨栏分隔群 → 允许合段
    abstract = [b for b in blocks if "A1" in (b.text or "")][0]
    assert (abstract.metadata or {}).get("layout_region") == "full-width"
    assert "A3" in abstract.text


def test_e2e_mid_page_spanning_heading_splits_bands():
    def draw(page):
        for i, y in enumerate((100, 114)):
            page.insert_text((50, y), f"L{i+1} left column body text line here", fontname="helv", fontsize=9)
            page.insert_text((320, y), f"R{i+1} right column body text line here", fontname="helv", fontsize=9)
        page.insert_text((200, 200), "3 SECTION TITLE ACROSS", fontname="helv", fontsize=14)
        for i, y in enumerate((260, 274)):
            page.insert_text((50, y), f"BL{i+1} below left column body text", fontname="helv", fontsize=9)
            page.insert_text((320, y), f"BR{i+1} below right column body text", fontname="helv", fontsize=9)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    texts = _texts(blocks)
    title = _pos(texts, "SECTION TITLE")
    assert _pos(texts, "L1") < _pos(texts, "R1") < title
    assert title < _pos(texts, "BL1") < _pos(texts, "BR1")


def test_e2e_three_columns():
    def draw(page):
        for i, y in enumerate((100, 114, 128)):
            page.insert_text((30, y), f"C1-{i+1} first col text", fontname="helv", fontsize=9)
            page.insert_text((220, y), f"C2-{i+1} second col text", fontname="helv", fontsize=9)
            page.insert_text((410, y), f"C3-{i+1} third col text", fontname="helv", fontsize=9)

    texts = _texts(PdfProvider().extract_from_bytes(_pdf(draw)))
    assert _pos(texts, "C1-3") < _pos(texts, "C2-1")
    assert _pos(texts, "C2-3") < _pos(texts, "C3-1")


def test_e2e_single_column_justified_paragraph_merges():
    """单栏两端对齐正文：整页口径下每行左右留白相等 → "居中" → 永远合不成段。
    对齐改为相对所在栏后，正文按段合并、编号标题独立。"""
    def draw(page):
        page.insert_text((50, 80), "1 Introduction", fontname="hebo", fontsize=14)
        for i in range(6):
            page.insert_text(
                (50, 110 + 12 * i),
                "Body paragraph line number %d fills the whole text column from left to right" % (i + 1),
                fontname="helv", fontsize=10,
            )
        page.insert_text((50, 182), "and ends here.", fontname="helv", fontsize=10)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    texts = _texts(blocks)
    assert texts[0].startswith("1 Introduction")
    body = [t for t in texts if "Body paragraph" in t]
    assert len(body) == 1, texts
    assert "line number 6" in body[0] and "ends here" in body[0]


def test_e2e_block_straddling_gutter_is_flagged_uncertain():
    """左对齐却探进栏间空白的块：几何上分不清归属，必须标 reading_order_uncertain，
    "没有环"不等于"顺序对"。"""
    def draw(page):
        left_text = "L{} left column body text line that fills its column"
        right_x = 50 + fitz.get_text_length(left_text.format(1), fontname="helv", fontsize=9) + 30
        for i, y in enumerate((100, 112, 124, 136, 148)):
            page.insert_text((50, y), left_text.format(i + 1), fontname="helv", fontsize=9)
            page.insert_text((right_x, y), f"R{i+1} right column body text line", fontname="helv", fontsize=9)
        # 起点探进 30pt 栏距 20pt，主体在右栏
        page.insert_text((right_x - 20, 200), "STRADDLE text reaching into the right column", fontname="helv", fontsize=9)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    straddle = [b for b in blocks if "STRADDLE" in (b.text or "")][0]
    meta = straddle.metadata or {}
    assert meta.get("reading_order_uncertain") is True
    assert "overlaps_gutter" in meta.get("reading_order_uncertain_reasons", [])
    clean = [b for b in blocks if "L1" in (b.text or "")][0]
    assert not (clean.metadata or {}).get("reading_order_uncertain")


def test_e2e_rotated_text_is_flagged_and_not_merged_with_body():
    def draw(page):
        for i, y in enumerate((100, 112, 124, 136)):
            page.insert_text((50, y), f"B{i+1} body line of ordinary horizontal text", fontname="helv", fontsize=9)
        page.insert_text((30, 300), "ROTATED sidebar text", fontname="helv", fontsize=9, rotate=90)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    rotated = [b for b in blocks if "ROTATED" in (b.text or "")][0]
    meta = rotated.metadata or {}
    assert meta.get("writing_direction") == "rotated"
    assert "rotated_text" in meta.get("reading_order_uncertain_reasons", [])
    assert "B1" not in rotated.text


def test_e2e_borderless_table_reads_row_major_and_is_flagged():
    """无框线四列表（PyMuPDF find_tables 抓不到）：单元格按行读，而不是先读完
    第一列再读第二列；区域标 ``grid_like``，但不占阅读顺序不确定通道。"""
    def draw(page):
        header = ["Model", "Size", "Data", "Score"]
        xs = [50, 200, 350, 500]
        for x, text in zip(xs, header):
            page.insert_text((x, 100), text, fontname="hebo", fontsize=9)
        for r in range(7):
            for c, x in enumerate(xs):
                page.insert_text((x, 116 + 14 * r), f"r{r}c{c}", fontname="helv", fontsize=9)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    texts = _texts(blocks)
    joined = " ".join(texts)
    assert joined.find("r0c0") < joined.find("r0c3") < joined.find("r1c0"), joined
    cells = [b for b in blocks if "r3c2" in (b.text or "")]
    assert cells and (cells[0].metadata or {}).get("layout_region") == "grid"
    assert (cells[0].metadata or {}).get("grid_like") is True
    # 区域类型存疑 ≠ 阅读顺序存疑：表格按行读本来就对，不该占用复审通道
    assert not (cells[0].metadata or {}).get("reading_order_uncertain")


def test_e2e_figure_labels_inside_drawings_are_figure_like():
    """矢量图内短标签标 ``figure_like``，邻栏正文不被偷走，也不占阅读顺序不确定通道。"""
    def draw(page):
        page.draw_rect(fitz.Rect(50, 80, 270, 250), color=(0, 0, 0), width=1.2)
        page.draw_rect(fitz.Rect(70, 100, 150, 160), color=(0, 0, 0), width=0.8)
        page.draw_rect(fitz.Rect(170, 100, 250, 160), color=(0, 0, 0), width=0.8)
        page.draw_rect(fitz.Rect(90, 190, 230, 235), color=(0, 0, 0), width=0.8)
        page.insert_text((80, 130), "Enc", fontname="helv", fontsize=8)
        page.insert_text((185, 130), "Dec", fontname="helv", fontsize=8)
        page.insert_text((80, 150), "Q", fontname="helv", fontsize=8)
        page.insert_text((120, 150), "K", fontname="helv", fontsize=8)
        page.insert_text((185, 150), "V", fontname="helv", fontsize=8)
        page.insert_text((140, 212), "Attn", fontname="helv", fontsize=8)
        for i, y in enumerate((100, 112, 124, 136, 148, 160, 172, 184)):
            page.insert_text(
                (320, y), f"R{i+1} right column body text line here",
                fontname="helv", fontsize=9,
            )
        for i, y in enumerate((280, 292, 304, 316)):
            page.insert_text(
                (50, y), f"L{i+1} left column body text line here",
                fontname="helv", fontsize=9,
            )

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    labels = [b for b in blocks if (b.text or "").strip() in {"Enc", "Dec", "Q", "K", "V", "Attn"}]
    assert len(labels) >= 4, [b.text for b in blocks]
    assert all((b.metadata or {}).get("figure_like") for b in labels)
    assert all((b.metadata or {}).get("layout_region") == "figure" for b in labels)
    assert all(not (b.metadata or {}).get("reading_order_uncertain") for b in labels)
    right = next(b for b in blocks if "R1" in (b.text or ""))
    left = next(b for b in blocks if "L1" in (b.text or ""))
    assert not (right.metadata or {}).get("figure_like")
    assert not (left.metadata or {}).get("figure_like")
    texts = _texts(blocks)
    assert _pos(texts, "R1") < _pos(texts, "R8")
    assert _pos(texts, "Enc") < _pos(texts, "L1")


def test_e2e_column_relative_alignment():
    """居中判断相对所在栏：左栏内居中的短行是 center，右栏正文不是 right。"""
    def draw(page):
        for i, y in enumerate((100, 112, 124, 136, 148)):
            page.insert_text((50, y), f"L{i+1} left column body text line here", fontname="helv", fontsize=9)
            page.insert_text((320, y), f"R{i+1} right column body text line here", fontname="helv", fontsize=9)
        centered = "E = mc2"
        width = fitz.get_text_length(centered, fontname="helv", fontsize=9)
        left_x0, left_x1 = 50, 50 + fitz.get_text_length(
            "L1 left column body text line here", fontname="helv", fontsize=9,
        )
        page.insert_text(((left_x0 + left_x1) / 2 - width / 2, 170), centered, fontname="helv", fontsize=9)

    blocks = PdfProvider().extract_from_bytes(_pdf(draw))
    equation = [b for b in blocks if "mc2" in (b.text or "")][0]
    assert equation.alignment == "center"
    assert (equation.metadata or {}).get("layout_region") == "column-1"
    right = [b for b in blocks if "R1" in (b.text or "")][0]
    assert right.alignment == "left"
