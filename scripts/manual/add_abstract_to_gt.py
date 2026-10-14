"""Add the missing Abstract entry to TOC-derived long-doc GTs.

PDF embedded TOCs almost never list "Abstract", but it is a real
section that any structure parser is expected to detect.  Scoring it
as a false positive would systematically punish correct behaviour, so
every GT gets an Abstract entry pinned to the physical block.

Run: python scripts/manual/add_abstract_to_gt.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from infrastructure.providers.pdf_provider import PdfProvider

DATA_DIR = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(DATA_DIR, "ground_truth")


def main():
    for fname in sorted(os.listdir(GT_DIR)):
        if not fname.endswith(".json"):
            continue
        gt_path = os.path.join(GT_DIR, fname)
        with open(gt_path, encoding="utf-8") as f:
            gt = json.load(f)

        headings = gt.get("headings", [])
        if not headings:
            print(f"{fname}: empty GT, skip")
            continue
        if any(h["title"].strip().lower() == "abstract" for h in headings):
            print(f"{fname}: already has Abstract")
            continue

        pdf_path = os.path.join(DATA_DIR, fname.replace(".json", ".pdf"))
        blocks = PdfProvider().extract(pdf_path)
        abstract_block = next(
            (b for b in blocks
             if b.type == "text" and b.text
             and b.text.strip().lower().rstrip(".:") == "abstract"),
            None,
        )
        if abstract_block is None:
            print(f"{fname}: no standalone Abstract block found, skip")
            continue

        first_id = min(h["block_id"] for h in headings if h["block_id"] >= 0)
        if abstract_block.id >= first_id:
            print(f"{fname}: Abstract block {abstract_block.id} not before "
                  f"first heading {first_id}, skip (manual check)")
            continue

        headings.insert(0, {
            "block_id": abstract_block.id,
            "title": "Abstract",
            "level": 1,
        })
        gt["gt_source"] = gt.get("gt_source", "") + " + manual Abstract entry"
        with open(gt_path, "w", encoding="utf-8") as f:
            json.dump(gt, f, ensure_ascii=False, indent=4)
        print(f"{fname}: inserted Abstract at block {abstract_block.id}")


if __name__ == "__main__":
    main()
