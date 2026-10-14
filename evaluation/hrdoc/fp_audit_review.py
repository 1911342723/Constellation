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
        "# 误报审计：作者抽查表",
        "",
        f"从论文统计的 {len(audit)} 条中按固定种子 {seed} 抽取 {len(chosen)} 条。",
        "每一条：看“系统判成标题的行”和它前后的原文，判断它到底是不是章节标题，",
        "然后把末尾 `author: ?` 的 `?` 改成 `agree`（同意 AI），或改成你自己的判定，后面可加备注。",
        "",
        "三种判定：",
        "",
        "- `genuine_heading`：真标题，只是参考答案没收录（含 Abstract、Acknowledgments、",
        "  References、Appendix、行内编号小节）。",
        "- `split_or_version_text`：属于参考答案里某个章节，但标题被拆成多行，或因 arXiv 版本不同而有出入。",
        "- `error`：不是章节标题（图表文字、公式/算法行、列表项、页眉、参考文献条目；",
        "  无编号的粗体段落标签、定理/证明标签也算 error）。",
        "",
        "原文片段的格式：`>>` 标出被判成标题的那一行；方括号里是该行的排版（字号、是否粗体、",
        "对齐方式），例如 `[9.0pt 粗体 右对齐]`。排版只是判断线索，不是判定。",
        "",
    ]
    meaning = {"genuine_heading": "真标题，参考答案漏收",
               "split_or_version_text": "标题拆行或版本差异",
               "error": "不是标题"}
    kinds = {"title_page_or_running_header": "题目页或页眉",
             "run_in_label_or_list_item": "行内标签或列表项",
             "figure_or_table_text": "图表文字",
             "reference_entry": "参考文献条目",
             "formula_or_algorithm_line": "公式或算法行",
             "body_text_line": "正文行"}
    align = {"left": "左对齐", "right": "右对齐", "center": "居中", "justify": "两端对齐"}
    for number, item in enumerate(chosen, 1):
        ctx = context.get(_key(item), {})
        page = ctx.get("page")
        link = f"https://arxiv.org/pdf/{item['doc']}" + (f"#page={page + 1}" if page is not None else "")
        verdict = f"`{item['verdict']}`（{meaning[item['verdict']]}"
        if item.get("error_kind"):
            verdict += f"：{kinds.get(item['error_kind'], item['error_kind'])}"
        lines += [
            f"## {number}. {item['doc']} #{item['pred_index']}",
            "",
            f"- 系统判成标题的行：**{item['title']}**（第 {ctx.get('level', '?')} 级，"
            f"第 {page + 1 if page is not None else '?'} 页）",
            f"- 原文页面：{link}",
            f"- 参考答案附近的章节：" + ("；".join(ctx.get("reference_sections_nearby", [])) or "（无）"),
            f"- **AI 判定：{verdict}）**",
            "",
            "```",
        ]
        for row in ctx.get("context", []):
            mark = ">>" if row.get("this") else "  "
            style = " ".join(part for part in (
                f"{row.get('font_size', '?')}pt",
                "粗体" if row.get("bold") else "",
                align.get(row.get("alignment", ""), row.get("alignment", "")),
            ) if part)
            lines.append(f"{mark} [{style}] {row.get('text', '')}")
        lines += ["```", "", "author: ?", ""]
    SHEET.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(chosen)} items -> {SHEET}")


def score() -> None:
    audit = {_key(i): i for i in json.loads(AUDIT.read_text(encoding="utf-8"))["items"]}
    text = SHEET.read_text(encoding="utf-8")
    pending = sum(1 for line in text.splitlines() if line.strip() == "author: ?")
    if pending:
        raise SystemExit(
            f"{pending} item(s) in {SHEET} still read 'author: ?'.\n"
            "Open the sheet, replace each '?' with 'agree' or with your verdict "
            "(genuine_heading / split_or_version_text / error), save, then run score again."
        )
    reviewed, current = [], None
    for line in text.splitlines():
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
