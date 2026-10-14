# -*- coding: utf-8 -*-
"""A01 验收度量：结构质量 — 调用数 — Token — 耗时，四条一起看。

方案 A01 的验收原文有两句约束，本脚本逐条遵守：

1. 「分别画出结构质量—调用数—Token—耗时曲线」——所以四个量各自成列，不合成单一「性价比」。
2. 「不能简单设一个 ``p > 0.9`` 就宣称可靠。标题存在概率、级别分差和最终树结构不确定性
   分开衡量」——所以报告里这三个不确定度分三列。

还有一条更重要的纪律（来自本仓的既往教训）：**没有真实模型语料就不宣称质量提升**。
本脚本用的是 **oracle 桩路由器**——它按 GT 回答区域内的标题，模拟「模型在该区域答对」。
因此这里的结构质量列只能读作「若模型在被选中的区域答对，结构质量能达到多少」，
**不是**真实模型的表现。真正能当结论的是调用数与 Token 两列：它们与模型质量无关，
只由选区策略决定。

三种模式对照：

- ``fast``：零模型（现有 ``parse_fast``）。调用数恒为 0，是「不调模型能做到多少」的下界。
- ``selective``：A01。只对不确定区域各发一次请求。
- ``full``：现有 ``parse``。整篇骨架进模型。

用法：``python -m evaluation.selective_review_curve``
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation.metrics import HeadingGT, HeadingPred, compute_section_f1
from evaluation.typography_robustness import load_corpus
from modules.parser.parser import CaliperParser
from modules.parser.schemas import ChapterNode, LLMRouterOutput
from modules.parser.titles import normalize_title

OUT_MD = os.path.join("experiment_results", "selective_review_curve.md")
OUT_JSON = os.path.join("experiment_results", "selective_review_curve_raw.json")

#: 骨架里的块标记是行首 ``[<id>]``（见 SkeletonCompressor 的输出）。
_BLOCK_MARKER = re.compile(r"^\[(\d+)\]", re.MULTILINE)


class _OracleStubRouter:
    """按 GT 回答「这段骨架里有哪些标题」的桩路由器。

    它读骨架里出现的块 id，回那些 id 上的 GT 标题。这是**oracle**，不是模型——
    用它是为了让「调用数 / Token」这两列与模型质量解耦：换任何模型都不改变选区策略
    发出多少请求、每次请求多长。结构质量列因此只能读作上界。
    """

    def __init__(self, gt: list[HeadingGT], *, flat: bool = False) -> None:
        self._by_id = {heading.block_id: heading for heading in gt}
        self._flat = flat
        self.calls = 0
        self.prompt_chars = 0

    def __call__(self, chunk: str, *_args: Any, **_kwargs: Any) -> LLMRouterOutput:
        self.calls += 1
        self.prompt_chars += len(chunk)
        ids = {int(match) for match in _BLOCK_MARKER.findall(chunk)}
        chapters = [
            ChapterNode(
                block_id=heading.block_id,
                title=heading.title,
                snippet=heading.title[:40],
                level=1 if self._flat else heading.level,
                confidence=0.95,
            )
            for block_id, heading in sorted(self._by_id.items())
            if block_id in ids
        ]
        return LLMRouterOutput(doc_title="", doc_authors="", chapters=chapters)


def _install(parser: CaliperParser, stub: _OracleStubRouter) -> None:
    parser.router.route = stub
    parser.router.route_chunk = lambda chunk, *a, **k: stub(chunk)


def _predictions(tree) -> list[HeadingPred]:
    out: list[HeadingPred] = []

    def walk(nodes) -> None:
        for node in nodes:
            out.append(HeadingPred(
                block_id=node.start_block_id,
                title=normalize_title(node.title),
                level=node.level,
            ))
            walk(node.children)

    walk(tree.nodes)
    return out


def _score(tree, gt: list[HeadingGT]) -> dict[str, Any]:
    normalized = [HeadingGT(h.block_id, normalize_title(h.title), h.level) for h in gt]
    result = compute_section_f1(normalized, _predictions(tree))
    return {
        "f1": round(result.f1, 4),
        "precision": round(result.precision, 4),
        "recall": round(result.recall, 4),
        "level_accuracy": round(result.hierarchy_accuracy, 4),
    }


def measure_document(entry: dict, *, flat: bool = False) -> list[dict[str, Any]]:
    blocks = entry["blocks"]
    gt = entry["gt"]
    rows: list[dict[str, Any]] = []

    # fast：零模型下界。
    CaliperParser.clear_cache()
    parser = CaliperParser()
    stub = _OracleStubRouter(gt, flat=flat)
    _install(parser, stub)
    started = time.perf_counter()
    fast_tree = parser.parse_fast(list(blocks))
    rows.append({
        "stem": entry["stem"], "variant": entry["variant"], "mode": "fast",
        "model_calls": stub.calls, "prompt_chars": stub.prompt_chars,
        "elapsed": round(time.perf_counter() - started, 4),
        **_score(fast_tree, gt),
        "uncertain_sites": None, "total_sites": None, "tree_uncertainty": None,
        "regions_planned": None, "regions_deferred": None,
    })

    # selective：A01。
    CaliperParser.clear_cache()
    parser = CaliperParser()
    stub = _OracleStubRouter(gt, flat=flat)
    _install(parser, stub)
    started = time.perf_counter()
    selective_tree, report = parser.parse_selective_with_report(list(blocks))
    rows.append({
        "stem": entry["stem"], "variant": entry["variant"], "mode": "selective",
        "model_calls": stub.calls, "prompt_chars": stub.prompt_chars,
        "elapsed": round(time.perf_counter() - started, 4),
        **_score(selective_tree, gt),
        "uncertain_sites": report.uncertain_sites,
        "total_sites": report.total_sites,
        "tree_uncertainty": round(report.tree_uncertainty, 4),
        "regions_planned": report.regions_planned,
        "regions_deferred": report.regions_deferred,
        "signals": report.signals,
    })

    # full：整篇骨架进模型。
    CaliperParser.clear_cache()
    parser = CaliperParser()
    stub = _OracleStubRouter(gt, flat=flat)
    _install(parser, stub)
    started = time.perf_counter()
    full_tree = parser.parse(list(blocks))
    rows.append({
        "stem": entry["stem"], "variant": entry["variant"], "mode": "full",
        "model_calls": stub.calls, "prompt_chars": stub.prompt_chars,
        "elapsed": round(time.perf_counter() - started, 4),
        **_score(full_tree, gt),
        "uncertain_sites": None, "total_sites": None, "tree_uncertainty": None,
        "regions_planned": None, "regions_deferred": None,
    })
    return rows


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for mode in ("fast", "selective", "full"):
        group = [row for row in rows if row["mode"] == mode]
        if not group:
            continue
        out[mode] = {
            "docs": len(group),
            "model_calls_sum": sum(row["model_calls"] for row in group),
            "prompt_chars_sum": sum(row["prompt_chars"] for row in group),
            "elapsed_sum": round(sum(row["elapsed"] for row in group), 3),
            "f1_mean": round(sum(row["f1"] for row in group) / len(group), 4),
            "level_accuracy_mean": round(
                sum(row["level_accuracy"] for row in group) / len(group), 4,
            ),
        }
    return out


def run() -> tuple[str, str]:
    corpus = load_corpus()
    rows: list[dict[str, Any]] = []
    for entry in corpus:
        print(f"[curve] {entry['stem']} ...", flush=True)
        rows.extend(measure_document(entry))
    summary = _aggregate(rows)

    lines = [
        "# A01 选择性复审：结构质量 — 调用数 — Token — 耗时",
        "",
        "> **口径声明**：路由器是按 GT 作答的 **oracle 桩**，不是真实模型。因此"
        "「结构质量」两列只能读作**上界**（若模型在被选中的区域答对能到多少）；"
        "真正可当结论的是**调用数**与**prompt 字符**两列——它们与模型质量无关，"
        "只由选区策略决定。没有真实模型语料就不宣称质量提升。",
        "",
        f"- 语料：`tests/data/mis_styled` 共 {len(corpus)} 篇（排版扰动集）",
        "- Token 用 prompt 字符数代替（1 token ≈ 2 个中文字符，估算口径与 evaluation 侧一致）",
        "",
        "## 三模式汇总",
        "",
        "| 模式 | 文档数 | 模型调用数 | prompt 字符 | 耗时(s) | F1(上界) | 级别准确率(上界) |",
        "|:-----|------:|---------:|----------:|-------:|--------:|---------------:|",
    ]
    for mode, values in summary.items():
        lines.append(
            f"| {mode} | {values['docs']} | {values['model_calls_sum']} | "
            f"{values['prompt_chars_sum']} | {values['elapsed_sum']} | "
            f"{values['f1_mean']:.4f} | {values['level_accuracy_mean']:.4f} |"
        )

    lines += [
        "",
        "## 三个不确定度分开报（selective 模式）",
        "",
        "标题存在概率、级别分差、树结构不确定性是三件不同的事。这里报后两者的文档级汇总；"
        "逐站点的存在概率与级别分差在 raw JSON 的 `signals` 里按信号计数。",
        "",
        "| 文档 | 扰动 | 站点数 | 不确定站点 | 复审区域 | 延后区域 | 树结构不确定度 |",
        "|:-----|:-----|------:|---------:|--------:|--------:|-------------:|",
    ]
    for row in rows:
        if row["mode"] != "selective":
            continue
        lines.append(
            f"| {row['stem']} | {row['variant']} | {row['total_sites']} | "
            f"{row['uncertain_sites']} | {row['regions_planned']} | "
            f"{row['regions_deferred']} | {row['tree_uncertainty']} |"
        )
    lines.append("")

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    with open(OUT_JSON, "w", encoding="utf-8") as handle:
        json.dump({"rows": rows, "summary": summary}, handle, ensure_ascii=False, indent=2)
    return OUT_MD, OUT_JSON


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    md, raw = run()
    print(f"报告: {md}")
    print(f"原始数据: {raw}")
