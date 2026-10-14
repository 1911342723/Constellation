"""Recompute word-level preservation from stored output text (no system is re-run).

Generative records store their Markdown output (``info.output_markdown``) and
Constellation records their image-free export (``info.export_markdown``); the
source is the PDF text layer read with PyMuPDF.

python -m evaluation.hrdoc.recompute_preservation --pdf-dir <pdfs> <run dir>...
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.hrdoc.evaluate import _PAGE_MARK, pdf_page_texts, word_preservation


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--pdf-dir", type=Path, required=True)
    cli.add_argument("runs", type=Path, nargs="+")
    args = cli.parse_args()
    for run in args.runs:
        count = 0
        for path in sorted(run.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            info = record.get("info", {})
            text = info.get("output_markdown") or info.get("export_markdown")
            if text is None:
                continue
            source = "\n".join(pdf_page_texts((args.pdf_dir / f"{record['doc']}.pdf").read_bytes()))
            info["word_preservation"] = word_preservation(source, _PAGE_MARK.sub(" ", text))
            path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            count += 1
        print(f"{run.name}: {count} records", flush=True)


if __name__ == "__main__":
    main()
