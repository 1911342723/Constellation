"""Re-score stored per-document records under the final scoring conventions.

No system is re-run: every record holds its predictions (text, page, level,
parent), and the generative baseline additionally holds its raw heading list.

For every record:
  metrics          title-ignored scoring (the main convention, all systems)
  metrics_generic  additionally without front/back-matter titles (sensitivity)
  metrics_v1       the metrics first written by evaluate.py (kept for audit)
Generative-baseline records are rebuilt from their raw headings: Markdown level
normalisation, pages located in the source text, parents from levels; their
unnormalised scoring is kept as metrics_raw.

python -m evaluation.hrdoc.rescore --runs <runs> --toc-dir <test_eval_toc> --lines-dir <test_eval> --pdf-dir <pdfs>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.hrdoc.evaluate import (Pred, load_gt, load_title, locate_pages, normalize_markdown_levels,
                                       parents_from_levels, pdf_page_texts, score, score_document)


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--runs", type=Path, required=True)
    cli.add_argument("--toc-dir", type=Path, required=True)
    cli.add_argument("--lines-dir", type=Path, required=True)
    cli.add_argument("--pdf-dir", type=Path, required=True)
    args = cli.parse_args()
    for run in sorted(p for p in args.runs.iterdir() if p.is_dir()):
        count = 0
        for path in sorted(run.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if "metrics" not in record or "error" in record:
                continue
            doc = record["doc"]
            gt = load_gt(args.toc_dir / f"{doc}.json")
            title = load_title(args.lines_dir, doc)
            record.setdefault("metrics_v1", record["metrics"])
            if record["system"] == "llm_markdown":
                info = record["info"]
                raw = info.get("raw_headings") or [[p["text"], p["page"], p["level"]] for p in record["predictions"]]
                info["raw_headings"] = raw
                raw_preds = parents_from_levels([Pred(t, pg, lv) for t, pg, lv in raw])
                record["metrics_raw"] = score(gt, raw_preds)
                preds = [Pred(t, pg, lv) for t, pg, lv in raw]
                texts = pdf_page_texts((args.pdf_dir / f"{doc}.pdf").read_bytes())
                preds = parents_from_levels(locate_pages(normalize_markdown_levels(preds), texts))
                record["predictions"] = [p.__dict__ for p in preds]
            else:
                preds = [Pred(p["text"], p["page"], p["level"], p.get("parent", -1)) for p in record["predictions"]]
            record["metrics"] = score_document(gt, preds, title)
            record["metrics_generic"] = score_document(gt, preds, title, generic=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            count += 1
        print(f"{run.name}: {count} records re-scored", flush=True)


if __name__ == "__main__":
    main()
