"""Reproducible extraction -> structure -> serialization acceptance on tests/data.

No GT is changed. Stored-ID scoring and candidate-independent exact-title
alignment diagnostics are separate. Model calls are opt-in (--mode model).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import time
from collections import Counter
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from evaluation.metrics import HeadingGT, HeadingPred, compute_section_f1
from infrastructure.providers.registry import get_provider
from modules.parser.calibration import CalibrationMap, confidence_domain, pipeline_fingerprint
from modules.parser.diagnostics import document_diagnostics
from modules.parser.document_tree import DocumentTree
from modules.parser.evidence import brier_score, expected_calibration_error
from modules.parser.heading_candidates import generate_heading_candidate_set
from modules.parser.parser import CaliperParser
from modules.parser.config import ParserConfig
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode
from modules.parser.stage_graph import STAGE_VERSIONS
from modules.parser.titles import structural_text
from infrastructure.ai.usage import UsageAccumulator, use_usage_accumulator

ROOT = Path(__file__).resolve().parents[1]


def title_key(text: str) -> str:
    text = structural_text(text).casefold()
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)、．]?\s+", "", text)
    return re.sub(r"\s+", "", text)


def alignment_snapshot(blocks, headings):
    """Audit against ALL extracted text, never against heading candidates."""
    index = {}
    by_id = {b.id: b for b in blocks}
    for block in blocks:
        if block.type == "text" and block.text:
            index.setdefault(title_key(block.text), []).append(block.id)
    aligned, audit = [], []
    for heading in headings:
        title = heading["title"]
        stored = heading.get("block_id", -1)
        block = by_id.get(stored)
        matches = index.get(title_key(title), [])
        if block and title_key(block.text or "") == title_key(title):
            chosen, status = stored, "stored_exact"
        elif len(matches) == 1:
            chosen, status = matches[0], "unique_exact_suggestion"
        elif heading.get("granularity") == "run-in":
            # Only author-labelled run-in GT may use a paragraph-prefix anchor.
            # This is an auditable suggestion, not a provider/candidate decision.
            key = title_key(title)
            matches = [b.id for b in blocks if b.type == "text" and
                       title_key(b.text or "").startswith(key) and
                       title_key(b.text or "")[len(key):len(key) + 1] in {".", ":", "。", "："}]
            chosen, status = (matches[0], "run_in_prefix_suggestion") if len(matches) == 1 else (None, "unlocated_or_run_in")
        else:
            chosen, status = None, "ambiguous" if matches else "unlocated_or_run_in"
        audit.append({"title": title, "stored_id": stored, "status": status,
                      "suggested_id": chosen, "matching_ids": matches,
                      "requires_independent_review": len(matches) > 1 or status != "stored_exact"})
        if chosen is not None:
            aligned.append(HeadingGT(chosen, title, heading["level"]))
    return sorted(aligned, key=lambda h: h.block_id), audit


def discover_cases(tier="all"):
    cases = []
    for name, suffix in [("mis_styled", "docx"), ("no_style", "pdf"), ("long_docs", "pdf")]:
        if tier != "all" and tier != name:
            continue
        for source in sorted((ROOT / "tests/data" / name).glob(f"*.{suffix}")):
            gt = source.with_suffix(".json") if name != "long_docs" else source.parent / "ground_truth" / f"{source.stem}.json"
            if gt.exists():
                cases.append((name, source, gt))
    return cases


def _predictions(nodes):
    out = []
    for node in nodes:
        if node.title != "Document":
            out.append(HeadingPred(node.start_block_id, node.title, node.level))
        out.extend(_predictions(node.children))
    return out


def evaluate_case(tier, source, gt_path, mode, relation_weight=2.8):
    started = time.perf_counter()
    data = source.read_bytes()
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    provider = get_provider(source.suffix[1:])
    blocks = provider.extract_from_bytes(data)
    skips = provider.drain_skips() if hasattr(provider, "drain_skips") else []
    aligned, audit = alignment_snapshot(blocks, gt["headings"])
    candidates = generate_heading_candidate_set(blocks)
    config = ParserConfig(hierarchy_relation_weight=relation_weight)
    model_calls = 0
    usage_snapshot = None
    model_name = None
    if mode == "model":
        parser = CaliperParser(parser_config=config)
        parser.clear_cache()
        usage = UsageAccumulator()
        with use_usage_accumulator(usage):
            tree = parser.parse(blocks)
        usage_snapshot = usage.snapshot()
        model_name = parser.router._client.model
        model_calls = parser.router._actual_model_calls
        calibration_samples = []
    else:
        resolver = IntervalResolver(blocks, parser_config=config, heading_candidates=candidates)
        observations = [ChapterNode(block_id=h.block_id, title=h.title, level=h.level) for h in aligned] if mode == "oracle" else []
        nodes = resolver.resolve(observations)
        tree = DocumentTree(nodes, lossless_fallback=resolver.lossless_fallback,
            preamble_content=CaliperParser._extract_preamble(resolver, nodes, had_anchors=bool(observations)))
        ids, emissions, labels = resolver._global_inference._last_trace
        truths = {h.block_id: h for h in aligned}
        by_id = {b.id: b for b in blocks}
        calibration_samples = []
        for bid, emission, label in zip(ids, emissions, labels, strict=True):
            level_total = sum(emission[f"L{i}"] for i in range(1, 7))
            best_level = max(range(1, 7), key=lambda i: emission[f"L{i}"])
            chosen_level = int(label[1:]) if label != "NONE" else best_level
            calibration_samples.append({"domain": confidence_domain(by_id[bid], mode == "oracle"),
                "heading_score": 1 - emission["NONE"], "heading_correct": bid in truths,
                "level_score": emission[f"L{chosen_level}"] / level_total,
                "level_correct": bid in truths and chosen_level == truths[bid].level,
                "is_gt_heading": bid in truths, "selected": label != "NONE"})
    preds = _predictions(tree.nodes) if not tree.lossless_fallback else []
    stored_gt = [HeadingGT(h["block_id"], h["title"], h["level"]) for h in gt["headings"] if h.get("block_id", -1) >= 0]
    stored_metric, aligned_metric = compute_section_f1(stored_gt, preds), compute_section_f1(aligned, preds)
    serialized = tree.to_json()
    diagnostics = document_diagnostics(blocks, tree, skips=skips)
    record = {"document": source.stem, "tier": tier, "source_sha256": hashlib.sha256(data).hexdigest(),
        "run_in_gt_count": sum(h.get("granularity") == "run-in" for h in gt["headings"]),
        "gt_sha256": hashlib.sha256(gt_path.read_bytes()).hexdigest(), "mode": mode,
        "annotation_alignment": "stored plus exact-title diagnostic; not independently reviewed",
        "gt_total": len(gt["headings"]), "gt_aligned": len(aligned), "alignment_audit": audit,
        "stored_id_metrics": asdict(stored_metric), "exact_title_snapshot_metrics": asdict(aligned_metric),
        "recall_including_unresolved_gt": aligned_metric.tp / max(len(gt["headings"]), 1),
        "blocks": len(blocks), "predictions": [asdict(p) for p in preds],
        "diagnostics": diagnostics, "quality_report": tree.quality_report(),
        "serialized_json_bytes": len(serialized.encode()), "serialization_valid": bool(json.loads(serialized)),
        "model_calls": model_calls, "elapsed_seconds": time.perf_counter() - started,
        "model": model_name, "token_usage": usage_snapshot,
        "provider_skip_codes": [s.code for s in skips], "calibration_samples": calibration_samples}
    return record


def fit_docx_calibration(records, relation_weight=2.8):
    # Split by LOGICAL STRUCTURE, not typography: no sibling variants leak
    # across fitting and held-out evaluation.
    train = [r for r in records if r["tier"] == "mis_styled" and r["document"].startswith(("cjk_paper__", "report__"))]
    heldout = [r for r in records if r["tier"] == "mis_styled" and r["document"].startswith("deep__")]
    if not train or not heldout:
        return None
    artifact = {"version": 1, "artifact_id": "data-docx-deterministic-2026-10-03", "domain": "docx:deterministic",
        "parser_config": ParserConfig(hierarchy_relation_weight=relation_weight).model_dump(exclude={"confidence_calibration_path"}),
        "pipeline_fingerprint": pipeline_fingerprint(), "train_documents": [r["document"] for r in train],
        "validation_documents": [r["document"] for r in heldout], "channels": {}, "validation": {}}
    for channel in ["heading", "level"]:
        def samples(docs, channel=channel):
            return [s for r in docs for s in r["calibration_samples"] if s["domain"] == artifact["domain"]
                    and (channel == "heading" or (s["is_gt_heading"] and s["selected"]))]
        training, validation = samples(train), samples(heldout)
        if not training or not validation:
            continue
        score_key, hit_key = f"{channel}_score", f"{channel}_correct"
        curve = CalibrationMap.fit([s[score_key] for s in training], [s[hit_key] for s in training])
        artifact["channels"][channel] = {"upper_bounds": list(curve.upper_bounds), "probabilities": list(curve.probabilities)}
        before = [s[score_key] for s in validation]
        after = [curve.predict(v) for v in before]
        hits = [s[hit_key] for s in validation]
        artifact["validation"][channel] = {"samples": len(hits), "ece_before": expected_calibration_error(before, hits),
            "ece_after": expected_calibration_error(after, hits), "brier_before": brier_score(before, hits), "brier_after": brier_score(after, hits)}
    return artifact


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--mode", choices=["deterministic", "oracle", "model"], default="deterministic")
    cli.add_argument("--tier", choices=["all", "mis_styled", "no_style", "long_docs"], default="all")
    cli.add_argument("--limit", type=int, default=0)
    cli.add_argument("--relation-weight", type=float, default=2.8)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    if args.output.exists():
        cli.error("output directory already exists; use a new run directory")
    args.output.mkdir(parents=True)
    records, errors = [], []
    cases = discover_cases(args.tier)
    if args.limit:
        cases = cases[:args.limit]
    for tier, source, gt in cases:
        try:
            record = evaluate_case(tier, source, gt, args.mode, args.relation_weight)
            records.append(record)
            print(f"{source.stem}: F1={record['exact_title_snapshot_metrics']['f1']:.3f}", flush=True)
        except Exception as exc:
            # Do not print upstream messages containing credentials/document text.
            errors.append({"document": source.stem, "error_type": type(exc).__name__})
            print(f"{source.stem}: FAILED ({type(exc).__name__})", flush=True)
    payload = {"mode": args.mode, "quality_claim": "real_model" if args.mode == "model" else "oracle_upper_bound" if args.mode == "oracle" else "deterministic_only",
        "pipeline_fingerprint": pipeline_fingerprint(), "stage_versions": STAGE_VERSIONS,
        "python": platform.python_version(), "packages": {name: version(name) for name in ["PyMuPDF", "python-docx", "pydantic"]},
        "relation_weight": args.relation_weight, "documents": records, "errors": errors}
    (args.output / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    calibration = fit_docx_calibration(records, args.relation_weight) if args.mode == "deterministic" else None
    if calibration:
        (args.output / "calibration.json").write_text(json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Algorithm acceptance — 2026-10-03", "", f"Mode: **{payload['quality_claim']}**. No GT files changed.",
        "", "Stored-ID scores and exact-title diagnostic snapshot scores are separate in results.json.",
        "Unresolved/run-in GT remains in the all-GT recall denominator. Coverage applies only to extracted blocks.",
        "", "| Tier | Documents | Diagnostic P | Diagnostic R | Diagnostic F1 | Unresolved GT |", "|---|---:|---:|---:|---:|---:|"]
    for tier in sorted({r["tier"] for r in records}):
        subset = [r for r in records if r["tier"] == tier]
        counts = Counter()
        for r in subset:
            counts.update({k: r["exact_title_snapshot_metrics"][k] for k in ["tp", "fp", "fn"]})
        p = counts["tp"] / max(counts["tp"] + counts["fp"], 1)
        recall = counts["tp"] / max(counts["tp"] + counts["fn"], 1)
        f1 = 2 * p * recall / max(p + recall, 1e-12)
        unresolved = sum(r["gt_total"] - r["gt_aligned"] for r in subset)
        lines.append(f"| {tier} | {len(subset)} | {p:.3f} | {recall:.3f} | {f1:.3f} | {unresolved} |")
    lines.extend(["", f"Failed documents: {len(errors)}. See per-document diagnostics and confidence channels in results.json."])
    (args.output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
