"""Aggregate Comp-HRDoc runs into tables, paired bootstrap intervals and figures.

Usage: python -m evaluation.hrdoc.report --runs <dir> --split test --out <dir>
Every number in the paper's Comp-HRDoc section is produced here from the
per-document JSON records written by ``evaluate``.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from evaluation.hrdoc.evaluate import summarize

SYSTEM_LABELS = {
    "outline": "Embedded bookmarks",
    "docling": "Docling 2.138",
    "llm_markdown": "Generative LLM (Markdown)",
    "det": "Constellation, deterministic",
    "model": "Constellation, full",
    "det-nooutline": "Constellation, deterministic",
    "model-nooutline": "Constellation, full",
    "model-nooutline-nosilence": "  w/o silence channel",
}


def load(run: Path) -> dict[str, dict]:
    records = {}
    for path in sorted(run.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if "metrics" in record:
            records[record["doc"]] = record
    return records


def doc_f1(m: dict) -> float:
    p = m["tp"] / max(m["pred"], 1)
    r = m["tp"] / max(m["gt"], 1)
    return 2 * p * r / max(p + r, 1e-12)


def micro(records: list[dict]) -> dict:
    tp = sum(r["metrics"]["tp"] for r in records)
    pred = sum(r["metrics"]["pred"] for r in records)
    gt = sum(r["metrics"]["gt"] for r in records)
    p, rc = tp / max(pred, 1), tp / max(gt, 1)
    return {"f1": 2 * p * rc / max(p + rc, 1e-12),
            "teds": sum(r["metrics"]["teds"] for r in records) / max(len(records), 1)}


def paired_bootstrap(a: dict[str, dict], b: dict[str, dict], docs: list[str], n: int = 5000,
                     seed: int = 7) -> dict:
    """Difference a - b in micro-F1 and macro-TEDS, resampling documents."""
    rng = random.Random(seed)
    base_a, base_b = micro([a[d] for d in docs]), micro([b[d] for d in docs])
    deltas = {"f1": [], "teds": []}
    for _ in range(n):
        sample = [docs[rng.randrange(len(docs))] for _ in docs]
        ma, mb = micro([a[d] for d in sample]), micro([b[d] for d in sample])
        for key in deltas:
            deltas[key].append(ma[key] - mb[key])
    out = {}
    for key, values in deltas.items():
        values.sort()
        out[key] = {"delta": base_a[key] - base_b[key], "lo": values[int(0.025 * n)], "hi": values[int(0.975 * n) - 1]}
    return out


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--runs", type=Path, required=True)
    cli.add_argument("--split", default="test")
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    runs = {path.name[len(args.split) + 1:]: load(path) for path in sorted(args.runs.glob(f"{args.split}-*"))
            if path.is_dir()}
    runs.update({path.name[4:]: load(path) for path in sorted(args.runs.glob("gen-*")) if path.is_dir()})
    summary = {name: summarize(list(records.values())) for name, records in runs.items() if records}
    # Sensitivity: front/back-matter titles removed from predictions and reference alike.
    summary_generic = {name: summarize([{**r, "metrics": r["metrics_generic"]} for r in records.values()
                                        if "metrics_generic" in r])
                       for name, records in runs.items() if records}

    comparisons = {}
    pairs = [("model-nooutline", "det-nooutline"), ("model-nooutline", "model-nooutline-nosilence"),
             ("model-nooutline", "docling"), ("model-nooutline", "docling-numlevels"),
             ("model-nooutline", "outline"), ("model", "det"),
             ("model", "model-nooutline"), ("det", "det-nooutline"),
             ("model-nooutline-layout", "model-nooutline"), ("model-nooutline-layout", "det-nooutline-layout"),
             ("model-nooutline-layout", "docling-numlevels"), ("det-nooutline-layout", "det-nooutline"),
             ("model-nooutline", "llm_markdown"), ("model-nooutline-rep4", "llm_markdown")]
    for left, right in pairs:
        if left in runs and right in runs:
            docs = sorted(set(runs[left]) & set(runs[right]))
            if docs:
                comparisons[f"{left} - {right}"] = {"documents": len(docs),
                                                    **paired_bootstrap(runs[left], runs[right], docs)}
    # Same-document view of the generative sample for every system.
    if "llm_markdown" in runs:
        docs = sorted(runs["llm_markdown"])
        summary_gen = {name: summarize([records[d] for d in docs if d in records])
                       for name, records in runs.items() if records}
    else:
        summary_gen = {}
    # Run-to-run variation of the full system (sampled router) on the generative sample.
    repeats = {}
    rep_names = ["model-nooutline"] + sorted(n for n in runs if n.startswith("model-nooutline-rep"))
    if len(rep_names) > 1:
        docs = sorted(set.intersection(*(set(runs[n]) for n in rep_names)))
        per_run = {n: micro([runs[n][d] for d in docs]) for n in rep_names}
        repeats = {"documents": len(docs), "runs": per_run,
                   **{f"{k}_{agg.__name__}": agg(r[k] for r in per_run.values())
                      for k in ("f1", "teds") for agg in (min, max)}}
    payload = {"split": args.split, "summary": summary, "comparisons": comparisons,
               "generative_sample_summary": summary_gen, "repeat_runs": repeats,
               "summary_generic": summary_generic}
    (args.out / "summary.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    lines = [f"# Comp-HRDoc results ({args.split})", "",
             "| System | Docs | P | R | F1 | Level acc. | Parent acc. | TEDS | Calls | Prompt tok. | Compl. tok. |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, s in summary.items():
        lines.append(f"| {name} | {s['documents']} | {s['micro_precision']:.3f} | {s['micro_recall']:.3f} | "
                     f"{s['micro_f1']:.3f} | {s['level_accuracy']:.3f} | {s['parent_accuracy']:.3f} | "
                     f"{s['macro_teds']:.3f} | {s.get('model_calls', '')} | {s.get('prompt_tokens', '')} | "
                     f"{s.get('completion_tokens', '')} |")
    lines += ["", "## Paired bootstrap (documents resampled, 95% interval)", ""]
    for key, c in comparisons.items():
        lines.append(f"- {key} (n={c['documents']}): ΔF1 {c['f1']['delta']:+.3f} [{c['f1']['lo']:+.3f}, {c['f1']['hi']:+.3f}], "
                     f"ΔTEDS {c['teds']['delta']:+.3f} [{c['teds']['lo']:+.3f}, {c['teds']['hi']:+.3f}]")
    if repeats:
        lines += ["", f"## Repeat runs of the full system (n={repeats['documents']} documents)", ""]
        lines += [f"- {n}: F1 {r['f1']:.3f}, TEDS {r['teds']:.3f}" for n, r in repeats["runs"].items()]
    (args.out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
