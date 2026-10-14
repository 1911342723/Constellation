"""Tests for Stage 2.5 candidate admission tightening.

Weak-only candidates face a higher admission bar (score > 0.80) and a
per-document cap (~15% of text blocks, strong signals exempt), so that
two-column LaTeX PDFs no longer flood the LLM window with noise.

Run: python -m pytest tests/test_candidate_admission.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.models import Block
from modules.parser.heading_candidates import (
    _body_font_size,
    generate_heading_candidates,
)


def _body(text: str, block_id: int, size: float = 10.0) -> Block:
    return Block(id=block_id, type="text", text=text, font_size=size)


def test_weak_only_heuristic_plus_centered_rejected():
    """heuristic(0.45) + centered(0.35) = 0.80 must NOT pass (needs > 0.80).

    The weak-only admission bar targets two-column LaTeX PDFs, which are
    *formatted* documents (varied font sizes, bold headings).  A real
    bold heading is included here so the document is non-featureless and
    the no-style standalone-line fallback stays disabled — on genuinely
    featureless documents a centered short line is legitimately admitted
    (covered by ``test_featureless_standalone_line_admitted``).
    """
    blocks = [
        _body("Plain body paragraph that goes on for a while " * 3, 0),
        Block(
            id=1, type="text", text="Just a centered short line",
            font_size=10.0, alignment="center",
        ),
        _body("More body text follows here " * 4, 2),
        # A bold heading makes the document non-featureless, so the
        # weak-only bar (not the no-style fallback) governs block 1.
        Block(id=3, type="text", text="A Bold Heading", font_size=10.0, is_bold=True),
    ]
    candidates = generate_heading_candidates(blocks)
    assert all(c.block_id != 1 for c in candidates)


def test_featureless_standalone_line_admitted():
    """No-style recovery: in a featureless document a short, non-sentence
    line is admitted as a heading candidate (the LLM otherwise never sees
    it and misses every non-keyword heading)."""
    blocks = [
        Block(id=0, type="text", text="Supervised methods require labeled training data.", font_size=10.0),
        Block(id=1, type="text", text="Unsupervised Methods", font_size=10.0),
        Block(id=2, type="text", text="Clustering algorithms discover natural groupings.", font_size=10.0),
    ]
    candidates = generate_heading_candidates(blocks)
    assert any(
        c.block_id == 1 and "standalone-line" in c.reasons for c in candidates
    )


def test_featureless_full_sentence_not_admitted():
    """Even in a featureless document, a complete sentence (ends with a
    period) is body text and must not become a standalone-line candidate."""
    blocks = [
        Block(id=0, type="text", text="This is a short sentence.", font_size=10.0),
        Block(id=1, type="text", text="Another plain body line that simply continues.", font_size=10.0),
    ]
    candidates = generate_heading_candidates(blocks)
    assert all(c.block_id != 0 for c in candidates)


def test_strong_numbering_signal_still_admitted():
    """A numbered heading keeps its seat regardless of tightening."""
    blocks = [
        _body("Plain body paragraph " * 5, 0),
        _body("2.1 Experimental Setup", 1),
        _body("More body text " * 5, 2),
    ]
    candidates = generate_heading_candidates(blocks)
    assert any(c.block_id == 1 and "numbering" in c.reasons for c in candidates)


def test_candidate_cap_keeps_strong_prunes_weak():
    """When over the cap, strong signals survive and weak ones are pruned."""
    blocks: list[Block] = []
    # 200 body blocks -> cap = max(30, 0.15 * ~300) ~= 45
    for i in range(200):
        blocks.append(_body(f"Body paragraph number {i} with enough text", i))
    # 80 weak-only candidates: bold + centered short lines (0.35+0.35+0.45
    # heuristic = score above the weak bar via three weak reasons).
    weak_ids = []
    for i in range(200, 280):
        weak_ids.append(i)
        blocks.append(Block(
            id=i, type="text", text=f"Decorated line {i}",
            font_size=10.0, is_bold=True, alignment="center",
        ))
    # 10 strong candidates: explicit heading style.
    strong_ids = []
    for i in range(280, 290):
        strong_ids.append(i)
        blocks.append(Block(
            id=i, type="text", text=f"{i - 279} Strong Heading",
            font_size=14.0, heading_level=1,
        ))

    candidates = generate_heading_candidates(blocks)
    candidate_ids = {c.block_id for c in candidates}

    assert set(strong_ids) <= candidate_ids, "strong candidates must all survive"
    text_blocks = sum(1 for b in blocks if b.type == "text" and b.text)
    cap = max(30, int(text_blocks * 0.15))
    assert len(candidates) <= cap


def test_body_font_size_char_weighted_mode():
    """Long body paragraphs must dominate over many short annotations."""
    blocks = []
    # 30 short figure annotations at 7pt.
    for i in range(30):
        blocks.append(_body("fig note", i, size=7.0))
    # 10 long body paragraphs at 10pt.
    for i in range(30, 40):
        blocks.append(_body("A long body paragraph with plenty of text. " * 5, i, size=10.0))

    assert _body_font_size(blocks) == 10.0


def test_cap_prunes_by_raw_score_not_document_order():
    """Late high-raw-score candidates must survive over early noise.

    Both groups saturate source_score at 1.0 and carry 3 reasons each,
    so any ranking based on the capped score degrades into document-
    order truncation — which silently dropped every genuine heading in
    the back half of long surveys.  Only the uncapped raw score
    separates them (1.25 vs 1.15).
    """
    blocks: list[Block] = []
    for i in range(100):
        blocks.append(_body(f"Body paragraph number {i} with plenty of text", i))
    # 40 early weak noise: bold+centered+heuristic, raw = 1.15.
    noise_ids = list(range(100, 140))
    for i in noise_ids:
        blocks.append(Block(
            id=i, type="text", text=f"Decorated banner {i}",
            font_size=10.0, is_bold=True, alignment="center",
        ))
    # 10 late stronger weak candidates: bold+slightly-large+heuristic,
    # raw = 1.25 (still weak-only: no strong structural signal).
    late_ids = list(range(140, 150))
    for i in late_ids:
        blocks.append(Block(
            id=i, type="text", text=f"Prominent line {i}",
            font_size=11.0, is_bold=True,
        ))

    candidates = generate_heading_candidates(blocks)
    candidate_ids = {c.block_id for c in candidates}

    # cap = max(30, 15% x 150) = 30 < 50 weak candidates -> pruning runs.
    assert len(candidates) <= 30
    assert set(late_ids) <= candidate_ids, (
        "high-raw-score candidates at the document tail were pruned; "
        "cap degraded into document-order truncation"
    )


def test_cap_strong_overflow_keeps_highest_scored():
    """When strong candidates alone blow the budget, prune by raw score.

    Reference lists produce hundreds of bare-numbering lines (raw 0.85)
    that are all "strong"; genuine headings carry compound signals
    (numbering + bold + large-font + heuristic, raw ~2.45) and must
    survive the strong-vs-strong pruning.
    """
    blocks: list[Block] = []
    for i in range(300):
        blocks.append(_body(f"Body paragraph number {i} with plenty of text", i))
    # 60 bare-numbering reference entries (strong via numbering alone).
    ref_ids = list(range(300, 360))
    for i in ref_ids:
        blocks.append(_body(f"{i - 299}. Author, Title, Conference {i}", i))
    # 10 genuine multi-signal headings at the tail.
    heading_ids = list(range(360, 370))
    for i in heading_ids:
        blocks.append(Block(
            id=i, type="text", text=f"{i - 359}. Genuine Section Heading",
            font_size=14.0, is_bold=True,
        ))

    candidates = generate_heading_candidates(blocks)
    candidate_ids = {c.block_id for c in candidates}

    # cap = max(30, 15% x 370) = 55 < 70 strong candidates.
    assert len(candidates) <= 55
    assert set(heading_ids) <= candidate_ids, (
        "multi-signal genuine headings were pruned in strong-overflow mode"
    )
    assert len(candidate_ids & set(ref_ids)) < len(ref_ids), (
        "expected some bare-numbering reference entries to be pruned"
    )


# ── Bare large numbers are not section numbering (2026-06-11 audit) ─

def test_year_opening_sentence_is_not_numbering():
    """"2026 was a great year" must not earn the numbering strong signal."""
    from modules.parser.heading_candidates import infer_numbering_level

    assert infer_numbering_level("2026 was a great year for AI") is None
    assert infer_numbering_level("100. Smith, J. et al. A study.") is None


def test_legitimate_numbering_still_recognised():
    from modules.parser.heading_candidates import infer_numbering_level

    assert infer_numbering_level("3.2.1 Methods") == 3
    assert infer_numbering_level("12. Conclusion") == 1
    assert infer_numbering_level("99. Final chapter") == 1
    # Multi-level numbers keep their dots and are always accepted.
    assert infer_numbering_level("10.1 Background") == 2
