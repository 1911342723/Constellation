"""Typography-robustness baseline on the mis-styled corpus.

The mis-styled corpus (``scripts/manual/create_mis_styled_docs.py``) renders one
logical structure under seven typographies, six of which are wrong in a way real
authors get wrong.  This script measures how much of the *intended* structure the
deterministic core recovers, under two settings:

- ``deterministic`` — candidate evidence only, no LLM votes at all;
- ``oracle`` — every GT heading is supplied as a perfectly aligned LLM vote with
  the correct level.  This is an **upper bound on any model**: it answers "if the
  LLM were always right, could the current fusion express the right answer?"
  It is not a claim about any real model.
- ``flat_votes`` — every GT heading is supplied as a perfectly aligned vote that
  says ``L1``.  A real deployed model behaves this way on typography that carries
  no depth signal (2026-09-03 live run against ``/parse/full``): it finds every
  heading and tells no depth from another.  This setting asks whether a reader
  who abstains on hierarchy destroys the hierarchy the document itself states.

Everything here is deterministic and LLM-free, so the numbers are reproducible
without an API key.  All settings are scored with the standard discipline of
``evaluation/metrics.py`` (block-id tolerance 3 + title Levenshtein >= 0.6).

Run: python -m evaluation.typography_robustness
Outputs: experiment_results/typography_robustness.md (+ _raw.json)
"""
from __future__ import annotations

import json
import os
import sys
from statistics import mean

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evaluation.metrics import HeadingGT, HeadingPred, compute_section_f1
from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.global_inference import GlobalHeadingInference
from modules.parser.heading_candidates import generate_heading_candidate_set
from modules.parser.schemas import ChapterNode
from modules.parser.titles import normalize_title

CORPUS_DIR = os.path.join("tests", "data", "mis_styled")
SETTINGS = ("deterministic", "oracle", "flat_votes")
OUT_MD = os.path.join("experiment_results", "typography_robustness.md")
OUT_JSON = os.path.join("experiment_results", "typography_robustness_raw.json")

VARIANT_ORDER = (
    "clean",
    "style_shift",
    "flat_style",
    "over_styled",
    "styled_labels",
    "numbering_shift",
    "font_inverted",
    "bold_everything",
)


def load_corpus() -> list[dict]:
    """Extract blocks and GT once per document (the slow step)."""
    corpus: list[dict] = []
    for fname in sorted(os.listdir(CORPUS_DIR)):
        if not fname.endswith(".docx"):
            continue
        stem = fname[:-5]
        gt_path = os.path.join(CORPUS_DIR, f"{stem}.json")
        if not os.path.exists(gt_path):
            continue
        with open(gt_path, encoding="utf-8") as handle:
            gt = json.load(handle)
        print(f"[load] {stem} ...", flush=True)
        blocks = DocxProvider().extract(os.path.join(CORPUS_DIR, fname))
        corpus.append({
            "stem": stem,
            "structure": gt["structure"],
            "variant": gt["variant"],
            "blocks": blocks,
            "gt": [
                HeadingGT(h["block_id"], h["title"], h["level"])
                for h in gt["headings"] if h["block_id"] >= 0
            ],
            "distractors": {
                d["block_id"] for d in gt.get("distractors", []) if d["block_id"] >= 0
            },
        })
    return corpus


def _votes(gt: list[HeadingGT], setting: str) -> list[ChapterNode]:
    """One perfectly aligned vote per GT heading, levelled per the setting.

    ``oracle`` carries the true level; ``flat_votes`` says ``L1`` everywhere,
    which is what a real model does on a document whose typography states no
    depth.  Both are perfectly aligned, so the only variable is the level.
    """
    return [
        ChapterNode(
            block_id=heading.block_id,
            title=heading.title,
            snippet=heading.title[:40],
            level=1 if setting == "flat_votes" else heading.level,
            confidence=0.95,
        )
        for heading in gt
    ]


def evaluate_document(entry: dict, setting: str) -> dict:
    """Decode one document under one setting and score it against GT."""
    blocks = entry["blocks"]
    candidate_set = generate_heading_candidate_set(blocks)
    votes = [] if setting == "deterministic" else _votes(entry["gt"], setting)
    decoded = GlobalHeadingInference(blocks, candidate_set).decode(votes)

    predictions = [
        HeadingPred(
            block_id=chapter.start_block_id,
            title=normalize_title(chapter.title),
            level=chapter.level,
        )
        for chapter in decoded
    ]
    gt = [
        HeadingGT(h.block_id, normalize_title(h.title), h.level) for h in entry["gt"]
    ]
    result = compute_section_f1(gt, predictions)

    candidate_ids = {c.block_id for c in candidate_set.candidates}
    predicted_ids = {p.block_id for p in predictions}
    gt_sequence = [(h.level, h.title) for h in gt]
    pred_sequence = [(p.level, p.title) for p in predictions]

    return {
        "stem": entry["stem"],
        "structure": entry["structure"],
        "variant": entry["variant"],
        "setting": setting,
        "gt_headings": len(gt),
        "predicted": len(predictions),
        "precision": round(result.precision, 4),
        "recall": round(result.recall, 4),
        "f1": round(result.f1, 4),
        "level_accuracy": round(result.hierarchy_accuracy, 4),
        "level_correct": result.level_correct,
        "level_total": result.level_total,
        "parent_relation_distance": result.parent_relation_distance,
        "exact_structure": gt_sequence == pred_sequence,
        "candidate_recall": round(
            sum(1 for h in gt if h.block_id in candidate_ids) / max(len(gt), 1), 4,
        ),
        "distractors": len(entry["distractors"]),
        "distractor_hits": len(entry["distractors"] & predicted_ids),
    }


