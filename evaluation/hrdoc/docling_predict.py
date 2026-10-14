"""Run Docling on Comp-HRDoc PDFs and store its section headers.

Runs in a separate environment that has ``docling`` installed (it does not
import the project).  OCR and table-structure recognition are disabled so that,
like Constellation, Docling reads only the born-digital text layer.  Every
``section_header`` / ``title`` item becomes a predicted heading with Docling's
own level and page.  Output: one ``<id>.json`` per document, consumed by
``evaluate --system docling``.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--pdf-dir", type=Path, required=True)
    cli.add_argument("--ids", type=Path, required=True, help="JSON list of document ids")
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--shard", default="0/1")
    args = cli.parse_args()

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from importlib.metadata import version

    options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    args.out.mkdir(parents=True, exist_ok=True)
    shard, shards = (int(x) for x in args.shard.split("/"))
    ids = json.loads(args.ids.read_text())[shard::shards]
    for doc_id in ids:
        target = args.out / f"{doc_id}.json"
        if target.exists():
            continue
        started = time.perf_counter()
        record = {"doc": doc_id, "docling": version("docling")}
        try:
            document = converter.convert(str(args.pdf_dir / f"{doc_id}.pdf")).document
            headings = []
            for item, _depth in document.iterate_items():
                label = str(getattr(item, "label", ""))
                if label.endswith("section_header") or label.endswith("title"):
                    page = item.prov[0].page_no - 1 if getattr(item, "prov", None) else 0
                    headings.append({"text": item.text, "page": page,
                                     "level": int(getattr(item, "level", 1) or 1), "label": label})
            record["headings"] = headings
        except Exception as exc:
            record["error"] = type(exc).__name__
        record["seconds"] = time.perf_counter() - started
        target.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        print(doc_id, len(record.get("headings", [])), record.get("error", ""), flush=True)


if __name__ == "__main__":
    main()
