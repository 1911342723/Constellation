"""Tests for PdfProvider — PDF document extraction.

Run: python -m pytest tests/test_pdf_provider.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.providers.pdf_provider import PdfProvider
from infrastructure.models import Block
from app.core.exceptions import ProviderError


# ── Helpers ────────────────────────────────────────────────────

def _make_simple_pdf() -> bytes:
    """Create a minimal PDF with two text paragraphs using PyMuPDF."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()

    # Paragraph 1: normal body text
    page.insert_text(
        (72, 72),
        "This is the first paragraph of the document.",
        fontname="helv",
        fontsize=12,
    )

    # Paragraph 2: bold heading-like text
    page.insert_text(
        (72, 100),
        "Chapter 1: Introduction",
        fontname="hebo",  # Helvetica Bold
        fontsize=16,
    )

    # Paragraph 3: normal body text
    page.insert_text(
        (72, 130),
        "This is the body text under the chapter heading.",
        fontname="helv",
        fontsize=12,
    )

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _make_table_pdf() -> bytes:
    """Create a PDF with a simple table."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()

    # Draw a simple table using insert_text
    page.insert_text((72, 72), "Name", fontname="hebo", fontsize=12)
    page.insert_text((200, 72), "Score", fontname="hebo", fontsize=12)
    page.insert_text((72, 90), "Alice", fontname="helv", fontsize=12)
    page.insert_text((200, 90), "95", fontname="helv", fontsize=12)
    page.insert_text((72, 108), "Bob", fontname="helv", fontsize=12)
    page.insert_text((200, 108), "87", fontname="helv", fontsize=12)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _make_image_pdf() -> bytes:
    """Create a PDF with an embedded image."""
    import fitz

    # Create a small red PNG image
    doc = fitz.open()
    page = doc.new_page()

    # Insert a small colored rectangle as a pseudo-image
    # (We can't easily create a real image in-memory without PIL,
    #  so we test that the provider handles pages with no images gracefully)
    page.insert_text((72, 72), "Page with text only", fontname="helv", fontsize=12)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ── Tests ──────────────────────────────────────────────────────

def test_extract_from_bytes_returns_blocks():
    """Basic smoke test: extract blocks from a simple PDF."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    assert len(blocks) > 0
    assert all(isinstance(b, Block) for b in blocks)


def test_text_blocks_have_content():
    """Text blocks should have non-empty text."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    text_blocks = [b for b in blocks if b.type == "text"]
    assert len(text_blocks) >= 2

    for b in text_blocks:
        assert b.text
        assert len(b.text.strip()) > 0


def test_font_size_detected():
    """Font size should be extracted from PDF spans."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    text_blocks = [b for b in blocks if b.type == "text"]
    sizes = {b.font_size for b in text_blocks if b.font_size}

    # We inserted 12pt and 16pt text
    assert len(sizes) >= 2
    assert any(s >= 14 for s in sizes)  # heading
    assert any(s <= 13 for s in sizes)  # body


def test_bold_detection():
    """Bold text should be detected via font flags."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    text_blocks = [b for b in blocks if b.type == "text"]
    bold_blocks = [b for b in text_blocks if b.is_bold]

    # At least one block should be bold (the heading)
    assert len(bold_blocks) >= 1


def test_heading_style_detection():
    """Blocks with larger font size should be flagged as heading style."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    text_blocks = [b for b in blocks if b.type == "text"]
    heading_blocks = [b for b in text_blocks if b.is_heading_style]

    # The 16pt heading should be detected
    assert len(heading_blocks) >= 1
    assert any("Chapter" in (b.text or "") for b in heading_blocks)


