"""Quantitative evaluation metrics for the Constellation pipeline.

Provides:
- Section F1 (heading detection precision / recall / F1)
- Hierarchy Accuracy (level-exact match among TP headings)
- Tree Edit Distance (Zhang-Shasha on predicted vs GT heading trees)
- Character Recall (text coverage)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class HeadingGT:
    """Ground-truth heading entry."""
    block_id: int
    title: str
    level: int


@dataclass
class HeadingPred:
    """Predicted heading entry."""
    block_id: int
    title: str
    level: int


@dataclass
class EvalResult:
    """Aggregated evaluation result."""
    # Section F1
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0

    # Hierarchy accuracy (among TPs)
    level_correct: int = 0
    level_total: int = 0
    hierarchy_accuracy: float = 0.0

    # Tree Edit Distance
    tree_edit_distance: float = 0.0

    # Character recall
    char_recall: float = 0.0

    # Detail lists
    tp_pairs: list = field(default_factory=list)
    fp_preds: list = field(default_factory=list)
    fn_gts: list = field(default_factory=list)


def compute_section_f1(
    gt_headings: List[HeadingGT],
    pred_headings: List[HeadingPred],
    block_id_tolerance: int = 3,
    title_sim_threshold: float = 0.6,
) -> EvalResult:
    """Compute Section F1, Hierarchy Accuracy, and TED.

    A predicted heading is a *true positive* if there exists a GT heading
    whose ``block_id`` is within ``block_id_tolerance`` **and** whose
    title has a Levenshtein similarity >= ``title_sim_threshold``.

    Matching is greedy in GT order — each GT heading is matched to at
    most one prediction, and vice versa.
    """
    from modules.parser.resolver import _levenshtein_ratio

    result = EvalResult()
    matched_pred_ids: set[int] = set()

    for gt in gt_headings:
        best_pred = None
        best_sim = 0.0
        for i, pred in enumerate(pred_headings):
            if i in matched_pred_ids:
                continue
            if abs(pred.block_id - gt.block_id) > block_id_tolerance:
                continue
            sim = _levenshtein_ratio(
                gt.title.strip().lower(),
                pred.title.strip().lower(),
            )
            if sim >= title_sim_threshold and sim > best_sim:
                best_sim = sim
                best_pred = (i, pred)

        if best_pred is not None:
            idx, pred = best_pred
            matched_pred_ids.add(idx)
            result.tp += 1
            result.tp_pairs.append((gt, pred))
            if gt.level == pred.level:
                result.level_correct += 1
            result.level_total += 1
        else:
            result.fn += 1
            result.fn_gts.append(gt)

    for i, pred in enumerate(pred_headings):
        if i not in matched_pred_ids:
            result.fp += 1
            result.fp_preds.append(pred)

    result.precision = result.tp / max(result.tp + result.fp, 1)
    result.recall = result.tp / max(result.tp + result.fn, 1)
    if result.precision + result.recall > 0:
        result.f1 = 2 * result.precision * result.recall / (result.precision + result.recall)
    else:
        result.f1 = 0.0

    result.hierarchy_accuracy = (
        result.level_correct / max(result.level_total, 1)
    )

    result.tree_edit_distance = _compute_tree_edit_distance(
        gt_headings, pred_headings,
    )

    return result


def compute_char_recall(
    original_chars: int,
    extracted_chars: int,
) -> float:
    """Compute character-level recall (coverage)."""
    if original_chars == 0:
        return 1.0
    return extracted_chars / original_chars


# ── Tree Edit Distance (simplified) ─────────────────────────

def _compute_tree_edit_distance(
    gt_headings: List[HeadingGT],
    pred_headings: List[HeadingPred],
) -> float:
    """Simplified tree edit distance based on heading sequences.

    Uses a DP sequence-edit-distance over the (level, title) tuples as
    an approximation of full Zhang-Shasha TED.  This is sufficient for
    comparing heading structures without requiring a full tree
    implementation.
    """
    seq_gt = [(h.level, h.title.strip().lower()) for h in gt_headings]
    seq_pr = [(h.level, h.title.strip().lower()) for h in pred_headings]
    return _sequence_edit_distance(seq_gt, seq_pr)


def _sequence_edit_distance(
    seq_a: list[tuple],
    seq_b: list[tuple],
) -> float:
    """Classic two-row DP edit distance over tuples."""
    m, n = len(seq_a), len(seq_b)
    prev = list(range(n + 1))
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        curr[0] = i
        for j in range(1, n + 1):
            cost = 0 if seq_a[i - 1] == seq_b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return float(prev[n])


def format_eval_report(
    result: EvalResult,
    doc_name: str = "",
) -> str:
    """Format an evaluation result as a Markdown report fragment."""
    lines = []
    if doc_name:
        lines.append(f"### {doc_name}")
        lines.append("")

    lines.append(f"| Metric | Value |")
    lines.append(f"|:-------|------:|")
    lines.append(f"| Precision | {result.precision:.4f} |")
    lines.append(f"| Recall | {result.recall:.4f} |")
    lines.append(f"| **F1** | **{result.f1:.4f}** |")
    lines.append(f"| TP / FP / FN | {result.tp} / {result.fp} / {result.fn} |")
    lines.append(f"| Hierarchy Accuracy | {result.hierarchy_accuracy:.4f} |")
    lines.append(f"| Tree Edit Distance | {result.tree_edit_distance:.1f} |")
    if result.char_recall > 0:
        lines.append(f"| Character Recall | {result.char_recall:.4f} |")

    if result.fn_gts:
        lines.append("")
        lines.append("**Missed headings (FN):**")
        for gt in result.fn_gts:
            lines.append(f"- [ID={gt.block_id}] L{gt.level}: {gt.title}")

    if result.fp_preds:
        lines.append("")
        lines.append("**False positives (FP):**")
        for pred in result.fp_preds:
            lines.append(f"- [ID={pred.block_id}] L{pred.level}: {pred.title}")

    return "\n".join(lines)
