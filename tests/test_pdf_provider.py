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
