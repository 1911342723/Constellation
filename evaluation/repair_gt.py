"""Repair long-document ground truth by aligning PDF TOC entries to block IDs.

Root cause being fixed (2026-06-10 diagnosis):
    The legacy GT generator extracted heading titles from the embedded
    PDF TOC but failed to locate them in the block sequence, writing
    ``block_id = 0`` as a silent failure marker.  Since the evaluation
    matcher requires ``|pred.block_id - gt.block_id| <= 3``, every
    mislocated GT entry is an automatic false negative — three papers
    scored F1 = 0.000 purely because of this.

This script:
    1. Reads the embedded TOC via PyMuPDF (authoritative, author-written,
       independent of the system under test).
    2. Parses the PDF with PdfProvider to obtain the block sequence.
    3. Fuzzy-aligns each TOC title to a block within the TOC page +/- 1.
    4. Rewrites the GT JSON (block_id = -1 for entries that cannot be
       located; never 0, which is a valid block id).
    5. Emits a Markdown repair report with per-entry confidence for
       manual spot-checking.

Documents without an embedded TOC (e.g. bert.pdf, resnet.pdf) are NOT
auto-labelled: generating GT from the system's own candidate generator
would be circular. They are listed in the report as requiring manual
annotation.

Run: python -m evaluation.repair_gt
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import fitz  # PyMuPDF

from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.resolver import _levenshtein_ratio

DATA_DIR = Path("tests/data/long_docs")
GT_DIR = DATA_DIR / "ground_truth"
REPORT_PATH = Path("experiment_results/gt_repair_report.md")

# Alignment thresholds
MIN_ACCEPT_SCORE = 0.55     # below this the entry is marked unlocated (-1)
LOW_CONFIDENCE = 0.75       # below this the entry is flagged for review
PAGE_SLACK = 1              # search blocks within toc_page +/- this


_NUM_PREFIX_RE = re.compile(
    r"^\s*(?:[\dIVXLCDM]+(?:\.\d+)*[.)]?|[A-Z][.)]|appendix\s+[a-z0-9]+)\s+",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Lowercase, NFKC-fold, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "")
    return " ".join(text.lower().split())


def _strip_numbering(text: str) -> str:
    """Drop leading section numbering ('3.1 ', 'IV. ', 'A) ')."""
    return _NUM_PREFIX_RE.sub("", text).strip()


def _match_score(toc_title: str, block_text: str) -> float:
    """Score how well a block's text matches a TOC title (0.0-1.0)."""
    a = _normalize(toc_title)
    b = _normalize(block_text)
    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    # Numbering-insensitive exact match ("Introduction" vs "1 Introduction")
    a_core = _strip_numbering(a)
    b_core = _strip_numbering(b)
    if a_core and a_core == b_core:
        return 0.98

    # Containment (block may carry trailing artifacts or the TOC may
    # omit numbering); guard BOTH sides against trivially short text —
    # a one-character block like a stray formula glyph ("d") must not
    # match "Diffusion Models ..." via startswith.
    if len(a_core) >= 6 and len(b_core) >= 6:
        if b_core.startswith(a_core) or a_core.startswith(b_core):
            return 0.92
        if a_core in b_core:
            return 0.85

    return _levenshtein_ratio(a_core, b_core)


