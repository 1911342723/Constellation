"""Contract tests for the evidence-first Stage 2.5 candidate model."""
from __future__ import annotations

import math

from infrastructure.models import Block
from modules.parser.heading_candidates import (
    analyze_candidate_regions,
    generate_heading_candidate_set,
    generate_heading_candidates,
)
from modules.parser.schemas import HeadingCandidate


def _text(
    block_id: int,
    text: str,
    *,
    region: str | None = None,
    **kwargs,
) -> Block:
    metadata = dict(kwargs.pop("metadata", {}) or {})
    if region is not None:
        metadata["layout_region_id"] = region
    return Block(
        id=block_id,
        type="text",
        text=text,
        font_size=kwargs.pop("font_size", 10.0),
        metadata=metadata,
        **kwargs,
    )


def test_semantic_level_never_becomes_numbering_level():
    blocks = [
        _text(0, "A body sentence that establishes the local baseline."),
        _text(1, "Abstract"),
    ]

    candidate = next(
        item for item in generate_heading_candidate_set(blocks).candidates
        if item.block_id == 1
    )

    assert candidate.semantic_level == 1
    assert candidate.semantic_level_probability is not None
    assert candidate.numbering_level is None
    assert "semantic_title" in candidate.evidence_kinds(polarity=1)
    assert not candidate.evidence_kinds() & {
        "visible_numbering", "effective_numbering",
    }


def test_correlated_style_surfaces_emit_one_atomic_evidence_family():
    block = _text(
        7,
        "  Styled Heading  ",
        font_size=14.0,
        is_bold=True,
        is_heading_style=True,
        heading_level=2,
        alignment="center",
        metadata={"outline_level": 1, "style_level": 2, "style_id": "Heading2"},
    )

    candidate = generate_heading_candidate_set([block]).candidates[0]
    structure = [
        atom for atom in candidate.evidence
        if atom.correlation_group == "explicit_structure"
    ]

    assert len(structure) == 1
    assert structure[0].kind == "explicit_heading_style"
    assert structure[0].observed_value["outline_level"] == 1
    assert len({atom.correlation_group for atom in candidate.evidence}) == len(
        candidate.evidence
    )
    assert candidate.anchor.char_offset == 2
    assert candidate.title_span is not None
    assert candidate.title_span.start == 2
    assert candidate.title_span.end == len(block.text.rstrip())
    assert all(atom.anchor == candidate.anchor for atom in candidate.evidence)


def test_probabilities_and_region_risk_are_complete_outputs():
    candidate = generate_heading_candidate_set([
        _text(0, "1. Experimental Setup", is_bold=True, font_size=14.0),
        _text(1, "Body text with enough words to establish a baseline."),
    ]).candidates[0]

    assert 0.0 <= candidate.heading_probability <= 1.0
    assert 0.0 <= candidate.promotion_probability <= 1.0
    assert set(candidate.level_probabilities) == {
        "NONE", "L1", "L2", "L3", "L4", "L5", "L6",
    }
    assert math.isclose(sum(candidate.level_probabilities.values()), 1.0)
    assert math.isclose(
        candidate.heading_probability,
        1.0 - candidate.level_probabilities["NONE"],
    )
    assert candidate.probability_quality == "heuristic"
    assert candidate.region_risk.region_id == candidate.region_id
    assert candidate.anchor.atom_id
    dumped = candidate.model_dump()
    for field in (
        "heading_probability",
        "level_probabilities",
        "promotion_probability",
        "region_risk",
        "anchor",
    ):
        assert field in dumped


def test_featureless_fallback_is_local_to_layout_region():
    blocks = [
        _text(0, "Formatted region body sentence.", region="formatted"),
        _text(1, "Not A Candidate Here", region="formatted"),
        _text(2, "A Bold Marker", region="formatted", is_bold=True),
        _text(3, "Plain region body sentence.", region="plain"),
        _text(4, "Recovered Local Heading", region="plain"),
        _text(5, "Plain region continuation sentence.", region="plain"),
    ]

    regions = analyze_candidate_regions(blocks)
    assert not regions["layout:formatted"].featureless
    assert regions["layout:plain"].featureless
    assert regions["layout:plain"].risk.band == "escape"

    candidates = generate_heading_candidate_set(blocks).candidates
    assert all(item.block_id != 1 for item in candidates)
    recovered = next(item for item in candidates if item.block_id == 4)
    assert "standalone_line" in recovered.evidence_kinds(polarity=1)
    assert recovered.admission == "escape"
    assert "featureless_region" in recovered.region_risk.factors


def test_full_candidate_set_is_not_deleted_by_legacy_route_cap():
    blocks = [
        _text(i, f"Body paragraph {i} with ordinary prose.") for i in range(300)
    ]
    heading_ids = list(range(300, 370))
    blocks.extend(
        _text(i, f"{i - 299}. Numbered section") for i in heading_ids
    )

    candidate_set = generate_heading_candidate_set(blocks)
    full_ids = {candidate.block_id for candidate in candidate_set.candidates}
    routed = generate_heading_candidates(blocks)

    assert set(heading_ids) <= full_ids
    assert len(routed) <= max(30, int(len(blocks) * 0.15))
    assert len(candidate_set.candidates) > len(routed)
    assert all(candidate.raw_score is None for candidate in candidate_set.candidates)


def test_caption_negative_remains_auditable_but_is_not_legacy_routed():
    blocks = [
        _text(0, "Body paragraph with ordinary prose."),
        _text(1, "Figure 1: Model architecture", is_bold=True),
    ]
    candidate_set = generate_heading_candidate_set(blocks)
    caption = next(item for item in candidate_set.candidates if item.block_id == 1)

    assert "caption_negative" in caption.evidence_kinds(polarity=-1)
    assert caption.admission == "audit_only"
    assert caption.heading_probability < 0.1
    assert 1 not in {item.block_id for item in generate_heading_candidates(blocks)}


def test_legacy_constructor_is_an_explicit_probability_projection():
    candidate = HeadingCandidate(block_id=9, title="Legacy", source_score=0.75)

    assert candidate.anchor.block_id == 9
    assert candidate.heading_probability == 0.75
    assert candidate.promotion_probability == 0.75
    assert candidate.raw_score is None
    assert candidate.legacy_projection()["source_score"] == 0.75
