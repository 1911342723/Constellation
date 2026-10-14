from __future__ import annotations

import json
import math
from types import SimpleNamespace

import fitz
import pytest

from infrastructure.models import Block
from infrastructure.providers.pdf_provider import PdfProvider
from infrastructure.providers.pdf_content_relations import annotate_content_relations
from modules.parser.calibration import CalibrationMap, calibrated_score, load_calibration, pipeline_fingerprint
from modules.parser.global_inference import GlobalHeadingInference, _Site
from modules.parser.router import LLMRouter
from modules.parser.schemas import ChapterNode, LLMAnchorVote, LLMRouterOutput


def _emission_votes(n, spans=None, models=None):
    block = Block(id=1, type="text", text="Scope")
    votes = [LLMAnchorVote(raw_block_id=1, aligned_block_id=1, title="Scope", level=2,
        confidence=.9, alignment_score=1, window_index=i if spans else 0,
        context_span=spans[i] if spans else None, model=models[i] if models else "default") for i in range(n)]
    return GlobalHeadingInference([block])._emission(_Site(1, block, votes=votes))


def test_repeated_level_votes_have_bounded_gain():
    one, many = _emission_votes(1), _emission_votes(50)
    prior_ratio = .25 / .42
    assert math.log((many["L2"] / many["L1"]) / prior_ratio) <= 2 * math.log((one["L2"] / one["L1"]) / prior_ratio) + 1e-8


def test_real_overlapping_windows_are_correlated_but_different_models_are_not():
    overlapping = _emission_votes(2, [(0, 100), (10, 110)])
    disjoint = _emission_votes(2, [(0, 100), (100, 200)])
    independent = _emission_votes(2, [(0, 100), (10, 110)], ["a", "b"])
    assert overlapping["L2"] / overlapping["L1"] < disjoint["L2"] / disjoint["L1"]
    assert independent == disjoint


def test_router_stamps_real_context_after_checkpoint_recovery():
    router = object.__new__(LLMRouter)
    router._client = SimpleNamespace(model="test-model")
    result = LLMRouterOutput(chapters=[ChapterNode(block_id=55, title="Heading", level=1)])
    router._stamp_observation(result, "[10] context\n[55] Heading\n[90 to 99] <Gap>")
    chapter = result.chapters[0]
    assert chapter._observation_context == (10, 100)
    assert chapter._observation_model == "test-model"
    assert "_observation_context" not in chapter.model_dump()
    assert result._observed_block_ids == {10, 55}


def _scanned():
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2))
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def test_ocr_text_enters_blocks_and_atoms_not_just_warning():
    engine = SimpleNamespace(recognize_page=lambda *a, **kw: {"text": "OCR_MARKER\n\nSecond paragraph", "confidence": .99})
    provider = PdfProvider(ocr_engine=engine)
    blocks = provider.extract_from_bytes(_scanned())
    assert sum("OCR_MARKER" in (b.text or "") for b in blocks) == 1
    text = [b for b in blocks if b.type == "text"]
    assert any("Second paragraph" in b.text for b in text)
    assert all(b.metadata["text_fidelity"] == "inferred" for b in text)
    assert all(b.metadata["atoms"][0]["block_id"] == b.id for b in text)
    assert all(b.metadata["page"] == 1 for b in text)
    from infrastructure.models import StructuralAtom
    assert all(StructuralAtom.model_validate(b.metadata["atoms"][0]).page == 1 for b in text)
    assert any(s.code == "pdf_page_ocr_applied" for s in provider.drain_skips())


@pytest.mark.parametrize("result", [{"text": ""}, {"text": "x", "confidence": float("nan")}, {"text": 123}, {"blocks": "invalid"}])
def test_invalid_ocr_is_not_reported_as_applied(result):
    provider = PdfProvider(ocr_engine=SimpleNamespace(recognize_page=lambda *a, **kw: result))
    provider.extract_from_bytes(_scanned())
    codes = [s.code for s in provider.drain_skips()]
    assert "pdf_page_ocr_failed" in codes
    assert "pdf_page_ocr_applied" not in codes