def align_toc_to_blocks(toc: list, blocks: list) -> list[dict]:
    """Align TOC entries to block ids.

    Args:
        toc: PyMuPDF ``get_toc()`` output — list of [level, title, page]
            with 1-based pages.
        blocks: PdfProvider Block list (metadata.page is 1-based).

    Returns:
        List of dicts: {block_id, title, level, score, matched_text, page}.
    """
    # Index text blocks by page for the page-window search
    by_page: dict[int, list] = {}
    for blk in blocks:
        if blk.type != "text" or not (blk.text or "").strip():
            continue
        page = (blk.metadata or {}).get("page")
        if isinstance(page, int):
            by_page.setdefault(page, []).append(blk)

    results = []
    last_matched_id = -1  # enforce monotonic ordering of matches

    for level, title, page in toc:
        candidates = []
        for p in range(page - PAGE_SLACK, page + PAGE_SLACK + 1):
            candidates.extend(by_page.get(p, []))

        best_id, best_score, best_text = -1, 0.0, ""
        for blk in candidates:
            # TOC order is document order: forbid going backwards,
            # which prevents repeated titles (e.g. "Introduction" in
            # running headers) from matching an earlier block.
            if blk.id <= last_matched_id:
                continue
            score = _match_score(title, blk.text)
            if score > best_score:
                best_id, best_score, best_text = blk.id, score, blk.text.strip()

        if best_score >= MIN_ACCEPT_SCORE:
            # Run-in headings (LaTeX \paragraph{...}: "Knowledge
            # Distillation. Approaches that use ...") share a block with
            # their body paragraph — no standalone heading block exists
            # at the block granularity, so no block-level parser can
            # ever anchor them.  Mark them instead of pretending the
            # paragraph block is a heading: they are excluded from
            # block-level scoring and reported separately.
            run_in = (
                len(best_text) > 100
                and len(best_text) > len(title.strip()) * 3
            )
            entry = {
                "block_id": -1 if run_in else best_id,
                "title": title.strip(),
                "level": int(level),
                "score": round(best_score, 3),
                "matched_text": best_text[:60],
                "page": page,
            }
            if run_in:
                entry["granularity"] = "run-in"
            else:
                last_matched_id = best_id
            results.append(entry)
        else:
            results.append({
                "block_id": -1,
                "title": title.strip(),
                "level": int(level),
                "score": round(best_score, 3),
                "matched_text": best_text[:60],
                "page": page,
            })

    return results


def _ensure_abstract_entry(headings: list[dict], blocks: list) -> list[dict]:
    """Prepend an Abstract entry when the TOC omits it.

    PDF embedded TOCs almost never list "Abstract", but it is a real
    section any structure parser is expected to detect; without this
    entry a correct detection is scored as a false positive.  Merged
    here (instead of a separate one-shot script) so that re-running the
    repair stays idempotent and never silently drops the patch.
    """
    if any((h.get("title") or "").strip().lower() == "abstract" for h in headings):
        return headings

    abstract_block = next(
        (b for b in blocks
         if b.type == "text" and b.text
         and b.text.strip().lower().rstrip(".:") == "abstract"),
        None,
    )
    if abstract_block is None:
        return headings

    located_ids = [h["block_id"] for h in headings if h.get("block_id", -1) >= 0]
    if located_ids and abstract_block.id >= min(located_ids):
        # Abstract block must precede the first located heading;
        # anything else needs manual inspection.
        return headings

    return [{
        "block_id": abstract_block.id,
        "title": "Abstract",
        "level": 1,
    }] + headings


def repair_document(pdf_path: Path, gt_path: Path) -> dict:
    """Repair one document's GT. Returns a stats dict for the report."""
    doc = fitz.open(pdf_path)
    toc = doc.get_toc()
    doc.close()

    stats = {
        "name": pdf_path.stem,
        "toc_entries": len(toc),
        "located": 0,
        "low_confidence": [],
        "unlocated": [],
        "run_in": [],
        "abstract_added": False,
        "skipped": False,
    }

    if not toc:
        stats["skipped"] = True
        return stats

    provider = PdfProvider()
    blocks = provider.extract(str(pdf_path))

    aligned = align_toc_to_blocks(toc, blocks)

    headings = []
    for entry in aligned:
        heading = {
            "block_id": entry["block_id"],
            "title": entry["title"],
            "level": entry["level"],
        }
        if entry.get("granularity"):
            heading["granularity"] = entry["granularity"]
        headings.append(heading)
        if entry.get("granularity") == "run-in":
            stats["run_in"].append(entry)
        elif entry["block_id"] == -1:
            stats["unlocated"].append(entry)
        else:
            stats["located"] += 1
            if entry["score"] < LOW_CONFIDENCE:
                stats["low_confidence"].append(entry)

    before = len(headings)
    headings = _ensure_abstract_entry(headings, blocks)
    stats["abstract_added"] = len(headings) > before

    # Preserve existing doc_title/doc_authors if present
    old = {}
    if gt_path.exists():
        with open(gt_path, encoding="utf-8") as f:
            old = json.load(f)

    gt_source = "pdf_embedded_toc"
    if stats["abstract_added"]:
        gt_source += " + abstract_entry"
    gt = {
        "doc_title": old.get("doc_title", ""),
        "doc_authors": old.get("doc_authors", ""),
        "gt_source": gt_source,
        "headings": headings,
    }
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(gt, f, ensure_ascii=False, indent=4)

    return stats


