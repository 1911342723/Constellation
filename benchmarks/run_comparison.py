"""
Constellation 横向对比实验 — 真实安装对比
==========================================

在 IBM Docling 公开数据集上，用 4 个已安装的方案做横向对比:

1. IBM Docling       — IBM 官方文档解析库
2. Microsoft MarkItDown — 微软官方 DOCX→Markdown 工具
3. Mammoth           — 老牌开源 DOCX→HTML/Markdown 库
4. Constellation     — 本文 LLM+FSM 解耦架构

对比维度: 标题识别 F1, 层级准确率, 字符覆盖, 公式/文本框, 速度
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

_BENCHMARK_DIR = Path(__file__).parent / "docling"
_GT_DIR = Path(__file__).parent / "ground_truth"
_OUTPUT = Path(__file__).parent / "comparison_report.md"


@dataclass
class ComparisonResult:
    tool_name: str
    filename: str
    headings_found: list
    heading_count: int
    output_text: str
    output_chars: int
    original_chars: int
    char_ratio: float
    has_formula: bool
    has_textbox: bool
    formula_count: int
    time_s: float


# ── 工具函数 ──────────────────────────────────────────

def _extract_md_headings(text: str) -> list:
    """从 Markdown 文本中提取标题"""
    headings = []
    for line in text.split('\n'):
        m = re.match(r'^(#{1,6})\s+(.+)$', line.strip())
        if m:
            headings.append({"title": m.group(2).strip(), "level": len(m.group(1))})
    return headings


def _detect_formulas(text: str) -> tuple[bool, int]:
    """检测 LaTeX 公式"""
    patterns = re.findall(r'\$\$.+?\$\$|\$[^$\n]+?\$|\\frac\{|\\sum|\\int|\\sqrt', text, re.DOTALL)
    return len(patterns) > 0, len(patterns)


def _detect_textbox_content(text: str, filename: str) -> bool:
    """检测文本框内容是否被保留"""
    if filename != 'textbox.docx':
        return False
    # textbox.docx 中的关键内容: 如果输出包含多段文本，说明文本框被提取了
    return len(text) > 500


# ══════════════════════════════════════════════════════════
#  方案 1: IBM Docling
# ══════════════════════════════════════════════════════════

def run_docling(docx_path: Path) -> ComparisonResult:
    """使用 IBM Docling 解析 DOCX"""
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()

    t0 = time.perf_counter()
    result = converter.convert(str(docx_path))
    doc = result.document
    elapsed = time.perf_counter() - t0

    # 导出 Markdown
    md_text = doc.export_to_markdown()

    # 提取标题
    headings = _extract_md_headings(md_text)

    has_formula, formula_count = _detect_formulas(md_text)
    has_textbox = _detect_textbox_content(md_text, docx_path.name)

    return ComparisonResult(
        tool_name="IBM Docling",
        filename=docx_path.name,
        headings_found=headings,
        heading_count=len(headings),
        output_text=md_text,
        output_chars=len(md_text),
        original_chars=0,
        char_ratio=0,
        has_formula=has_formula,
        has_textbox=has_textbox,
        formula_count=formula_count,
        time_s=elapsed,
    )


# ══════════════════════════════════════════════════════════
#  方案 2: Microsoft MarkItDown
# ══════════════════════════════════════════════════════════

def run_markitdown(docx_path: Path) -> ComparisonResult:
    """使用 MarkItDown 转换 DOCX"""
    from markitdown import MarkItDown

    md = MarkItDown()
    t0 = time.perf_counter()
    result = md.convert(str(docx_path))
    elapsed = time.perf_counter() - t0

    text = result.text_content or ""
    headings = _extract_md_headings(text)
    has_formula, formula_count = _detect_formulas(text)
    has_textbox = _detect_textbox_content(text, docx_path.name)

    return ComparisonResult(
        tool_name="MarkItDown",
        filename=docx_path.name,
        headings_found=headings,
        heading_count=len(headings),
        output_text=text,
        output_chars=len(text),
        original_chars=0,
        char_ratio=0,
        has_formula=has_formula,
        has_textbox=has_textbox,
        formula_count=formula_count,
        time_s=elapsed,
    )


# ══════════════════════════════════════════════════════════
#  方案 3: Mammoth
# ══════════════════════════════════════════════════════════

def run_mammoth(docx_path: Path) -> ComparisonResult:
    """使用 Mammoth 转换 DOCX → Markdown"""
    import mammoth

    t0 = time.perf_counter()
    with open(docx_path, "rb") as f:
        result = mammoth.convert_to_markdown(f)
    elapsed = time.perf_counter() - t0

    text = result.value or ""
    headings = _extract_md_headings(text)
    has_formula, formula_count = _detect_formulas(text)
    has_textbox = _detect_textbox_content(text, docx_path.name)

    return ComparisonResult(
        tool_name="Mammoth",
        filename=docx_path.name,
        headings_found=headings,
        heading_count=len(headings),
        output_text=text,
        output_chars=len(text),
        original_chars=0,
        char_ratio=0,
        has_formula=has_formula,
        has_textbox=has_textbox,
        formula_count=formula_count,
        time_s=elapsed,
    )


# ══════════════════════════════════════════════════════════
#  方案 4: Constellation
# ══════════════════════════════════════════════════════════

def run_constellation(docx_path: Path) -> ComparisonResult:
    """使用 Constellation 全流水线解析"""
    from infrastructure.providers.docx_provider import DocxProvider
    from modules.parser.parser import CaliperParser

    CaliperParser.clear_cache()
    provider = DocxProvider()

    t0 = time.perf_counter()
    blocks = provider.extract(str(docx_path))
    parser = CaliperParser()
    tree = parser.parse(blocks)
    elapsed = time.perf_counter() - t0

    headings = []
    def walk(nodes):
        for n in nodes:
            headings.append({"title": n.title, "level": n.level})
            if n.children:
                walk(n.children)
    walk(tree.nodes)

    full_text = tree.to_markdown()
    original_chars = sum(len(b.text) for b in blocks if b.text)

    has_formula, formula_count = _detect_formulas(full_text)
    has_textbox = _detect_textbox_content(full_text, docx_path.name)

    return ComparisonResult(
        tool_name="Constellation",
        filename=docx_path.name,
        headings_found=headings,
        heading_count=len(headings),
        output_text=full_text,
        output_chars=len(full_text),
        original_chars=original_chars,
        char_ratio=len(full_text) / max(original_chars, 1),
        has_formula=has_formula,
        has_textbox=has_textbox,
        formula_count=formula_count,
        time_s=elapsed,
    )


# ══════════════════════════════════════════════════════════
#  评估
# ══════════════════════════════════════════════════════════

def evaluate_headings(result: ComparisonResult, gt_path: Path) -> dict:
    if not gt_path.exists():
        return {"f1": None, "precision": None, "recall": None,
                "hierarchy_acc": None, "tp": 0, "fp": 0, "fn": 0}

    with open(gt_path, encoding="utf-8") as f:
        gt_data = json.load(f)

    gt_headings = gt_data.get("headings", [])

    if not gt_headings:
        fp = result.heading_count
        return {
            "f1": 1.0 if fp == 0 else 0.0,
            "precision": 1.0 if fp == 0 else 0.0,
            "recall": 1.0,
            "hierarchy_acc": 1.0 if fp == 0 else 0.0,
            "tp": 0, "fp": fp, "fn": 0,
        }

    gt_titles = [h["title"][:15].lower().strip() for h in gt_headings]
    pred_titles = [h["title"][:15].lower().strip() for h in result.headings_found]

    tp = 0
    matched_gt = set()
    level_correct = 0

    for pi, pt in enumerate(pred_titles):
        for gi, gt in enumerate(gt_titles):
            if gi in matched_gt:
                continue
            if pt == gt or pt in gt or gt in pt:
                tp += 1
                matched_gt.add(gi)
                if result.headings_found[pi]["level"] == gt_headings[gi]["level"]:
                    level_correct += 1
                break

    fp = len(pred_titles) - tp
    fn = len(gt_titles) - tp
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    hierarchy_acc = level_correct / max(tp, 1)

    return {"f1": f1, "precision": precision, "recall": recall,
            "hierarchy_acc": hierarchy_acc, "tp": tp, "fp": fp, "fn": fn}


# ══════════════════════════════════════════════════════════
#  报告
# ══════════════════════════════════════════════════════════

def _fmt(val, fmt=".4f"):
    if val is None: return "—"
    return f"{val:{fmt}}"

def generate_report(all_results, all_metrics, tools, filenames):
    lines = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    lines.append("# 🔬 Constellation 横向对比实验报告")
    lines.append("")
    lines.append(f"> **生成时间**: {now}")
    lines.append(f"> **数据集**: IBM Docling 官方测试套件 (MIT License)")
    lines.append(f"> **对比方案**: {' / '.join(tools)}")
    lines.append("")

    # ── 1. F1 对比 ────────────────────────────────────
    lines.append("---")
    lines.append("## 1. 标题识别 F1 对比")
    lines.append("")
    h = "| 文件名 |"
    s = "|:-------|"
    for t in tools:
        h += f" {t} |"
        s += " ---:|"
    lines.append(h)
    lines.append(s)

    for i, fname in enumerate(filenames):
        row = f"| `{fname}` |"
        for t in tools:
            f1 = all_metrics[t][i].get('f1')
            row += f" {_fmt(f1)} |"
        lines.append(row)

    lines.append("")
    lines.append("**平均 F1:**")
    for t in tools:
        vals = [m['f1'] for m in all_metrics[t] if m['f1'] is not None]
        avg = sum(vals) / len(vals) if vals else 0
        lines.append(f"- {t}: **{avg:.4f}**")
    lines.append("")

    # ── 2. 层级准确率 ────────────────────────────────
    lines.append("---")
    lines.append("## 2. 层级准确率对比")
    lines.append("")
    h = "| 文件名 |"
    s = "|:-------|"
    for t in tools:
        h += f" {t} |"
        s += " ---:|"
    lines.append(h)
    lines.append(s)

    for i, fname in enumerate(filenames):
        row = f"| `{fname}` |"
        for t in tools:
            ha = all_metrics[t][i].get('hierarchy_acc')
            row += f" {_fmt(ha)} |"
        lines.append(row)
    lines.append("")

    # ── 3. 特殊能力 ──────────────────────────────────
    lines.append("---")
    lines.append("## 3. 特殊能力对比")
    lines.append("")
    lines.append("| 能力 |" + "".join(f" {t} |" for t in tools))
    lines.append("|:-----|" + " :---:|" * len(tools))

    # 公式
    row = "| 数学公式 (equations.docx) |"
    for t in tools:
        eq = [r for r in all_results[t] if r.filename == 'equations.docx']
        row += " ✅ |" if eq and eq[0].has_formula else " ❌ |"
    lines.append(row)

    # 文本框
    row = "| 浮动文本框 (textbox.docx) |"
    for t in tools:
        tb = [r for r in all_results[t] if r.filename == 'textbox.docx']
        row += " ✅ |" if tb and (tb[0].has_textbox or tb[0].output_chars > 500) else " ❌ |"
    lines.append(row)

    # LLM
    row = "| 需要 LLM |"
    for t in tools:
        row += " ✅ |" if "Constellation" in t else " ❌ |"
    lines.append(row)
    lines.append("")

    # ── 4. 输出字符 & 速度 ───────────────────────────
    lines.append("---")
    lines.append("## 4. 输出字符数与处理速度")
    lines.append("")
    h = "| 文件名 |"
    s = "|:-------|"
    for t in tools:
        h += f" {t} 字符 | {t} 耗时 |"
        s += " ---:| ---:|"
    lines.append(h)
    lines.append(s)

    for i, fname in enumerate(filenames):
        row = f"| `{fname}` |"
        for t in tools:
            r = all_results[t][i]
            row += f" {r.output_chars:,} | {r.time_s:.3f}s |"
        lines.append(row)
    lines.append("")

    # ── 5. 总结表 ────────────────────────────────────
    lines.append("---")
    lines.append("## 5. 综合评分")
    lines.append("")
    h = "| 指标 |"
    s = "|:-----|"
    for t in tools:
        h += f" {t} |"
        s += " ---:|"
    lines.append(h)
    lines.append(s)

    # 平均 F1
    row = "| 平均 F1 |"
    for t in tools:
        vals = [m['f1'] for m in all_metrics[t] if m['f1'] is not None]
        avg = sum(vals)/len(vals) if vals else 0
        row += f" **{avg:.4f}** |"
    lines.append(row)

    # 平均层级准确率
    row = "| 平均层级准确率 |"
    for t in tools:
        vals = [m['hierarchy_acc'] for m in all_metrics[t] if m['hierarchy_acc'] is not None]
        avg = sum(vals)/len(vals) if vals else 0
        row += f" **{avg:.4f}** |"
    lines.append(row)

    # 总耗时
    row = "| 总耗时 |"
    for t in tools:
        total = sum(r.time_s for r in all_results[t])
        row += f" {total:.2f}s |"
    lines.append(row)

    lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════

def main():
    import logging
    logging.basicConfig(level=logging.WARNING)

    docx_files = sorted(_BENCHMARK_DIR.glob("*.docx"))
    if not docx_files:
        print("❌ 未找到 DOCX 文件")
        return

    print(f"🔬 横向对比: {len(docx_files)} 个文件")
    print()

    runners = [
        ("IBM Docling", run_docling),
        ("MarkItDown", run_markitdown),
        ("Mammoth", run_mammoth),
        ("Constellation", run_constellation),
    ]

    tools = [name for name, _ in runners]
    all_results = {name: [] for name in tools}
    all_metrics = {name: [] for name in tools}

    # 基线字符数
    from infrastructure.providers.docx_provider import DocxProvider
    provider = DocxProvider()
    baseline = {}
    for p in docx_files:
        blocks = provider.extract(str(p))
        baseline[p.name] = sum(len(b.text) for b in blocks if b.text)

    for tool_name, runner in runners:
        print(f"{'─'*50}")
        print(f"🔧 {tool_name}")
        for docx_path in docx_files:
            print(f"   {docx_path.name} ...", end=" ", flush=True)
            try:
                r = runner(docx_path)
                r.original_chars = baseline.get(docx_path.name, 0)
                r.char_ratio = r.output_chars / max(r.original_chars, 1)

                gt = _GT_DIR / (docx_path.stem + ".json")
                m = evaluate_headings(r, gt)

                all_results[tool_name].append(r)
                all_metrics[tool_name].append(m)

                f1s = f"F1={m['f1']:.2f}" if m['f1'] is not None else "—"
                print(f"✓ ({r.heading_count}h, {f1s}, {r.time_s:.3f}s)")

            except Exception as e:
                print(f"✗ {e}")
                empty = ComparisonResult(
                    tool_name=tool_name, filename=docx_path.name,
                    headings_found=[], heading_count=0,
                    output_text="", output_chars=0,
                    original_chars=baseline.get(docx_path.name, 0),
                    char_ratio=0, has_formula=False, has_textbox=False,
                    formula_count=0, time_s=0,
                )
                all_results[tool_name].append(empty)
                all_metrics[tool_name].append(
                    {"f1": 0, "precision": 0, "recall": 0,
                     "hierarchy_acc": 0, "tp": 0, "fp": 0, "fn": 0})

    filenames = [p.name for p in docx_files]
    report = generate_report(all_results, all_metrics, tools, filenames)

    with open(_OUTPUT, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n{'═'*50}")
    print(f"📊 报告: {_OUTPUT}")
    print()
    print(report)


if __name__ == "__main__":
    main()
