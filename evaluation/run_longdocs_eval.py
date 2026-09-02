"""Long-document (PDF) evaluation runner — post-fix rerun.

Reruns the full pipeline (Stage 1-4, real LLM) over the long_docs PDF
set with the repaired ground truth, producing a layered report that can
be compared against the pre-fix baseline (FINAL_EXPERIMENT_REPORT 3.3,
avg F1=0.176 with broken GT).

bert/resnet are excluded: they have no embedded TOC and await manual
annotation (see experiment_results/gt_repair_report.md).

Usage:
    python -m evaluation.run_longdocs_eval                # all 10 docs
    python -m evaluation.run_longdocs_eval --docs vit gpt3  # subset

Outputs:
    experiment_results/longdocs_rerun.md
    experiment_results/longdocs_rerun_raw.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation.metrics import HeadingGT, compute_section_f1
from evaluation.run_evaluation import extract_pred_headings, load_ground_truth

LONG_DOCS_DIR = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(LONG_DOCS_DIR, "ground_truth")
OUT_MD = os.path.join("experiment_results", "longdocs_rerun.md")
OUT_JSON = os.path.join("experiment_results", "longdocs_rerun_raw.json")

# bert / resnet excluded: no embedded TOC, manual GT pending.
DEFAULT_DOCS = [
    "attention_is_all_you_need",
    "chain_of_thought",
    "diffusion_models",
    "gpt3",
    "llm_survey",
    "nist_cybersecurity",
    "openai_gpt4_tech_report",
    "reinforcement_learning",
    "transformer_survey",
    "vit",
]


def evaluate_pdf(stem: str) -> dict:
    from infrastructure.providers.pdf_provider import PdfProvider
    from modules.parser.parser import CaliperParser

    CaliperParser.clear_cache()

    pdf_path = os.path.join(LONG_DOCS_DIR, f"{stem}.pdf")
    gt_path = os.path.join(GT_DIR, f"{stem}.json")

    blocks = PdfProvider().extract(pdf_path)

    gt_data = load_ground_truth(gt_path)
    skipped_unlocated = 0
    gt_headings: list[HeadingGT] = []
    for h in gt_data.get("headings", []):
        if h.get("block_id", -1) < 0:
            skipped_unlocated += 1
            continue
        gt_headings.append(
            HeadingGT(block_id=h["block_id"], title=h["title"], level=h["level"])
        )

    parser = CaliperParser()
    t0 = time.perf_counter()
    tree = parser.parse(blocks)
    elapsed = time.perf_counter() - t0

    preds = extract_pred_headings(tree.nodes)
    result = compute_section_f1(gt_headings, preds)

    return {
        "doc": stem,
        "blocks": len(blocks),
        "gt_headings": len(gt_headings),
        "gt_skipped_unlocated": skipped_unlocated,
        "pred_headings": len(preds),
        "precision": round(result.precision, 4),
        "recall": round(result.recall, 4),
        "f1": round(result.f1, 4),
        "hierarchy_accuracy": round(result.hierarchy_accuracy, 4),
        "tp": result.tp,
        "fp": result.fp,
        "fn": result.fn,
        "elapsed_s": round(elapsed, 1),
        "fn_titles": [g.title[:60] for g in result.fn_gts][:20],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", nargs="*", default=None)
    args = ap.parse_args()

    docs = args.docs or DEFAULT_DOCS
    rows: list[dict] = []

    for stem in docs:
        print(f"[eval] {stem} ...", flush=True)
        try:
            row = evaluate_pdf(stem)
        except Exception as exc:  # keep going; record the failure
            row = {"doc": stem, "error": str(exc)[:200]}
        rows.append(row)
        print(f"  -> {json.dumps({k: row.get(k) for k in ('f1', 'recall', 'precision', 'elapsed_s', 'error') if k in row})}",
              flush=True)
        # Incremental save so progress survives interruption.
        os.makedirs("experiment_results", exist_ok=True)
        with open(OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)

    ok = [r for r in rows if "error" not in r]

    lines = ["# Long-Document Rerun (post-fix, repaired GT)", ""]
    import datetime
    from app.core.config.settings import settings
    model = settings.llm_model
    lines.append(f"> Generated {datetime.date.today()} | model: {model} | "
                 f"fixes in effect: small-caps join, inline merge, candidate "
                 f"tightening, out-of-candidate downgrade, repaired GT")
    lines.append("> Baseline for comparison: FINAL_EXPERIMENT_REPORT 3.3 "
                 "(avg F1=0.176, recall=0.569 on 12 docs with broken GT, DeepSeek)")
    lines.append("")
    lines.append("| Document | Blocks | GT | Pred | P | R | **F1** | Hier.Acc | Time |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['doc']} | ERROR: {r['error'][:60]} |")
            continue
        lines.append(
            f"| {r['doc']} | {r['blocks']} | {r['gt_headings']} | {r['pred_headings']} "
            f"| {r['precision']:.3f} | {r['recall']:.3f} | **{r['f1']:.3f}** "
            f"| {r['hierarchy_accuracy']:.3f} | {r['elapsed_s']}s |"
        )
    if ok:
        def avg(key: str) -> float:
            return sum(r[key] for r in ok) / len(ok)
        lines.append(
            f"| **Average ({len(ok)})** | | | | {avg('precision'):.3f} "
            f"| {avg('recall'):.3f} | **{avg('f1'):.3f}** "
            f"| {avg('hierarchy_accuracy'):.3f} | {avg('elapsed_s'):.1f}s |"
        )
    lines.append("")
    lines.append("## Missed headings (FN) per document")
    lines.append("")
    for r in ok:
        if r["fn_titles"]:
            lines.append(f"### {r['doc']} ({r['fn']} FN)")
            for t in r["fn_titles"]:
                lines.append(f"- {t}")
            lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()