def main() -> None:
    pdfs = sorted(DATA_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {DATA_DIR}")
        return

    all_stats = []
    for pdf_path in pdfs:
        gt_path = GT_DIR / f"{pdf_path.stem}.json"
        print(f"Repairing {pdf_path.name} ...")
        stats = repair_document(pdf_path, gt_path)
        all_stats.append(stats)
        if stats["skipped"]:
            print("  -> no embedded TOC, needs manual annotation")
        else:
            print(
                f"  -> {stats['located']}/{stats['toc_entries']} located, "
                f"{len(stats['low_confidence'])} low-confidence, "
                f"{len(stats['unlocated'])} unlocated, "
                f"{len(stats['run_in'])} run-in"
                + (", +Abstract" if stats["abstract_added"] else "")
            )

    # ── Markdown report ──
    lines = [
        "# GT Repair Report",
        "",
        "> Generated by `evaluation/repair_gt.py`.",
        "> GT source: PDF embedded TOC (author-written, independent of the system under test).",
        "> `block_id = -1` marks entries that could not be located and need manual fixing.",
        "",
        "| Document | TOC entries | Located | Low-conf | Unlocated | Run-in | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in all_stats:
        if s["skipped"]:
            lines.append(
                f"| {s['name']} | 0 | - | - | - | - | **NO TOC — manual annotation required** |"
            )
        else:
            rate = s["located"] / max(s["toc_entries"], 1) * 100
            lines.append(
                f"| {s['name']} | {s['toc_entries']} | {s['located']} ({rate:.0f}%) | "
                f"{len(s['low_confidence'])} | {len(s['unlocated'])} | {len(s['run_in'])} | OK |"
            )

    detail_sections = []
    for s in all_stats:
        if s["skipped"] or (
            not s["low_confidence"] and not s["unlocated"] and not s["run_in"]
        ):
            continue
        detail_sections.append(f"\n### {s['name']}\n")
        if s["unlocated"]:
            detail_sections.append("**Unlocated (block_id=-1, fix manually):**\n")
            for e in s["unlocated"]:
                detail_sections.append(
                    f"- L{e['level']} `{e['title']}` (page {e['page']}, best score {e['score']})"
                )
        if s["run_in"]:
            detail_sections.append(
                "\n**Run-in headings (no standalone heading block exists; "
                "excluded from block-level scoring):**\n"
            )
            for e in s["run_in"]:
                detail_sections.append(
                    f"- L{e['level']} `{e['title']}` (page {e['page']}, "
                    f"merged into: `{e['matched_text']}`)"
                )
        if s["low_confidence"]:
            detail_sections.append("\n**Low-confidence matches (spot-check):**\n")
            for e in s["low_confidence"]:
                detail_sections.append(
                    f"- L{e['level']} `{e['title']}` -> block {e['block_id']} "
                    f"(score {e['score']}, text: `{e['matched_text']}`)"
                )

    if detail_sections:
        lines.append("\n## Details")
        lines.extend(detail_sections)

    REPORT_PATH.parent.mkdir(exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
