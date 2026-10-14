from __future__ import annotations

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.parser.heading_candidates import generate_heading_candidates
from modules.parser.parser import _compute_blocks_hash
from modules.parser.resolver import IntervalResolver
from modules.parser.router import LLMRouter
from modules.parser.schemas import ChapterNode, LLMRouterOutput
from infrastructure.models import Block


def test_cache_hash_includes_complete_block_payload():
    base = Block(
        id=0,
        type="text",
        text="Same visible text",
        metadata={"page": 1, "bbox": [0, 0, 10, 10]},
    )
    moved = Block(
        id=0,
        type="text",
        text="Same visible text",
        metadata={"page": 2, "bbox": [0, 20, 10, 30]},
    )
    image_a = Block(id=1, type="image", image_data="data:image/png;base64,aaa")
    image_b = Block(id=1, type="image", image_data="data:image/png;base64,bbb")

    assert _compute_blocks_hash([base]) != _compute_blocks_hash([moved])
    assert _compute_blocks_hash([image_a]) != _compute_blocks_hash([image_b])


def test_cache_hash_compacts_large_images_but_stays_content_sensitive():
    """Bulky base64 payloads are digested, not serialised, into the key."""
    big_a = Block(id=0, type="image", image_data="A" * 500_000)
    big_a2 = Block(id=0, type="image", image_data="A" * 500_000)
    big_b = Block(id=0, type="image", image_data="B" * 500_000)
    big_longer = Block(id=0, type="image", image_data="A" * 500_001)

    # Determinism and content sensitivity survive the compaction.
    assert _compute_blocks_hash([big_a]) == _compute_blocks_hash([big_a2])
    assert _compute_blocks_hash([big_a]) != _compute_blocks_hash([big_b])
    assert _compute_blocks_hash([big_a]) != _compute_blocks_hash([big_longer])


def test_heading_candidates_include_structural_signals_and_skip_captions():
    blocks = [
        Block(id=0, type="text", text="Body paragraph.", font_size=12),
        Block(
            id=1,
            type="text",
            text="1. Introduction",
            font_size=14,
            is_bold=True,
            has_heading_numbering=True,
        ),
        Block(
            id=2,
            type="text",
            text="Figure 1: Model architecture",
            font_size=14,
            is_bold=True,
        ),
        Block(id=3, type="text", text="Abstract", font_size=12),
    ]

    candidates = generate_heading_candidates(blocks)
    ids = {candidate.block_id for candidate in candidates}

    assert 1 in ids
    assert 2 not in ids
    assert 3 in ids
    intro = next(candidate for candidate in candidates if candidate.block_id == 1)
    assert intro.numbering_level == 1


def test_router_downgrades_non_candidate_llm_anchors():
    """Out-of-candidate anchors enter the low-confidence channel instead of being dropped."""
    result = LLMRouter._validate_and_filter_anchors(
        LLMRouterOutput(
            chapters=[
                ChapterNode(block_id=1, title="Allowed", level=1),
                ChapterNode(block_id=2, title="Out of table", level=1),
            ]
        ),
        max_block_id=5,
        allowed_block_ids={1},
    )

    assert [chapter.start_block_id for chapter in result.chapters] == [1, 2]
    in_table, out_of_table = result.chapters
    assert not in_table.out_of_candidate
    assert in_table.confidence == 1.0
    assert out_of_table.out_of_candidate
    assert out_of_table.confidence < 1.0


def test_router_hard_drops_non_candidate_anchors_when_downgrade_disabled():
    """Legacy hard-filter behaviour is preserved behind the config switch."""
    result = LLMRouter._validate_and_filter_anchors(
        LLMRouterOutput(
            chapters=[
                ChapterNode(block_id=1, title="Allowed", level=1),
                ChapterNode(block_id=2, title="Invented", level=1),
            ]
        ),
        max_block_id=5,
        allowed_block_ids={1},
        downgrade_out_of_candidate=False,
    )

    assert [chapter.start_block_id for chapter in result.chapters] == [1]


