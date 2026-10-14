"""Failure-chain diagnosis for the three F1=0.000 papers (BERT/ResNet/ViT).

Dumps per-stage intermediate artifacts WITHOUT spending LLM quota:

  Stage 0  PDF TOC bookmarks            -> explains how auto-GT was built
  Stage 1  Block physical features      -> PdfProvider extraction fidelity
  Stage 2  Skeleton chunks              -> is the heading visible to the LLM?
  Stage 2.5 Heading candidates          -> did the rule-based generator fire?

For every *reference heading* (expert-known section titles of each paper)
the script reports the full survival chain:

  found in blocks? -> visible in skeleton? -> in candidate table? -> why not?

Optionally (``--with-llm``) it also runs Stage 3 and dumps the raw LLM
anchors vs. the post-filter anchors, attributing every dropped anchor.

Run:
    python scripts/manual/diagnose_failure_chain.py
    python scripts/manual/diagnose_failure_chain.py --docs bert resnet
    python scripts/manual/diagnose_failure_chain.py --with-llm
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from infrastructure.providers.pdf_provider import PdfProvider
from modules.parser.compressor import SkeletonCompressor
from modules.parser.heading_candidates import (
    _body_font_size,
    generate_heading_candidates,
    infer_numbering_level,
    infer_style_level,
    is_caption,
    _semantic_level,
)
from modules.parser.resolver import _levenshtein_ratio

DATA_DIR = os.path.join("tests", "data", "long_docs")
GT_DIR = os.path.join(DATA_DIR, "ground_truth")
OUT_DIR = os.path.join("experiment_results", "diagnosis")

# ── Expert reference headings (paper section structures) ─────
# These act as the diagnosis oracle, independent of the broken auto-GT.

REFERENCE_HEADINGS: dict[str, list[tuple[str, int]]] = {
    "bert": [
        ("Abstract", 1),
        ("1 Introduction", 1),
        ("2 Related Work", 1),
        ("2.1 Unsupervised Feature-based Approaches", 2),
        ("2.2 Unsupervised Fine-tuning Approaches", 2),
        ("2.3 Transfer Learning from Supervised Data", 2),
        ("3 BERT", 1),
        ("3.1 Pre-training BERT", 2),
        ("3.2 Fine-tuning BERT", 2),
        ("4 Experiments", 1),
        ("4.1 GLUE", 2),
        ("4.2 SQuAD v1.1", 2),
        ("4.3 SQuAD v2.0", 2),
        ("4.4 SWAG", 2),
        ("5 Ablation Studies", 1),
        ("5.1 Effect of Pre-training Tasks", 2),
        ("5.2 Effect of Model Size", 2),
        ("5.3 Feature-based Approach with BERT", 2),
        ("6 Conclusion", 1),
        ("References", 1),
        ("A Additional Details for BERT", 1),
        ("A.1 Illustration of the Pre-training Tasks", 2),
        ("A.2 Pre-training Procedure", 2),
        ("A.3 Fine-tuning Procedure", 2),
        ("A.4 Comparison of BERT, ELMo ,and OpenAI GPT", 2),
        ("B Detailed Experimental Setup", 1),
        ("B.1 Detailed Descriptions for the GLUE Benchmark Experiments.", 2),
        ("C Additional Ablation Studies", 1),
        ("C.1 Effect of Number of Training Steps", 2),
        ("C.2 Ablation for Different Masking Procedures", 2),
    ],
    "resnet": [
        ("Abstract", 1),
        ("1. Introduction", 1),
        ("2. Related Work", 1),
        ("3. Deep Residual Learning", 1),
        ("3.1. Residual Learning", 2),
        ("3.2. Identity Mapping by Shortcuts", 2),
        ("3.3. Network Architectures", 2),
        ("3.4. Implementation", 2),
        ("4. Experiments", 1),
        ("4.1. ImageNet Classification", 2),
        ("4.2. CIFAR-10 and Analysis", 2),
        ("4.3. Object Detection on PASCAL and MS COCO", 2),
        ("References", 1),
        ("A. Object Detection Baselines", 1),
        ("B. Object Detection Improvements", 1),
        ("C. ImageNet Localization", 1),
    ],
    "vit": [
        ("Abstract", 1),
        ("1 Introduction", 1),
        ("2 Related Work", 1),
        ("3 Method", 1),
        ("3.1 Vision Transformer (ViT)", 2),
        ("3.2 Fine-tuning and Higher Resolution", 2),
        ("4 Experiments", 1),
        ("4.1 Setup", 2),
        ("4.2 Comparison to State of the Art", 2),
        ("4.3 Pre-training Data Requirements", 2),
        ("4.4 Scaling Study", 2),
        ("4.5 Inspecting Vision Transformer", 2),
        ("4.6 Self-supervision", 2),
        ("5 Conclusion", 1),
        ("A Multihead Self-attention", 1),
        ("B Experiment details", 1),
        ("B.1 Training", 2),
        ("B.1.1 Fine-tuning", 3),
        ("B.1.2 Self-supervision", 3),
        ("C Additional Results", 1),
        ("D Additional Analyses", 1),
        ("D.1 SGD vs. Adam for ResNets", 2),
        ("D.2 Transformer shape", 2),
        ("D.3 Head Type and class token", 2),
        ("D.4 Positional Embedding", 2),
        ("D.5 Empirical Computational Costs", 2),
        ("D.6 Axial Attention", 2),
        ("D.7 Attention Distance", 2),
        ("D.8 Attention Maps", 2),
        ("D.9 ObjectNet Results", 2),
        ("D.10 VTAB Breakdown", 2),
    ],
}

TITLE_SIM_THRESHOLD = 0.6  # same as evaluation/metrics.py


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _strip_numbering(text: str) -> str:
    """Remove leading numbering like '3.1.', 'A.', 'B.1.1' for fallback matching."""
    import re
    return re.sub(r"^[0-9A-Za-z]+(?:\.[0-9]+)*[.)]?\s+", "", text).strip()


# ── Stage 0: TOC / GT inspection ─────────────────────────────

def dump_toc(pdf_path: str) -> list:
    import fitz
    doc = fitz.open(pdf_path)
    try:
        return doc.get_toc() or []
    finally:
        doc.close()


def load_gt(doc_name: str) -> dict:
    gt_path = os.path.join(GT_DIR, f"{doc_name}.json")
    if not os.path.exists(gt_path):
        return {}
    with open(gt_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Reference-heading matching ───────────────────────────────

def match_reference_to_blocks(ref_title: str, blocks) -> dict:
    """Find the best-matching text block for a reference heading.

    Tries full-title match first, then numbering-stripped match for
    cases where the PDF extractor split the number from the title.
    """
    best = {"block_id": None, "similarity": 0.0, "block_text": None, "mode": None}
    ref_full = _norm(ref_title)
    ref_bare = _norm(_strip_numbering(ref_title))

    for b in blocks:
        if b.type != "text" or not b.text:
            continue
        text = _norm(b.text)
        if not text or len(text) > 300:
            continue
        sim_full = _levenshtein_ratio(ref_full, text)
        mode = "full"
        sim = sim_full
        if ref_bare and ref_bare != ref_full:
            sim_bare = _levenshtein_ratio(ref_bare, text)
            if sim_bare > sim:
                sim = sim_bare
                mode = "numbering-stripped"
        if sim > best["similarity"]:
            best = {
                "block_id": b.id,
                "similarity": round(sim, 3),
                "block_text": (b.text or "").strip()[:120],
                "mode": mode,
            }
    return best


def explain_candidate_score(block, body_size: float) -> dict:
    """Re-compute the candidate score breakdown for one block.

    Mirrors generate_heading_candidates() scoring so we can attribute
    exactly why a block failed the 0.55 cut-off.
    """
    title = " ".join((block.text or "").strip().split())
    text_len = len(title)
    short = text_len <= 140
    medium = text_len <= 220
    breakdown = {}
    score = 0.0

    if is_caption(title):
        return {"rejected": "caption-pattern", "score": 0.0, "breakdown": {}}

    style_level = infer_style_level(block)
    if block.is_heading_style or style_level is not None:
        breakdown["explicit-style"] = 1.0
        score += 1.0

    numbering_level = infer_numbering_level(title)
    if block.has_heading_numbering and numbering_level is None:
        numbering_level = 1
    if numbering_level is not None and short:
        breakdown["numbering"] = 0.85
        score += 0.85

    semantic_level = _semantic_level(title)
    if semantic_level is not None and short:
        breakdown["semantic-title"] = 0.65
        score += 0.65

    font_ratio = None
    if block.font_size and body_size > 0:
        font_ratio = round(block.font_size / body_size, 3)
        if font_ratio >= 1.25 and medium:
            breakdown["large-font"] = 0.8
            score += 0.8
        elif font_ratio >= 1.10 and short:
            breakdown["slightly-large-font"] = 0.45
            score += 0.45

    if block.is_bold and short:
        breakdown["bold"] = 0.35
        score += 0.35

    if block.alignment and block.alignment.lower() == "center" and short:
        breakdown["centered"] = 0.35
        score += 0.35

    if block.is_potential_title(min_body_size=body_size):
        breakdown["block-title-heuristic"] = 0.45
        score += 0.45

    rejected = None
    if not breakdown or score < 0.55:
        rejected = f"score {score:.2f} < 0.55"
    elif text_len > 220 and "explicit-style" not in breakdown:
        rejected = "text too long without explicit style"

    return {
        "score": round(score, 2),
        "breakdown": breakdown,
        "font_ratio": font_ratio,
        "text_len": text_len,
        "rejected": rejected,
    }


# ── Per-document diagnosis ───────────────────────────────────

def diagnose_document(doc_name: str, with_llm: bool = False) -> dict:
    pdf_path = os.path.join(DATA_DIR, f"{doc_name}.pdf")
    report: dict = {"doc": doc_name}

    # Stage 0: TOC + existing GT
    toc = dump_toc(pdf_path)
    gt = load_gt(doc_name)
    gt_headings = gt.get("headings", [])
    report["stage0_toc"] = {
        "toc_entries": len(toc),
        "toc_sample": toc[:10],
        "gt_headings": len(gt_headings),
        "gt_block_id_zero_count": sum(
            1 for h in gt_headings if h.get("block_id", -1) == 0
        ),
    }

    # Stage 1: physical features
    provider = PdfProvider()
    blocks = provider.extract(pdf_path)
    text_blocks = [b for b in blocks if b.type == "text" and b.text]
    font_sizes = [b.font_size for b in text_blocks if b.font_size]
    body_size = _body_font_size(blocks)
    report["stage1_blocks"] = {
        "total_blocks": len(blocks),
        "text_blocks": len(text_blocks),
        "body_font_size": body_size,
        "font_size_min": min(font_sizes) if font_sizes else None,
        "font_size_max": max(font_sizes) if font_sizes else None,
        "font_size_median": statistics.median(font_sizes) if font_sizes else None,
        "bold_blocks": sum(1 for b in text_blocks if b.is_bold),
        "heading_style_blocks": sum(1 for b in text_blocks if b.is_heading_style),
    }

    # Stage 2: skeleton
    compressor = SkeletonCompressor()
    chunks = compressor.compress(blocks)
    skeleton_all = "\n".join(chunks)
    original_chars = sum(len(b.text or "") for b in blocks)
    report["stage2_skeleton"] = {
        "windows": len(chunks),
        "skeleton_chars": len(skeleton_all),
        "original_chars": original_chars,
        "compression_pct": round((1 - len(skeleton_all) / max(original_chars, 1)) * 100, 1),
    }

    # Stage 2.5: candidates
    candidates = generate_heading_candidates(blocks)
    cand_ids = {c.block_id for c in candidates}
    report["stage25_candidates"] = {
        "candidate_count": len(candidates),
        "sample": [
            {"block_id": c.block_id, "score": c.source_score,
             "reasons": c.reasons, "title": c.title[:60]}
            for c in candidates[:15]
        ],
    }

    # Reference-heading survival chain
    refs = REFERENCE_HEADINGS[doc_name]
    block_map = {b.id: b for b in blocks}
    chain = []
    n_found = n_skeleton = n_candidate = 0

    for ref_title, ref_level in refs:
        entry: dict = {"ref_title": ref_title, "ref_level": ref_level}
        m = match_reference_to_blocks(ref_title, blocks)
        entry["block_match"] = m

        found = m["similarity"] >= TITLE_SIM_THRESHOLD
        entry["found_in_blocks"] = found
        if found:
            n_found += 1
            bid = m["block_id"]
            blk = block_map[bid]
            entry["physical_features"] = {
                "font_size": blk.font_size,
                "is_bold": blk.is_bold,
                "alignment": blk.alignment,
                "is_heading_style": blk.is_heading_style,
                "heading_level": blk.heading_level,
                "has_heading_numbering": blk.has_heading_numbering,
                "font_ratio_vs_body": (
                    round(blk.font_size / body_size, 3)
                    if blk.font_size and body_size else None
                ),
            }
            in_skeleton = f"[{bid}]" in skeleton_all
            entry["visible_in_skeleton"] = in_skeleton
            if in_skeleton:
                n_skeleton += 1
            in_cand = bid in cand_ids
            entry["in_candidate_table"] = in_cand
            if in_cand:
                n_candidate += 1
                cand = next(c for c in candidates if c.block_id == bid)
                entry["candidate_info"] = {
                    "score": cand.source_score, "reasons": cand.reasons,
                }
            else:
                entry["candidate_miss_analysis"] = explain_candidate_score(blk, body_size)
        chain.append(entry)

    report["reference_chain"] = chain
    report["chain_summary"] = {
        "reference_headings": len(refs),
        "found_in_blocks": n_found,
        "visible_in_skeleton": n_skeleton,
        "in_candidate_table": n_candidate,
        "block_recall_upper_bound": round(n_found / len(refs), 3),
        "candidate_recall_upper_bound": round(n_candidate / len(refs), 3),
    }

    # Stage 3 (optional): LLM raw vs filtered anchors
    if with_llm:
        report["stage3_llm"] = run_llm_stage(blocks, candidates)

    return report


def run_llm_stage(blocks, candidates) -> dict:
    """Run Stage 3 with a spy on the anchor filter to capture drops."""
    from modules.parser.parser import CaliperParser
    from modules.parser.router import LLMRouter

    capture: list[dict] = []
    orig = LLMRouter._validate_and_filter_anchors

    def spy(result, max_block_id=-1, allowed_block_ids=None, **kwargs):
        raw = [
            {"block_id": ch.start_block_id, "level": ch.level, "title": ch.title}
            for ch in result.chapters
        ]
        out = orig(
            result, max_block_id=max_block_id,
            allowed_block_ids=allowed_block_ids, **kwargs,
        )
        kept_ids = {ch.start_block_id for ch in out.chapters}
        capture.append({
            "allowed_ids": sorted(allowed_block_ids) if allowed_block_ids else None,
            "raw_anchors": raw,
            "kept_anchors": [a for a in raw if a["block_id"] in kept_ids],
            "downgraded_anchors": [
                {"block_id": ch.start_block_id, "title": ch.title}
                for ch in out.chapters if ch.out_of_candidate
            ],
            "dropped_anchors": [a for a in raw if a["block_id"] not in kept_ids],
        })
        return out

    LLMRouter._validate_and_filter_anchors = staticmethod(spy)
    try:
        parser = CaliperParser()
        parser.clear_cache()
        tree = parser.parse(blocks)
        pred = []

        def _collect(nodes):
            for n in nodes:
                pred.append({"block_id": n.start_block_id, "level": n.level, "title": n.title})
                _collect(n.children)

        _collect(tree.nodes)
        total_raw = sum(len(c["raw_anchors"]) for c in capture)
        total_dropped = sum(len(c["dropped_anchors"]) for c in capture)
        return {
            "windows_routed": len(capture),
            "raw_anchor_total": total_raw,
            "dropped_anchor_total": total_dropped,
            "final_tree_headings": pred,
            "window_details": capture,
        }
    except Exception as exc:  # LLM/network failures must not kill the offline dump
        return {"error": str(exc), "window_details": capture}
    finally:
        LLMRouter._validate_and_filter_anchors = staticmethod(orig)


# ── Markdown summary ─────────────────────────────────────────

def render_markdown(reports: list[dict]) -> str:
    lines = ["# Round4 Failure-Chain Diagnosis (BERT / ResNet / ViT)", ""]
    lines.append("Diagnosis oracle: expert-known section structures of the three "
                 "papers, matched with the same Levenshtein >= 0.6 rule the "
                 "evaluator uses.")
    lines.append("")

    lines.append("## 1. GT Layer (Stage 0)")
    lines.append("")
    lines.append("| Doc | PDF TOC entries | GT headings | GT block_id==0 | Verdict |")
    lines.append("|---|---:|---:|---:|---|")
    for r in reports:
        s0 = r["stage0_toc"]
        if s0["gt_headings"] == 0:
            verdict = "GT EMPTY -> F1 locked at 0 regardless of system output"
        elif s0["gt_block_id_zero_count"] >= max(s0["gt_headings"] - 1, 1):
            verdict = ("GT block_id all 0 -> position match (tol=3) fails for "
                       "every real prediction -> F1 locked at 0")
        else:
            verdict = "GT usable"
        lines.append(
            f"| {r['doc']} | {s0['toc_entries']} | {s0['gt_headings']} "
            f"| {s0['gt_block_id_zero_count']} | {verdict} |"
        )
    lines.append("")

    lines.append("## 2. Pipeline Layer (Stages 1 - 2.5)")
    lines.append("")
    lines.append("| Doc | Blocks | Body font | Font range | Windows | Compression % "
                 "| Candidates | Ref headings | Found in blocks | In candidates |")
    lines.append("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")
    for r in reports:
        s1, s2, s25, cs = (r["stage1_blocks"], r["stage2_skeleton"],
                           r["stage25_candidates"], r["chain_summary"])
        lines.append(
            f"| {r['doc']} | {s1['total_blocks']} | {s1['body_font_size']:.1f} "
            f"| {s1['font_size_min']:.1f}-{s1['font_size_max']:.1f} "
            f"| {s2['windows']} | {s2['compression_pct']} | {s25['candidate_count']} "
            f"| {cs['reference_headings']} | {cs['found_in_blocks']} "
            f"| {cs['in_candidate_table']} |"
        )
    lines.append("")

    for r in reports:
        cs = r["chain_summary"]
        lines.append(f"## 3. Survival chain: {r['doc']}")
        lines.append("")
        lines.append(f"- Block-level recall upper bound: **{cs['block_recall_upper_bound']:.1%}**")
        lines.append(f"- Candidate-level recall upper bound: **{cs['candidate_recall_upper_bound']:.1%}**")
        lines.append("")
        lines.append("| Ref heading | Found (sim) | block_id | font/body | bold | head-style | In skeleton | In candidates | Miss reason |")
        lines.append("|---|---|---:|---|---|---|---|---|---|")
        for e in r["reference_chain"]:
            m = e["block_match"]
            if not e["found_in_blocks"]:
                lines.append(
                    f"| {e['ref_title'][:45]} | NO ({m['similarity']:.2f}) | - | - | - | - | - | - "
                    f"| best block text: '{(m['block_text'] or '')[:40]}' |"
                )
                continue
            pf = e["physical_features"]
            miss = ""
            if not e["in_candidate_table"]:
                ma = e.get("candidate_miss_analysis", {})
                bd = ",".join(f"{k}={v}" for k, v in ma.get("breakdown", {}).items())
                miss = f"{ma.get('rejected', '?')} ({bd or 'no signals'})"
            cand_mark = "YES" if e["in_candidate_table"] else "**NO**"
            if e["in_candidate_table"]:
                ci = e["candidate_info"]
                cand_mark = f"YES ({ci['score']:.2f})"
            lines.append(
                f"| {e['ref_title'][:45]} | yes ({m['similarity']:.2f}) | {m['block_id']} "
                f"| {pf['font_ratio_vs_body'] or '-'} | {pf['is_bold']} | {pf['is_heading_style']} "
                f"| {'yes' if e['visible_in_skeleton'] else 'NO'} | {cand_mark} | {miss} |"
            )
        lines.append("")

        if "stage3_llm" in r:
            s3 = r["stage3_llm"]
            lines.append(f"### Stage 3 LLM dump: {r['doc']}")
            lines.append("")
            if "error" in s3:
                lines.append(f"- Pipeline error: `{s3['error']}`")
            else:
                lines.append(f"- Windows routed: {s3['windows_routed']}")
                lines.append(f"- Raw LLM anchors: {s3['raw_anchor_total']}")
                lines.append(f"- Dropped by filter: {s3['dropped_anchor_total']}")
                lines.append(f"- Final tree headings: {len(s3['final_tree_headings'])}")
            lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Failure-chain diagnosis")
    ap.add_argument("--docs", nargs="*", default=["bert", "resnet", "vit"],
                    choices=list(REFERENCE_HEADINGS.keys()))
    ap.add_argument("--with-llm", action="store_true",
                    help="Also run Stage 3 (spends LLM quota)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    reports = []
    for doc in args.docs:
        print(f"=== Diagnosing {doc} ===")
        r = diagnose_document(doc, with_llm=args.with_llm)
        reports.append(r)
        out_json = os.path.join(OUT_DIR, f"{doc}_diagnosis.json")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        cs = r["chain_summary"]
        print(f"  blocks={r['stage1_blocks']['total_blocks']}, "
              f"candidates={r['stage25_candidates']['candidate_count']}, "
              f"ref found {cs['found_in_blocks']}/{cs['reference_headings']}, "
              f"in candidates {cs['in_candidate_table']}/{cs['reference_headings']}")

    md = render_markdown(reports)
    md_path = os.path.join("experiment_results", "round4_failure_diagnosis.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\nReport written to: {md_path}")
    print(f"Per-doc JSON dumps in: {OUT_DIR}/")


if __name__ == "__main__":
    main()
