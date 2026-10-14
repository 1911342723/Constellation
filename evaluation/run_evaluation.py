"""Automated evaluation runner for the Constellation pipeline.

``--num-runs`` performs independent calls.  Every attempt is retained in raw
JSON; failed attempts never enter metric means or standard deviations.
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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation.metrics import (
    EvalResult,
    HeadingGT,
    HeadingPred,
    compute_block_coverage,
    compute_char_recall,
    compute_markdown_char_coverage,
    compute_section_f1,
    format_eval_report,
)
from modules.parser.document_tree import content_text_length

logger = logging.getLogger(__name__)


def load_ground_truth(gt_path: Path) -> dict:
    """Load a ground-truth JSON file."""
    with open(gt_path, encoding="utf-8") as file:
        return json.load(file)


def extract_pred_headings(document_nodes) -> list[HeadingPred]:
    """Flatten a DocumentNode tree into HeadingPred entries."""
    preds: list[HeadingPred] = []

    def walk(nodes):
        for node in nodes:
            is_virtual_document = (
                node.title == "Document"
                and node.start_block_id == 0
                and node.section_type == "section"
                and not node.children
            )
            if not is_virtual_document:
                preds.append(HeadingPred(
                    block_id=node.start_block_id,
                    title=node.title,
                    level=node.level,
                ))
            if node.children:
                walk(node.children)

    walk(document_nodes)
    return preds


def evaluate_single_doc(
    docx_path: Path,
    gt_path: Path,
    *,
    use_async: bool = False,
) -> tuple[EvalResult, float]:
    """Parse one document and return ``(metrics, elapsed_seconds)``."""
    from infrastructure.providers.docx_provider import DocxProvider
    from modules.parser.parser import CaliperParser

    CaliperParser.clear_cache()
    blocks = DocxProvider().extract(str(docx_path))
    if not blocks:
        raise RuntimeError(f"DocxProvider extracted 0 blocks from {docx_path.name}")

    gt_data = load_ground_truth(gt_path)
    gt_headings = [
        HeadingGT(block_id=h["block_id"], title=h["title"], level=h["level"])
        for h in gt_data.get("headings", [])
    ]

    parser = CaliperParser()
    started = time.perf_counter()
    if use_async:
        import asyncio
        tree = asyncio.run(parser.async_parse(blocks))
    else:
        tree = parser.parse(blocks)
    elapsed = time.perf_counter() - started

    result = compute_section_f1(gt_headings, extract_pred_headings(tree.nodes))
    original_chars = sum(content_text_length(block.to_markdown()) for block in blocks)
    result.char_recall = compute_char_recall(
        original_chars, tree.get_stats()["total_content_chars"],
    )
    result.markdown_char_coverage = compute_markdown_char_coverage(
        original_chars, len(tree.to_markdown()),
    )

    covered_block_ids: set[int] = set()

    def collect_coverage(nodes):
        for node in nodes:
            covered_block_ids.update(range(node.start_block_id, node.end_block_id + 1))
            collect_coverage(node.children)

    collect_coverage(tree.nodes)
    if tree.preamble_content and tree.nodes:
        first_start = min(node.start_block_id for node in tree.nodes)
        covered_block_ids.update(range(0, first_start))
    result.block_coverage = compute_block_coverage(len(blocks), covered_block_ids)
    return result, elapsed


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _result_to_raw(result: EvalResult) -> dict[str, Any]:
    """Serialise metrics without leaking Python dataclass objects."""
    payload = asdict(result)
    payload["tp_pairs"] = [
        {"gt": asdict(gt), "pred": asdict(pred)} for gt, pred in result.tp_pairs
    ]
    return payload


def evaluate_with_repeats(
    docx_path: Path,
    gt_path: Path,
    *,
    num_runs: int = 1,
    use_async: bool = False,
) -> tuple[dict, list[EvalResult]]:
    """Run all requested attempts and aggregate successful attempts only.

    The return shape remains ``(summary, successful_results)`` for backward
    compatibility.  ``summary['runs']`` contains both successful and failed
    attempt records, including ``status`` and ``error``.
    """
    if num_runs < 1:
        raise ValueError("num_runs must be at least 1")

    successful_results: list[EvalResult] = []
    successful_times: list[float] = []
    run_records: list[dict[str, Any]] = []

    for run_index in range(1, num_runs + 1):
        if num_runs > 1:
            logger.info("  Run %d/%d for %s", run_index, num_runs, docx_path.name)
        try:
            result, elapsed = evaluate_single_doc(
                docx_path, gt_path, use_async=use_async,
            )
            successful_results.append(result)
            successful_times.append(elapsed)
            run_records.append({
                "run_index": run_index,
                "status": "success",
                "error": None,
                "elapsed_seconds": elapsed,
                "metrics": _result_to_raw(result),
                "matching_details": result.matching_details,
                # This runner does not receive provider usage from the LLM API.
                "token_accounting_kind": "unavailable",
            })
        except Exception as exc:  # one failed round must not discard other rounds
            logger.error("Run %d failed for %s: %s", run_index, docx_path.name, exc)
            run_records.append({
                "run_index": run_index,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": None,
                "metrics": None,
                "matching_details": [],
                "token_accounting_kind": "unavailable",
            })

    metric_fields = {
        "f1": "f1",
        "precision": "precision",
        "recall": "recall",
        "hierarchy_acc": "hierarchy_accuracy",
        "all_gt_level_acc": "all_gt_level_accuracy",
        "all_gt_level_f1": "all_gt_level_f1",
        "parent_relation_distance": "parent_relation_distance",
        "heading_sequence_distance": "heading_sequence_edit_distance",
        # Historical output key retained; now explicitly parent-relation based.
        "ted": "tree_edit_distance",
        "char_recall": "char_recall",
        "block_coverage": "block_coverage",
        "markdown_char_coverage": "markdown_char_coverage",
    }
    summary: dict[str, Any] = {
        "file": docx_path.name,
        "num_runs": num_runs,
        "requested_runs": num_runs,
        "successful_runs": len(successful_results),
        "failed_runs": num_runs - len(successful_results),
        "status": (
            "success" if len(successful_results) == num_runs
            else "partial" if successful_results else "error"
        ),
        "errors": [record["error"] for record in run_records if record["error"]],
        "runs": run_records,
        "matching_details": [
            {"run_index": record["run_index"], "matches": record["matching_details"]}
            for record in run_records if record["status"] == "success"
        ],
        "token_accounting_kind": "unavailable",
    }
    for output_name, attribute in metric_fields.items():
        values = [float(getattr(result, attribute)) for result in successful_results]
        summary[f"{output_name}_mean"] = _mean(values)
        summary[f"{output_name}_std"] = _std(values)
    summary["time_mean"] = _mean(successful_times)
    summary["time_std"] = _std(successful_times)
    return summary, successful_results


def _provenance() -> dict[str, Any]:
    return {
        "runner": "evaluation.run_evaluation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
    }


def _algorithm_config_snapshot() -> dict[str, Any]:
    """Record the default algorithm controls used by ``evaluate_single_doc``."""
    from app.core.config.settings import settings
    from modules.parser.config import CompressorConfig, ParserConfig, ResolverConfig

    return {
        "compressor": CompressorConfig().model_dump(mode="json"),
        "resolver": ResolverConfig().model_dump(mode="json"),
        "parser": ParserConfig().model_dump(mode="json"),
        "llm": {
            "model": settings.llm_model,
            "max_input_tokens": settings.llm_max_input_tokens,
            "input_token_safety_margin": settings.llm_input_token_safety_margin,
        },
    }


def _default_raw_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_raw.json")


def run_evaluation(
    data_dir: Path,
    gt_dir: Path,
    output_path: Path | None = None,
    use_async: bool = False,
    num_runs: int = 1,
    raw_output_path: Path | None = None,
) -> str:
    """Evaluate all documents with GT and emit Markdown plus auditable JSON."""
    if num_runs < 1:
        raise ValueError("num_runs must be at least 1")
    docx_files = sorted(data_dir.rglob("*.docx"))
    if not docx_files:
        return f"No .docx files found in {data_dir}"

    report_lines = [
        "# Constellation Evaluation Report", "",
        f"Data directory: `{data_dir}`",
        f"Ground truth directory: `{gt_dir}`",
        f"Runs per document: {num_runs}", "",
    ]
    summaries: list[dict[str, Any]] = []

    for docx_path in docx_files:
        gt_path = gt_dir / f"{docx_path.stem}.json"
        if not gt_path.exists():
            logger.info("Skipping %s (no ground truth)", docx_path.name)
            continue
        summary, results = evaluate_with_repeats(
            docx_path, gt_path, num_runs=num_runs, use_async=use_async,
        )
        summaries.append(summary)
        if results:
            report_lines.append(format_eval_report(results[0], docx_path.name))
            report_lines.append("")
            report_lines.append(
                f"*Status={summary['status']}; successful runs "
                f"{summary['successful_runs']}/{summary['requested_runs']}; "
                f"F1={summary['f1_mean']:.4f}+/-{summary['f1_std']:.4f}*"
            )
        else:
            report_lines.extend([
                f"### {docx_path.name}",
                f"**ERROR**: all {num_runs} runs failed.",
            ])
        for error in summary["errors"]:
            report_lines.append(f"- Failed run: `{error}`")
        report_lines.append("")

    successful_summaries = [s for s in summaries if s["successful_runs"] > 0]
    if successful_summaries:
        report_lines.extend([
            "---", "## Summary", "",
            "| File | Status | Success | F1 (mean+/-std) | Hier.Acc (TP-only) | All-GT Level F1 | Parent Rel. Dist. | Time(s) |",
            "|:-----|:-------|--------:|----------------:|-------------------:|----------------:|------------------:|--------:|",
        ])
        for row in successful_summaries:
            report_lines.append(
                f"| {row['file']} | {row['status']} | "
                f"{row['successful_runs']}/{row['requested_runs']} | "
                f"{row['f1_mean']:.4f}+/-{row['f1_std']:.4f} | "
                f"{row['hierarchy_acc_mean']:.4f} | {row['all_gt_level_f1_mean']:.4f} | "
                f"{row['parent_relation_distance_mean']:.1f} | {row['time_mean']:.2f} |"
            )
        report_lines.extend([
            "",
            f"**Average F1**: {_mean([r['f1_mean'] for r in successful_summaries]):.4f}",
            "**Average Hierarchy Accuracy (TP-only)**: "
            f"{_mean([r['hierarchy_acc_mean'] for r in successful_summaries]):.4f}",
            "**Average All-GT Level F1**: "
            f"{_mean([r['all_gt_level_f1_mean'] for r in successful_summaries]):.4f}",
        ])

    report = "\n".join(report_lines)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        logger.info("Report written to %s", output_path)

    resolved_raw_path = raw_output_path or (_default_raw_path(output_path) if output_path else None)
    if resolved_raw_path:
        raw_payload = {
            "schema_version": 1,
            "config": {
                "data_dir": str(data_dir),
                "ground_truth_dir": str(gt_dir),
                "num_runs": num_runs,
                "use_async": use_async,
                "algorithm": _algorithm_config_snapshot(),
                "matching": {"block_id_tolerance": 3, "title_similarity_threshold": 0.6},
            },
            "provenance": _provenance(),
            "token_accounting_kind": "unavailable",
            "documents": summaries,
        }
        resolved_raw_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_raw_path.write_text(
            json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        logger.info("Raw results written to %s", resolved_raw_path)
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Constellation evaluation runner")
    parser.add_argument("--data-dir", type=Path, default=Path("tests/data"))
    parser.add_argument("--gt-dir", type=Path, default=Path("evaluation/ground_truth"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/evaluation_report.md"))
    parser.add_argument("--raw-output", type=Path, default=None)
    parser.add_argument("--async", dest="use_async", action="store_true")
    parser.add_argument("--num-runs", type=int, default=1)
    args = parser.parse_args()
    print(run_evaluation(
        data_dir=args.data_dir,
        gt_dir=args.gt_dir,
        output_path=args.output,
        raw_output_path=args.raw_output,
        use_async=args.use_async,
        num_runs=args.num_runs,
    ))


if __name__ == "__main__":
    main()
