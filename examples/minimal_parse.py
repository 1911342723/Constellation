"""Smallest end-to-end Constellation run.

Usage:
    python examples/minimal_parse.py <document.docx | document.pdf>

Prerequisites:
    1. pip install -r requirements.txt
    2. Set LLM_API_KEY / LLM_BASE_URL / LLM_MODEL in .env
       (Stage 3 calls the model; Stages 1, 2, 2.5 and 4 run offline)

Output:
    - a section-tree outline on stdout
    - <document>.md in the current directory (lossless Markdown)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.parser.parser import CaliperParser


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    path = sys.argv[1]
    ext = os.path.splitext(path)[1].lower()

    # Stage 1: physical extraction
    if ext == ".docx":
        from infrastructure.providers.docx_provider import DocxProvider
        blocks = DocxProvider().extract(path)
    elif ext == ".pdf":
        from infrastructure.providers.pdf_provider import PdfProvider
        blocks = PdfProvider().extract(path)
    else:
        print(f"Unsupported format: {ext} (expected .docx or .pdf)")
        sys.exit(1)

    print(f"Stage 1 complete: {len(blocks)} blocks")

    # Stages 2-4: skeleton compression -> LLM candidate confirmation -> closure
    parser = CaliperParser()
    tree = parser.parse(blocks)

    stats = tree.get_stats()
    print(f"Title:    {stats['doc_title']}")
    print(f"Sections: {stats['total_sections']} (max depth {stats['max_depth']})")

    def _print_node(node, indent=0):
        print("  " * indent + f"- L{node.level} {node.title}")
        for child in node.children:
            _print_node(child, indent + 1)

    for node in tree.nodes:
        _print_node(node)

    out_path = os.path.splitext(os.path.basename(path))[0] + ".md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tree.to_markdown())
    print(f"Markdown written to: {out_path}")


if __name__ == "__main__":
    main()
