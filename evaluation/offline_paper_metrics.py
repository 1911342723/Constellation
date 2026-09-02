"""Offline (LLM-free) metrics for the paper: compression, candidate
recall, candidate budget, and lossless coverage.

Everything here is deterministic and reproducible without an API key:

1. **Skeleton compression** — chars/tokens of the Stage-2 skeleton vs
   the original text, on the 12 long PDFs.
2. **Candidate recall vs TOC GT** — fraction of ground-truth headings
   (located entries only) whose block_id is proposed by the Stage-2.5
   candidate generator; plus the candidate budget (candidates / text
   blocks).  This bounds the recall of strict candidate routing and
   motivates the downgrade channel.
3. **Lossless closure coverage** — forced full-document closure with a
   single pseudo anchor; verifies that interval slicing + Markdown
   assembly preserve text content (DOCX benchmark set).
4. **No-style candidate recall** — on the 4 synthetic no-style PDFs
   (uniform font, no explicit styles), fraction of GT headings whose
   *title* appears in the candidate table (title matching is robust to
   block-id drift across provider versions).

Run: python -m evaluation.offline_paper_metrics
Outputs: experiment_results/offline_paper_metrics.md (+ _raw.json)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.parser.compressor import SkeletonCompressor
from modules.parser.heading_candidates import (
    generate_heading_candidate_set,
    generate_heading_candidates,
    select_route_candidates,
)

LONG_DOCS = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(LONG_DOCS, "ground_truth")
OUT_MD = os.path.join("experiment_results", "offline_paper_metrics.md")
OUT_JSON = os.path.join("experiment_results", "offline_paper_metrics_raw.json")


def estimate_tokens(text: str) -> int:
    """CJK chars count 1 token; ASCII words count ~1.3 tokens."""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_words = len([w for w in text.split() if any(c.isascii() for c in w)])
    return cjk + int(ascii_words * 1.3)


def long_doc_metrics() -> list[dict]:
    from infrastructure.providers.pdf_provider import PdfProvider

    rows = []
    for fname in sorted(os.listdir(LONG_DOCS)):
        if not fname.endswith(".pdf"):
            continue
        stem = fname[:-4]
        print(f"[long] {stem} ...", flush=True)
        blocks = PdfProvider().extract(os.path.join(LONG_DOCS, fname))
        text_blocks = [b for b in blocks if b.type == "text" and b.text]
        original_text = "\n".join(b.text for b in text_blocks)

        candidate_set = generate_heading_candidate_set(blocks)
        candidates = select_route_candidates(candidate_set, blocks)
        chunks = SkeletonCompressor().compress(
            blocks,
            candidates=candidates,
            region_risks=candidate_set.region_risks,
        )
        skeleton_text = "\n".join(chunks)
        cand_ids = {c.block_id for c in candidates}

        gt_path = os.path.join(GT_DIR, f"{stem}.json")
        gt_located = gt_in_cand = None
        gt_source = ""
        if os.path.exists(gt_path):
            with open(gt_path, encoding="utf-8") as f:
                gt = json.load(f)
            gt_source = gt.get("gt_source", "")
            located = [h for h in gt.get("headings", []) if h.get("block_id", -1) >= 0]
            gt_located = len(located)
            gt_in_cand = sum(1 for h in located if h["block_id"] in cand_ids)

        orig_chars = len(original_text)
        skel_chars = len(skeleton_text)
        orig_tok = estimate_tokens(original_text)
        skel_tok = estimate_tokens(skeleton_text)

        rows.append({
            "doc": stem,
            "blocks": len(blocks),
            "text_blocks": len(text_blocks),
            "route_views": len(chunks),
            "orig_chars": orig_chars,
            "skel_chars": skel_chars,
            "char_compression_pct": round((1 - skel_chars / max(orig_chars, 1)) * 100, 1),
            "orig_tokens": orig_tok,
            "skel_tokens": skel_tok,
            "token_savings_pct": round((1 - skel_tok / max(orig_tok, 1)) * 100, 1),
            "candidates": len(candidates),
            "candidate_budget_pct": round(len(candidates) / max(len(text_blocks), 1) * 100, 1),
            "gt_source": gt_source,
            "gt_located": gt_located,
            "gt_in_candidates": gt_in_cand,
            "candidate_recall_pct": (
                round(gt_in_cand / gt_located * 100, 1)
                if gt_located else None
            ),
        })
    return rows


def no_style_metrics() -> list[dict]:
    """Candidate recall on the synthetic no-style PDF set (title match)."""
    from infrastructure.providers.pdf_provider import PdfProvider

    def _norm(text: str) -> str:
        return " ".join((text or "").lower().split())

    no_style_dir = os.path.join("tests", "data", "no_style")
    rows = []
    for fname in sorted(os.listdir(no_style_dir)):
        if not fname.endswith(".pdf"):
            continue
        stem = fname[:-4]
        gt_path = os.path.join(no_style_dir, f"{stem}.json")
        if not os.path.exists(gt_path):
            continue
        print(f"[no-style] {stem} ...", flush=True)
        blocks = PdfProvider().extract(os.path.join(no_style_dir, fname))
        text_blocks = [b for b in blocks if b.type == "text" and b.text]
        candidates = generate_heading_candidates(blocks)
        cand_titles = {_norm(c.title) for c in candidates}

        with open(gt_path, encoding="utf-8") as f:
            gt = json.load(f)
        headings = gt.get("headings", [])
        hit = sum(
            1 for h in headings
            if _norm(h.get("title", "")) in cand_titles
            or any(_norm(h.get("title", "")) in t for t in cand_titles)
        )
        rows.append({
            "doc": stem,
            "text_blocks": len(text_blocks),
            "candidates": len(candidates),
            "candidate_budget_pct": round(len(candidates) / max(len(text_blocks), 1) * 100, 1),
            "gt_headings": len(headings),
            "gt_in_candidates": hit,
            "candidate_recall_pct": round(hit / max(len(headings), 1) * 100, 1),
        })
    return rows


def coverage_metrics() -> list[dict]:
    """Forced-closure lossless coverage on every DOCX test sample.

    Verifies Proposition 1 directly, without clamping:

    - **Partition check**: every block id is owned by exactly one node
      interval (no duplicates, no gaps).  Reported as ``partition_ok``.
    - **Character conservation**: each covered text block's characters
      are counted exactly once.  The earlier version additionally added
      ``node.title`` for promoted orphan nodes — double-counting the
      title block already present in the interval — and then hid the
      resulting >100% values behind a ``min(..., 100)`` clamp, which
      also made the metric structurally blind to genuine duplication.
      Coverage is now reported unclamped and must be exactly 100.0.
    """
    from pathlib import Path

    from infrastructure.providers.docx_provider import DocxProvider
    from modules.parser.config import ResolverConfig
    from modules.parser.resolver import IntervalResolver
    from modules.parser.schemas import ChapterNode

    rows = []
    for doc_path in sorted(Path("tests/data").rglob("*.docx")):
        try:
            blocks = DocxProvider().extract(str(doc_path))
        except Exception as exc:
            print(f"[cov] {doc_path.name}: extract failed ({exc})")
            continue
        if not blocks:
            continue
        block_by_id = {b.id: b for b in blocks}
        original_chars = sum(
            len(b.text.strip()) for b in blocks
            if b.type == "text" and b.text
        )
        resolver = IntervalResolver(blocks, ResolverConfig())
        nodes = resolver.resolve([
            ChapterNode(block_id=0, title="Test Root", level=1, snippet="Root"),
        ])

        assembled = 0
        owner_count: dict[int, int] = {}

        def _walk(node, id_map, owners):
            count = 0
            for i in range(node.start_block_id, node.end_block_id + 1):
                owners[i] = owners.get(i, 0) + 1
                b = id_map.get(i)
                if b is not None and b.type == "text" and b.text:
                    count += len(b.text.strip())
            for child in node.children:
                count += _walk(child, id_map, owners)
            return count

        for root in nodes:
            assembled += _walk(root, block_by_id, owner_count)

        duplicated = sum(1 for v in owner_count.values() if v > 1)
        missing = sum(1 for b in blocks if b.id not in owner_count)
        partition_ok = duplicated == 0 and missing == 0

        coverage = assembled / max(original_chars, 1) * 100
        rows.append({
            "doc": doc_path.name,
            "original_chars": original_chars,
            "assembled_chars": assembled,
            "coverage_pct": round(coverage, 2),
            "partition_ok": partition_ok,
            "duplicated_block_ids": duplicated,
            "missing_block_ids": missing,
        })
        print(
            f"[cov] {doc_path.name}: {coverage:.2f}% "
            f"(partition_ok={partition_ok})"
        )
    return rows


def main() -> None:
    long_rows = long_doc_metrics()
    no_style_rows = no_style_metrics()
    cov_rows = coverage_metrics()

    os.makedirs("experiment_results", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"long_docs": long_rows, "no_style": no_style_rows,
                   "coverage": cov_rows}, f,
                  ensure_ascii=False, indent=2)

    lines = [
        "# Offline Paper Metrics (LLM-free, deterministic)",
        "",
        "> Generated by `evaluation/offline_paper_metrics.py`. Fully reproducible without an API key.",
        "",
        "## 1. Long-PDF sparse control view & candidate routing",
        "",
        "> The control view follows the production candidate-aware sparse path. "
        "Route views are counted before model-specific token-budget sharding.",
        "",
        "| Document | Text blocks | Route views | Char savings % | Token savings % | Candidates | Budget % | Cand. recall % |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in long_rows:
        recall = f"{r['candidate_recall_pct']}" if r["candidate_recall_pct"] is not None else "n/a"
        lines.append(
            f"| {r['doc']} | {r['text_blocks']} | {r['route_views']} | {r['char_compression_pct']} "
            f"| {r['token_savings_pct']} | {r['candidates']} | {r['candidate_budget_pct']} | {recall} |"
        )
    n = len(long_rows)
    if n:
        lines.append(
            f"| **Average** | | | "
            f"{sum(r['char_compression_pct'] for r in long_rows)/n:.1f} "
            f"| {sum(r['token_savings_pct'] for r in long_rows)/n:.1f} "
            f"| | {sum(r['candidate_budget_pct'] for r in long_rows)/n:.1f} | |"
        )
        with_gt = [r for r in long_rows if r["candidate_recall_pct"] is not None]
        if with_gt:
            total_located = sum(r["gt_located"] for r in with_gt)
            total_hit = sum(r["gt_in_candidates"] for r in with_gt)
            lines.append("")
            lines.append(
                f"Candidate recall (micro, {len(with_gt)} docs with GT): "
                f"**{total_hit}/{total_located} = {total_hit/total_located*100:.1f}%**"
            )

    lines += [
        "",
        "## 2. No-style PDF candidate recall (title match)",
        "",
        "| Document | Text blocks | Candidates | Budget % | GT headings | In candidates | Recall % |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in no_style_rows:
        lines.append(
            f"| {r['doc']} | {r['text_blocks']} | {r['candidates']} "
            f"| {r['candidate_budget_pct']} | {r['gt_headings']} "
            f"| {r['gt_in_candidates']} | {r['candidate_recall_pct']} |"
        )
    if no_style_rows:
        ns_total = sum(r["gt_headings"] for r in no_style_rows)
        ns_hit = sum(r["gt_in_candidates"] for r in no_style_rows)
        lines.append("")
        lines.append(
            f"No-style candidate recall (micro): "
            f"**{ns_hit}/{ns_total} = {ns_hit/max(ns_total,1)*100:.1f}%**"
        )

    lines += [
        "",
        "## 3. Forced-closure lossless coverage (DOCX)",
        "",
        "> Coverage is unclamped and must be exactly 100.0; `partition` verifies",
        "> that every block id is owned by exactly one node interval.",
        "",
        "| Document | Original chars | Assembled chars | Coverage % | Partition |",
        "|---|---:|---:|---:|---|",
    ]
    for r in cov_rows:
        lines.append(
            f"| {r['doc']} | {r['original_chars']} | {r['assembled_chars']} "
            f"| {r['coverage_pct']} | {'ok' if r['partition_ok'] else 'VIOLATED'} |"
        )

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nWritten: {OUT_MD}")


if __name__ == "__main__":
    main()
