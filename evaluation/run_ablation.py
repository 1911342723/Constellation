"""Ablation experiment runner for Constellation.

Implements four ablation studies:

1. State Phantom Projection — parallel vs speculative vs serial
2. Fuzzy anchor search radius r — sweep {0, 1, 3, 5, 10, 20}
3. RLE dynamic prefix length — sweep {15, 25, 35, 50, 75}
4. Compression on/off — w/ vs w/o RLE

All experiments use the quantitative evaluation framework (Section F1,
Hierarchy Accuracy, TED) against ground truth annotations.

Usage:
    python -m evaluation.run_ablation --data-dir tests/data --gt-dir evaluation/ground_truth
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from evaluation.metrics import (
    HeadingGT,
    HeadingPred,
    EvalResult,
    compute_section_f1,
    compute_char_recall,
)
from evaluation.run_evaluation import extract_pred_headings, load_ground_truth
from modules.parser.config import (
    CompressorConfig,
    ParserConfig,
    ResolverConfig,
)

logger = logging.getLogger(__name__)


def _parse_and_evaluate(
    blocks,
    gt_headings: list[HeadingGT],
    *,
    compressor_config: CompressorConfig | None = None,
    resolver_config: ResolverConfig | None = None,
    parser_config: ParserConfig | None = None,
    use_async: bool = True,
) -> tuple[EvalResult, float, int]:
    """Parse *blocks* and evaluate against *gt_headings*.

    Returns (EvalResult, elapsed_seconds, skeleton_chars).
    """
    from modules.parser.parser import CaliperParser, _doc_cache
    from modules.parser.compressor import SkeletonCompressor

    _doc_cache.clear()

    parser = CaliperParser(
        compressor_config=compressor_config,
        resolver_config=resolver_config,
        parser_config=parser_config,
    )

    compressor = SkeletonCompressor(config=compressor_config)
    skeleton_chunks = compressor.compress(blocks)
    skeleton_chars = sum(len(c) for c in skeleton_chunks)

    t0 = time.perf_counter()
    if use_async:
        tree = asyncio.run(parser.async_parse(blocks))
    else:
        tree = parser.parse(blocks)
    elapsed = time.perf_counter() - t0

    pred_headings = extract_pred_headings(tree.nodes)
    result = compute_section_f1(gt_headings, pred_headings)

    return result, elapsed, skeleton_chars


def _load_doc_and_gt(docx_path: Path, gt_path: Path):
    """Load blocks and ground truth from disk."""
    from infrastructure.providers.docx_provider import DocxProvider

    provider = DocxProvider()
    blocks = provider.extract(str(docx_path))
    gt_data = load_ground_truth(gt_path)
    gt_headings = [
        HeadingGT(block_id=h["block_id"], title=h["title"], level=h["level"])
        for h in gt_data.get("headings", [])
    ]
    return blocks, gt_headings


# ═══════════════════════════════════════════════════════════════
# Ablation 1: State Phantom Projection
# ═══════════════════════════════════════════════════════════════

def ablation_phantom(blocks, gt_headings: list[HeadingGT]) -> list[dict]:
    """Compare parallel / speculative / serial routing strategies."""
    configs = [
        ("parallel (no phantom)", ParserConfig(enable_speculative_execution=True, speculative_boundary_tolerance=999)),
        ("speculative (tolerance=1)", ParserConfig(enable_speculative_execution=True, speculative_boundary_tolerance=1)),
        ("serial (full phantom)", ParserConfig(enable_speculative_execution=False)),
    ]
    rows = []
    for label, pc in configs:
        result, elapsed, _ = _parse_and_evaluate(
            blocks, gt_headings, parser_config=pc,
        )
        rows.append({
            "strategy": label,
            "f1": result.f1,
            "precision": result.precision,
            "recall": result.recall,
            "hierarchy_acc": result.hierarchy_accuracy,
            "ted": result.tree_edit_distance,
            "time_s": elapsed,
        })
        logger.info("[Phantom] %s: F1=%.4f HierAcc=%.4f Time=%.2fs",
                    label, result.f1, result.hierarchy_accuracy, elapsed)
    return rows


# ═══════════════════════════════════════════════════════════════
# Ablation 2: Search Radius r
# ═══════════════════════════════════════════════════════════════

def ablation_radius(blocks, gt_headings: list[HeadingGT]) -> list[dict]:
    """Sweep fuzzy anchor search radius r in {0, 1, 3, 5, 10, 20}."""
    radii = [0, 1, 3, 5, 10, 20]
    rows = []
    for r in radii:
        rc = ResolverConfig(fuzzy_anchor_radius=r)
        result, elapsed, _ = _parse_and_evaluate(
            blocks, gt_headings, resolver_config=rc,
        )
        rows.append({
            "radius": r,
            "f1": result.f1,
            "precision": result.precision,
            "recall": result.recall,
            "hierarchy_acc": result.hierarchy_accuracy,
            "ted": result.tree_edit_distance,
        })
        logger.info("[Radius] r=%d: F1=%.4f HierAcc=%.4f", r, result.f1, result.hierarchy_accuracy)
    return rows


# ═══════════════════════════════════════════════════════════════
# Ablation 3: RLE Dynamic Prefix Length
# ═══════════════════════════════════════════════════════════════

def ablation_rle_prefix(blocks, gt_headings: list[HeadingGT]) -> list[dict]:
    """Sweep RLE dynamic prefix min length in {15, 25, 35, 50, 75}."""
    lengths = [15, 25, 35, 50, 75]
    rows = []
    for pl in lengths:
        cc = CompressorConfig(rle_dynamic_prefix_min_length=pl)
        result, elapsed, skeleton_chars = _parse_and_evaluate(
            blocks, gt_headings, compressor_config=cc,
        )
        rows.append({
            "prefix_len": pl,
            "f1": result.f1,
            "hierarchy_acc": result.hierarchy_accuracy,
            "skeleton_chars": skeleton_chars,
        })
        logger.info("[RLE Prefix] len=%d: F1=%.4f skeleton=%d chars",
                    pl, result.f1, skeleton_chars)
    return rows


# ═══════════════════════════════════════════════════════════════
# Ablation 4: Compression On/Off
# ═══════════════════════════════════════════════════════════════

def ablation_compression(blocks, gt_headings: list[HeadingGT]) -> list[dict]:
    """Compare w/ vs w/o RLE compression."""
    configs = [
        ("w/ RLE", CompressorConfig(enable_rle=True)),
        ("w/o RLE", CompressorConfig(enable_rle=False)),
    ]
    rows = []
    for label, cc in configs:
        result, elapsed, skeleton_chars = _parse_and_evaluate(
            blocks, gt_headings, compressor_config=cc,
        )
        rows.append({
            "mode": label,
            "f1": result.f1,
            "hierarchy_acc": result.hierarchy_accuracy,
            "skeleton_chars": skeleton_chars,
            "time_s": elapsed,
        })
        logger.info("[Compression] %s: F1=%.4f skeleton=%d chars Time=%.2fs",
                    label, result.f1, skeleton_chars, elapsed)
    return rows


# ═══════════════════════════════════════════════════════════════
# Report Generation
# ═══════════════════════════════════════════════════════════════

def _format_ablation_report(
    phantom_rows: list[dict],
    radius_rows: list[dict],
    prefix_rows: list[dict],
    compression_rows: list[dict],
    doc_name: str = "",
) -> str:
    lines = [
        "# Ablation Study Report",
        "",
        f"Document: `{doc_name}`" if doc_name else "",
        "",
        "## 1. State Phantom Projection",
        "",
        "| Strategy | F1 | Precision | Recall | Hier.Acc | TED | Time(s) |",
        "|:---------|---:|----------:|-------:|---------:|----:|--------:|",
    ]
    for r in phantom_rows:
        lines.append(
            f"| {r['strategy']} | {r['f1']:.4f} | {r['precision']:.4f} | "
            f"{r['recall']:.4f} | {r['hierarchy_acc']:.4f} | "
            f"{r['ted']:.1f} | {r['time_s']:.2f} |"
        )

    lines += [
        "",
        "## 2. Fuzzy Anchor Search Radius r",
        "",
        "| r | F1 | Precision | Recall | Hier.Acc | TED |",
        "|--:|---:|----------:|-------:|---------:|----:|",
    ]
    for r in radius_rows:
        lines.append(
            f"| {r['radius']} | {r['f1']:.4f} | {r['precision']:.4f} | "
            f"{r['recall']:.4f} | {r['hierarchy_acc']:.4f} | {r['ted']:.1f} |"
        )

    lines += [
        "",
        "## 3. RLE Dynamic Prefix Length",
        "",
        "| Prefix Len | F1 | Hier.Acc | Skeleton Chars |",
        "|-----------:|---:|---------:|---------------:|",
    ]
    for r in prefix_rows:
        lines.append(
            f"| {r['prefix_len']} | {r['f1']:.4f} | "
            f"{r['hierarchy_acc']:.4f} | {r['skeleton_chars']} |"
        )

    lines += [
        "",
        "## 4. Compression On/Off",
        "",
        "| Mode | F1 | Hier.Acc | Skeleton Chars | Time(s) |",
        "|:-----|---:|---------:|---------------:|--------:|",
    ]
    for r in compression_rows:
        lines.append(
            f"| {r['mode']} | {r['f1']:.4f} | "
            f"{r['hierarchy_acc']:.4f} | {r['skeleton_chars']} | {r['time_s']:.2f} |"
        )

    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="Constellation ablation experiments")
    parser.add_argument("--data-dir", type=Path, default=Path("tests/data"))
    parser.add_argument("--gt-dir", type=Path, default=Path("evaluation/ground_truth"))
    parser.add_argument("--output", type=Path, default=Path("evaluation/ablation_report.md"))
    parser.add_argument("--doc", type=str, default=None,
                        help="Specific docx filename to test (without path)")
    args = parser.parse_args()

    data_dir = args.data_dir
    gt_dir = args.gt_dir

    docx_files = sorted(data_dir.rglob("*.docx"))
    if args.doc:
        docx_files = [f for f in docx_files if f.name == args.doc]

    if not docx_files:
        print(f"No .docx files found in {data_dir}")
        return

    full_report_lines = []

    for docx_path in docx_files:
        gt_path = gt_dir / (docx_path.stem + ".json")
        if not gt_path.exists():
            logger.info("Skipping %s (no ground truth at %s)", docx_path.name, gt_path)
            continue

        logger.info("=" * 60)
        logger.info("Running ablation on: %s", docx_path.name)
        logger.info("=" * 60)

        blocks, gt_headings = _load_doc_and_gt(docx_path, gt_path)

        phantom_rows = ablation_phantom(blocks, gt_headings)
        radius_rows = ablation_radius(blocks, gt_headings)
        prefix_rows = ablation_rle_prefix(blocks, gt_headings)
        compression_rows = ablation_compression(blocks, gt_headings)

        report = _format_ablation_report(
            phantom_rows, radius_rows, prefix_rows, compression_rows,
            doc_name=docx_path.name,
        )
        full_report_lines.append(report)

    full_report = "\n\n---\n\n".join(full_report_lines)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(full_report)
        logger.info("Ablation report written to %s", args.output)

    print(full_report)


if __name__ == "__main__":
    main()
