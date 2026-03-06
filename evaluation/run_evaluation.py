"""Automated evaluation runner for the Constellation pipeline.

Usage:
    python -m evaluation.run_evaluation [--data-dir tests/data] [--gt-dir evaluation/ground_truth]

Loads benchmark DOCX files, runs the full parsing pipeline (with real LLM),
compares results against ground-truth annotations, and outputs a Markdown
report with Section F1, Hierarchy Accuracy, TED, and Character Recall.

Supports ``--num-runs N`` for statistical significance reporting
(mean +/- std over N independent LLM calls).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from evaluation.metrics import (
    HeadingGT,
    HeadingPred,
    EvalResult,
    compute_section_f1,
    compute_char_recall,
    format_eval_report,
)

logger = logging.getLogger(__name__)


def load_ground_truth(gt_path: Path) -> dict:
    """Load a ground-truth JSON file."""
    with open(gt_path, encoding="utf-8") as f:
        return json.load(f)


def extract_pred_headings(document_nodes) -> list[HeadingPred]:
    """Flatten a DocumentNode tree into HeadingPred entries (recursive)."""
    preds: list[HeadingPred] = []

    def walk(nodes):
        for node in nodes:
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
    """Parse a single document and evaluate against ground truth.

    Returns (EvalResult, elapsed_seconds).
    """
    from infrastructure.providers.docx_provider import DocxProvider
    from modules.parser.parser import CaliperParser

    CaliperParser.clear_cache()

    provider = DocxProvider()
    blocks = provider.extract(str(docx_path))
    if not blocks:
        raise RuntimeError(f"DocxProvider extracted 0 blocks from {docx_path.name}")

    gt_data = load_ground_truth(gt_path)
    gt_headings = [
        HeadingGT(
            block_id=h["block_id"],
            title=h["title"],
            level=h["level"],
        )
        for h in gt_data.get("headings", [])
    ]

    parser = CaliperParser()
    t0 = time.perf_counter()

    if use_async:
        import asyncio
        tree = asyncio.run(parser.async_parse(blocks))
    else:
        tree = parser.parse(blocks)

    elapsed = time.perf_counter() - t0

    pred_headings = extract_pred_headings(tree.nodes)

    result = compute_section_f1(gt_headings, pred_headings)

    original_chars = sum(len(b.to_markdown()) for b in blocks)
    stats = tree.get_stats()
    extracted_chars = stats["total_content_chars"]
    result.char_recall = compute_char_recall(original_chars, extracted_chars)

    return result, elapsed


def evaluate_with_repeats(
    docx_path: Path,
    gt_path: Path,
    *,
    num_runs: int = 1,
    use_async: bool = False,
) -> tuple[dict, list[EvalResult]]:
    """Run evaluation *num_runs* times and return aggregated statistics.

    Returns (summary_dict, list_of_results).
    The summary_dict contains mean and std for each metric.
    """
    all_results: list[EvalResult] = []
    all_times: list[float] = []

    for run_idx in range(num_runs):
        if num_runs > 1:
            logger.info("  Run %d/%d for %s", run_idx + 1, num_runs, docx_path.name)
        result, elapsed = evaluate_single_doc(
            docx_path, gt_path, use_async=use_async,
        )
        all_results.append(result)
        all_times.append(elapsed)

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    def _std(vals: list[float]) -> float:
        if len(vals) < 2:
            return 0.0
        m = _mean(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))

    f1_vals = [r.f1 for r in all_results]
    prec_vals = [r.precision for r in all_results]
    rec_vals = [r.recall for r in all_results]
    ha_vals = [r.hierarchy_accuracy for r in all_results]
    ted_vals = [r.tree_edit_distance for r in all_results]
    cr_vals = [r.char_recall for r in all_results]

    summary = {
        "file": docx_path.name,
        "num_runs": num_runs,
        "f1_mean": _mean(f1_vals), "f1_std": _std(f1_vals),
        "precision_mean": _mean(prec_vals), "precision_std": _std(prec_vals),
        "recall_mean": _mean(rec_vals), "recall_std": _std(rec_vals),
        "hierarchy_acc_mean": _mean(ha_vals), "hierarchy_acc_std": _std(ha_vals),
        "ted_mean": _mean(ted_vals), "ted_std": _std(ted_vals),
        "char_recall_mean": _mean(cr_vals), "char_recall_std": _std(cr_vals),
        "time_mean": _mean(all_times), "time_std": _std(all_times),
    }
    return summary, all_results


def run_evaluation(
    data_dir: Path,
    gt_dir: Path,
    output_path: Path | None = None,
    use_async: bool = False,
    num_runs: int = 1,
) -> str:
    """Run evaluation on all documents with matching ground truth.

    Returns the full Markdown report as a string.
    """
    docx_files = sorted(data_dir.rglob("*.docx"))
    if not docx_files:
        return f"No .docx files found in {data_dir}"

    report_lines = [
        "# Constellation Evaluation Report",
        "",
        f"Data directory: `{data_dir}`",
        f"Ground truth directory: `{gt_dir}`",
        f"Runs per document: {num_runs}",
        "",
    ]

    summary_rows: list[dict] = []

    for docx_path in docx_files:
        gt_name = docx_path.stem + ".json"
        gt_path = gt_dir / gt_name
        if not gt_path.exists():
            logger.info("Skipping %s (no ground truth)", docx_path.name)
            continue

        logger.info("Evaluating %s ...", docx_path.name)
        try:
            summary, results = evaluate_with_repeats(
                docx_path, gt_path,
                num_runs=num_runs,
                use_async=use_async,
            )
            report_lines.append(format_eval_report(results[0], docx_path.name))
            if num_runs > 1:
                report_lines.append("")
                report_lines.append(
                    f"*({num_runs} runs: "
                    f"F1={summary['f1_mean']:.4f}+/-{summary['f1_std']:.4f}, "
                    f"HierAcc={summary['hierarchy_acc_mean']:.4f}+/-{summary['hierarchy_acc_std']:.4f})*"
                )
            report_lines.append("")
            summary_rows.append(summary)
        except Exception as e:
            logger.error("Failed on %s: %s", docx_path.name, e)
            report_lines.append(f"### {docx_path.name}")
            report_lines.append(f"**ERROR**: {e}")
            report_lines.append("")

    if summary_rows:
        report_lines.append("---")
        report_lines.append("## Summary")
        report_lines.append("")
        if num_runs > 1:
            report_lines.append(
                "| File | F1 (mean+/-std) | Precision | Recall | Hier.Acc | TED | CharRecall | Time(s) |"
            )
            report_lines.append(
                "|:-----|----------------:|----------:|-------:|---------:|----:|-----------:|--------:|"
            )
            for r in summary_rows:
                report_lines.append(
                    f"| {r['file']} | {r['f1_mean']:.4f}+/-{r['f1_std']:.4f} | "
                    f"{r['precision_mean']:.4f} | {r['recall_mean']:.4f} | "
                    f"{r['hierarchy_acc_mean']:.4f} | {r['ted_mean']:.1f} | "
                    f"{r['char_recall_mean']:.4f} | {r['time_mean']:.2f} |"
                )
        else:
            report_lines.append(
                "| File | F1 | Precision | Recall | Hier.Acc | TED | CharRecall | Time(s) |"
            )
            report_lines.append(
                "|:-----|---:|----------:|-------:|---------:|----:|-----------:|--------:|"
            )
            for r in summary_rows:
                report_lines.append(
                    f"| {r['file']} | {r['f1_mean']:.4f} | {r['precision_mean']:.4f} | "
                    f"{r['recall_mean']:.4f} | {r['hierarchy_acc_mean']:.4f} | "
                    f"{r['ted_mean']:.1f} | {r['char_recall_mean']:.4f} | {r['time_mean']:.2f} |"
                )

        avg_f1 = sum(r["f1_mean"] for r in summary_rows) / len(summary_rows)
        avg_ha = sum(r["hierarchy_acc_mean"] for r in summary_rows) / len(summary_rows)
        report_lines.append("")
        report_lines.append(f"**Average F1**: {avg_f1:.4f}")
        report_lines.append(f"**Average Hierarchy Accuracy**: {avg_ha:.4f}")

    report = "\n".join(report_lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info("Report written to %s", output_path)

    return report


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Constellation evaluation runner")
    parser.add_argument("--data-dir", type=Path, default=Path("tests/data"))
    parser.add_argument("--gt-dir", type=Path, default=Path("evaluation/ground_truth"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/evaluation_report.md"))
    parser.add_argument("--async", dest="use_async", action="store_true")
    parser.add_argument("--num-runs", type=int, default=1,
                        help="Number of independent runs per document for statistical significance")
    args = parser.parse_args()

    report = run_evaluation(
        data_dir=args.data_dir,
        gt_dir=args.gt_dir,
        output_path=args.output,
        use_async=args.use_async,
        num_runs=args.num_runs,
    )
    print(report)


if __name__ == "__main__":
    main()
