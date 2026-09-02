"""Threshold-sensitivity sweep for the candidate admission policy.

Reviewers reasonably ask where the candidate-generation constants come
from — the strong-evidence probability floor (0.40), the weak-only
probability floor (0.25), and the per-document budget cap (15%).  This script perturbs each
one over a grid (the others held at their production default) and
measures the effect on candidate recall (block-id discipline, the located
GT headings of Table 3) and the candidate budget (candidates / text
blocks).  The sweep must be rerun whenever the provider or evidence model
changes; its output, rather than hard-coded historical numbers, is the
paper's reproducibility artifact.

Everything is deterministic and LLM-free.  PDF extraction (the slow part)
runs once; only the cheap candidate pass is repeated per grid point.

Run: python -m evaluation.threshold_sensitivity
Outputs: experiment_results/threshold_sensitivity.md (+ _raw.json)
"""
from __future__ import annotations

import json
import os
import sys
from statistics import mean

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.parser.heading_candidates import (
    _BASE_MIN_SCORE,
    _MAX_CANDIDATE_RATIO,
    _WEAK_ONLY_MIN_SCORE,
    generate_heading_candidates,
)

LONG_DOCS = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(LONG_DOCS, "ground_truth")
OUT_MD = os.path.join("experiment_results", "threshold_sensitivity.md")
OUT_JSON = os.path.join("experiment_results", "threshold_sensitivity_raw.json")

# kwarg name -> (production default, grid)
SWEEPS = {
    "base_min_score": (_BASE_MIN_SCORE, [0.30, 0.35, 0.40, 0.45, 0.50]),
    "weak_only_min": (_WEAK_ONLY_MIN_SCORE, [0.15, 0.20, 0.25, 0.30, 0.35]),
    "max_ratio": (_MAX_CANDIDATE_RATIO, [0.10, 0.125, 0.15, 0.175, 0.20]),
}

PARAM_LABEL = {
    "base_min_score": "Strong-evidence probability floor (default 0.40)",
    "weak_only_min": "Weak-only probability floor (default 0.25)",
    "max_ratio": "Budget cap ratio (default 0.15)",
}


def load_corpus():
    """Extract blocks + located GT once per document (the slow step)."""
    from infrastructure.providers.pdf_provider import PdfProvider

    corpus = []
    for fname in sorted(os.listdir(LONG_DOCS)):
        if not fname.endswith(".pdf"):
            continue
        stem = fname[:-4]
        gt_path = os.path.join(GT_DIR, f"{stem}.json")
        if not os.path.exists(gt_path):
            continue
        print(f"[load] {stem} ...", flush=True)
        blocks = PdfProvider().extract(os.path.join(LONG_DOCS, fname))
        text_blocks = sum(1 for b in blocks if b.type == "text" and b.text)
        with open(gt_path, encoding="utf-8") as f:
            gt = json.load(f)
        located = [h["block_id"] for h in gt.get("headings", [])
                   if h.get("block_id", -1) >= 0]
        corpus.append((stem, blocks, located, text_blocks))
    return corpus


def evaluate(corpus, **overrides):
    """Aggregate candidate recall + budget under one threshold setting."""
    total_located = total_hit = total_cand = total_tb = 0
    per_doc_recall = []
    for _stem, blocks, located, text_blocks in corpus:
        cands = generate_heading_candidates(blocks, **overrides)
        cand_ids = {c.block_id for c in cands}
        hit = sum(1 for bid in located if bid in cand_ids)
        total_located += len(located)
        total_hit += hit
        total_cand += len(cands)
        total_tb += text_blocks
        if located:
            per_doc_recall.append(hit / len(located) * 100)
    return {
        "micro_recall": round(total_hit / max(total_located, 1) * 100, 1),
        "macro_recall": round(mean(per_doc_recall) if per_doc_recall else 0.0, 1),
        "budget": round(total_cand / max(total_tb, 1) * 100, 1),
        "candidates": total_cand,
        "hit": total_hit,
        "located": total_located,
    }


def main() -> None:
    corpus = load_corpus()
    baseline = evaluate(corpus)
    print(f"[baseline] micro={baseline['micro_recall']}% "
          f"macro={baseline['macro_recall']}% budget={baseline['budget']}%", flush=True)

    results = {"baseline": baseline, "sweeps": {}}
    for param, (default, grid) in SWEEPS.items():
        rows = []
        for value in grid:
            r = evaluate(corpus, **{param: value})
            r["value"] = value
            r["is_default"] = abs(value - default) < 1e-9
            rows.append(r)
            print(f"[{param}={value}] micro={r['micro_recall']}% "
                  f"macro={r['macro_recall']}% budget={r['budget']}%", flush=True)
        results["sweeps"][param] = rows

    os.makedirs("experiment_results", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    lines = [
        "# Threshold Sensitivity of Candidate Admission",
        "",
        "> Generated by `evaluation/threshold_sensitivity.py`. Deterministic, LLM-free, reproducible without an API key.",
        f"> Recall is the block-id discipline of Table 3 ({baseline['located']} located GT headings across 12 long PDFs);",
        "> budget = candidates / text blocks. Each sweep perturbs one threshold; the others stay at production defaults.",
        "",
        f"**Production defaults**: strong-evidence probability floor {_BASE_MIN_SCORE}, weak-only probability floor "
        f"{_WEAK_ONLY_MIN_SCORE}, budget cap {_MAX_CANDIDATE_RATIO:.2f}. "
        f"Baseline: micro recall **{baseline['micro_recall']}%**, macro "
        f"**{baseline['macro_recall']}%**, budget **{baseline['budget']}%**.",
        "",
    ]
    for param, rows in results["sweeps"].items():
        lines += [
            f"## {PARAM_LABEL[param]}",
            "",
            "| Value | Micro recall % | Macro recall % | Budget % | Candidates |",
            "|---|---:|---:|---:|---:|",
        ]
        for r in rows:
            mark = " *(default)*" if r["is_default"] else ""
            lines.append(
                f"| {r['value']}{mark} | {r['micro_recall']} | {r['macro_recall']} "
                f"| {r['budget']} | {r['candidates']} |"
            )
        lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nWritten: {OUT_MD}")


if __name__ == "__main__":
    main()
