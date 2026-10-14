"""Author spot-check of the AI-assisted false-positive audit.

    python -m evaluation.hrdoc.fp_audit_review sheet [--n 20] [--seed 1]
    python -m evaluation.hrdoc.fp_audit_review score

``sheet`` writes ``fp_audit_review.md``: a seeded sample of audited items with
the predicted line, its neighbouring source lines and typography, the nearby
reference sections, a link to the PDF page and the AI verdict. For each item
the reviewer replaces ``?`` on the ``author:`` line with ``agree`` or with the
verdict they would give (``genuine_heading``, ``split_or_version_text`` or
``error``), optionally followed by a note.

``score`` reads the filled sheet, reports the agreement with the AI verdicts and
writes ``fp_audit_author_review.json`` next to the audit.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

HR = Path(__file__).resolve().parents[2] / "experiment_results/hrdoc_2026-10-13"
AUDIT = HR / "fp_audit.json"
SAMPLE = HR / "fp_audit_sample.json"
SHEET = HR / "fp_audit_review.md"
RESULT = HR / "fp_audit_author_review.json"
VERDICTS = ("genuine_heading", "split_or_version_text", "error")

_ITEM = re.compile(r"^## (\d+)\. (\S+) #(\d+)$")
_AUTHOR = re.compile(r"^author:\s*(\S+)(.*)$")


def _key(item: dict) -> tuple[str, int]:
    return item["doc"], item["pred_index"]


def _reported_items() -> list[dict]:
    """Audited items that are still false positives, as counted in the paper."""
    unmatched = {}
    for path in (HR / "runs/test-model-nooutline").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if "metrics" in record:
            unmatched[record["doc"]] = set(record["metrics"]["unmatched_pred"])
    items = json.loads(AUDIT.read_text(encoding="utf-8"))["items"]
    return [i for i in items if i["title"][:80] in unmatched.get(i["doc"], set())]


def sheet(n: int, seed: int) -> None:
    audit = _reported_items()
    context = {_key(i): i for i in json.loads(SAMPLE.read_text(encoding="utf-8"))["items"]}
    chosen = sorted(random.Random(seed).sample(audit, min(n, len(audit))), key=_key)

    lines = [
        "# False-positive audit: author spot-check",
        "",
        f"{len(chosen)} of {len(audit)} audited items (seed {seed}).",
        "For each item, replace `?` on the `author:` line with `agree`, or with the verdict you",
        "would give: `genuine_heading`, `split_or_version_text` or `error`. A note may follow.",
        "",
        "- `genuine_heading`: a section title of the document that the reference omits",
        "  (incl. Abstract, Acknowledgments, References, Appendix, run-in numbered subsections).",
        "- `split_or_version_text`: part of a referenced section title (split across lines) or",
        "  text that differs only because the fetched arXiv version differs.",
        "- `error`: not a section title (unnumbered bold paragraph labels and theorem/proof",
        "  labels count as errors).",
        "",
    ]
    for number, item in enumerate(chosen, 1):
        ctx = context.get(_key(item), {})
        page = ctx.get("page")
        link = f"https://arxiv.org/pdf/{item['doc']}" + (f"#page={page + 1}" if page is not None else "")
        lines += [
            f"## {number}. {item['doc']} #{item['pred_index']}",
            "",
            f"- predicted: **{item['title']}** (level {ctx.get('level', '?')}, "
            f"page {page + 1 if page is not None else '?'}) — {link}",
            f"- AI verdict: `{item['verdict']}`"
            + (f" ({item['error_kind']})" if item.get("error_kind") else ""),
            "- nearby reference sections: "
            + ("; ".join(ctx.get("reference_sections_nearby", [])) or "(none)"),
            "",
            "```",
        ]
        for row in ctx.get("context", []):
            mark = ">>" if row.get("this") else "  "
            style = f"{row.get('font_size', '?')}pt{' bold' if row.get('bold') else ''} {row.get('alignment', '')}"
            lines.append(f"{mark} [{style.strip()}] {row.get('text', '')}")
        lines += ["```", "", "author: ?", ""]
    SHEET.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(chosen)} items -> {SHEET}")


def score() -> None:
    audit = {_key(i): i for i in json.loads(AUDIT.read_text(encoding="utf-8"))["items"]}
    reviewed, current = [], None
    for line in SHEET.read_text(encoding="utf-8").splitlines():
        if match := _ITEM.match(line.strip()):
            current = (match.group(2), int(match.group(3)))
        elif current and (match := _AUTHOR.match(line.strip())):
            answer, note = match.group(1), match.group(2).strip()
            if answer == "?":
                raise SystemExit(f"item {current[0]} #{current[1]} has not been reviewed yet")
            ai = audit[current]["verdict"]
            author = ai if answer == "agree" else answer
            if author not in VERDICTS:
                raise SystemExit(f"item {current[0]} #{current[1]}: unknown verdict {answer!r}")
            reviewed.append({"doc": current[0], "pred_index": current[1], "ai_verdict": ai,
                             "author_verdict": author, "note": note})
            current = None
    if not reviewed:
        raise SystemExit("no reviewed items found")

    agree = sum(r["ai_verdict"] == r["author_verdict"] for r in reviewed)
    changes = Counter(f"{r['ai_verdict']} -> {r['author_verdict']}"
                      for r in reviewed if r["ai_verdict"] != r["author_verdict"])
    result = {
        "protocol": "Author spot-check of a seeded sample of the AI-assisted audit "
                    "(evaluation/hrdoc/fp_audit_review.py); the AI verdicts in fp_audit.json are unchanged.",
        "reviewed": len(reviewed),
        "agreement": agree,
        "disagreements": dict(changes),
        "items": reviewed,
    }
    RESULT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"agreement {agree}/{len(reviewed)} = {agree / len(reviewed):.0%}")
    for change, count in changes.most_common():
        print(f"  {change}: {count}")
    print(f"-> {RESULT}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("sheet", help="write the review sheet")
    make.add_argument("--n", type=int, default=20)
    make.add_argument("--seed", type=int, default=1)
    sub.add_parser("score", help="score the filled sheet")
    args = parser.parse_args()
    if args.command == "sheet":
        sheet(args.n, args.seed)
    else:
        score()


if __name__ == "__main__":
    main()
