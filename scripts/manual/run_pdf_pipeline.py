"""End-to-end PDF pipeline smoke run.

Converts benchmark .docx files to PDF, runs the full
Constellation pipeline (PdfProvider -> CaliperParser),
and writes Markdown output for manual verification.

Run: python scripts/manual/run_pdf_pipeline.py
"""
from __future__ import annotations

import os
import sys
import logging

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Ensure project root is on sys.path
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

import fitz  # PyMuPDF
from infrastructure.providers.docx_provider import DocxProvider
from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.parser import CaliperParser


BENCHMARK_DIR = os.path.join(PROJECT_ROOT, "tests", "data", "benchmarks")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "test_output_pdf")


def docx_to_pdf_bytes(docx_path: str) -> bytes:
    """Convert a .docx file to PDF bytes using DocxProvider + PyMuPDF.

    Strategy: extract blocks from the docx, then render them into a
    new PDF document with PyMuPDF.  This preserves text content and
    basic formatting for pipeline testing.
    """
    blocks = DocxProvider().extract(docx_path)

    doc = fitz.open()
    page = doc.new_page()
    y = 72  # start 1 inch from top
    page_width = page.rect.width - 144  # 1-inch margins

    for block in blocks:
        if block.type == "text" and block.text:
            text = block.text
            fontsize = block.font_size if block.font_size else 12
            fontname = "hebo" if block.is_bold else "helv"

            # Handle multi-line text
            lines = text.split("\n")
            for line in lines:
                if not line.strip():
                    y += fontsize * 0.5
                    continue

                # Check if we need a new page
                if y > page.rect.height - 72:
                    page = doc.new_page()
                    y = 72

                # Determine x position based on alignment
                if block.alignment == "center":
                    # Estimate text width
                    text_width = len(line) * fontsize * 0.5
                    x = max(72, (page.rect.width - text_width) / 2)
                elif block.alignment == "right":
                    x = page_width
                else:
                    x = 72

                try:
                    page.insert_text(
                        (x, y), line, fontname=fontname, fontsize=fontsize,
                    )
                except Exception:
                    # Fallback to Helvetica if font fails
                    page.insert_text(
                        (x, y), line, fontname="helv", fontsize=fontsize,
                    )
                y += fontsize * 1.4

        elif block.type == "table" and block.text:
            # Render table as monospace text
            if y > page.rect.height - 120:
                page = doc.new_page()
                y = 72

            for line in block.text.split("\n"):
                if y > page.rect.height - 72:
                    page = doc.new_page()
                    y = 72
                page.insert_text((72, y), line, fontname="cour", fontsize=9)
                y += 12

        elif block.type == "image":
            # Insert a placeholder for images
            if y > page.rect.height - 72:
                page = doc.new_page()
                y = 72
            caption = block.caption or "[Image]"
            page.insert_text((72, y), caption, fontname="helv", fontsize=10)
            y += 20

        y += 6  # spacing between blocks

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def run_pipeline(docx_name: str) -> None:
    """Run the full pipeline: docx -> pdf -> blocks -> markdown."""
    docx_path = os.path.join(BENCHMARK_DIR, docx_name)
    if not os.path.exists(docx_path):
        print(f"  SKIP: {docx_path} not found")
        return

    print(f"\n{'='*60}")
    print(f"  {docx_name}")
    print(f"{'='*60}")

    # Step 1: docx -> PDF bytes
    print("  [1/3] Converting .docx -> PDF bytes...")
    pdf_bytes = docx_to_pdf_bytes(docx_path)
    print(f"         PDF size: {len(pdf_bytes)} bytes")

    # Save PDF for reference
    pdf_path = os.path.join(OUTPUT_DIR, docx_name.replace(".docx", ".pdf"))
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    print(f"         Saved: {pdf_path}")

    # Step 2: PDF bytes -> Blocks via PdfProvider
    print("  [2/3] Extracting blocks via PdfProvider...")
    provider = PdfProvider()
    blocks = provider.extract_from_bytes(pdf_bytes)
    print(f"         Extracted {len(blocks)} blocks")

    for b in blocks[:5]:
        preview = (b.text or "")[:60].replace("\n", " ")
        print(f"           [{b.id}] {b.type}: {preview}")
    if len(blocks) > 5:
        print(f"           ... and {len(blocks) - 5} more")

    # Step 3: Blocks -> DocumentTree -> Markdown
    print("  [3/3] Running CaliperParser...")
    parser = CaliperParser()
    try:
        tree = parser.parse(blocks)
        sections = tree.to_markdown_sections()
        print(f"         Title: {tree.doc_title}")
        print(f"         Sections: {len(sections)}")

        # Write markdown output
        md_path = os.path.join(OUTPUT_DIR, docx_name.replace(".docx", ".md"))
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# {tree.doc_title}\n\n")
            if tree.doc_authors:
                f.write(f"*{tree.doc_authors}*\n\n")
            for sec in sections:
                f.write(sec["content"])
                f.write("\n\n---\n\n")
        print(f"         Markdown: {md_path}")

        # Print first section preview
        if sections:
            preview = sections[0]["content"][:300]
            print(f"\n  Preview ({sections[0]['title']}):")
            for line in preview.split("\n")[:8]:
                print(f"    {line}")

    except Exception as e:
        print(f"  ERROR: {e}")

    parser.clear_cache()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    docx_files = [f for f in os.listdir(BENCHMARK_DIR) if f.endswith(".docx")]
    docx_files.sort()

    print(f"Benchmark dir: {BENCHMARK_DIR}")
    print(f"Output dir:    {OUTPUT_DIR}")
    print(f"Files:         {len(docx_files)}")

    for docx_name in docx_files:
        run_pipeline(docx_name)

    print(f"\n{'='*60}")
    print(f"  Done! Check output in: {OUTPUT_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
