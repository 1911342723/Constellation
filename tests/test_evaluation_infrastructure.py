from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evaluation import paper_experiments
from evaluation import run_evaluation as evaluation_runner
from evaluation.metrics import (
    EvalResult,
    HeadingGT,
    HeadingPred,
    _compute_parent_relation_distance,
    compute_section_f1,
)
from infrastructure.models import Block
from modules.parser.document_tree import DocumentTree
from modules.parser.schemas import DocumentNode


def test_section_matching_is_global_maximum_not_gt_order_greedy(monkeypatch):
    """GT A greedily wants P0, but global matching reserves P0 for GT B."""
    from modules.parser import resolver

    similarities = {
        ("a", "p0"): 0.95,
        ("a", "p1"): 0.80,
        ("b", "p0"): 0.85,
        ("b", "p1"): 0.10,
    }
    monkeypatch.setattr(
        resolver,
        "_levenshtein_ratio",
        lambda left, right: similarities[(left, right)],
    )
    gt = [HeadingGT(0, "A", 1), HeadingGT(1, "B", 1)]
    pred = [HeadingPred(0, "P0", 1), HeadingPred(1, "P1", 1)]

    result = compute_section_f1(
        gt, pred, block_id_tolerance=1, title_sim_threshold=0.6,
    )

    assert result.tp == 2
    assert [(pair[0].title, pair[1].title) for pair in result.tp_pairs] == [
        ("A", "P1"), ("B", "P0"),
    ]
    assert len(result.matching_details) == 2


def test_all_gt_hierarchy_metrics_count_false_negatives_as_wrong():
    gt = [HeadingGT(0, "A", 1), HeadingGT(10, "B", 2)]
    pred = [HeadingPred(0, "A", 1)]

    result = compute_section_f1(gt, pred, block_id_tolerance=0)

    assert result.hierarchy_accuracy == 1.0  # compatibility: TP-only
    assert result.hierarchy_accuracy_tp_only == 1.0
    assert result.all_gt_level_accuracy == 0.5
    assert result.all_gt_level_recall == 0.5
    assert result.all_gt_level_precision == 1.0
    assert result.all_gt_level_f1 == pytest.approx(2 / 3)


def test_parent_relation_distance_detects_reparenting():
    gt = [
        HeadingGT(0, "Parent", 1),
        HeadingGT(1, "Child", 2),
    ]
    pred = [
        HeadingPred(0, "Parent", 1),
        HeadingPred(1, "Child", 1),
    ]

    assert _compute_parent_relation_distance(gt, pred) == 2.0
    result = compute_section_f1(gt, pred, block_id_tolerance=0)
    assert result.parent_relation_distance == 2.0
    assert result.tree_edit_distance == result.parent_relation_distance


def test_evaluate_with_repeats_records_failures_and_excludes_them(monkeypatch, tmp_path):
    attempts = iter([
        (EvalResult(f1=0.5, precision=0.5), 1.0),
        RuntimeError("gateway down"),
        (EvalResult(f1=1.0, precision=1.0), 3.0),
    ])

    def fake_evaluate(*_args, **_kwargs):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(evaluation_runner, "evaluate_single_doc", fake_evaluate)
    summary, results = evaluation_runner.evaluate_with_repeats(
        tmp_path / "doc.docx", tmp_path / "doc.json", num_runs=3,
    )

    assert len(results) == 2
    assert summary["successful_runs"] == 2
    assert summary["failed_runs"] == 1
    assert summary["status"] == "partial"
    assert summary["f1_mean"] == 0.75
    assert summary["time_mean"] == 2.0
    assert [record["status"] for record in summary["runs"]] == [
        "success", "error", "success",
    ]
    assert "gateway down" in summary["runs"][1]["error"]
    assert summary["token_accounting_kind"] == "unavailable"


class _FakeProvider:
    def extract(self, _path):
        return [Block(id=0, type="text", text="A", is_heading_style=True, heading_level=1)]


class _FakeCompressor:
    def __init__(self, config=None):
        self.config = config

    def compress(self, _blocks):
        return ["[BLOCK_ID:0] A"]


class _RepeatParser:
    def __init__(self):
        from modules.parser.config import CompressorConfig, ParserConfig, ResolverConfig

        self._compressor_config = CompressorConfig()
        self._resolver_config = ResolverConfig()
        self._parser_config = ParserConfig()
        self.calls = 0

    def clear_cache(self):
        return None

    def parse_with_timing(self, _blocks):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("transient")
        f1_title = "A" if self.calls == 1 else "wrong"
        tree = DocumentTree(nodes=[DocumentNode(
            title=f1_title,
            level=1,
            start_block_id=0,
            end_block_id=0,
            content="A",
        )])
        timing = SimpleNamespace(
            stage2_compress=0.5,
            stage3_route=float(self.calls),
            stage4_resolve=1.0,
            total=float(self.calls),
            skeleton_chars=123,
            window_count=1,
            heading_count=1,
            candidate_count=1,
        )
        return tree, timing


