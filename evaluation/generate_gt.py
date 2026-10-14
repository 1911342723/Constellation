"""Generate ground-truth JSON for benchmark documents.

Uses DocxProvider to extract blocks, then applies physical-feature
heuristics to identify headings.  Output is a GT JSON file that can
be manually verified and corrected.

Run: python -m evaluation.generate_gt
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.providers.docx_provider import DocxProvider


GT_SCHEMA = {
    "doc_title": "",
    "doc_authors": "",
    "headings": [],
}


def generate_gt_for_docx(docx_path: str) -> dict:
    """Generate a GT dict for a single .docx file."""
    provider = DocxProvider()
    blocks = provider.extract(docx_path)

    # Find title: first block that is_potential_title() or first text block
    doc_title = ""
    headings = []

    for b in blocks:
        if b.type != "text" or not b.text:
            continue
        if not doc_title:
            doc_title = b.text.strip()[:100]
        if b.is_potential_title():
            headings.append({
                "block_id": b.id,
                "title": b.text.strip(),
                "level": b.heading_level if b.heading_level else 1,
            })

    return {
        "doc_title": doc_title,
        "doc_authors": "",
        "headings": headings,
    }


def main():
    benchmark_dirs = [
        ("tests/data/benchmarks", "evaluation/ground_truth"),
    ]

    for data_dir, gt_dir in benchmark_dirs:
        if not os.path.isdir(data_dir):
            continue
        os.makedirs(gt_dir, exist_ok=True)

        for fname in sorted(os.listdir(data_dir)):
            if not fname.endswith(".docx"):
                continue

            gt_name = fname.replace(".docx", ".json")
            gt_path = os.path.join(gt_dir, gt_name)

            # Skip if GT already exists
            if os.path.exists(gt_path):
                print(f"  SKIP {gt_name} (already exists)")
                continue

            docx_path = os.path.join(data_dir, fname)
            print(f"  Generating {gt_name} from {fname}...")
            gt = generate_gt_for_docx(docx_path)

            with open(gt_path, "w", encoding="utf-8") as f:
                json.dump(gt, f, ensure_ascii=False, indent=4)

            print(f"    -> {len(gt['headings'])} headings found")


if __name__ == "__main__":
    main()