def test_metadata_contains_source_and_page():
    """Every block should have source='pdf' and page number in metadata."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    for b in blocks:
        assert b.metadata is not None
        assert b.metadata.get("source") == "pdf"
        assert b.metadata.get("page") == 1


def test_sequential_block_ids():
    """Block IDs should be sequential starting from 0."""
    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    ids = [b.id for b in blocks]
    assert ids == list(range(len(blocks)))


def test_empty_page_returns_empty_list():
    """A PDF with one blank page should return an empty list."""
    import fitz

    doc = fitz.open()
    doc.new_page()  # empty page with no content
    pdf_bytes = doc.tobytes()
    doc.close()

    blocks = PdfProvider().extract_from_bytes(pdf_bytes)
    assert blocks == []


def test_invalid_bytes_raises_provider_error():
    """Non-PDF bytes should raise ProviderError."""
    try:
        PdfProvider().extract_from_bytes(b"not a pdf")
        assert False, "Should have raised ProviderError"
    except ProviderError:
        pass


def test_unsupported_extension_raises():
    """extract() with non-PDF extension should raise ProviderError."""
    try:
        PdfProvider().extract("document.docx")
        assert False, "Should have raised ProviderError"
    except ProviderError as e:
        assert "Unsupported" in str(e)


def test_alignment_detection_centered():
    """Centered text should be detected."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    # Insert text roughly centered
    page_width = page.rect.width
    text = "Centered Title"
    text_width = len(text) * 8  # rough estimate
    x = (page_width - text_width) / 2
    page.insert_text((x, 72), text, fontname="hebo", fontsize=16)

    pdf_bytes = doc.tobytes()
    doc.close()

    blocks = PdfProvider().extract_from_bytes(pdf_bytes)
    text_blocks = [b for b in blocks if b.type == "text"]

    # At least one block should be center-aligned
    center_blocks = [b for b in text_blocks if b.alignment == "center"]
    assert len(center_blocks) >= 1


def test_multipage_extraction():
    """Multi-page PDF should extract blocks from all pages."""
    import fitz

    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            f"Content of page {i + 1}",
            fontname="helv",
            fontsize=12,
        )

    pdf_bytes = doc.tobytes()
    doc.close()

    blocks = PdfProvider().extract_from_bytes(pdf_bytes)
    pages = {b.metadata.get("page") for b in blocks}

    assert 1 in pages
    assert 2 in pages
    assert 3 in pages


# ── End-to-end PDF → DocumentTree tests ────────────────────

def test_e2e_pdf_to_document_tree():
    """Full pipeline: PDF bytes -> PdfProvider -> CaliperParser -> DocumentTree.

    Uses mocked LLM so the test runs without an API key.
    """
    from unittest.mock import MagicMock, patch
    from modules.parser.parser import CaliperParser
    from modules.parser.schemas import LLMRouterOutput, ChapterNode

    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    # Mock the LLM to return a simple heading structure
    mock_output = LLMRouterOutput(
        doc_title="Test Document",
        doc_authors="",
        chapters=[
            ChapterNode(
                title="Chapter 1", start_block_id=0,
                level=1, snippet="Chapter 1",
            ),
        ],
    )

    parser = CaliperParser()
    mock_route = MagicMock(return_value=mock_output)
    with patch.object(parser.router, "route", mock_route):
        tree = parser.parse(blocks)

    # Verify the LLM was called exactly once with skeleton text
    assert mock_route.call_count == 1
    call_args = mock_route.call_args
    skeleton_text = call_args[0][0]  # first positional arg
    assert "Constellation" in skeleton_text
    assert len(skeleton_text) > 0

    stats = tree.get_stats()
    assert stats["total_sections"] >= 1
    assert stats["total_content_chars"] > 0

    parser.clear_cache()


def test_e2e_pdf_to_markdown():
    """Full pipeline: PDF bytes -> PdfProvider -> CaliperParser -> Markdown."""
    from unittest.mock import MagicMock, patch
    from modules.parser.parser import CaliperParser
    from modules.parser.schemas import LLMRouterOutput, ChapterNode

    pdf_bytes = _make_simple_pdf()
    blocks = PdfProvider().extract_from_bytes(pdf_bytes)

    mock_output = LLMRouterOutput(
        doc_title="Test Document",
        doc_authors="",
        chapters=[
            ChapterNode(
                title="Chapter 1", start_block_id=0,
                level=1, snippet="Chapter 1",
            ),
        ],
    )

    parser = CaliperParser()
    mock_route = MagicMock(return_value=mock_output)
    with patch.object(parser.router, "route", mock_route):
        tree = parser.parse(blocks)

    # Verify LLM was called
    assert mock_route.call_count == 1

    sections = tree.to_markdown_sections()
    assert len(sections) >= 1
    md = sections[0]["content"]
    assert len(md) > 0

    parser.clear_cache()


def test_e2e_pdf_block_order_preserved():
    """PDF blocks should be in physical reading order (page, y-position)."""
    import fitz

    doc = fitz.open()
    # Page 1: two paragraphs
    page1 = doc.new_page()
    page1.insert_text((72, 72), "First paragraph on page 1", fontname="helv", fontsize=12)
    page1.insert_text((72, 200), "Second paragraph on page 1", fontname="helv", fontsize=12)
    # Page 2: one paragraph
    page2 = doc.new_page()
    page2.insert_text((72, 72), "Paragraph on page 2", fontname="helv", fontsize=12)

    pdf_bytes = doc.tobytes()
    doc.close()

    blocks = PdfProvider().extract_from_bytes(pdf_bytes)
    texts = [b.text for b in blocks if b.type == "text"]

    # Order should be: first paragraph, second paragraph, page 2 paragraph
    assert texts[0].startswith("First paragraph")
    assert texts[1].startswith("Second paragraph")
    assert texts[2].startswith("Paragraph on page 2")


