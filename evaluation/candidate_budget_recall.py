# -*- coding: utf-8 -*-
"""A05 验收：候选原始 Recall / 路由视图 Recall / 最终标题 Recall 三条曲线。

方案 A05 的验收原文：「报告候选原始 Recall、路由视图 Recall、最终标题 Recall 三条曲线，
并按文档前中后三段统计；预算翻倍时收益应可量化，**不能用固定候选比例掩盖某类文档漏检**。」

三条曲线必须分开看，因为漏检发生在哪一层，修法完全不同：

- **候选原始 Recall**：GT 标题有没有成为候选。掉在这里 = 证据规则没覆盖到，路由预算再大
  也救不回来（漏的那个块从未进入候选表）。
- **路由视图 Recall**：候选里有多少被送给模型看。掉在这里 = **预算分配**问题，正是 A05 要修的。
- **最终标题 Recall**：解码后有多少 GT 标题真的出现在树里。掉在这里 = 解码/层级判定问题。

**按前中后三段统计**是这条验收的要点：一个全局 85% 的路由召回，可能是「前段 100%、后段
55%」——固定候选比例 + 同分偏向早出现的 block，正是这样把标题密集的后半篇饿死的。
只看全局平均会把它整片掩盖掉。

最终召回用**确定性解码**（无模型投票），因此这份报告完全不依赖模型，可随时复跑。

用法：``python -m evaluation.candidate_budget_recall``
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from evaluation.typography_robustness import load_corpus
from modules.parser.global_inference import GlobalHeadingInference
from modules.parser.heading_candidates import (
    _MAX_CANDIDATE_RATIO,
    generate_heading_candidate_set,
    select_route_candidates,
)

OUT_MD = os.path.join("experiment_results", "candidate_budget_recall.md")
OUT_JSON = os.path.join("experiment_results", "candidate_budget_recall_raw.json")

#: 预算档：默认比例与它的两倍（「预算翻倍时收益应可量化」）。
BUDGET_RATIOS = (_MAX_CANDIDATE_RATIO, _MAX_CANDIDATE_RATIO * 2)


def _thirds(block_ids: list[int], total_blocks: int) -> dict[str, list[int]]:
    """按块位置把 GT 标题切成前 / 中 / 后三段。"""
    if total_blocks <= 0:
        return {"front": [], "middle": [], "back": []}
    buckets: dict[str, list[int]] = {"front": [], "middle": [], "back": []}
    for block_id in block_ids:
        position = block_id / max(total_blocks - 1, 1)
        if position < 1 / 3:
            buckets["front"].append(block_id)
        elif position < 2 / 3:
            buckets["middle"].append(block_id)
        else:
            buckets["back"].append(block_id)
    return buckets


def _recall(gt_ids: list[int], present: set[int]) -> float | None:
    if not gt_ids:
        return None
    return round(sum(1 for block_id in gt_ids if block_id in present) / len(gt_ids), 4)


def measure_document(entry: dict, ratio: float) -> dict[str, Any]:
    blocks = entry["blocks"]
    gt_ids = [heading.block_id for heading in entry["gt"]]
    candidate_set = generate_heading_candidate_set(blocks)
    raw_ids = {candidate.block_id for candidate in candidate_set.candidates}
    routed = select_route_candidates(candidate_set, blocks, max_ratio=ratio)
    routed_ids = {candidate.block_id for candidate in routed}
    decoded = GlobalHeadingInference(
        blocks, candidate_set, routed_block_ids=routed_ids,
    ).decode([])
    final_ids = {chapter.start_block_id for chapter in decoded}

    buckets = _thirds(gt_ids, len(blocks))
    row: dict[str, Any] = {
        "stem": entry["stem"],
        "variant": entry["variant"],
        "ratio": ratio,
        "blocks": len(blocks),
        "gt_headings": len(gt_ids),
        "candidates_raw": len(raw_ids),
        "candidates_routed": len(routed_ids),
        "recall_candidate_raw": _recall(gt_ids, raw_ids),
        "recall_route_view": _recall(gt_ids, routed_ids),
        "recall_final": _recall(gt_ids, final_ids),
    }
    for name, ids in buckets.items():
        row[f"n_{name}"] = len(ids)
        row[f"recall_candidate_raw_{name}"] = _recall(ids, raw_ids)
        row[f"recall_route_view_{name}"] = _recall(ids, routed_ids)
        row[f"recall_final_{name}"] = _recall(ids, final_ids)
    return row


def synthetic_stress_corpus() -> list[dict]:
    """合成压力语料：候选数远超预算，且**真标题集中在后半篇**。

    为什么必须有这一组：排版扰动集只有几十到上百块，候选比例上限（0.15）根本咬不住——
    两档预算下路由候选数都是 272，完全一样。也就是说那份语料**测不到** A05 要修的东西。
    A05 描述的失败模式是「同分排序偏向早出现的 block，标题密集的后半篇缺少模型观察」，
    所以夹具必须同时满足两条：候选数远超预算，且真标题在后半篇。

    构造：前半篇塞大量「加粗短句」型干扰候选（有候选资格、不是标题），后半篇放带编号的
    真标题。这样若预算按位置/分数平均发，后半篇的真标题就会被前半篇的干扰候选挤掉。
    """
    from evaluation.metrics import HeadingGT
    from infrastructure.models import Block

    corpus: list[dict] = []
    for label, distractors, headings in (
        ("stress__back_loaded", 240, 40),
        ("stress__back_loaded_dense", 400, 60),
    ):
        blocks: list[Block] = []
        gt: list[HeadingGT] = []
        # 前半篇：加粗短句干扰（会成为候选，但不是标题）。
        for index in range(distractors):
            blocks.append(Block(
                id=len(blocks), type="text", text=f"要点 {index}：这是一条加粗的强调句。",
                is_bold=True, font_size=10.5,
            ))
            blocks.append(Block(
                id=len(blocks), type="text",
                text=f"第 {index} 段正文内容，字号为正文字号。" * 4, font_size=10.5,
            ))
        # 后半篇：带编号的真标题 + 正文。
        for index in range(headings):
            chapter = index // 4 + 1
            section = index % 4 + 1
            title = f"{chapter}.{section} 后半篇小节 {index}"
            blocks.append(Block(
                id=len(blocks), type="text", text=title,
                is_bold=True, font_size=12.0,
            ))
            gt.append(HeadingGT(blocks[-1].id, title, 2))
            blocks.append(Block(
                id=len(blocks), type="text",
                text=f"后半篇第 {index} 节正文内容。" * 4, font_size=10.5,
            ))
        corpus.append({
            "stem": label, "variant": "synthetic_stress", "structure": "stress",
            "blocks": blocks, "gt": gt, "distractors": set(),
        })
    return corpus


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(sum(present) / len(present), 4) if present else None


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ratio in BUDGET_RATIOS:
        group = [row for row in rows if row["ratio"] == ratio]
        if not group:
            continue
        summary: dict[str, Any] = {
            "docs": len(group),
            "routed_sum": sum(row["candidates_routed"] for row in group),
            "raw_sum": sum(row["candidates_raw"] for row in group),
        }
        for layer in ("candidate_raw", "route_view", "final"):
            summary[f"recall_{layer}"] = _mean([row[f"recall_{layer}"] for row in group])
            for third in ("front", "middle", "back"):
                summary[f"recall_{layer}_{third}"] = _mean(
                    [row[f"recall_{layer}_{third}"] for row in group]
                )
        out[f"ratio={ratio:g}"] = summary
    return out


def run() -> tuple[str, str]:
    corpus = load_corpus()
    stress = synthetic_stress_corpus()
    rows: list[dict[str, Any]] = []
    stress_rows: list[dict[str, Any]] = []
    for entry in corpus:
        print(f"[recall] {entry['stem']} ...", flush=True)
        for ratio in BUDGET_RATIOS:
            rows.append(measure_document(entry, ratio))
    for entry in stress:
        print(f"[recall] {entry['stem']} ...", flush=True)
        for ratio in BUDGET_RATIOS:
            stress_rows.append(measure_document(entry, ratio))
    summary = _aggregate(rows)
    stress_summary = _aggregate(stress_rows)

    lines = [
        "# A05 候选预算：三条召回曲线 × 文档前中后三段",
        "",
        "> 最终召回用**确定性解码**（无模型投票），因此本报告完全不依赖模型，可随时复跑。",
        "",
        f"- 语料：`tests/data/mis_styled` 共 {len(corpus)} 篇（排版扰动集）",
        f"- 预算档：候选比例 {BUDGET_RATIOS[0]:g} 与其两倍 {BUDGET_RATIOS[1]:g}",
        "",
        "## 三层召回（全局 + 前中后三段）",
        "",
        "| 预算 | 路由候选数 | 层 | 全局 | 前段 | 中段 | 后段 |",
        "|:-----|---------:|:---|-----:|-----:|-----:|-----:|",
    ]
    labels = {
        "candidate_raw": "候选原始",
        "route_view": "路由视图",
        "final": "最终标题",
    }
    for key, values in summary.items():
        for layer, label in labels.items():
            lines.append(
                f"| {key} | {values['routed_sum']} | {label} | "
                f"{values[f'recall_{layer}']} | {values[f'recall_{layer}_front']} | "
                f"{values[f'recall_{layer}_middle']} | {values[f'recall_{layer}_back']} |"
            )

    lines += [
        "",
        "## 预算翻倍的收益",
        "",
        "「预算翻倍时收益应可量化」——两档之间路由视图召回的差值就是这个收益。"
        "若差值为 0 而路由召回已是 1.0，说明本语料在默认预算下就没有被预算饿死的漏检；"
        "这不能推广到所有文档，只能说明**这份语料**不是被预算掩盖的那一类。",
        "",
    ]
    if len(BUDGET_RATIOS) == 2:
        low = summary.get(f"ratio={BUDGET_RATIOS[0]:g}", {})
        high = summary.get(f"ratio={BUDGET_RATIOS[1]:g}", {})
        if low and high:
            for third in ("front", "middle", "back"):
                delta = (high[f"recall_route_view_{third}"] or 0) - (
                    low[f"recall_route_view_{third}"] or 0
                )
                lines.append(
                    f"- {third} 段路由召回：{low[f'recall_route_view_{third}']} → "
                    f"{high[f'recall_route_view_{third}']}（收益 {delta:+.4f}）"
                )
            lines.append(
                f"- 路由候选总数：{low['routed_sum']} → {high['routed_sum']}"
            )
    lines.append("")

    lines += [
        "## 合成压力语料（预算真正咬住、真标题集中在后半篇）",
        "",
        "排版扰动集只有几十到上百块，候选比例上限根本咬不住——上表两档的路由候选数完全"
        "相同，说明那份语料**测不到** A05 要修的东西。A05 描述的失败模式是「同分排序偏向"
        "早出现的 block，标题密集的后半篇缺少模型观察」，所以这一组夹具同时满足两条："
        "候选数远超预算，且真标题全在后半篇。",
        "",
        "| 预算 | 路由候选数 | 层 | 全局 | 前段 | 中段 | 后段 |",
        "|:-----|---------:|:---|-----:|-----:|-----:|-----:|",
    ]
    for key, values in stress_summary.items():
        for layer, label in labels.items():
            lines.append(
                f"| {key} | {values['routed_sum']} | {label} | "
                f"{values[f'recall_{layer}']} | {values[f'recall_{layer}_front']} | "
                f"{values[f'recall_{layer}_middle']} | {values[f'recall_{layer}_back']} |"
            )
    lines += [
        "",
        "**要看的是后段那一列**：它是被饿死的那一段。前段召回高而后段低，就是「固定候选比例"
        "掩盖某类文档漏检」的确切形态；区域配额（保底 + 疑难追加）就是为了让后段不掉。",
        "",
        "### 如实结论：本次两组语料都**没让预算上限咬住**",
        "",
    ]
    bound_note: list[str] = []
    for label, group in (("排版扰动集", summary), ("合成压力语料", stress_summary)):
        counts = {key: values["routed_sum"] for key, values in group.items()}
        distinct = len(set(counts.values()))
        bound_note.append(
            f"- {label}：两档预算的路由候选总数为 {counts} —— "
            + ("**相同**，说明上限没有咬住。" if distinct == 1 else "不同，上限确实咬住。")
        )
    lines += bound_note
    lines += [
        "",
        "也就是说：**这两组语料测不到 A05 要修的东西**。原因是确定性候选层的阈值先把干扰候选"
        "滤掉了，剩下的候选数还没到上限。第一组（排版扰动集）文档太短；第二组刻意造了 240~400"
        "条「加粗短句」干扰，但它们连候选门槛都没过。",
        "",
        "所以本报告的三条召回曲线度量的是**候选层与解码层**，不是预算分配。预算分配这一层的"
        "正确性改由单元用例直接钉住（见 `tests/test_candidate_budget_quota.py`）：区域保底、"
        "疑难追加、全局保留三份各自的行为，以及「把上限压到极小」时后段不被饿死。",
        "",
        "要在真实数据上量到预算饿死，需要一份候选数确实超过 `0.15 × 正文块数` 的长文档"
        "（Round4 记录里 `gpt3.pdf` 是 5151 块 / 2254 候选，属于这一类），这些样本不在仓库里。"
        "缺样本就如实说缺，不把「没测到」写成「没问题」。",
        "",
    ]

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    with open(OUT_JSON, "w", encoding="utf-8") as handle:
        json.dump({
            "rows": rows, "summary": summary,
            "stress_rows": stress_rows, "stress_summary": stress_summary,
        }, handle, ensure_ascii=False, indent=2)
    return OUT_MD, OUT_JSON


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    md, raw = run()
    print(f"报告: {md}")
    print(f"原始数据: {raw}")
