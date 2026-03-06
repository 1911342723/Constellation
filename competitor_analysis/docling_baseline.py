"""Docling baseline — IBM open-source DOCX parser.

Runs Docling on benchmark documents and extracts structural information
for comparison against Constellation.

Install: pip install docling
Usage:  python -m competitor_analysis.docling_baseline
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def convert_docling(filepath: Path) -> dict:
    """Parse a DOCX file with Docling and return structural metrics."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return {
            "error": "docling not installed — pip install docling",
            "chars": 0,
            "headings": 0,
            "max_depth": 0,
            "time_s": 0,
        }

    t0 = time.perf_counter()
    try:
        converter = DocumentConverter()
        result = converter.convert(str(filepath))

        doc = result.document
        md_text = doc.export_to_markdown()
        chars = len(md_text.strip())

        heading_count = 0
        max_depth = 0
        for line in md_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                level = 0
                for ch in stripped:
                    if ch == "#":
                        level += 1
                    else:
                        break
                heading_count += 1
                max_depth = max(max_depth, level)

        elapsed = time.perf_counter() - t0
        return {
            "chars": chars,
            "headings": heading_count,
            "max_depth": max_depth,
            "time_s": elapsed,
        }
    except Exception as e:
        return {
            "error": str(e),
            "chars": 0,
            "headings": 0,
            "max_depth": 0,
            "time_s": time.perf_counter() - t0,
        }


def main():
    root_dir = Path(__file__).parent.parent
    data_dir = root_dir / "tests" / "data"

    if not data_dir.exists():
        print(f"Data dir not found: {data_dir}")
        return

    docx_files = sorted(data_dir.rglob("*.docx"))
    if not docx_files:
        print("No .docx files found")
        return

    print("=" * 70)
    print("  Docling Baseline Evaluation")
    print("=" * 70)

    for fpath in docx_files:
        print(f"\n--- {fpath.name} ---")
        result = convert_docling(fpath)
        if "error" in result:
            print(f"  Error: {result['error']}")
        else:
            print(f"  Characters: {result['chars']}")
            print(f"  Headings found: {result['headings']}")
            print(f"  Max heading depth: {result['max_depth']}")
            print(f"  Time: {result['time_s']:.2f}s")


if __name__ == "__main__":
    main()
