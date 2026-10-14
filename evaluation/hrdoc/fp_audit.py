"""Sample false positives of one run for a manual audit (reference left unchanged).

Writes <out>/fp_audit_sample.json with, for each sampled false positive, the
predicted title, level, page, the neighbouring source lines and the nearest
reference sections, so that each item can be judged as
  genuine_heading  a section title in the document that the reference omits
  error            not a section title (list item, figure text, title page...)
The verdicts are stored in experiment_results/hrdoc_2026-10-13/fp_audit.json.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from evaluation.hrdoc.evaluate import Pred, _blocks, load_gt, match, norm


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--run", type=Path, required=True)
    cli.add_argument("--toc-dir", type=Path, required=True)
    cli.add_argument("--pdf-dir", type=Path, required=True)
    cli.add_argument("--n", type=int, default=100)
    cli.add_argument("--seed", type=int, default=20261014)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()

    pool = []
    for path in sorted(args.run.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if "metrics" not in record:
            continue
        gt = load_gt(args.toc_dir / f"{record['doc']}.json")
        preds = [Pred(p["text"], p["page"], p["level"]) for p in record["predictions"]]
        matched = set(match(gt, preds).values())
        pool += [(record["doc"], i) for i in range(len(preds)) if i not in matched]
    random.Random(args.seed).shuffle(pool)
    sample = sorted(pool[:args.n])

    items = []
    block_cache: dict[str, list] = {}
    for doc, index in sample:
        record = json.loads((args.run / f"{doc}.json").read_text(encoding="utf-8"))
        pred = record["predictions"][index]
        if doc not in block_cache:
            data = (args.pdf_dir / f"{doc}.pdf").read_bytes()
            block_cache[doc] = [b for b in _blocks(data, False) if b.type == "text" and b.text]
        blocks = block_cache[doc]
        key = norm(pred["text"])[:40]
        position = next((i for i, b in enumerate(blocks)
                         if int((b.metadata or {}).get("page", 0)) - 1 == pred["page"]
                         and norm(b.text).startswith(key)), None)
        context = []
        if position is not None:
            for b in blocks[max(0, position - 2): position + 3]:
                context.append({"text": b.text[:160], "font_size": b.font_size, "bold": b.is_bold,
                                "alignment": b.alignment, "this": b is blocks[position]})
        gt = load_gt(args.toc_dir / f"{doc}.json")
        nearby = [s.text[:80] for s in gt if abs(s.page - pred["page"]) <= 1]
        items.append({"doc": doc, "pred_index": index, "title": pred["text"], "level": pred["level"],
                      "page": pred["page"], "context": context, "reference_sections_nearby": nearby})
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "fp_audit_sample.json").write_text(
        json.dumps({"run": str(args.run), "seed": args.seed, "pool": len(pool), "items": items},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(pool), "false positives;", len(items), "sampled")


if __name__ == "__main__":
    main()
