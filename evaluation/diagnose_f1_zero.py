"""Diagnose the three F1=0.000 long-document failures (BERT / ResNet / ViT).

Per-stage offline dump (no LLM quota needed):

1. GT health audit over all long_docs ground-truth files
   (empty headings, block_id=0 placeholders, duplicated ids).
2. Stage 1 dump: physical features of extracted blocks
   (font-size distribution, bold counts, is_potential_title hits).
3. Stage 2.5 dump: candidate table vs. known real section titles,
   to tell "candidate generator went silent" apart from "GT is broken".

Run: python -m evaluation.diagnose_f1_zero
Outputs:
    experiment_results/f1_zero_diagnosis.md
    experiment_results/f1_zero_diagnosis_raw.json
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.heading_candidates import (
    _body_font_size,
    generate_heading_candidates,
)

LONG_DOCS_DIR = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(LONG_DOCS_DIR, "ground_truth")
OUT_MD = os.path.join("experiment_results", "f1_zero_diagnosis.md")
OUT_JSON = os.path.join("experiment_results", "f1_zero_diagnosis_raw.json")

# Failure cases + one healthy control (attention, F1=0.809).
TARGET_DOCS = {
    "bert": [
        "1 Introduction",
        "2 Related Work",
        "3 BERT",
        "4 Experiments",
        "5 Ablation Studies",
        "6 Conclusion",
        "References",
    ],
    "resnet": [
        "1. Introduction",
        "2. Related Work",
        "3. Deep Residual Learning",
        "4. Experiments",
        "References",
    ],
    "vit": [
        "1 Introduction",
        "2 Related Work",
        "3 Method",
        "4 Experiments",
        "5 Conclusion",
        "References",
    ],
    "attention_is_all_you_need": [
        "1 Introduction",
        "2 Background",
        "3 Model Architecture",
        "7 Conclusion",
        "References",
    ],
}


def norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def audit_gt(gt_path: str) -> dict:
    with open(gt_path, encoding="utf-8") as f:
        gt = json.load(f)
    headings = gt.get("headings", [])
    ids = [h.get("block_id", 0) for h in headings]
    return {
        "headings": len(headings),
        "id_zero": sum(1 for i in ids if i == 0),
        "id_duplicates": sum(c - 1 for c in Counter(ids).values() if c > 1),
        "monotonic": all(a <= b for a, b in zip(ids, ids[1:], strict=False)) if ids else True,
        "doc_title": gt.get("doc_title", ""),
    }


def find_block_matches(blocks, expected_title: str):
    """Find blocks whose text equals / starts with the expected title."""
    target = norm(expected_title)
    hits = []
    for b in blocks:
        if b.type != "text" or not b.text:
            continue
        text = norm(b.text)
        if text == target or text.startswith(target):
            hits.append(b)
        # PDF extraction often drops the numbering into a separate span;
        # also try matching the title without its leading number.
        else:
            bare = target.split(" ", 1)[-1]
            if len(bare) > 3 and text == bare:
                hits.append(b)
    return hits


def block_features(b, body_size: float) -> dict:
    return {
        "block_id": b.id,
        "text": (b.text or "")[:80],
        "font_size": b.font_size,
        "font_ratio": round(b.font_size / body_size, 3) if b.font_size and body_size else None,
        "is_bold": b.is_bold,
        "is_heading_style": b.is_heading_style,
        "alignment": b.alignment,
        "is_potential_title": b.is_potential_title(min_body_size=body_size),
        "page": (b.metadata or {}).get("page"),
    }


def diagnose_doc(stem: str, expected_titles: list[str]) -> dict:
    pdf_path = os.path.join(LONG_DOCS_DIR, f"{stem}.pdf")
    provider = PdfProvider()
    blocks = provider.extract(pdf_path)

    text_blocks = [b for b in blocks if b.type == "text" and b.text]
    body_size = _body_font_size(blocks)
    font_sizes = sorted({round(b.font_size, 1) for b in text_blocks if b.font_size})
    candidates = generate_heading_candidates(blocks)
    candidate_id_set = {c.block_id for c in candidates}

    title_probes = []
    for title in expected_titles:
        hits = find_block_matches(blocks, title)
        probe = {
            "expected": title,
            "found_in_blocks": len(hits),
            "in_candidates": any(h.id in candidate_id_set for h in hits),
            "matches": [block_features(h, body_size) for h in hits[:3]],
        }
        title_probes.append(probe)

    reason_counter = Counter()
    for c in candidates:
        for r in c.reasons:
            reason_counter[r] += 1

    return {
        "stem": stem,
        "total_blocks": len(blocks),
        "text_blocks": len(text_blocks),
        "body_font_size": body_size,
        "distinct_font_sizes": font_sizes,
        "bold_blocks": sum(1 for b in text_blocks if b.is_bold),
        "potential_title_blocks": sum(
            1 for b in text_blocks if b.is_potential_title(min_body_size=body_size)
        ),
        "candidates": len(candidates),
        "candidate_reasons": dict(reason_counter),
        "title_probes": title_probes,
    }


def main() -> None:
    report: dict = {"gt_audit": {}, "docs": {}}

    for fname in sorted(os.listdir(GT_DIR)):
        if fname.endswith(".json"):
            report["gt_audit"][fname[:-5]] = audit_gt(os.path.join(GT_DIR, fname))

    for stem, titles in TARGET_DOCS.items():
        print(f"diagnosing {stem} ...")
        report["docs"][stem] = diagnose_doc(stem, titles)

    os.makedirs("experiment_results", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    lines = ["# F1=0 Failure Diagnosis (BERT / ResNet / ViT)", ""]
    lines.append("> Generated by `evaluation/diagnose_f1_zero.py` - offline, no LLM calls.")
    lines.append("")

    lines.append("## 1. GT Health Audit (all long_docs)")
    lines.append("")
    lines.append("| GT file | headings | block_id=0 | duplicate ids | monotonic |")
    lines.append("|---|---:|---:|---:|---|")
    for name, a in report["gt_audit"].items():
        flag = ""
        if a["headings"] == 0:
            flag = " **(EMPTY)**"
        elif a["id_zero"] > a["headings"] * 0.3:
            flag = " **(PLACEHOLDER-HEAVY)**"
        lines.append(
            f"| {name}{flag} | {a['headings']} | {a['id_zero']} "
            f"| {a['id_duplicates']} | {a['monotonic']} |"
        )
    lines.append("")

    lines.append("## 2. Per-Document Stage 1 / 2.5 Dump")
    lines.append("")
    for stem, d in report["docs"].items():
        lines.append(f"### {stem}")
        lines.append("")
        lines.append(f"- blocks: {d['total_blocks']} (text {d['text_blocks']})")
        lines.append(f"- body font size (median): {d['body_font_size']}")
        lines.append(f"- distinct font sizes: {d['distinct_font_sizes']}")
        lines.append(
            f"- bold blocks: {d['bold_blocks']}, "
            f"is_potential_title hits: {d['potential_title_blocks']}"
        )
        lines.append(
            f"- Stage 2.5 candidates: {d['candidates']} "
            f"(reasons: {d['candidate_reasons']})"
        )
        lines.append("")
        lines.append("| expected real title | found in blocks | in candidate table | features of first match |")
        lines.append("|---|---:|---|---|")
        for probe in d["title_probes"]:
            feat = ""
            if probe["matches"]:
                m = probe["matches"][0]
                feat = (
                    f"id={m['block_id']}, size={m['font_size']}, "
                    f"ratio={m['font_ratio']}, bold={m['is_bold']}, "
                    f"pot_title={m['is_potential_title']}"
                )
            lines.append(
                f"| {probe['expected']} | {probe['found_in_blocks']} "
                f"| {probe['in_candidates']} | {feat} |"
            )
        lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"written: {OUT_MD}")
    print(f"written: {OUT_JSON}")


if __name__ == "__main__":
    main()