def test_paper_experiment_runs_every_round_and_uses_success_denominator(monkeypatch, tmp_path):
    monkeypatch.setattr(paper_experiments, "DocxProvider", _FakeProvider)
    monkeypatch.setattr(paper_experiments, "SkeletonCompressor", _FakeCompressor)
    monkeypatch.setattr(paper_experiments, "generate_heading_candidates", lambda _blocks: [])
    gt_path = tmp_path / "doc.json"
    gt_path.write_text(json.dumps({
        "headings": [{"block_id": 0, "title": "A", "level": 1}],
    }), encoding="utf-8")
    parser = _RepeatParser()

    result = paper_experiments.evaluate_document(
        str(tmp_path / "doc.docx"), str(gt_path), parser, num_runs=3,
    )

    assert parser.calls == 3
    assert result.successful_runs == 2
    assert result.failed_runs == 1
    assert result.status == "partial"
    assert [record["status"] for record in result.run_records] == [
        "success", "error", "success",
    ]
    # Successful F1 values are 1 and 0; failed run is excluded.
    assert result.section_f1 == 0.5
    assert result.total_time == 2.0
    assert result.aggregate_std["section_f1"] == pytest.approx(2 ** -0.5)
    assert result.token_accounting_kind == "estimated"
    assert "not provider-reported" in result.token_accounting["method"]


def test_paper_raw_json_contains_config_provenance_matching_and_token_kind(tmp_path):
    document = paper_experiments.DocumentResult(
        name="doc",
        status="success",
        requested_runs=1,
        successful_runs=1,
        matching_details=[{"run_index": 1, "matches": []}],
        run_records=[{
            "run_index": 1,
            "status": "success",
            "matching_details": [],
            "token_accounting_kind": "estimated",
        }],
    )
    baseline = paper_experiments.ExperimentResults(
        documents=[document], config_name="Full", config={"parser": {}},
    )
    raw_path = tmp_path / "raw.json"

    paper_experiments.write_raw_results(
        baseline, {}, str(raw_path), num_runs=1, stage1_only=False,
    )
    payload = json.loads(raw_path.read_text(encoding="utf-8"))

    assert payload["config"]["num_runs"] == 1
    assert payload["provenance"]["runner"] == "evaluation.paper_experiments"
    assert payload["token_accounting_kind"] == "estimated"
    raw_doc = payload["baseline"]["documents"][0]
    assert "matching_details" in raw_doc
    assert raw_doc["run_records"][0]["token_accounting_kind"] == "estimated"



