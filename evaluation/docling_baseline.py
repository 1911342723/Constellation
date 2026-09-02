"""Deterministic heading-detection baseline: Docling vs the Constellation
candidate layer, on the 12 long PDFs, under a *system-agnostic* title
matching discipline.

Why a title-only discipline.  The paper's primary candidate-recall number
(474/488 = 97.1%) matches ground-truth headings by ``block_id`` (an
identifier internal to Constellation's ``PdfProvider``).  An external
system such as Docling does not share those ids, so a fair cross-system
comparison must drop the ``block_id`` constraint and match on heading
*text* only.  We reuse the same Levenshtein ratio and 0.6 threshold as
``evaluation/metrics.compute_section_f1`` and re-score the Constellation
candidate layer under the identical title discipline, so both columns are
directly comparable.

Both systems here are deterministic and LLM-free:

- **Docling** runs its layout model and emits ``section_header`` / ``title``
  items.  OCR and table-structure are disabled — the corpus is born-digital
  and Constellation likewise reads the embedded text layer only.
- **Constellation candidate layer** is the Stage-2.5 deterministic heading
  candidate generator (``generate_heading_candidates``), the recall ceiling
  that bounds candidate routing.  It is a high-recall *intermediate* set
  handed to the LLM, not a final decision — so we report its recall and its
  budget (candidates / text blocks), not a precision that would
  misrepresent its role.  Docling's headers are a *final* decision, so for
  Docling we additionally report precision / F1.

Run: python -m evaluation.docling_baseline
Outputs: experiment_results/docling_baseline.md (+ _raw.json)
No API key required.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LONG_DOCS = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(LONG_DOCS, "ground_truth")
OUT_MD = os.path.join("experiment_results", "docling_baseline.md")
OUT_JSON = os.path.join("experiment_results", "docling_baseline_raw.json")

TITLE_SIM_THRESHOLD = 0.6
DOCLING_HEADING_LABELS = {"section_header", "title"}


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def title_match_recall(gt_titles: list[str], pred_titles: list[str]):
    """Greedy title-only matching (no block_id), Levenshtein >= threshold.

    Returns (tp, missed_gt, matched_pred_count, match_log).  Each GT title
    is matched to at most one prediction and vice versa; ``tp`` is the
    number of GT titles recovered (recall numerator), ``matched_pred_count``
    is the number of distinct predictions consumed (precision numerator).
    """
    from modules.parser.resolver import _levenshtein_ratio

    matched_pred: set[int] = set()
    tp = 0
    missed: list[str] = []
    match_log: list[tuple[str, str, float]] = []
    norm_preds = [_norm(p) for p in pred_titles]

    for gt in gt_titles:
        g = _norm(gt)
        best_i, best_sim = None, 0.0
        for i, p in enumerate(norm_preds):
            if i in matched_pred:
                continue
            sim = _levenshtein_ratio(g, p)
            if sim >= TITLE_SIM_THRESHOLD and sim > best_sim:
                best_sim, best_i = sim, i
        if best_i is not None:
            matched_pred.add(best_i)
            tp += 1
            match_log.append((gt, pred_titles[best_i], round(best_sim, 3)))
        else:
            missed.append(gt)

    return tp, missed, len(matched_pred), match_log


def build_docling_converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = False              # born-digital corpus; match Constellation's text-layer-only reading
    opts.do_table_structure = False  # headings only; skip the table model
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


def docling_headings(pdf_path: str, converter) -> list[str]:
    result = converter.convert(pdf_path)
    doc = result.document
    out = []
    for t in doc.texts:
        label = str(getattr(t, "label", "")).split(".")[-1].lower()
        if label in DOCLING_HEADING_LABELS:
            txt = (t.text or "").strip()
            if txt:
                out.append(txt)
    return out


def constellation_candidate_titles(pdf_path: str):
    from infrastructure.providers.pdf_provider import PdfProvider
    from modules.parser.heading_candidates import generate_heading_candidates

    blocks = PdfProvider().extract(pdf_path)
    text_blocks = [b for b in blocks if b.type == "text" and b.text]
    candidates = generate_heading_candidates(blocks)
    return [c.title for c in candidates], len(text_blocks)


def main() -> None:
    recorded_rows: dict[str, dict] = {}
    try:
        converter = build_docling_converter()
        docling_mode = "rerun_docling_2.77.0"
    except ModuleNotFoundError as exc:
        converter = None
        docling_mode = "recorded_docling_2.77.0"
        if os.path.exists(OUT_JSON):
            with open(OUT_JSON, encoding="utf-8") as f:
                recorded_rows = {row["doc"]: row for row in json.load(f)}
        if not recorded_rows:
            raise RuntimeError(
                "Docling is not installed and no recorded baseline artifact exists"
            ) from exc
        print(
            "[docling] optional dependency missing; retaining recorded "
            "Docling 2.77.0 columns and recomputing current candidate columns",
            flush=True,
        )
    rows = []

    for fname in sorted(os.listdir(LONG_DOCS)):
        if not fname.endswith(".pdf"):
            continue
        stem = fname[:-4]
        pdf_path = os.path.join(LONG_DOCS, fname)
        gt_path = os.path.join(GT_DIR, f"{stem}.json")
        if not os.path.exists(gt_path):
            continue
        with open(gt_path, encoding="utf-8") as f:
            gt = json.load(f)
        gt_titles = [h.get("title", "") for h in gt.get("headings", []) if h.get("title")]
        if not gt_titles:
            continue

        if converter is not None:
            t0 = time.time()
            dl_titles = docling_headings(pdf_path, converter)
            dl_t = time.time() - t0
            dl_tp, dl_missed, dl_pred_used, _ = title_match_recall(gt_titles, dl_titles)
            dl_recall = dl_tp / len(gt_titles)
            dl_precision = dl_pred_used / max(len(dl_titles), 1)
            dl_f1 = (
                2 * dl_precision * dl_recall / (dl_precision + dl_recall)
                if (dl_precision + dl_recall) > 0 else 0.0
            )
            docling_fields = {
                "docling_headers": len(dl_titles),
                "docling_tp": dl_tp,
                "docling_recall": round(dl_recall * 100, 1),
                "docling_precision": round(dl_precision * 100, 1),
                "docling_f1": round(dl_f1 * 100, 1),
                "docling_missed": dl_missed,
                "docling_seconds": round(dl_t, 1),
            }
        else:
            previous = recorded_rows.get(stem)
            if previous is None:
                raise RuntimeError(f"Recorded Docling row missing for {stem}")
            docling_fields = {
                key: previous[key]
                for key in (
                    "docling_headers", "docling_tp", "docling_recall",
                    "docling_precision", "docling_f1", "docling_missed",
                    "docling_seconds",
                )
            }

        cand_titles, text_blocks = constellation_candidate_titles(pdf_path)
        c_tp, c_missed, _, _ = title_match_recall(gt_titles, cand_titles)
        c_recall = c_tp / len(gt_titles)

        row = {
            "doc": stem,
            "gt_titles": len(gt_titles),
            "docling_provenance": docling_mode,
            **docling_fields,
            "cand_titles": len(cand_titles),
            "cand_tp": c_tp,
            "cand_recall": round(c_recall * 100, 1),
            "cand_budget_pct": round(len(cand_titles) / max(text_blocks, 1) * 100, 1),
            "cand_missed": c_missed,
        }
        rows.append(row)
        print(
            f"[docling] {stem}: GT={len(gt_titles)} "
            f"Docling R={row['docling_recall']}% (P={row['docling_precision']}%) "
            f"Cand R={row['cand_recall']}%",
            flush=True,
        )

    os.makedirs("experiment_results", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    total_gt = sum(r["gt_titles"] for r in rows)
    dl_total_tp = sum(r["docling_tp"] for r in rows)
    c_total_tp = sum(r["cand_tp"] for r in rows)
    n = len(rows)
    dl_micro = dl_total_tp / max(total_gt, 1) * 100
    c_micro = c_total_tp / max(total_gt, 1) * 100
    dl_macro = sum(r["docling_recall"] for r in rows) / max(n, 1)
    c_macro = sum(r["cand_recall"] for r in rows) / max(n, 1)

    lines = [
        "# Docling vs Constellation Candidate Layer — Heading Detection Recall",
        "",
        "> Generated by `evaluation/docling_baseline.py`. Deterministic, LLM-free, reproducible without an API key.",
        "> Matching discipline: title-only Levenshtein ratio >= "
        f"{TITLE_SIM_THRESHOLD} (same ratio/threshold as `compute_section_f1`),"
        " greedy, no block_id constraint (block_id is internal to Constellation).",
        "> Docling: OCR and table-structure disabled (born-digital corpus; text-layer only, matching Constellation).",
        f"> Docling provenance: `{docling_mode}`; candidate columns are recomputed from the current provider.",
        "> GT = all TOC/manual ground-truth heading titles per document.",
        "",
        "| Document | GT | Docling hdrs | Docling R% | Docling P% | Docling F1 | Cand. R% | Cand. budget% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['doc']} | {r['gt_titles']} | {r['docling_headers']} "
            f"| {r['docling_recall']} | {r['docling_precision']} | {r['docling_f1']} "
            f"| {r['cand_recall']} | {r['cand_budget_pct']} |"
        )
    lines += [
        "",
        f"**Docling recall** — micro {dl_total_tp}/{total_gt} = **{dl_micro:.1f}%**, macro **{dl_macro:.1f}%**.",
        "",
        f"**Constellation candidate recall (same title discipline)** — micro {c_total_tp}/{total_gt} = "
        f"**{c_micro:.1f}%**, macro **{c_macro:.1f}%**.",
        "",
        "## Missed ground-truth headings",
        "",
    ]
    for r in rows:
        if r["docling_missed"]:
            lines.append(f"- **{r['doc']}** Docling missed ({len(r['docling_missed'])}): "
                         + "; ".join(r["docling_missed"]))
    lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nWritten: {OUT_MD}")
    print(f"Docling micro recall {dl_micro:.1f}%  |  Candidate micro recall {c_micro:.1f}%")


if __name__ == "__main__":
    main()