def test_provider_protocol_compliance():
    """PdfProvider should satisfy the BaseProvider protocol."""
    from infrastructure.providers.base import BaseProvider

    provider = PdfProvider()
    assert isinstance(provider, BaseProvider)


# ── Small-caps phantom-space regression (ViT / ICLR template) ──

def test_smallcaps_phantom_space_joined():
    """Small-caps spans with near-zero gaps must join without spaces.

    Span geometry copied from the real ViT paper ("2 RELATED WORK"):
    the leading capital and the rest of the word are separate spans
    with gaps of 0.0-0.6pt, while real word boundaries carry either an
    explicit leading space or a gap comparable to a space width.
    """
    spans = [
        {"text": "R", "size": 11.96, "bbox": (126.8, 226.9, 134.8, 238.8)},
        {"text": "ELATED", "size": 9.56, "bbox": (135.4, 228.7, 174.5, 238.3)},
        {"text": " W", "size": 11.96, "bbox": (174.5, 226.9, 189.3, 238.8)},
        {"text": "ORK", "size": 9.56, "bbox": (189.8, 228.7, 211.2, 238.3)},
    ]
    assert PdfProvider._join_line_spans(spans) == "RELATED WORK"


def test_join_line_spans_real_word_gap_preserved():
    """Spans separated by a space-width gap must keep the word boundary."""
    spans = [
        {"text": "Hello", "size": 12.0, "bbox": (72.0, 100.0, 100.0, 112.0)},
        # gap of 3.4pt (> 12 * 0.18 = 2.16) -> real space
        {"text": "world", "size": 12.0, "bbox": (103.4, 100.0, 130.0, 112.0)},
    ]
    assert PdfProvider._join_line_spans(spans) == "Hello world"


def test_inline_segments_merged_number_and_title():
    """Section number and title on one baseline must merge into one line.

    Line geometry copied from the real ViT paper: "2" and "RELATED
    WORK" are emitted as two separate PyMuPDF lines with full vertical
    overlap and a 12.5pt horizontal gap.
    """
    lines = [
        {
            "bbox": (108.3, 226.9, 114.3, 238.8),
            "spans": [{"text": "2", "size": 11.96, "bbox": (108.3, 226.9, 114.3, 238.8)}],
        },
        {
            "bbox": (126.8, 226.9, 211.2, 238.8),
            "spans": [
                {"text": "R", "size": 11.96, "bbox": (126.8, 226.9, 134.8, 238.8)},
                {"text": "ELATED", "size": 9.56, "bbox": (135.4, 228.7, 174.5, 238.3)},
                {"text": " W", "size": 11.96, "bbox": (174.5, 226.9, 189.3, 238.8)},
                {"text": "ORK", "size": 9.56, "bbox": (189.8, 228.7, 211.2, 238.3)},
            ],
        },
    ]
    merged = PdfProvider._merge_inline_segments(lines)
    assert len(merged) == 1
    assert PdfProvider._join_line_spans(merged[0]["spans"]) == "2 RELATED WORK"


def test_inline_segments_distinct_baselines_not_merged():
    """Lines on different baselines must stay separate."""
    lines = [
        {
            "bbox": (72.0, 100.0, 200.0, 112.0),
            "spans": [{"text": "First line", "size": 12.0, "bbox": (72.0, 100.0, 200.0, 112.0)}],
        },
        {
            "bbox": (72.0, 120.0, 200.0, 132.0),
            "spans": [{"text": "Second line", "size": 12.0, "bbox": (72.0, 120.0, 200.0, 132.0)}],
        },
    ]
    merged = PdfProvider._merge_inline_segments(lines)
    assert len(merged) == 2


# ── Body font size: character-weighted mode regression ─────────