def test_standard_runner_writes_auditable_raw_json(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    gt_dir = tmp_path / "gt"
    data_dir.mkdir()
    gt_dir.mkdir()
    (data_dir / "doc.docx").write_bytes(b"placeholder")
    (gt_dir / "doc.json").write_text('{"headings": []}', encoding="utf-8")

    metric = EvalResult(f1=1.0, precision=1.0, recall=1.0)
    metric.matching_details = [{"gt_index": 0, "pred_index": 0}]
    monkeypatch.setattr(
        evaluation_runner,
        "evaluate_single_doc",
        lambda *_args, **_kwargs: (metric, 0.25),
    )
    output_path = tmp_path / "report.md"
    raw_path = tmp_path / "report_raw.json"

    evaluation_runner.run_evaluation(
        data_dir,
        gt_dir,
        output_path=output_path,
        raw_output_path=raw_path,
        num_runs=2,
    )
    payload = json.loads(raw_path.read_text(encoding="utf-8"))

    assert payload["config"]["algorithm"]["parser"]["strict_first_routing"] is True
    assert payload["provenance"]["runner"] == "evaluation.run_evaluation"
    assert payload["token_accounting_kind"] == "unavailable"
    document = payload["documents"][0]
    assert document["successful_runs"] == 2
    assert document["matching_details"][0]["matches"]
    assert all(run["token_accounting_kind"] == "unavailable" for run in document["runs"])



def test_paper_report_keeps_error_and_latex_tables(tmp_path):
    document = paper_experiments.DocumentResult(
        name="doc",
        status="success",
        requested_runs=1,
        successful_runs=1,
        section_f1=1.0,
    )
    baseline = paper_experiments.ExperimentResults(documents=[document])
    output = tmp_path / "paper.md"

    report = paper_experiments.generate_report(baseline, {}, str(output))

    assert "## Table 4: Error Analysis" in report
    assert "## LaTeX Table 1" in report
    assert "\\begin{table*}" in report


def test_paper_setup_failure_marks_token_accounting_unavailable(monkeypatch, tmp_path):
    class FailingProvider:
        def extract(self, _path):
            raise RuntimeError("cannot extract")

    monkeypatch.setattr(paper_experiments, "DocxProvider", FailingProvider)

    result = paper_experiments.evaluate_document(
        str(tmp_path / "doc.docx"), None, object(), num_runs=2,
    )

    assert result.status == "error"
    assert result.successful_runs == 0
    assert result.failed_runs == 2
    assert result.token_accounting_kind == "unavailable"
    assert result.token_accounting == {
        "kind": "unavailable",
        "reason": "stage1_or_stage2_failed_before_estimation",
    }
    assert result.run_records[0]["token_accounting_kind"] == "unavailable"
    assert result.run_records[0]["token_accounting"] == result.token_accounting


def test_paper_experiment_aggregates_real_stage2_timing_and_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(paper_experiments, "DocxProvider", _FakeProvider)
    monkeypatch.setattr(paper_experiments, "SkeletonCompressor", _FakeCompressor)
    monkeypatch.setattr(paper_experiments, "generate_heading_candidates", lambda _blocks: [])
    gt_path = tmp_path / "doc.json"
    gt_path.write_text('{"headings": []}', encoding="utf-8")

    result = paper_experiments.evaluate_document(
        str(tmp_path / "doc.docx"), str(gt_path), _RepeatParser(), num_runs=1,
    )

    assert result.stage2_time == 0.5
    assert result.skeleton_chars == 123
    assert result.window_count == 1
    assert result.aggregate_std["stage2_time"] == 0.0
    assert result.aggregate_std["skeleton_chars"] == 0.0


def test_paper_char_recall_is_not_markdown_length_coverage(monkeypatch, tmp_path):
    class ContentProvider:
        def extract(self, _path):
            return [Block(id=0, type="text", text="abcdefghij")]

    class ContentParser(_RepeatParser):
        def parse_with_timing(self, _blocks):
            self.calls += 1
            tree = DocumentTree(nodes=[DocumentNode(
                title="A heading that adds Markdown characters",
                level=1,
                start_block_id=0,
                end_block_id=0,
                content="a",
            )])
            timing = SimpleNamespace(
                stage2_compress=0.1,
                stage3_route=0.1,
                stage4_resolve=0.1,
                total=0.3,
                skeleton_chars=10,
                window_count=1,
                heading_count=1,
                candidate_count=0,
            )
            return tree, timing

    monkeypatch.setattr(paper_experiments, "DocxProvider", ContentProvider)
    monkeypatch.setattr(paper_experiments, "SkeletonCompressor", _FakeCompressor)
    monkeypatch.setattr(paper_experiments, "generate_heading_candidates", lambda _blocks: [])

    result = paper_experiments.evaluate_document(
        str(tmp_path / "content.docx"), None, ContentParser(), num_runs=1,
    )

    assert result.char_recall == pytest.approx(0.1)
    assert result.markdown_char_coverage == 1.0


def test_paper_heading_extraction_ignores_lossless_pseudo_root():
    pseudo_root = DocumentNode(
        title="Document",
        level=1,
        start_block_id=0,
        end_block_id=0,
        content="body",
        section_type="section",
    )

    assert paper_experiments._collect_pred_headings([pseudo_root]) == []



def test_paper_stage2_prepass_uses_production_candidate_aware_contract(monkeypatch, tmp_path):
    events: list[str] = []

    class ProductionShapeParser(_RepeatParser):
        def __init__(self):
            super().__init__()
            self.router = SimpleNamespace(fit_skeleton_chunks=self._fit)

        def _candidate_views(self, _blocks):
            events.append("candidate-set")
            return object(), [object()]

        def _compress_candidate_aware(self, _blocks, _candidate_set, route_candidates):
            events.append("compress-sparse")
            assert len(route_candidates) == 1
            return ["candidate-aware-skeleton"]

        def _fit(self, chunks, candidates=None):
            events.append("budget-shard")
            assert len(candidates) == 1
            return chunks

        def parse_with_timing(self, blocks):
            events.append("parse")
            return super().parse_with_timing(blocks)

    class ForbiddenStandaloneCompressor:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("production prepass must not instantiate standalone compressor")

    monkeypatch.setattr(paper_experiments, "DocxProvider", _FakeProvider)
    monkeypatch.setattr(
        paper_experiments, "SkeletonCompressor", ForbiddenStandaloneCompressor,
    )
    parser = ProductionShapeParser()

    result = paper_experiments.evaluate_document(
        str(tmp_path / "doc.docx"), None, parser, num_runs=1,
    )

    assert result.status == "success"
    assert events[:4] == [
        "candidate-set", "compress-sparse", "budget-shard", "parse",
    ]



def test_paper_ablation_configs_do_not_report_dead_legacy_knobs():
    experiments = paper_experiments.run_ablation([], "unused", num_runs=1)

    assert "No Fuzzy (radius=0)" not in experiments
    assert "No Font Cross-Val" not in experiments
    assert "No RLE" not in experiments
    assert experiments["No Sparse Skeleton"].config["compressor"][
        "enable_candidate_sparse"
    ] is False
    assert experiments["No Strict Risk Validation"].config["parser"][
        "strict_first_routing"
    ] is False
