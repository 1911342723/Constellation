"""Re-align manually-annotated GT block_ids to the current block stream.

repair_gt.py only handles documents with an embedded PDF TOC; manual GT
(bert, resnet, ...) must be re-aligned separately whenever provider
behaviour changes the block stream.  Alignment is by exact / prefix
title match with a monotonicity constraint — every entry must locate
with similarity >= 0.9, otherwise the script aborts without writing.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.resolver import _levenshtein_ratio

DATA_DIR = Path("tests/data/long_docs")
GT_DIR = DATA_DIR / "ground_truth"


def norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def realign(stem: str) -> None:
    gt_path = GT_DIR / f"{stem}.json"
    gt = json.load(open(gt_path, encoding="utf-8"))
    if gt.get("gt_source") != "manual_expert_reference":
        print(f"{stem}: gt_source={gt.get('gt_source')}, skip (handled by repair_gt)")
        return

    provider = PdfProvider()
    blocks = provider.extract(str(DATA_DIR / f"{stem}.pdf"))
    text_blocks = [b for b in blocks if b.type == "text" and (b.text or "").strip()]

    new_ids = []
    last_id = -1
    for h in gt["headings"]:
        t = norm(h["title"])
        best_id, best_sim = -1, 0.0
        for b in text_blocks:
            if b.id <= last_id:
                continue
            bt = norm(b.text)
            if bt == t:
                sim = 1.0
            elif bt.startswith(t) and len(bt) < len(t) * 3:
                sim = 0.9
            else:
                sim = _levenshtein_ratio(t, bt)
            if sim > best_sim:
                best_id, best_sim = b.id, sim
        if best_sim < 0.9:
            print(f"{stem}: FAILED to locate {h['title']!r} (best sim {best_sim:.2f}) — aborting, no write")
            return
        new_ids.append(best_id)
        last_id = best_id

    changed = sum(
        1 for h, nid in zip(gt["headings"], new_ids, strict=True) if h["block_id"] != nid
    )
    for h, nid in zip(gt["headings"], new_ids, strict=True):
        h["block_id"] = nid

    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=4)
    print(f"{stem}: {len(new_ids)} headings, {changed} ids shifted, written")


if __name__ == "__main__":
    for p in sorted(GT_DIR.glob("*.json")):
        realign(p.stem)