def _aggregate(rows: list[dict]) -> dict:
    """Micro-average within one (variant, setting) group."""
    level_correct = sum(row["level_correct"] for row in rows)
    level_total = sum(row["level_total"] for row in rows)
    distractors = sum(row["distractors"] for row in rows)
    return {
        "documents": len(rows),
        "precision": round(mean(row["precision"] for row in rows), 4),
        "recall": round(mean(row["recall"] for row in rows), 4),
        "f1": round(mean(row["f1"] for row in rows), 4),
        "level_accuracy": round(level_correct / max(level_total, 1), 4),
        "level_correct": level_correct,
        "level_total": level_total,
        "parent_relation_distance": sum(row["parent_relation_distance"] for row in rows),
        "exact_structure": sum(row["exact_structure"] for row in rows),
        "candidate_recall": round(mean(row["candidate_recall"] for row in rows), 4),
        "distractors": distractors,
        "distractor_hits": sum(row["distractor_hits"] for row in rows),
    }


def _percent(value: float) -> str:
    return f"{value * 100:.1f}"


def main() -> None:
    corpus = load_corpus()
    if not corpus:
        raise SystemExit(
            f"No documents in {CORPUS_DIR}. "
            "Run: python scripts/manual/create_mis_styled_docs.py"
        )

    rows: list[dict] = []
    for setting in SETTINGS:
        for entry in corpus:
            row = evaluate_document(entry, setting)
            rows.append(row)
            print(
                f"[{setting:13s}] {row['stem']:28s} "
                f"F1={_percent(row['f1'])}% level={_percent(row['level_accuracy'])}% "
                f"exact={'Y' if row['exact_structure'] else 'n'} "
                f"distractor_hits={row['distractor_hits']}/{row['distractors']}",
                flush=True,
            )

    summary = {
        setting: {
            variant: _aggregate([
                row for row in rows
                if row["setting"] == setting and row["variant"] == variant
            ])
            for variant in VARIANT_ORDER
        }
        for setting in SETTINGS
    }

    os.makedirs("experiment_results", exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as handle:
        json.dump({"summary": summary, "per_document": rows}, handle,
                  ensure_ascii=False, indent=2)

    structures = sorted({entry["structure"] for entry in corpus})
    lines = [
        "# Typography Robustness of the Deterministic Core",
        "",
        "> Generated by `evaluation/typography_robustness.py`. Deterministic, LLM-free, reproducible without an API key.",
        f"> Corpus: `tests/data/mis_styled/` — {len(structures)} logical structures "
        f"({', '.join(structures)}) x {len(VARIANT_ORDER)} typographies = {len(corpus)} DOCX documents,",
        "> regenerate with `python scripts/manual/create_mis_styled_docs.py`.",
        "> Scoring: `evaluation/metrics.py` discipline (|block_id delta| <= 3 and title Levenshtein >= 0.6).",
        "> `level accuracy` is computed on matched pairs only; `exact` counts documents whose "
        "(level, title) sequence equals GT exactly.",
        "> `oracle` supplies every GT heading as a perfectly aligned vote with the correct level — "
        "an upper bound on any model, not a measurement of one.",
        "> `flat_votes` supplies the same perfectly aligned votes but says `L1` for every heading — "
        "the observed behaviour of a real deployed model on typography that states no depth.",
        "",
        "## Summary by typography",
        "",
    ]
    for setting in SETTINGS:
        label = {
            "deterministic": "Deterministic only (no LLM votes)",
            "oracle": "Oracle votes (perfect model upper bound)",
            "flat_votes": "Flat votes (every heading voted L1, as observed live)",
        }[setting]
        lines += [
            f"### {label}",
            "",
            "| Typography | Heading F1 % | Level acc % | Exact structure | Parent-relation dist | Candidate recall % | Distractor hits |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for variant in VARIANT_ORDER:
            agg = summary[setting][variant]
            lines.append(
                f"| `{variant}` | {_percent(agg['f1'])} | {_percent(agg['level_accuracy'])} "
                f"| {agg['exact_structure']}/{agg['documents']} "
                f"| {agg['parent_relation_distance']:.0f} "
                f"| {_percent(agg['candidate_recall'])} "
                f"| {agg['distractor_hits']}/{agg['distractors']} |"
            )
        lines.append("")

    lines += ["## Per document", "", "| Document | Setting | F1 % | Level acc % | Exact | Pred/GT | Distractor hits |", "|---|---|---:|---:|:-:|---:|---:|"]
    for row in rows:
        lines.append(
            f"| `{row['stem']}` | {row['setting']} | {_percent(row['f1'])} "
            f"| {_percent(row['level_accuracy'])} "
            f"| {'Y' if row['exact_structure'] else 'n'} "
            f"| {row['predicted']}/{row['gt_headings']} "
            f"| {row['distractor_hits']}/{row['distractors']} |"
        )
    lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"\nWritten: {OUT_MD}")


if __name__ == "__main__":
    main()
