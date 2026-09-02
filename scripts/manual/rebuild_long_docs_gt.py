"""Rebuild ground truth for the three broken long-doc GTs (bert/resnet/vit).

GT policy (decided 2026-06-11, see round4_failure_diagnosis.md):

- The heading list comes from the expert-known section structure of each
  paper (REFERENCE_HEADINGS in diagnose_failure_chain.py).
- ``block_id`` points at the physical block that carries the heading.
  For run-in headings merged into a body paragraph by PyMuPDF (bert),
  the merged block's id is used - that IS where the heading lives.
- ``title`` is the heading text as it physically appears in the block
  (so a perfect prediction can reach similarity 1.0).
- Matches below the confidence bar land in a review queue; resolved
  entries are pinned in MANUAL_OVERRIDES after human inspection.

Run: python scripts/manual/rebuild_long_docs_gt.py [--write]
Without --write it only prints the proposal + review queue.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.resolver import _levenshtein_ratio
from scripts.manual.diagnose_failure_chain import (
    DATA_DIR,
    GT_DIR,
    REFERENCE_HEADINGS,
    _strip_numbering,
)

# Human-verified pins: (doc, ref_title) -> block_id or None (= drop from GT).
# Populated after reviewing the proposal queue (2026-06-11 review pass):
# - "3 BERT": greedy matcher hit body-text "BERT" at block 50; the real
#   heading block is 79 (bold, 12pt, between 2.3@75 and 3.1@126).
# - "B.1 ...": heading is split across two physical lines (658 + 659);
#   block 658 carries the heading prefix and is the anchor.
MANUAL_OVERRIDES: dict[tuple[str, str], int | None] = {
    ("bert", "3 BERT"): 79,
    ("bert", "B.1 Detailed Descriptions for the GLUE Benchmark Experiments."): 658,
}

_WS_RE = re.compile(r"\s+")


def _norm(t: str) -> str:
    return _WS_RE.sub(" ", (t or "").strip().lower())


def _candidates_for(ref_title: str, blocks) -> list[dict]:
    """Score every plausible block for one reference heading."""
    ref_full = _norm(ref_title)
    ref_bare = _norm(_strip_numbering(ref_title))
    out = []

    for b in blocks:
        if b.type != "text" or not b.text:
            continue
        raw = _WS_RE.sub(" ", b.text.strip())
        text = raw.lower()
        if not text:
            continue

        mode = None
        sim = 0.0
        title = None

        if text == ref_full or text == ref_bare:
            mode, sim, title = "exact", 1.0, raw
        elif text.startswith(ref_full + " "):
            mode, sim, title = "run-in", 0.98, raw[: len(ref_full)]
        elif ref_bare != ref_full and text.startswith(ref_bare + " "):
            mode, sim, title = "run-in", 0.97, raw[: len(ref_bare)]
        elif len(text) <= 200:
            s_full = _levenshtein_ratio(ref_full, text)
            s_bare = _levenshtein_ratio(ref_bare, text) if ref_bare != ref_full else 0.0
            sim = max(s_full, s_bare)
            if sim >= 0.6:
                mode, title = "fuzzy", raw

        if mode:
            out.append({
                "block_id": b.id, "sim": round(sim, 3), "mode": mode,
                "title": title, "block_text": raw[:100],
                "font_size": b.font_size, "is_bold": b.is_bold,
            })

    out.sort(key=lambda c: (-c["sim"], c["block_id"]))
    return out[:3]


def rebuild_doc(doc_name: str) -> tuple[dict, list[dict]]:
    pdf_path = os.path.join(DATA_DIR, f"{doc_name}.pdf")
    blocks = PdfProvider().extract(pdf_path)
    refs = REFERENCE_HEADINGS[doc_name]

    # First text block as doc title fallback (matches generate_gt behaviour)
    doc_title = ""
    for b in blocks:
        if b.type == "text" and b.text and b.text.strip():
            doc_title = b.text.strip()[:100]
            break

    proposals = []
    for ref_title, level in refs:
        cands = _candidates_for(ref_title, blocks)
        proposals.append({
            "ref_title": ref_title, "level": level, "cands": cands,
        })

    # Greedy assignment by confidence so one block serves one heading.
    order = sorted(
        range(len(proposals)),
        key=lambda i: -(proposals[i]["cands"][0]["sim"] if proposals[i]["cands"] else 0.0),
    )
    used: set[int] = set()
    review: list[dict] = []
    headings_by_index: dict[int, dict] = {}

    for i in order:
        p = proposals[i]
        key = (doc_name, p["ref_title"])
        if key in MANUAL_OVERRIDES:
            pinned = MANUAL_OVERRIDES[key]
            if pinned is None:
                review.append({**p, "status": "dropped-by-override"})
                continue
            blk = next(b for b in blocks if b.id == pinned)
            raw = _WS_RE.sub(" ", (blk.text or "").strip())
            bare = _strip_numbering(p["ref_title"])
            title = raw[: len(bare)] if _norm(raw).startswith(_norm(bare)) else raw[:120]
            headings_by_index[i] = {
                "block_id": pinned, "title": title, "level": p["level"],
                "confidence": "manual",
            }
            used.add(pinned)
            continue

        chosen = None
        for c in p["cands"]:
            if c["block_id"] not in used:
                chosen = c
                break
        if chosen is None or chosen["sim"] < 0.75:
            review.append({
                **p,
                "status": "needs-review" if chosen else "not-found",
                "chosen": chosen,
            })
            continue
        used.add(chosen["block_id"])
        headings_by_index[i] = {
            "block_id": chosen["block_id"], "title": chosen["title"],
            "level": p["level"],
            "confidence": chosen["mode"],
        }

    headings = [headings_by_index[i] for i in sorted(headings_by_index)]
    gt = {
        "doc_title": doc_title,
        "doc_authors": "",
        "gt_source": "manual_expert_reference",
        "headings": [
            {"block_id": h["block_id"], "title": h["title"], "level": h["level"]}
            for h in headings
        ],
        "_meta": {
            "source": "expert-reference + physical block matching",
            "script": "scripts/manual/rebuild_long_docs_gt.py",
            "matched": len(headings),
            "reference_total": len(refs),
            "confidence_detail": [
                {"title": h["title"][:50], "block_id": h["block_id"],
                 "confidence": h["confidence"]}
                for h in headings
            ],
        },
    }
    return gt, review


def main():
    ap = argparse.ArgumentParser()
    # vit is intentionally NOT in the default list: its GT was already
    # rebuilt from the embedded TOC by evaluation/repair_gt.py.
    ap.add_argument("--docs", nargs="*", default=["bert", "resnet"],
                    choices=list(REFERENCE_HEADINGS.keys()))
    ap.add_argument("--write", action="store_true",
                    help="Write GT files (otherwise dry-run)")
    args = ap.parse_args()

    for doc in args.docs:
        print(f"\n=== {doc} ===")
        gt, review = rebuild_doc(doc)
        print(f"matched {gt['_meta']['matched']}/{gt['_meta']['reference_total']} headings")

        # Monotonicity audit: heading block ids must be strictly increasing.
        ids = [h["block_id"] for h in gt["headings"]]
        if ids != sorted(ids):
            print("  WARNING: non-monotonic block ids - inspect before writing!")

        for r in review:
            print(f"\n  [REVIEW:{r['status']}] {r['ref_title']} (L{r['level']})")
            for c in r["cands"]:
                print(f"    cand block={c['block_id']} sim={c['sim']} mode={c['mode']} "
                      f"bold={c['is_bold']} size={c['font_size']} :: {c['block_text'][:70]}")

        if args.write:
            gt.pop("_meta", None)
            gt_path = os.path.join(GT_DIR, f"{doc}.json")
            with open(gt_path, "w", encoding="utf-8") as f:
                json.dump(gt, f, ensure_ascii=False, indent=4)
            print(f"  -> wrote {gt_path}")


if __name__ == "__main__":
    main()