def _make_downgrade_fixture():
    """Blocks + candidates where block 4 is a real heading missed by candidates."""
    from modules.parser.parser import CaliperParser

    blocks = [
        Block(id=0, type="text", text="1. Introduction", font_size=16,
              is_bold=True, has_heading_numbering=True),
        Block(id=1, type="text", text="Body paragraph one." * 3, font_size=12),
        Block(id=2, type="text", text="Body paragraph two." * 3, font_size=12),
        # Real heading the candidate generator missed (plain font but bold+short).
        Block(id=3, type="text", text="Hidden Subsection", font_size=12, is_bold=True),
        Block(id=4, type="text", text=(
            "This is a long body paragraph that should never be accepted "
            "as a heading anchor because it is clearly running prose. " * 4
        ), font_size=12),
    ]
    candidates = generate_heading_candidates(blocks)
    assert 3 not in {c.block_id for c in candidates}
    parser = CaliperParser()
    return parser, blocks, candidates


def test_parser_keeps_verified_out_of_candidate_anchor():
    parser, blocks, candidates = _make_downgrade_fixture()
    output = LLMRouterOutput(chapters=[
        ChapterNode(block_id=0, title="1. Introduction", level=1),
        ChapterNode(block_id=3, title="Hidden Subsection", level=2,
                    out_of_candidate=True, confidence=0.4),
    ])

    verified = parser._verify_downgraded_anchors(output, blocks, candidates)

    kept_ids = [ch.start_block_id for ch in verified.chapters]
    assert kept_ids == [0, 3]
    rescued = verified.chapters[1]
    assert rescued.out_of_candidate
    assert rescued.confidence == 0.4  # stays low for the resolver's wide search


def test_parser_drops_hallucinated_out_of_candidate_anchor():
    parser, blocks, candidates = _make_downgrade_fixture()
    output = LLMRouterOutput(chapters=[
        ChapterNode(block_id=3, title="Completely Different Title", level=2,
                    out_of_candidate=True, confidence=0.4),
    ])

    verified = parser._verify_downgraded_anchors(output, blocks, candidates)

    assert verified.chapters == []


def test_parser_drops_prose_out_of_candidate_anchor():
    parser, blocks, candidates = _make_downgrade_fixture()
    prose_text = blocks[4].text[:40]
    output = LLMRouterOutput(chapters=[
        ChapterNode(block_id=4, title=prose_text, level=2,
                    out_of_candidate=True, confidence=0.4),
    ])

    verified = parser._verify_downgraded_anchors(output, blocks, candidates)

    assert verified.chapters == []


def test_parser_drops_out_of_candidate_anchors_without_blocks():
    parser, _blocks, _candidates = _make_downgrade_fixture()
    output = LLMRouterOutput(chapters=[
        ChapterNode(block_id=3, title="Hidden Subsection", level=2,
                    out_of_candidate=True, confidence=0.4),
    ])

    verified = parser._verify_downgraded_anchors(output, None, None)

    assert verified.chapters == []


# ── Precision-regression tightening (2026-06-11) ─────────────

def _verify_single(parser, blocks, candidates, block_id: int, title: str):
    """Run one out-of-candidate anchor through the downgrade channel."""
    output = LLMRouterOutput(chapters=[
        ChapterNode(block_id=block_id, title=title, level=2,
                    out_of_candidate=True, confidence=0.4),
    ])
    return parser._verify_downgraded_anchors(output, blocks, candidates)


def _precision_fixture(extra_blocks):
    """Fixture with body context plus caller-supplied probe blocks."""
    from modules.parser.parser import CaliperParser

    blocks = [
        Block(id=0, type="text", text="1. Introduction", font_size=16,
              is_bold=True, has_heading_numbering=True),
        Block(id=1, type="text", text="Plain body text. " * 6, font_size=12),
    ]
    blocks.extend(extra_blocks)
    candidates = generate_heading_candidates(blocks)
    return CaliperParser(), blocks, candidates


def test_downgrade_rejects_sentence_final_bold_label():
    """Bold figure-label prose ('Standard prompting works well.') must not pass."""
    probe = Block(id=2, type="text",
                  text="Standard prompting alone is not enough for this.",
                  font_size=12, is_bold=True)
    parser, blocks, candidates = _precision_fixture([probe])

    verified = _verify_single(parser, blocks, candidates, 2, probe.text)

    assert verified.chapters == []


def test_downgrade_rejects_capital_letter_prose():
    """'A method for ...' style prose must not match the appendix pattern."""
    probe = Block(id=2, type="text",
                  text="A method for computing answers", font_size=12)
    parser, blocks, candidates = _precision_fixture([probe])

    verified = _verify_single(parser, blocks, candidates, 2, probe.text)

    assert verified.chapters == []


