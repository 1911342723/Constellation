"""Unified paper experiment framework for Constellation.

Every requested LLM run is executed.  Raw output records successful and failed
runs separately; aggregate metrics use successful runs only.  Token counts in
this module are text-based estimates and are never labelled as provider usage.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation.metrics import (
    HeadingGT,
    HeadingPred,
    compute_block_coverage,
    compute_char_recall,
    compute_markdown_char_coverage,
    compute_section_f1,
)
from infrastructure.providers import DocxProvider
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig, ParserConfig, ResolverConfig
from modules.parser.document_tree import content_text_length
from modules.parser.heading_candidates import generate_heading_candidates
from modules.parser.parser import CaliperParser

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass
class DocumentResult:
    """Aggregated successful-run results and full per-run audit data."""

    name: str
    status: str = "pending"
    error: str | None = None
    requested_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    run_records: list[dict[str, Any]] = field(default_factory=list)
    aggregate_std: dict[str, float] = field(default_factory=dict)

    block_count: int = 0
    original_chars: int = 0
    stage1_time: float = 0.0
    skeleton_chars: float = 0.0
    compression_ratio: float = 0.0
    window_count: float = 0.0
    candidate_count: float = 0.0
    stage2_time: float = 0.0

    heading_count: float = 0.0
    section_f1: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    hierarchy_accuracy: float = 0.0  # TP-only compatibility metric
    all_gt_level_accuracy: float = 0.0
    all_gt_level_f1: float = 0.0
    parent_relation_distance: float = 0.0
    heading_sequence_edit_distance: float = 0.0
    ted: float = 0.0  # compatibility alias: parent-relation distance
    char_recall: float = 0.0
    block_coverage: float = 0.0
    markdown_char_coverage: float = 0.0
    stage3_time: float = 0.0
    stage4_time: float = 0.0
    total_time: float = 0.0

    skeleton_tokens: int = 0
    full_text_tokens: int = 0
    token_savings_pct: float = 0.0
    token_accounting_kind: str = "estimated"
    token_accounting: dict[str, Any] = field(default_factory=dict)

    false_positives: float = 0.0
    false_negatives: float = 0.0
    level_errors: float = 0.0
    matching_details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExperimentResults:
    """Results across documents for one algorithm configuration."""

    documents: List[DocumentResult] = field(default_factory=list)
    config_name: str = ""
    config: dict[str, Any] = field(default_factory=dict)


def estimate_tokens(text: str) -> int:
    """Deterministic rough estimate (~4 Latin chars or ~2 CJK chars/token)."""
    if not text:
        return 0
    cjk_chars = sum(1 for char in text if "一" <= char <= "鿿")
    latin_chars = len(text) - cjk_chars
    return (latin_chars // 4) + (cjk_chars // 2)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _parser_snapshot(parser: CaliperParser) -> dict[str, Any]:
    """Return algorithm and non-secret model provenance."""
    client = getattr(getattr(parser, "router", None), "_client", None)
    return {
        "compressor": parser._compressor_config.model_dump(mode="json"),
        "resolver": parser._resolver_config.model_dump(mode="json"),
        "parser": parser._parser_config.model_dump(mode="json"),
        "llm": {
            "model": getattr(client, "model", ""),
            "max_input_tokens": getattr(client, "max_input_tokens", None),
            "input_token_safety_margin": getattr(client, "input_token_safety_margin", None),
        },
    }


def _collect_pred_headings(nodes) -> list[HeadingPred]:
    headings: list[HeadingPred] = []
    for node in nodes:
        is_virtual_document = (
            node.title == "Document"
            and node.start_block_id == 0
            and node.section_type == "section"
            and not node.children
        )
        if not is_virtual_document:
            headings.append(HeadingPred(
                block_id=node.start_block_id, title=node.title, level=node.level,
            ))
        headings.extend(_collect_pred_headings(node.children))
    return headings


def _collect_covered_ids(nodes, accumulator: set[int]) -> None:
    for node in nodes:
        accumulator.update(range(node.start_block_id, node.end_block_id + 1))
        _collect_covered_ids(node.children, accumulator)


def evaluate_document(
    docx_path: str,
    gt_path: Optional[str],
    parser: CaliperParser,
    num_runs: int = 1,
    stage1_only: bool = False,
) -> DocumentResult:
    """Evaluate one document and aggregate only successful full-pipeline runs."""
    if num_runs < 1:
        raise ValueError("num_runs must be at least 1")
    name = os.path.splitext(os.path.basename(docx_path))[0]
    result = DocumentResult(name=name, requested_runs=0 if stage1_only else num_runs)

    try:
        extension = os.path.splitext(docx_path)[1].lower()
        if extension == ".pdf":
            from infrastructure.providers.pdf_provider import PdfProvider
            provider = PdfProvider()
        else:
            provider = DocxProvider()
        started = time.perf_counter()
        blocks = provider.extract(docx_path)
        result.stage1_time = time.perf_counter() - started
        result.block_count = len(blocks)
        result.original_chars = sum(len(block.text or "") for block in blocks)
        original_markdown_chars = sum(len(block.to_markdown()) for block in blocks)
        original_content_chars = sum(
            content_text_length(block.to_markdown()) for block in blocks
        )

        # Measure the exact Stage-2 request shape used by the production
        # parser: one CandidateSet, candidate-aware compression, then final
        # input-budget sharding.  The compatibility branch supports the small
        # parser doubles used by evaluation infrastructure tests.
        started = time.perf_counter()
        if all(hasattr(parser, name) for name in (
            "_candidate_views", "_compress_candidate_aware", "router",
        )):
            candidate_set, route_candidates = parser._candidate_views(blocks)
            skeleton_chunks = parser._compress_candidate_aware(
                blocks, candidate_set, route_candidates,
            )
            skeleton_chunks = parser.router.fit_skeleton_chunks(
                skeleton_chunks,
                candidates=(
                    route_candidates
                    if parser._parser_config.enable_heading_candidates else None
                ),
            )
        else:
            compressor = SkeletonCompressor(config=parser._compressor_config)
            skeleton_chunks = compressor.compress(blocks)
            route_candidates = generate_heading_candidates(blocks)
        result.stage2_time = time.perf_counter() - started
        result.skeleton_chars = sum(len(chunk) for chunk in skeleton_chunks)
        result.window_count = len(skeleton_chunks)
        result.candidate_count = float(len(route_candidates))
        result.compression_ratio = (
            1 - result.skeleton_chars / max(result.original_chars, 1)
        ) * 100
        result.skeleton_tokens = estimate_tokens("\n".join(skeleton_chunks))
        result.full_text_tokens = estimate_tokens(
            "\n".join(block.text or "" for block in blocks)
        )
        result.token_savings_pct = (
            1 - result.skeleton_tokens / max(result.full_text_tokens, 1)
        ) * 100
        result.token_accounting = {
            "kind": "estimated",
            "method": "character heuristic; not provider-reported API usage",
            "skeleton_input_estimate": result.skeleton_tokens,
            "full_text_estimate": result.full_text_tokens,
        }
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        result.failed_runs = result.requested_runs
        # No estimate exists when extraction/compression fails before the
        # accounting fields above are populated.  Do not leave the dataclass
        # default ("estimated") behind, because that would make the document
        # record contradict its failed-run record.
        result.token_accounting_kind = "unavailable"
        result.token_accounting = {
            "kind": "unavailable",
            "reason": "stage1_or_stage2_failed_before_estimation",
        }
        result.run_records.append({
            "run_index": None,
            "status": "error",
            "error": result.error,
            "phase": "stage1_or_stage2",
            "matching_details": [],
            "token_accounting_kind": "unavailable",
            "token_accounting": result.token_accounting,
        })
        return result

    if stage1_only:
        result.status = "success"
        return result

    try:
        gt_headings: list[HeadingGT] = []
        if gt_path and os.path.exists(gt_path):
            with open(gt_path, encoding="utf-8") as file:
                gt_data = json.load(file)
            gt_headings = [
                HeadingGT(block_id=h["block_id"], title=h["title"], level=h["level"])
                for h in gt_data.get("headings", [])
            ]
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        result.failed_runs = num_runs
        result.run_records.append({
            "run_index": None,
            "status": "error",
            "error": result.error,
            "phase": "ground_truth",
            "matching_details": [],
            "token_accounting_kind": "estimated",
        })
        return result

    successful_payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    for run_index in range(1, num_runs + 1):
        try:
            parser.clear_cache()
            tree, timings = parser.parse_with_timing(blocks)
            rendered_markdown = tree.to_markdown()
            markdown_coverage = compute_markdown_char_coverage(
                original_markdown_chars, len(rendered_markdown),
            )
            covered_ids: set[int] = set()
            _collect_covered_ids(tree.nodes, covered_ids)
            if tree.preamble_content and tree.nodes:
                first_start = min(node.start_block_id for node in tree.nodes)
                covered_ids.update(range(0, first_start))
            block_coverage = compute_block_coverage(len(blocks), covered_ids)

            eval_result = (
                compute_section_f1(gt_headings, _collect_pred_headings(tree.nodes))
                if gt_headings else None
            )
            payload = {
                "stage2_time": timings.stage2_compress,
                "stage3_time": timings.stage3_route,
                "stage4_time": timings.stage4_resolve,
                "total_time": timings.total,
                "skeleton_chars": float(timings.skeleton_chars),
                "window_count": float(timings.window_count),
                "heading_count": float(timings.heading_count),
                "candidate_count": float(timings.candidate_count),
                "markdown_char_coverage": markdown_coverage,
                "char_recall": compute_char_recall(
                    original_content_chars,
                    tree.get_stats()["total_content_chars"],
                ),
                "block_coverage": block_coverage,
                "section_f1": eval_result.f1 if eval_result else 0.0,
                "precision": eval_result.precision if eval_result else 0.0,
                "recall": eval_result.recall if eval_result else 0.0,
                "hierarchy_accuracy": eval_result.hierarchy_accuracy if eval_result else 0.0,
                "all_gt_level_accuracy": eval_result.all_gt_level_accuracy if eval_result else 0.0,
                "all_gt_level_f1": eval_result.all_gt_level_f1 if eval_result else 0.0,
                "parent_relation_distance": eval_result.parent_relation_distance if eval_result else 0.0,
                "heading_sequence_edit_distance": eval_result.heading_sequence_edit_distance if eval_result else 0.0,
                "ted": eval_result.tree_edit_distance if eval_result else 0.0,
                "false_positives": float(eval_result.fp) if eval_result else 0.0,
                "false_negatives": float(eval_result.fn) if eval_result else 0.0,
                "level_errors": (
                    float(eval_result.level_total - eval_result.level_correct)
                    if eval_result else 0.0
                ),
            }
            matching_details = eval_result.matching_details if eval_result else []
            false_positive_details = (
                [asdict(item) for item in eval_result.fp_preds] if eval_result else []
            )
            false_negative_details = (
                [asdict(item) for item in eval_result.fn_gts] if eval_result else []
            )
            successful_payloads.append(payload)
            result.run_records.append({
                "run_index": run_index,
                "status": "success",
                "error": None,
                "metrics": payload,
                "matching_details": matching_details,
                "false_positive_details": false_positive_details,
                "false_negative_details": false_negative_details,
                "token_accounting_kind": "estimated",
                "token_accounting": result.token_accounting,
            })
            result.matching_details.append({
                "run_index": run_index, "matches": matching_details,
            })
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("[Eval] Run %d failed for %s: %s", run_index, name, exc)
            errors.append(error)
            result.run_records.append({
                "run_index": run_index,
                "status": "error",
                "error": error,
                "metrics": None,
                "matching_details": [],
                "token_accounting_kind": "estimated",
                "token_accounting": result.token_accounting,
            })

    result.successful_runs = len(successful_payloads)
    result.failed_runs = num_runs - result.successful_runs
    result.status = (
        "success" if result.successful_runs == num_runs
        else "partial" if result.successful_runs else "error"
    )
    result.error = "; ".join(errors) if errors else None

    aggregate_fields = (
        "stage2_time", "stage3_time", "stage4_time", "total_time",
        "skeleton_chars", "window_count", "heading_count",
        "candidate_count", "markdown_char_coverage", "char_recall",
        "block_coverage", "section_f1", "precision", "recall",
        "hierarchy_accuracy", "all_gt_level_accuracy", "all_gt_level_f1",
        "parent_relation_distance", "heading_sequence_edit_distance", "ted",
        "false_positives", "false_negatives", "level_errors",
    )
    for field_name in aggregate_fields:
        successful_values = [
            float(payload[field_name]) for payload in successful_payloads
        ]
        setattr(result, field_name, _mean(successful_values))
        result.aggregate_std[field_name] = _std(successful_values)
    return result


def run_ablation(
    docx_paths: List[str], gt_dir: str, num_runs: int = 1,
) -> Dict[str, ExperimentResults]:
    """Run ablations, preserving the requested repeat count for every config."""
    configs = {
        "Full System": {
            "compressor": CompressorConfig(), "resolver": ResolverConfig(),
            "parser": ParserConfig(enable_speculative_execution=True),
        },
        "No Sparse Skeleton": {
            "compressor": CompressorConfig(enable_candidate_sparse=False),
            "resolver": ResolverConfig(),
            "parser": ParserConfig(enable_speculative_execution=True),
        },
        "No Candidate Router": {
            "compressor": CompressorConfig(), "resolver": ResolverConfig(),
            "parser": ParserConfig(enable_speculative_execution=True, enable_heading_candidates=False),
        },
        "No Strict Risk Validation": {
            "compressor": CompressorConfig(), "resolver": ResolverConfig(),
            "parser": ParserConfig(
                enable_speculative_execution=True,
                strict_first_routing=False,
            ),
        },
        "Serial Only": {
            "compressor": CompressorConfig(), "resolver": ResolverConfig(),
            "parser": ParserConfig(enable_speculative_execution=False),
        },
    }

    results: dict[str, ExperimentResults] = {}
    for config_name, config in configs.items():
        CaliperParser.clear_cache()
        parser = CaliperParser(
            compressor_config=config["compressor"],
            resolver_config=config["resolver"],
            parser_config=config["parser"],
        )
        experiment = ExperimentResults(
            config_name=config_name, config=_parser_snapshot(parser),
        )
        for docx_path in docx_paths:
            name = os.path.splitext(os.path.basename(docx_path))[0]
            experiment.documents.append(evaluate_document(
                docx_path, os.path.join(gt_dir, f"{name}.json"), parser,
                num_runs=num_runs,
            ))
        results[config_name] = experiment
        parser.clear_cache()
    return results


def _successful_documents(documents: list[DocumentResult]) -> list[DocumentResult]:
    """Documents eligible for cross-document means."""
    return [document for document in documents if document.successful_runs > 0]


def generate_report(
    baseline: ExperimentResults,
    ablations: Dict[str, ExperimentResults],
    output_path: str,
) -> str:
    """Generate a concise paper-oriented Markdown report."""
    lines = [
        "# Constellation Experimental Results", "",
        "## Table 1: Per-Document Performance", "",
        "| Document | Status | Runs | Blocks | Compress % | Candidates | Headings | F1 | Prec | Rec | Hier Acc (TP-only) | All-GT Level F1 | Parent Rel. Dist. | Block Cov | MD Char Cov | Total(ms) |",
        "|" + "|".join(["---"] * 16) + "|",
    ]
    for document in baseline.documents:
        lines.append(
            f"| {document.name} | {document.status} | {document.successful_runs}/{document.requested_runs} | "
            f"{document.block_count} | {document.compression_ratio:.1f} | {document.candidate_count:.1f} | "
            f"{document.heading_count:.1f} | {document.section_f1:.3f} | {document.precision:.3f} | "
            f"{document.recall:.3f} | {document.hierarchy_accuracy:.3f} | "
            f"{document.all_gt_level_f1:.3f} | {document.parent_relation_distance:.1f} | "
            f"{document.block_coverage:.3f} | {document.markdown_char_coverage:.3f} | "
            f"{document.total_time * 1000:.0f} |"
        )

    successful = _successful_documents(baseline.documents)
    if successful:
        lines.append(
            f"| **Successful-doc average** | — | — | "
            f"{_mean([d.block_count for d in successful]):.0f} | "
            f"{_mean([d.compression_ratio for d in successful]):.1f} | "
            f"{_mean([d.candidate_count for d in successful]):.1f} | "
            f"{_mean([d.heading_count for d in successful]):.1f} | "
            f"{_mean([d.section_f1 for d in successful]):.3f} | "
            f"{_mean([d.precision for d in successful]):.3f} | "
            f"{_mean([d.recall for d in successful]):.3f} | "
            f"{_mean([d.hierarchy_accuracy for d in successful]):.3f} | "
            f"{_mean([d.all_gt_level_f1 for d in successful]):.3f} | "
            f"{_mean([d.parent_relation_distance for d in successful]):.1f} | "
            f"{_mean([d.block_coverage for d in successful]):.3f} | "
            f"{_mean([d.markdown_char_coverage for d in successful]):.3f} | "
            f"{_mean([d.total_time * 1000 for d in successful]):.0f} |"
        )

    lines.extend([
        "", "## Table 2: Ablation Study", "",
        "| Configuration | Successful docs | Avg F1 | Avg Hier Acc (TP-only) | Avg All-GT Level F1 | Avg Parent Rel. Dist. |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name, experiment in ablations.items():
        docs = _successful_documents(experiment.documents)
        lines.append(
            f"| {name} | {len(docs)}/{len(experiment.documents)} | "
            f"{_mean([d.section_f1 for d in docs]):.3f} | "
            f"{_mean([d.hierarchy_accuracy for d in docs]):.3f} | "
            f"{_mean([d.all_gt_level_f1 for d in docs]):.3f} | "
            f"{_mean([d.parent_relation_distance for d in docs]):.1f} |"
        )

    lines.extend([
        "", "## Table 3: Token Efficiency (Estimated)", "",
        "> Token counts use a deterministic character heuristic; they are not provider-reported API usage.", "",
        "| Document | Accounting kind | Full estimate | Skeleton estimate | Savings % |",
        "|---|---|---:|---:|---:|",
    ])
    for document in baseline.documents:
        lines.append(
            f"| {document.name} | {document.token_accounting_kind} | "
            f"{document.full_text_tokens} | {document.skeleton_tokens} | "
            f"{document.token_savings_pct:.1f} |"
        )

    lines.extend([
        "", "## Table 4: Error Analysis", "",
        "| Document | Status | FP | FN | Level Errors | Predicted Headings |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for document in baseline.documents:
        lines.append(
            f"| {document.name} | {document.status} | "
            f"{document.false_positives:.1f} | {document.false_negatives:.1f} | "
            f"{document.level_errors:.1f} | {document.heading_count:.1f} |"
        )

    lines.extend([
        "", "## LaTeX Table 1 (copy-paste)", "", "```latex",
        "\\begin{table*}[t]", "\\centering",
        "\\caption{Per-document performance of Constellation (successful runs only).}",
        "\\label{tab:results}",
        "\\begin{tabular}{l|c|c|c|c|c|c}", "\\hline",
        "Document & Blocks & Compress\\% & Headings & F1 & All-GT Level F1 & Time(ms) \\\\",
        "\\hline",
    ])
    for document in baseline.documents:
        lines.append(
            f"{document.name} & {document.block_count} & {document.compression_ratio:.0f}\\% & "
            f"{document.heading_count:.1f} & {document.section_f1:.3f} & "
            f"{document.all_gt_level_f1:.3f} & {document.total_time * 1000:.0f} \\\\"
        )
    if successful:
        lines.extend([
            "\\hline",
            f"\\textbf{{Average}} & {_mean([d.block_count for d in successful]):.0f} & "
            f"{_mean([d.compression_ratio for d in successful]):.0f}\\% & "
            f"{_mean([d.heading_count for d in successful]):.1f} & "
            f"\\textbf{{{_mean([d.section_f1 for d in successful]):.3f}}} & "
            f"{_mean([d.all_gt_level_f1 for d in successful]):.3f} & "
            f"{_mean([d.total_time * 1000 for d in successful]):.0f} \\\\",
        ])
    lines.extend(["\\hline", "\\end{tabular}", "\\end{table*}", "```"])

    failed_records = [
        (document.name, record)
        for document in baseline.documents
        for record in document.run_records
        if record.get("status") == "error"
    ]
    lines.extend(["", "## Failed Runs", ""])
    if failed_records:
        for name, record in failed_records:
            lines.append(
                f"- {name}, run {record.get('run_index')}: {record.get('error')}"
            )
    else:
        lines.append("No failed runs.")

    report = "\n".join(lines)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    Path(output_path).write_text(report, encoding="utf-8")
    return report


def _provenance() -> dict[str, Any]:
    return {
        "runner": "evaluation.paper_experiments",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
    }


def write_raw_results(
    baseline: ExperimentResults,
    ablations: Dict[str, ExperimentResults],
    output_path: str,
    *,
    num_runs: int,
    stage1_only: bool,
) -> dict[str, Any]:
    """Write complete experiment configuration, provenance and per-run records."""
    payload = {
        "schema_version": 1,
        "config": {
            "num_runs": num_runs,
            "stage1_only": stage1_only,
            "baseline": baseline.config,
            "ablations": {name: experiment.config for name, experiment in ablations.items()},
            "matching": {"block_id_tolerance": 3, "title_similarity_threshold": 0.6},
        },
        "provenance": _provenance(),
        "token_accounting_kind": "estimated",
        "token_accounting_note": "character heuristic; no provider usage was available",
        "baseline": asdict(baseline),
        "ablations": {name: asdict(experiment) for name, experiment in ablations.items()},
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    argument_parser = argparse.ArgumentParser(description="Constellation paper experiment runner")
    argument_parser.add_argument("--data-dir", default="tests/data/benchmarks")
    argument_parser.add_argument("--gt-dir", default="evaluation/ground_truth")
    argument_parser.add_argument("--num-runs", type=int, default=1)
    argument_parser.add_argument("--stage1-only", action="store_true")
    argument_parser.add_argument("--output", default="evaluation/paper_report.md")
    argument_parser.add_argument("--raw-output", default=None)
    args = argument_parser.parse_args()
    if args.num_runs < 1:
        argument_parser.error("--num-runs must be at least 1")

    docx_files = sorted([
        os.path.join(args.data_dir, name)
        for name in os.listdir(args.data_dir)
        if name.endswith((".docx", ".pdf"))
    ])
    if not docx_files:
        print(f"No .docx or .pdf files found in {args.data_dir}")
        return

    baseline_parser = CaliperParser()
    baseline = ExperimentResults(
        config_name="Full System", config=_parser_snapshot(baseline_parser),
    )
    for docx_path in docx_files:
        name = os.path.splitext(os.path.basename(docx_path))[0]
        baseline.documents.append(evaluate_document(
            docx_path,
            os.path.join(args.gt_dir, f"{name}.json"),
            baseline_parser,
            num_runs=args.num_runs,
            stage1_only=args.stage1_only,
        ))
    baseline_parser.clear_cache()

    ablations: dict[str, ExperimentResults] = {}
    if not args.stage1_only:
        ablations = run_ablation(docx_files, args.gt_dir, num_runs=args.num_runs)

    generate_report(baseline, ablations, args.output)
    raw_output = args.raw_output or str(
        Path(args.output).with_name(f"{Path(args.output).stem}_raw.json")
    )
    write_raw_results(
        baseline, ablations, raw_output,
        num_runs=args.num_runs, stage1_only=args.stage1_only,
    )
    print(f"Report written to: {args.output}")
    print(f"Raw results written to: {raw_output}")


if __name__ == "__main__":
    main()
