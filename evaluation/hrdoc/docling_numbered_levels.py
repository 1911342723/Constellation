"""Derived baseline: Docling headings with levels from their section numbering.

Docling 2.138 returns every PDF section header at level 1. A user can recover
depth from visible numbering, so this stronger baseline re-levels Docling's
headings: "3.2.1" -> 3, "II." -> 1, IEEE "A." -> 2; an unnumbered heading
keeps the level of the previous numbered top-level context (1). Headings and
pages are Docling's; nothing is re-run.

python -m evaluation.hrdoc.docling_numbered_levels --runs <runs> --split test ...
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from evaluation.hrdoc.evaluate import Pred, load_gt, load_title, parents_from_levels, score_document

_ARABIC = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
_ROMAN = re.compile(r"^\s*[IVXLC]+\.\s+\S")
_LETTER = re.compile(r"^\s*[A-Z]\.\s+\S")


def numbering_level(text: str) -> int | None:
    match = _ARABIC.match(text)
    if match:
        return min(match.group(1).count(".") + 1, 6)
    if _ROMAN.match(text):
        return 1
    if _LETTER.match(text):
        return 2
    return None


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--runs", type=Path, required=True)
    cli.add_argument("--split", default="test")
    cli.add_argument("--toc-dir", type=Path, required=True)
    cli.add_argument("--lines-dir", type=Path, required=True)
    args = cli.parse_args()
    source = args.runs / f"{args.split}-docling"
    target = args.runs / f"{args.split}-docling-numlevels"
    target.mkdir(exist_ok=True)
    for path in sorted(source.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        preds = [Pred(p["text"], p["page"], numbering_level(p["text"]) or 1) for p in record["predictions"]]
        preds = parents_from_levels(preds)
        gt = load_gt(args.toc_dir / f"{record['doc']}.json")
        title = load_title(args.lines_dir, record["doc"])
        out = {"doc": record["doc"], "system": "docling-numlevels", "derived_from": source.name,
               "metrics": score_document(gt, preds, title),
               "metrics_generic": score_document(gt, preds, title, generic=True),
               "info": record.get("info", {}), "predictions": [p.__dict__ for p in preds],
               "seconds": record.get("seconds", 0.0)}
        (target / path.name).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(target, len(list(target.glob("*.json"))))


if __name__ == "__main__":
    main()