def test_ancestor_search_corrects_numbered_root_without_fabricating_titles():
    blocks = [Block(id=i, type="text", text=t) for i, t in enumerate(["1 Root", "1.1 Child", "2 Next Root"])]
    emissions = [{"NONE": .01, **{f"L{i}": .001 for i in range(1, 7)}} for _ in blocks]
    emissions[0]["L1"] = .99
    emissions[1].update(L1=.1, L2=.9)
    emissions[2].update(L1=.55, L2=.95)
    decoder = GlobalHeadingInference(blocks)
    assert decoder._viterbi(emissions) == ["L1", "L2", "L2"]
    assert decoder._decode_with_ancestors([_Site(b.id, b) for b in blocks], emissions) == ["L1", "L2", "L1"]


def _block(bid, text="", kind="text", bbox=None, role=None, page=0):
    return Block(id=bid, type=kind, text=text, metadata={"page": page, "bbox": bbox or [0, 0, 100, 20], "source_role": role})


def test_caption_and_footnote_relations_preserve_source_sequence():
    blocks = [_block(0, "Body reference [1]"), _block(1, kind="image", bbox=[0, 50, 100, 100]),
              _block(2, "Figure 1: Example", bbox=[0, 102, 100, 112]), _block(3, "1 Explanation", role="footnote")]
    annotate_content_relations(blocks)
    assert [b.id for b in blocks] == [0, 1, 2, 3]
    assert blocks[2].metadata["content_relations"][0]["target_block_id"] == 1
    assert blocks[3].metadata["content_relations"][0]["target_block_id"] == 0


def test_ambiguous_relation_remains_unresolved_and_never_crosses_pages():
    blocks = [_block(0, "Ref [1]"), _block(1, "Also [1]"), _block(2, "1 Note", role="footnote"),
              _block(3, "1 Other note", role="footnote", page=1)]
    annotate_content_relations(blocks)
    assert blocks[2].metadata["content_relations"][0]["status"] == "needs_confirmation"
    assert blocks[3].metadata["content_relations"][0]["target_block_id"] is None


def test_footnotes_and_sidebars_are_delivered_not_silently_excluded():
    from infrastructure.providers.pdf_page_roles import split_canonical_and_excluded
    from infrastructure.blocks import compact_block
    from modules.parser.heading_candidates import generate_heading_candidate_set
    blocks = [_block(0, "Main text [1]"), _block(1, "1 Important note", role="footnote"),
              _block(2, "Side explanation", role="marginal"), _block(3, "Page 1", role="footer")]
    canonical, excluded = split_canonical_and_excluded(blocks)
    assert [b.id for b in canonical] == [0, 1, 2]
    assert [b.id for b in excluded] == [3]
    annotate_content_relations(canonical)
    assert compact_block(canonical[1]).metadata["content_relations"]
    candidates = generate_heading_candidate_set(canonical)
    assert all("margin_negative" in c.evidence_kinds(polarity=-1) for c in candidates.candidates if c.block_id in {1, 2})


def test_relations_survive_tree_export_and_cross_section_attachment():
    from modules.parser.resolver import IntervalResolver
    from modules.parser.document_tree import DocumentTree
    blocks = [_block(0, "1 First"), _block(1, "Body reference [1]"),
              _block(2, "2 Second"), _block(3, "1 Note for first section", role="footnote")]
    for i in [0, 2]:
        blocks[i].is_heading_style, blocks[i].heading_level = True, 1
    annotate_content_relations(blocks)
    resolver = IntervalResolver(blocks)
    tree = DocumentTree(resolver.resolve([ChapterNode(block_id=i, title=blocks[i].text, level=1) for i in [0, 2]]))
    nodes = tree.to_dict()
    relations = [r for n in nodes for r in n["content_relations"]]
    assert relations and relations[0]["target_section_start_block_id"] == 0
    assert "Note for first section" in tree.to_markdown()
    assert json.loads(tree.to_json())["quality_contract"]["confidence_is_accuracy"] is False


def test_real_data_alignment_audit_is_independent_of_candidates():
    from evaluation.algorithm_acceptance import alignment_snapshot
    blocks = [Block(id=5, type="text", text="1 Real title"),
              Block(id=6, type="text", text="2.1 Inline title. Remaining body text")]
    snapshot, audit = alignment_snapshot(blocks, [
        {"block_id": 99, "title": "Real title", "level": 1},
        {"block_id": -1, "title": "2.1 Inline title", "level": 2, "granularity": "run-in"}])
    assert [h.block_id for h in snapshot] == [5, 6]
    assert audit[1]["status"] == "run_in_prefix_suggestion"