def test_downgrade_accepts_appendix_numbering():
    """Real appendix headings ('B.1 Details') must still pass."""
    probe = Block(id=2, type="text",
                  text="B.1 Details of the GLUE Benchmark", font_size=12)
    parser, blocks, candidates = _precision_fixture([probe])

    verified = _verify_single(parser, blocks, candidates, 2, probe.text)

    assert [ch.start_block_id for ch in verified.chapters] == [2]


def test_downgrade_rejects_long_cjk_prose():
    """CJK prose (no spaces, >50 chars) must not pass via the bold path."""
    probe = Block(id=2, type="text",
                  text="这是一段没有任何空格的中文正文内容它明显是一句话而不是一个标题因为它实在太长了完全超过了标题的合理长度",
                  font_size=12, is_bold=True)
    parser, blocks, candidates = _precision_fixture([probe])

    verified = _verify_single(parser, blocks, candidates, 2, probe.text)

    assert verified.chapters == []


def test_downgrade_keeps_short_bold_runin_heading():
    """Run-in LaTeX headings (bold, short, no final period) must survive."""
    probe = Block(id=2, type="text", text="Scaled Dot-Product Attention",
                  font_size=12, is_bold=True)
    parser, blocks, candidates = _precision_fixture([probe])

    verified = _verify_single(parser, blocks, candidates, 2, probe.text)

    assert [ch.start_block_id for ch in verified.chapters] == [2]


def test_resolver_inverse_audit_promotes_missed_heading_candidate():
    blocks = [
        Block(
            id=0,
            type="text",
            text="1. Introduction",
            font_size=16,
            is_bold=True,
            has_heading_numbering=True,
        ),
        Block(id=1, type="text", text="Intro body text.", font_size=12),
        Block(
            id=2,
            type="text",
            text="1.1 Method",
            font_size=14,
            is_bold=True,
            has_heading_numbering=True,
        ),
        Block(id=3, type="text", text="Method body text.", font_size=12),
    ]
    candidates = generate_heading_candidates(blocks)
    resolver = IntervalResolver(blocks, heading_candidates=candidates)

    nodes = resolver.resolve([
        ChapterNode(block_id=0, title="1. Introduction", level=1),
    ])

    assert nodes[0].title == "1. Introduction"
    assert nodes[0].children
    assert nodes[0].children[0].title == "1.1 Method"
    assert nodes[0].children[0].level == 2


def test_pdf_provider_filters_headers_and_preserves_two_column_order():
    import fitz

    from infrastructure.providers.pdf_provider import PdfProvider

    doc = fitz.open()
    for page_no in range(2):
        page = doc.new_page()
        page.insert_text((72, 24), "Repeated Paper Header", fontsize=9)
        page.insert_text((72, 100), f"Left column page {page_no + 1}", fontsize=12)
        page.insert_text((330, 100), f"Right column page {page_no + 1}", fontsize=12)
        page.insert_text((300, 820), str(page_no + 1), fontsize=9)
    pdf_bytes = doc.tobytes()
    doc.close()

    blocks = PdfProvider().extract_from_bytes(pdf_bytes)
    texts = [block.text for block in blocks if block.type == "text"]

    assert "Repeated Paper Header" not in texts
    assert "1" not in texts
    assert texts[:4] == [
        "Left column page 1",
        "Right column page 1",
        "Left column page 2",
        "Right column page 2",
    ]


def test_docx_provider_detects_localized_heading_style():
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE

    from infrastructure.providers.docx_provider import DocxProvider

    doc = Document()
    style = doc.styles.add_style("标题 2", WD_STYLE_TYPE.PARAGRAPH)
    paragraph = doc.add_paragraph("本地化标题")
    paragraph.style = style

    block = DocxProvider()._process_paragraph(paragraph, 0)

    assert block is not None
    assert block.is_heading_style
    assert block.heading_level == 2
    assert block.metadata["style"] == "标题 2"


def test_section_f1_hierarchy_accuracy_uses_matched_pairs_not_position():
    from evaluation.metrics import HeadingGT, HeadingPred, compute_section_f1

    gt = [
        HeadingGT(block_id=10, title="A", level=1),
        HeadingGT(block_id=20, title="B", level=2),
    ]
    pred = [
        HeadingPred(block_id=20, title="B", level=3),
        HeadingPred(block_id=10, title="A", level=1),
    ]

    result = compute_section_f1(gt, pred, block_id_tolerance=0)

    assert result.tp == 2
    assert result.hierarchy_accuracy == 0.5
