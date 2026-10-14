"""Diagnose why GT headings miss the candidate table on low-recall docs.

For each located GT heading, report whether it:
1. was admitted before the cap (and with what reasons/score),
2. was pruned by the cap,
3. never passed admission (and what the block looks like physically).

Run: python scripts/manual/diagnose_candidate_recall.py diffusion_models openai_gpt4_tech_report
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import modules.parser.heading_candidates as hc
from infrastructure.providers.pdf_provider import PdfProvider

LONG_DOCS = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(LONG_DOCS, "ground_truth")


def diagnose(stem: str) -> None:
    print(f"\n{'=' * 70}\n{stem}\n{'=' * 70}")
    blocks = PdfProvider().extract(os.path.join(LONG_DOCS, f"{stem}.pdf"))
    with open(os.path.join(GT_DIR, f"{stem}.json"), encoding="utf-8") as f:
        gt = json.load(f)
    located = [h for h in gt.get("headings", []) if h.get("block_id", -1) >= 0]

    # Pre-cap candidates: temporarily disable the cap.
    orig_cap = hc._cap_candidates
    hc._cap_candidates = lambda cands, blocks: cands
    try:
        pre_cap = hc.generate_heading_candidates(blocks)
    finally:
        hc._cap_candidates = orig_cap
    post_cap = hc.generate_heading_candidates(blocks)

    pre_ids = {c.block_id: c for c in pre_cap}
    post_ids = {c.block_id for c in post_cap}

    n_admit = n_capped = n_reject = 0
    print(f"GT located: {len(located)} | pre-cap candidates: {len(pre_cap)} | post-cap: {len(post_cap)}")
    for h in located:
        bid = h["block_id"]
        title = h.get("title", "")[:50]
        if bid in post_ids:
            n_admit += 1
            continue
        if bid in pre_ids:
            n_capped += 1
            c = pre_ids[bid]
            print(f"  [CAPPED] #{bid} score={c.source_score:.2f} reasons={c.reasons} | {title}")
        else:
            n_reject += 1
            b = blocks[bid] if bid < len(blocks) else None
            if b is None:
                print(f"  [REJECT] #{bid} <block out of range> | {title}")
                continue
            meta = b.metadata or {}
            print(
                f"  [REJECT] #{bid} type={b.type} len={len(b.text or '')} "
                f"font={b.font_size} bold={b.is_bold} style={b.is_heading_style} "
                f"align={meta.get('alignment')} | text={(b.text or '')[:60]!r} | GT={title}"
            )
    print(f"\nSummary: in-candidates={n_admit}, pruned-by-cap={n_capped}, never-admitted={n_reject}")


if __name__ == "__main__":
    for stem in sys.argv[1:] or ["diffusion_models", "openai_gpt4_tech_report"]:
        diagnose(stem)