def _make_annotation_heavy_pdf() -> bytes:
    """PDF where small-font spans outnumber body spans but carry less text.

    Mimics the GPT-4 technical report failure mode: dozens of short
    7pt reference/annotation lines vs a handful of long 10pt body
    paragraphs.  A span-count mode picks 7pt; the character-weighted
    mode must pick 10pt.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page()

    y = 40.0
    for i in range(30):  # 30 spans x ~10 chars = ~300 chars at 7pt
        page.insert_text((40, y), f"[{i}] Ref.", fontname="helv", fontsize=7)
        y += 9

    body_line = "This is a long body paragraph carrying the bulk of text. " * 2
    for _ in range(6):  # 6 spans x ~116 chars = ~700 chars at 10pt
        page.insert_text((40, y), body_line[:80], fontname="helv", fontsize=10)
        y += 14

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_body_font_size_char_weighted_not_span_count():
    """Char-weighted mode must resist flooding by short small-font spans."""
    import fitz

    doc = fitz.open(stream=_make_annotation_heavy_pdf(), filetype="pdf")
    try:
        estimated = PdfProvider()._estimate_body_font_size(doc)
    finally:
        doc.close()

    assert abs(estimated - 10.0) < 0.5, (
        f"body font size estimated {estimated}pt; span-count mode would "
        "have picked 7pt and broken heading detection document-wide"
    )


def test_annotation_flood_does_not_flag_body_as_heading():
    """With a correct 10pt body estimate, 10pt text is NOT heading-style.

    If the estimate collapsed to 7pt (old span-count bug), every 10pt
    body line would exceed 7 * 1.15 = 8.05pt and be flagged as a
    heading — the GPT-4 report had 69% of blocks mis-flagged this way.
    """
    blocks = PdfProvider().extract_from_bytes(_make_annotation_heavy_pdf())

    body_blocks = [
        b for b in blocks
        if b.type == "text" and b.font_size and abs(b.font_size - 10.0) < 0.5
    ]
    assert body_blocks, "expected 10pt body blocks in fixture"
    flagged = [b for b in body_blocks if b.is_heading_style]
    assert not flagged, (
        f"{len(flagged)}/{len(body_blocks)} body blocks mis-flagged as "
        "heading-style; body font size estimate is likely wrong"
    )


# ── Soft-hyphen line merging (2026-06-11 audit regression) ─────

def _line_entry(bid: int, text: str, y0: float):
    """Build a (page, column, y0, x0, Block) entry for _merge_text_lines."""
    block = Block(
        id=bid,
        type="text",
        text=text,
        font_size=10.0,
        alignment="left",
        metadata={
            "source": "pdf",
            "page": 1,
            "line_index": bid,
            "bbox": [72.0, y0, 300.0, y0 + 10.0],
            "page_width": 612.0,
            "page_height": 792.0,
            "layout_column": 1,
        },
    )
    return (0, 1, y0, 72.0, block)


def test_merge_soft_hyphen_joins_word_without_space():
    """"infor-" + "mation continues" must merge to "information continues"."""
    entries = [
        _line_entry(0, "This study presents detailed infor-", 100.0),
        _line_entry(1, "mation about the corpus we analysed.", 112.0),
    ]

    merged = PdfProvider()._merge_text_lines(entries)

    assert len(merged) == 1
    text = merged[0][4].text
    assert "information about" in text
    assert "infor- mation" not in text


def test_merge_double_dash_is_not_treated_as_hyphenation():
    """A line ending in "--" (em-dash style) must keep both dashes; the
    old rstrip("- ") stripped them all and glued the words together."""
    entries = [
        _line_entry(0, "The outcome was unexpected --", 100.0),
        _line_entry(1, "every metric improved at once.", 112.0),
    ]

    merged = PdfProvider()._merge_text_lines(entries)

    assert len(merged) == 1
    text = merged[0][4].text
    assert "--" in text
    assert "unexpected -- every" in text


# ── B05: 行末连字符——是否同段 vs 如何连接分离 ─────────────────
def test_merge_ordinary_hyphen_compound_word_is_kept():
    """``state-of-the-`` + ``art`` 必须还原成 ``state-of-the-art``：末尾 ``-`` 是
    连字符复合词的一部分，旧规则一律删杠会粘成 ``state-of-theart`` (B05 复现)。"""
    merged = PdfProvider()._merge_text_lines([
        _line_entry(0, "A documented state-of-the-", 100.0),
        _line_entry(1, "art system that works.", 112.0),
    ])
    assert merged[0][4].text == "A documented state-of-the-art system that works."


def test_merge_numbering_hyphen_kept_covid19():
    """``COVID-`` + ``19``：连字符左右非"拉丁小写断词"，保留连字符不粘字。"""
    merged = PdfProvider()._merge_text_lines([
        _line_entry(0, "The outbreak of COVID-", 100.0),
        _line_entry(1, "19 spread fast.", 112.0),
    ])
    assert merged[0][4].text == "The outbreak of COVID-19 spread fast."


def test_merge_minus_sign_kept():
    """行末减号 ``-`` + 数字：保留（是负号/减号，不是断词）。"""
    merged = PdfProvider()._merge_text_lines([
        _line_entry(0, "The delta was -", 100.0),
        _line_entry(1, "5 degrees overnight.", 112.0),
    ])
    assert merged[0][4].text == "The delta was -5 degrees overnight."


def test_merge_url_hyphen_kept():
    """URL 内断行的连字符保留，且不插空格：``foo-`` + ``bar`` → ``foo-bar``。"""
    merged = PdfProvider()._merge_text_lines([
        _line_entry(0, "See http://example.com/foo-", 100.0),
        _line_entry(1, "bar for details here.", 112.0),
    ])
    assert "foo-bar" in merged[0][4].text


def test_merge_cjk_lines_joined_without_western_space():
    """中文连续正文跨行不机械插西文空格。"""
    merged = PdfProvider()._merge_text_lines([
        _line_entry(0, "这是中文的第一行", 100.0),
        _line_entry(1, "第二行继续同一段。", 112.0),
    ])
    assert merged[0][4].text == "这是中文的第一行第二行继续同一段。"


def test_merge_soft_hyphen_join_is_reversible_via_atoms():
    """软连字符合并后，源原子物理文本不被就地篡改（可撤销规范化）：``infor-``
    原样留在 atom.text，删杠只体现在 provenance.canonical_core。"""
    a = _line_entry(0, "This study presents detailed infor-", 100.0)
    b = _line_entry(1, "mation about the corpus.", 112.0)
    # 给每行附一个携带物理源文本的原子，模拟真实 _extract_text 产物。
    a[4].metadata["atoms"] = [{"text": "This study presents detailed infor-",
                               "font_size": 10.0, "provenance": {}}]
    b[4].metadata["atoms"] = [{"text": "mation about the corpus.",
                               "font_size": 10.0, "provenance": {}}]

    merged = PdfProvider()._merge_text_lines([a, b])

    assert merged[0][4].text == "This study presents detailed information about the corpus."
    atoms = merged[0][4].metadata["atoms"]
    # 源原子物理文本原样保留（未就地删杠），规范化写在 provenance 里。
    assert atoms[0]["text"] == "This study presents detailed infor-"
    assert atoms[0]["provenance"].get("canonical_core") == "This study presents detailed infor"


def test_reference_entry_numbering_not_flagged():
    """"100. Author..." is a reference entry, not heading numbering."""
    assert not PdfProvider._has_heading_numbering("100. Smith, J. (2020).")
    assert PdfProvider._has_heading_numbering("12. Conclusion")
    assert PdfProvider._has_heading_numbering("3.2.1 Methods")


def test_merge_respects_document_body_font():
    """A uniformly 16pt document must still merge its body lines: the
    title heuristic is judged against the real body font, not 12pt."""
    entries = [
        _line_entry(0, "First line of a paragraph in large print", 100.0),
        _line_entry(1, "second line continues the same paragraph.", 112.0),
    ]
    for e in entries:
        e[4].font_size = 16.0

    merged = PdfProvider()._merge_text_lines(entries, body_font_size=16.0)

    assert len(merged) == 1, (
        "16pt body lines failed to merge: is_potential_title used the "
        "12pt default instead of the document body font"
    )


def test_extract_text_emits_atom_for_each_physical_line_without_fitz():
    class _Rect:
        width = 600.0
        height = 800.0

    class _Page:
        rect = _Rect()

        @staticmethod
        def get_text(mode):
            assert mode == "dict"
            return {
                "blocks": [{
                    "type": 0,
                    "lines": [
                        {
                            "bbox": (20.0, 20.0, 120.0, 32.0),
                            "spans": [{
                                "text": "First physical line",
                                "size": 10.0,
                                "font": "Body",
                                "flags": 0,
                                "bbox": (20.0, 20.0, 120.0, 32.0),
                            }],
                        },
                        {
                            "bbox": (20.0, 40.0, 130.0, 52.0),
                            "spans": [{
                                "text": "Second physical line",
                                "size": 10.0,
                                "font": "Body",
                                "flags": 0,
                                "bbox": (20.0, 40.0, 130.0, 52.0),
                            }],
                        },
                    ],
                }],
            }

    entries = PdfProvider()._extract_text(_Page(), 0, 10.0)

    assert len(entries) == 2
    atoms = [entry[4].metadata["atoms"][0] for entry in entries]
    assert [atom["source_span"]["line"] for atom in atoms] == [0, 1]
    assert [atom["text"] for atom in atoms] == [
        "First physical line", "Second physical line",
    ]
    assert all(atom["char_start"] == 0 for atom in atoms)


def test_extract_text_marks_high_table_overlap_as_noncanonical():
    class _Rect:
        width = 600.0
        height = 800.0

    class _Page:
        rect = _Rect()

        @staticmethod
        def get_text(_mode):
            return {"blocks": [{
                "type": 0,
                "lines": [{
                    "bbox": (10.0, 10.0, 90.0, 30.0),
                    "spans": [{
                        "text": "Table cell",
                        "size": 10.0,
                        "font": "Body",
                        "flags": 0,
                        "bbox": (10.0, 10.0, 90.0, 30.0),
                    }],
                }],
            }]}

    entries = PdfProvider()._extract_text(
        _Page(), 0, 10.0, table_bboxes=[(0.0, 0.0, 100.0, 50.0)],
    )
    metadata = entries[0][4].metadata

    assert metadata["artifact"] is True
    assert metadata["in_table"] is True
    assert metadata["canonical"] is False
    assert metadata["atoms"][0]["provenance"]["in_table"] is True


# ── P02: 合行收集后一次完成 —— 逐字符等价 + 增长曲线 ─────────────

def _naive_merge_reference(entries):
    """朴素参考实现：照旧逻辑逐行拼接 + 每完成一段全量重算 offset。

    这是 P02 验收①的对照物——用它对同一批合成输入重算一遍，断言新实现的合并
    文本与每个 atom 的 (char_start, char_end) 与它逐字相等。逻辑刻意用最直白的
    "每并入一行就重新拼整段文本、段末一次性重算全部 atom 偏移"，与被测的收集式
    实现完全独立。连接规则复用 provider 的 _join_wrapped_lines（是否同段仍由
    _can_merge_text_lines 判定），因此对照的是"实现方式"而非"连接规则"。
    """
    prov = PdfProvider()
    typical = prov._typical_line_width(entries)
    sorted_entries = sorted(entries, key=prov._entry_sort_key)
    out = []
    for entry in sorted_entries:
        page, column, y0, x0, block = entry
        if not out or not prov._can_merge_text_lines(out[-1][4], block, 0.0, typical):
            # 段落结束：一次性重算上一段 atom 偏移。
            if out:
                prev = out[-1][4]
                prov._recalculate_atom_offsets(
                    prev.text or "", (prev.metadata or {}).get("atoms", [])
                )
            out.append(entry)
            continue
        prev_block = out[-1][4]
        joined, drop = prov._join_wrapped_lines(prev_block.text or "", block.text or "")
        prev_block.text = joined
        prev_meta = prev_block.metadata or {}
        curr_meta = block.metadata or {}
        prev_atoms = prev_meta.setdefault("atoms", [])
        curr_atoms = list(curr_meta.get("atoms", []))
        if drop and prev_atoms:
            last = prev_atoms[-1]
            src = str(last.get("text", ""))
            if src.rstrip().endswith(("-", "\u00ad")):
                prov_meta = last.setdefault("provenance", {})
                prov_meta["canonical_core"] = src.rstrip()[:-1]
                prov_meta.pop("canonical_text", None)
                prov_meta["join_normalization"] = "soft-hyphen-removed"
        prev_atoms.extend(curr_atoms)
        # 旧实现在合并时会更新 union bbox——mergeability 判定靠它看"上一行"的 y1，
        # 参考实现必须同样更新，否则会因段首 bbox 不更新而误切段落。
        prev_bbox = prev_meta.get("bbox") or [0, 0, 0, 0]
        curr_bbox = curr_meta.get("bbox") or [0, 0, 0, 0]
        prev_meta["bbox"] = [
            min(float(prev_bbox[0]), float(curr_bbox[0])),
            min(float(prev_bbox[1]), float(curr_bbox[1])),
            max(float(prev_bbox[2]), float(curr_bbox[2])),
            max(float(prev_bbox[3]), float(curr_bbox[3])),
        ]
        prev_block.metadata = prev_meta
    if out:
        prev = out[-1][4]
        prov._recalculate_atom_offsets(
            prev.text or "", (prev.metadata or {}).get("atoms", [])
        )
    return out


def _para_entries(n_lines: int, base_id: int = 0):
    """构造 n_lines 条同段物理行（每行 1 个携带物理文本的 atom）。"""
    entries = []
    for i in range(n_lines):
        text = f"body fragment number {base_id + i} continues the paragraph here"
        block = Block(
            id=base_id + i, type="text", text=text, font_size=10.0, alignment="left",
            metadata={
                "source": "pdf", "page": 1, "line_index": base_id + i,
                "bbox": [72.0, 100.0 + i * 12.0, 500.0, 110.0 + i * 12.0],
                "page_width": 612.0, "page_height": 792.0, "layout_column": 1,
                "atoms": [{"text": text, "font_size": 10.0, "provenance": {}}],
            },
        )
        entries.append((0, 1, 100.0 + i * 12.0, 72.0, block))
    return entries


def test_p02_char_level_equivalence_vs_naive_reference():
    """P02①：合并文本与每个 atom 的 (start,end) 与朴素参考实现逐字相等。"""
    import copy

    # 含软连字符边界的一段，确保规范化路径也被对照到。
    def _build():
        entries = _para_entries(20)
        # 让第 5 行以断词连字符结尾，第 6 行续接（触发删杠 + canonical_core）。
        entries[5][4].text = "This carries hyphen-ated infor-"
        entries[5][4].metadata["atoms"][0]["text"] = "This carries hyphen-ated infor-"
        entries[6][4].text = "mation across the wrapped line boundary here"
        entries[6][4].metadata["atoms"][0]["text"] = (
            "mation across the wrapped line boundary here"
        )
        return entries

    new_result = PdfProvider()._merge_text_lines(_build())
    ref_result = _naive_merge_reference(_build())

    assert len(new_result) == len(ref_result) == 1
    new_block, ref_block = new_result[0][4], ref_result[0][4]
    # 合并文本逐字相等。
    assert new_block.text == ref_block.text
    # 每个 atom 的偏移与源文本逐字相等。
    new_atoms = new_block.metadata["atoms"]
    ref_atoms = ref_block.metadata["atoms"]
    assert len(new_atoms) == len(ref_atoms)
    for na, ra in zip(new_atoms, ref_atoms):
        assert (na["char_start"], na["char_end"]) == (ra["char_start"], ra["char_end"])
        assert na["text"] == ra["text"]
        assert na.get("provenance", {}).get("canonical_core") == \
            ra.get("provenance", {}).get("canonical_core")


def test_p02_growth_curve_is_not_superlinear():
    """P02②：128/256/512 行三档计时，512 档耗时 < 128 档 x 12（宽松上界防 CI 抖动）。

    旧的原位重扫实现呈 ~4 倍/档超线性（512 相对 128 约 16 倍）；收集后一次完成
    接近线性。这里断言宽松上界并在失败信息里给出实测三个数字（也见 stdout）。
    """
    import time

    def _time(n):
        best = float("inf")
        for _ in range(3):  # 取三次最小，降抖动
            entries = _para_entries(n)
            t0 = time.perf_counter()
            PdfProvider()._merge_text_lines(entries)
            best = min(best, time.perf_counter() - t0)
        return best

    t128, t256, t512 = _time(128), _time(256), _time(512)
    print(f"\n[P02 growth] 128={t128*1000:.2f}ms 256={t256*1000:.2f}ms 512={t512*1000:.2f}ms")
    # 宽松上界：避免 CI 抖动误报，但仍能挡住 O(n²) 回归（那会是 ~16 倍）。
    assert t512 < t128 * 12 + 0.05, (
        f"合行增长疑似超线性：128={t128*1000:.2f}ms 256={t256*1000:.2f}ms "
        f"512={t512*1000:.2f}ms（512 应 < 128 x 12）"
    )



# ── E01: 页眉页脚保留来源 + 按角色展示（真实调用方入口）─────────

def _make_repeating_header_pdf() -> bytes:
    """多页 PDF：每页顶部同一页眉行 + 唯一正文 + 每页底部页码。

    第 1 页额外有一个"只出现一页、不重复"的页顶大字号标题，用于验收"真实页顶
    标题不被判成页眉"。
    """
    import fitz

    doc = fitz.open()
    for i in range(4):
        page = doc.new_page()
        # 重复页眉（每页相同）。
        page.insert_text((72, 20), "Confidential — Acme Corp Internal", fontname="helv", fontsize=8)
        if i == 0:
            # 只出现一页的真实页顶标题（大字号）。
            page.insert_text((72, 60), "Annual Report 2026", fontname="hebo", fontsize=20)
        # 唯一正文。
        page.insert_text((72, 400), f"Unique body content for page {i + 1}.", fontname="helv", fontsize=12)
        # 重复页脚（页码归一化后是同一模式）。
        page.insert_text((72, 770), f"Page {i + 1}", fontname="helv", fontsize=8)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_e01_repeating_header_tagged_not_deleted():
    """重复页眉不再从块流删除——审计视图能拿到它并带 role_reason。"""
    audit = PdfProvider().extract_audit_view(_make_repeating_header_pdf())

    header_blocks = [
        b for b in audit["blocks"]
        if (b.metadata or {}).get("source_role") == "header"
    ]
    assert header_blocks, "重复页眉应被标 source_role=header 而非删除"
    for b in header_blocks:
        assert (b.metadata or {}).get("role_reason")
        assert 0.0 <= (b.metadata or {}).get("role_confidence", -1) <= 1.0
    # 被排除的角色块不在 canonical 正文里（默认交付行为向后兼容）。
    canonical_texts = [b.text for b in audit["canonical"]]
    assert not any("Confidential" in (t or "") for t in canonical_texts)
    # 但仍在完整序列里可恢复。
    assert any("Confidential" in (b.text or "") for b in audit["blocks"])


def test_e01_real_top_title_not_flagged_as_header():
    """只出现一页、不重复的页顶大字号标题不被判成页眉，仍是 canonical 正文。"""
    audit = PdfProvider().extract_audit_view(_make_repeating_header_pdf())
    title_blocks = [b for b in audit["blocks"] if "Annual Report 2026" in (b.text or "")]
    assert title_blocks, "预期存在页顶标题块"
    for b in title_blocks:
        assert (b.metadata or {}).get("source_role") not in (
            "header", "footer",
        ), "真实页顶标题被误判成页眉/页脚"
    # 该标题在默认 canonical 交付里保留。
    assert any("Annual Report 2026" in (b.text or "") for b in audit["canonical"])


def test_e01_default_extract_backward_compatible():
    """默认 extract_from_bytes 只回 canonical（与旧'直接删页眉页脚'口径一致）。"""
    blocks = PdfProvider().extract_from_bytes(_make_repeating_header_pdf())
    assert not any("Confidential" in (b.text or "") for b in blocks)
    assert not any((b.metadata or {}).get("source_role") == "header" for b in blocks)


# ── E03: 按页选择 OCR 通道（不引依赖）────────────────────────

def test_e03_native_text_page_not_flagged():
    """原生文本页判定为 native_text、needs_ocr=False。"""
    pages = PdfProvider().classify_page_types(_make_simple_pdf())
    assert pages
    assert all(p["page_type"] == "native_text" for p in pages)
    assert all(not p["needs_ocr"] for p in pages)
    assert all(p["reason"] for p in pages)


def _make_scanned_like_pdf() -> bytes:
    """近似扫描页：整页大图覆盖、几乎无可提取文本。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    # 用一张覆盖整页的位图模拟扫描件（1x1 放大填充）。
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2))
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_e03_scanned_page_flagged_needs_ocr_without_engine():
    """扫描页判定 needs_ocr=True，且默认无引擎时进 warnings 报'后端缺位'，不假装识别。"""
    from infrastructure.providers.registry import extract_blocks_with_skips

    pages = PdfProvider().classify_page_types(_make_scanned_like_pdf())
    assert any(p["needs_ocr"] and p["page_type"] == "scanned" for p in pages)

    # 从真实 registry 入口走一遍，确认 needs_ocr 进了 skips（warning code 固定）。
    _blocks, skips = extract_blocks_with_skips("pdf", _make_scanned_like_pdf())
    ocr_skips = [s for s in skips if s.code == "pdf_page_needs_ocr"]
    assert ocr_skips, "扫描页应登记 pdf_page_needs_ocr warning"
    assert any("OCR 后端缺位" in s.message for s in ocr_skips)
    assert all(s.retryable for s in ocr_skips)


def test_e03_injected_ocr_engine_is_called():
    """注入满足协议的 OCR 引擎后，扫描页会调用它并登记 pdf_page_ocr_applied。"""
    calls = []

    class _FakeOcr:
        def recognize_page(self, page_image, *, page_number):
            calls.append(page_number)
            return {"text": "recognized", "confidence": 0.9, "source_type": "ocr"}

    provider = PdfProvider(ocr_engine=_FakeOcr())
    provider.extract_from_bytes(_make_scanned_like_pdf())
    skips = provider.drain_skips()
    assert calls, "注入的 OCR 引擎应被调用"
    assert any(s.code == "pdf_page_ocr_applied" for s in skips)
