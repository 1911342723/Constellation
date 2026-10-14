"""Sensitivity of the Comp-HRDoc scores to the title-matching rule.

The main rule compares the shorter of the two normalised titles with the same-length
prefix of the other (so that a run-in heading reported together with its paragraph
can match). This script re-scores the stored predictions of every test run under a
strict rule: whole normalised titles must reach the same similarity threshold, and
when both titles carry a leading section number the numbers must be equal. Records
are not modified; the result is written to <out>/strict_matching.json.

python -m evaluation.hrdoc.strict_matching --runs <runs> --toc-dir <test_eval_toc> \
    --lines-dir <test_eval> --out <report dir>
"""
from __future__ import annotations

import argparse
import json
from difflib import SequenceMatcher
from pathlib import Path

from evaluation.hrdoc import evaluate
from evaluation.hrdoc.evaluate import Pred, load_gt, load_title, norm, score_document


def _split_number(text: str) -> tuple[str, str]:
    text = (text or "").replace("**", "")
    match = evaluate._LEAD_NUMBER.match(text)
    if not match:
        return "", norm(text)
    return norm(match.group(0)), norm(text[match.end():])


def strict_title_score(gt_text: str, pred_text: str) -> float:
    g_num, g_body = _split_number(gt_text)
    p_num, p_body = _split_number(pred_text)
    if g_num and p_num and g_num != p_num:
        return 0.0
    if not g_body or not p_body:
        return 1.0 if norm(gt_text) == norm(pred_text) else 0.0
    return SequenceMatcher(None, g_body, p_body, autojunk=False).ratio()


def micro(rows: list[dict]) -> dict:
    tp = sum(m["tp"] for m in rows)
    p = tp / max(sum(m["pred"] for m in rows), 1)
    r = tp / max(sum(m["gt"] for m in rows), 1)
    return {"precision": p, "recall": r, "f1": 2 * p * r / max(p + r, 1e-12),
            "teds": sum(m["teds"] for m in rows) / max(len(rows), 1), "documents": len(rows)}


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cli.add_argument("--runs", type=Path, required=True)
    cli.add_argument("--toc-dir", type=Path, required=True)
    cli.add_argument("--lines-dir", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--split", default="test")
    args = cli.parse_args()

    evaluate.title_score = strict_title_score  # used by evaluate.match
    result = {}
    for run in sorted(p for p in args.runs.glob(f"{args.split}-*") if p.is_dir()):
        main_rows, strict_rows = [], []
        for path in sorted(run.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if "metrics" not in record or "error" in record:
                continue
            doc = record["doc"]
            preds = [Pred(p["text"], p["page"], p["level"], p.get("parent", -1)) for p in record["predictions"]]
            strict_rows.append(score_document(load_gt(args.toc_dir / f"{doc}.json"), preds,
                                              load_title(args.lines_dir, doc)))
            main_rows.append(record["metrics"])
        name = run.name[len(args.split) + 1:]
        result[name] = {"main": micro(main_rows), "strict": micro(strict_rows)}
        print(f"{name}: F1 {result[name]['main']['f1']:.3f} -> {result[name]['strict']['f1']:.3f}, "
              f"TEDS {result[name]['main']['teds']:.3f} -> {result[name]['strict']['teds']:.3f}", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "strict_matching.json").write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