def test_numbered_wrong_actual_parent_is_visible_even_with_legal_levels():
    from modules.parser.hierarchy import evaluate_soft_constraints
    chapters = [ChapterNode(block_id=0, title="**1 First**", level=1),
                ChapterNode(block_id=2, title="**2.1 Misplaced child**", level=2)]
    report = evaluate_soft_constraints(chapters)
    finding = report.by_kind("numbering_actual_parent")[0]
    assert finding.block_ids == (0, 2)


def test_usage_tracking_restores_outer_request():
    from infrastructure.ai.usage import UsageAccumulator, use_usage_accumulator, record_llm_usage
    outer, inner = UsageAccumulator(), UsageAccumulator()
    with use_usage_accumulator(outer):
        with pytest.raises(RuntimeError), use_usage_accumulator(inner):
            record_llm_usage("test", 2, 3)
            raise RuntimeError("test")
        record_llm_usage("test", 7, 8)
    assert inner.snapshot()["total_tokens"] == 5
    assert outer.snapshot()["total_tokens"] == 15


def test_toc_same_title_figure_label_does_not_steal_numbered_heading():
    label = _block(0, "Attention")
    heading = _block(1, "3.2 Attention")
    heading.is_bold = True
    entries = [(0, 0, 20, 0, label), (0, 0, 100, 0, heading)]
    PdfProvider()._apply_toc_metadata(entries, [{"title": "Attention", "page": 1,
        "level": 2, "destination": {"page": 1}, "index": 0}])
    assert not label.metadata.get("toc_destination")
    assert heading.metadata["toc_level"] == 2


def test_front_matter_is_preserved_but_not_promoted_by_model():
    from modules.parser.resolver import IntervalResolver
    from modules.parser.document_tree import DocumentTree
    from modules.parser.heading_candidates import generate_heading_candidate_set
    blocks = [_block(0, "Cover Title"), _block(1, "Author Name"), _block(2, "Abstract"), _block(3, "Body text.")]
    blocks[2].is_heading_style = True
    entries = [(0, 0, i * 20, 0, b) for i, b in enumerate(blocks)]
    PdfProvider._mark_front_matter(entries)
    assert blocks[0].metadata["semantic_role"] == "front_matter"
    candidate_set = generate_heading_candidate_set(blocks)
    resolver = IntervalResolver(blocks, heading_candidates=candidate_set)
    nodes = resolver.resolve([ChapterNode(block_id=i, title=blocks[i].text, level=1) for i in [0, 1, 2]])
    assert all(n.start_block_id >= 2 for n in nodes)
    preamble = "\n".join(b.to_markdown() for b in blocks[:2])
    assert "Cover Title" in DocumentTree(nodes, preamble_content=preamble).to_markdown()


def test_isotonic_calibration_handles_ties_and_is_domain_scoped():
    curve = CalibrationMap.fit([.1, .1, .3, .8], [True, False, False, True])
    assert list(curve.probabilities) == sorted(curve.probabilities)
    artifact = {"domain": "docx:deterministic", "channels": {"heading": {"upper_bounds": list(curve.upper_bounds), "probabilities": list(curve.probabilities)}}}
    assert calibrated_score(artifact, "heading", .9, "pdf:model")["probability"] is None
    assert calibrated_score(artifact, "heading", .9, "docx:deterministic")["quality"] == "calibrated_on_holdout"


@pytest.mark.parametrize("failure", ["overlap", "stale", "nonmonotonic"])
def test_invalid_calibration_artifact_rejected(tmp_path, failure):
    data = {"version": 1, "train_documents": ["train"], "validation_documents": ["test"],
            "domain": "docx:deterministic", "pipeline_fingerprint": pipeline_fingerprint(),
            "channels": {"heading": {"upper_bounds": [.2, .9], "probabilities": [.1, .8]}}}
    if failure == "overlap":
        data["validation_documents"] = ["train"]
    elif failure == "stale":
        data["pipeline_fingerprint"] = "old"
    else:
        data["channels"]["heading"]["probabilities"] = [.9, .1]
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load_calibration(str(path))
