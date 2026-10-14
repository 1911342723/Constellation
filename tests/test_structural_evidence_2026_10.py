"""Evidence repairs found on Comp-HRDoc development documents (2026-10)."""
from infrastructure.models import Block
from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.config import ParserConfig
from modules.parser.heading_candidates import (
    bibliography_zone,
    generate_heading_candidate_set,
    infer_numbering_level,
    is_caption,
)
from modules.parser.titles import has_dot_leader, is_metadata_line, is_non_lexical


def _text(i, text, **kw):
    return Block(id=i, type="text", text=text, **kw)


def test_numeric_table_rows_are_not_section_numbers():
    assert infer_numbering_level("2.44 2.75 3.54") is None
    assert infer_numbering_level("0.5 × 10") is None
    assert infer_numbering_level("3.1 The Table Algorithm") == 2


def test_ieee_letter_numbering():
    assert infer_numbering_level("A. Zero Collision MAC") == 2


def test_non_lexical_fragments():
    for text in ["29", "(34)", "1000000", "λc,1 = 1 − γc", "π1(Si(uv))", "𝑡 + 1"]:
        assert is_non_lexical(text), text
    for text in ["1 Introduction", "引言", "AI", "The λ-calculus", "Q&A"]:
        assert not is_non_lexical(text), text


def test_metadata_lines_and_dot_leaders():
    assert is_metadata_line("(luis.sanabria@upf.edu)")
    assert is_metadata_line("(Dated: February 20, 2017)")
    assert not is_metadata_line("Published Results")
    assert has_dot_leader("3.8 NLI . . . . . . . 12")
    assert not has_dot_leader("3.8 NLI")


def test_standalone_caption_labels():
    assert is_caption("TABLE I") and is_caption("Fig. 4: overview")
    assert not is_caption("Table Tennis") and not is_caption("Tables and figures")


def test_tex_bold_font_names():
    for font in ["ABCDEF+CMBX12", "NimbusRomNo9L-Medi", "SFBX1200", "MinionPro-Semibold"]:
        assert PdfProvider._span_is_bold({"font": font, "flags": 0}), font
    for font in ["CMR10", "NimbusRomNo9L-Regu", "CMMI10"]:
        assert not PdfProvider._span_is_bold({"font": font, "flags": 0}), font


def test_bibliography_zone_and_negative_evidence():
    blocks = [
        _text(0, "1 Introduction", is_bold=True, font_size=12),
        _text(1, "Body text " * 20, font_size=10),
        _text(2, "References", is_bold=True, font_size=12),
        _text(3, "1. David Aldous. Probability distributions on cladograms.", font_size=9),
        _text(4, "A. Smith and B. Jones. A paper title. 2001.", font_size=9),
        _text(5, "A Proof of Lemma 1", is_bold=True, font_size=12),
    ]
    assert bibliography_zone(blocks) == {3, 4}
    candidates = {c.block_id: c for c in generate_heading_candidate_set(blocks).candidates}
    assert "bibliography_negative" in candidates[3].evidence_kinds(polarity=-1)


def test_pdf_font_flag_is_not_an_explicit_style():
    block = _text(0, "Number of tree nodes (1000x)", font_size=16, is_heading_style=True,
                  metadata={"source": "pdf"})
    body = [_text(i, "Body text " * 20, font_size=10, metadata={"source": "pdf"}) for i in range(1, 6)]
    candidate = generate_heading_candidate_set([block, *body]).candidates[0]
    assert "explicit_heading_style" not in candidate.evidence_kinds(polarity=1)


def test_silence_likelihood_ratio_is_validated():
    assert ParserConfig().llm_silence_likelihood_ratio == 0.12
    try:
        ParserConfig(llm_silence_likelihood_ratio=0.0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("ratio 0 must be rejected")
