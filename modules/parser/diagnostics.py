# -*- coding: utf-8 -*-
"""方案 12.2：给指标面板的「未测量」维度补**生产者**。

面板（``evaluation/metrics_panel.py``）此前有一半维度输出 ``{"status":"未测量"}``。那是
**正确行为**——缺数据填 0 会被误读成「测到了且为零」——但缺的是生产者：没有任何代码
把这些事实算出来写进实验产物。本模块就是那些生产者。

## 三条纪律

1. **只报算得出来的**。每个函数要么给数字，要么给 ``None`` / 不给这个键，让面板继续标
   未测量。绝不为了让面板好看而估一个数（峰值 RSS 在 Windows 上取不到就是取不到）。
2. **算的是可数事实，不是印象**。「恰好一次」是逐 atom 数归属次数，不是「覆盖率看起来
   够高」；表格用 E04 已有的 ``coverage_is_exact``，不另造一套判定。
3. **纯函数，不碰管线**。输入是 ``blocks`` / ``tree`` / provider 的跳过记录，输出是 dict。
   这样它既能被评测脚本调用，也能被单测用手搭的输入钉住，而不需要真实模型。

## 归属为什么按「直接归属」算

``DocumentNode`` 的区间是**嵌套**的：父节点的 ``[start, end]`` 天然覆盖所有子节点。所以
「一个块被几个节点覆盖」永远 ≥ 树深，拿它当「恰好一次」的判据只会永远失败。真正要查的
不变量是**直接归属**：覆盖它、且它的任何子节点都不覆盖它的那个节点，必须恰好一个。
兄弟区间重叠时会出现两个直接归属者——那才是真正的重复，也是这条断言要抓的东西。
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "document_diagnostics",
    "extraction_facts",
    "closure_facts",
    "table_facts",
    "delivery_facts",
    "normalization_facts",
    "language_facts",
]

#: 规范化痕迹的键 → 面板上的中文名。这些键由 provider 在做变换时写进 atom 的
#: ``provenance``；这里只统计出现次数，不改写它们。
_NORMALIZATION_KEYS = {
    "join_normalization": "断词与拼接归一",
    "canonical_text": "空白归一",
    "offset_alignment": "偏移对齐降级",
    "source_role": "来源角色标注",
    "soft_hyphen": "连字归一",
}

#: Markdown 里的资源引用（图片）。用来数「被引用但缺失」。
_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")


def _is_cjk(char: str) -> bool:
    return "CJK" in unicodedata.name(char, "")


# ── 提取层 ────────────────────────────────────────────────────────────────


def extraction_facts(
    blocks: Sequence[Any], *, skips: Iterable[Any] = ()
) -> dict[str, Any]:
    """提取层：未识别元素数、乱码/扫描页、阅读顺序不确定边数。

    数据源是 provider **已经产出**的信号，本函数只做聚合：
    ``metadata['source'] == 'unknown_element'``（DOCX 的受控占位）、
    跳过记录里的 ``pdf_page_needs_ocr``（E03 的页判定）、
    ``metadata['reading_order_uncertain']``（PDF 阅读顺序环）。

    **GT 侧顺序边仍然缺位**：阅读顺序准确率要拿预测边与 GT 边比，而 GT 数据集没有标
    顺序。所以这里只报预测侧的不确定边数，准确率留给面板标未测量——报一个没有对照的
    「准确率」是编数字。
    """
    skip_codes: list[str] = [getattr(skip, "code", "") for skip in skips]
    unknown_blocks = sum(
        1 for block in blocks
        if (getattr(block, "metadata", None) or {}).get("source") == "unknown_element"
    )
    uncertain = sum(
        1 for block in blocks
        if (getattr(block, "metadata", None) or {}).get("reading_order_uncertain")
    )
    pages = {
        (getattr(block, "metadata", None) or {}).get("page")
        for block in blocks
    }
    pages.discard(None)
    ocr_pages = sum(1 for code in skip_codes if code == "pdf_page_needs_ocr")

    facts: dict[str, Any] = {
        "unrecognized_element_count": unknown_blocks + skip_codes.count("docx_unknown_element"),
        "reading_order_uncertain_blocks": uncertain,
        # 顺序边总数 = 相邻块对数。给出分母，读者才知道「3 条不确定」是多还是少。
        "reading_order_edges": max(len(blocks) - 1, 0),
        "ocr_pages": ocr_pages,
    }
    if pages:
        facts["page_count"] = len(pages)
        facts["garbled_page_ratio"] = ocr_pages / len(pages)
    return facts


# ── 闭合层 ────────────────────────────────────────────────────────────────


def _node_spans(nodes: Sequence[Any]) -> list[tuple[int, int, Any]]:
    out: list[tuple[int, int, Any]] = []

    def walk(items: Sequence[Any]) -> None:
        for node in items:
            out.append((int(node.start_block_id), int(node.end_block_id), node))
            walk(getattr(node, "children", ()) or ())

    walk(nodes)
    return out


def _direct_owners(spans: Sequence[tuple[int, int, Any]], block_id: int) -> list[Any]:
    """覆盖 ``block_id`` 且其子节点都不覆盖它的节点——见模块说明的「直接归属」。"""
    owners: list[Any] = []
    for start, end, node in spans:
        if not (start <= block_id <= end):
            continue
        children = getattr(node, "children", ()) or ()
        if any(
            int(child.start_block_id) <= block_id <= int(child.end_block_id)
            for child in children
        ):
            continue
        owners.append(node)
    return owners


def closure_facts(blocks: Sequence[Any], tree: Any) -> dict[str, Any]:
    """闭合层：每个来源 atom 的归属计数 + 未归属清单（「恰好一次」校验）。

    atom 是 provider 记在 ``metadata['atoms']`` 里的最小可审计源片段。没有 atom 的块
    （表格 / 图片 / 纯合成块）按块本身记一个单位——否则「无 atom 的来源」会静默不进账，
    账本就漏了它该管的那部分。
    """
    spans = _node_spans(getattr(tree, "nodes", ()) or ())
    preamble_end = -1
    if getattr(tree, "preamble_content", None) and spans:
        preamble_end = min(start for start, _end, _node in spans) - 1

    duplicated: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []
    total_units = 0

    for block in blocks:
        block_id = getattr(block, "id", None)
        if block_id is None:
            continue
        metadata = getattr(block, "metadata", None) or {}
        atoms = metadata.get("atoms") or []
        units = [str(atom.get("atom_id", f"block:{block_id}")) for atom in atoms] or [
            f"block:{block_id}"
        ]
        total_units += len(units)
        if block_id <= preamble_end:
            continue  # 前言区间由 preamble_content 承载，算一次归属
        owners = _direct_owners(spans, block_id)
        if len(owners) == 0:
            unattributed.extend(
                {"unit": unit, "block_id": block_id} for unit in units
            )
        elif len(owners) > 1:
            duplicated.extend(
                {"unit": unit, "block_id": block_id, "owners": len(owners)}
                for unit in units
            )

    return {
        "source_units_total": total_units,
        "duplicated_units": len(duplicated),
        "unattributed_units": len(unattributed),
        # 清单截断到前 20 条：账本是给人看的，全量清单该进产物文件而不是指标面板。
        "duplicated_sample": duplicated[:20],
        "unattributed_sample": unattributed[:20],
        "exactly_once": not duplicated and not unattributed,
    }


# ── 表格层 ────────────────────────────────────────────────────────────────


def table_facts(blocks: Sequence[Any]) -> dict[str, Any]:
    """表格层：cell 归属账本 + 未归属 cell 清单。

    判定直接用 E04 的 :meth:`TableModel.coverage_is_exact`——不另造一套。它抓的正是
    「渲染结果照样有文字、覆盖其实错了」这种缺陷（合并起始格被丢掉时它会红）。
    """
    from infrastructure.models.table_model import TableModel

    tables = 0
    with_model = 0
    exact = 0
    missing: list[dict[str, Any]] = []
    overlapping = 0

    for block in blocks:
        if getattr(block, "type", None) != "table":
            continue
        tables += 1
        payload = (getattr(block, "table_data", None) or {}).get("model")
        if not isinstance(payload, dict):
            continue
        with_model += 1
        model = TableModel.model_validate(payload)
        if model.coverage_is_exact():
            exact += 1
            continue
        seen: set[tuple[int, int]] = set()
        duplicated = False
        for cell in model.cells:
            for position in cell.covers():
                if position in seen:
                    duplicated = True
                seen.add(position)
        if duplicated:
            overlapping += 1
        expected = {
            (row, col) for row in range(model.n_rows) for col in range(model.n_cols)
        }
        for position in sorted(expected - seen)[:20]:
            missing.append({
                "block_id": getattr(block, "id", None),
                "row": position[0],
                "col": position[1],
            })

    facts: dict[str, Any] = {
        "table_count": tables,
        "tables_with_model": with_model,
        "tables_coverage_exact": exact,
        "tables_with_overlapping_cells": overlapping,
        "unattributed_cells_sample": missing[:20],
        "unattributed_cells": len(missing),
    }
    if with_model:
        facts["coverage_exact_ratio"] = exact / with_model
    return facts


# ── 交付层 ────────────────────────────────────────────────────────────────


def delivery_facts(
    tree: Any,
    blocks: Sequence[Any],
    *,
    known_asset_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """交付层：各视图输出字节数、各视图声明范围齐全性、被引用但缺失的资源数。

    ``known_asset_ids=None`` 表示本次没有挂 AssetStore（资源按引用外置未启用），此时
    「缺失资源数」不可判——如实不给这个键，而不是报 0。
    """
    full = tree.to_markdown()
    sections = tree.to_markdown_sections()
    facts: dict[str, Any] = {
        "view_bytes": {
            "full_markdown": len(full.encode("utf-8")),
            "sections_markdown": sum(
                len((item.get("content") or "").encode("utf-8")) for item in sections
            ),
            "json": len(tree.to_json().encode("utf-8"))
            if hasattr(tree, "to_json") else None,
        },
    }

    # 范围齐全性：树声明的块区间并集是否覆盖全部块 id。
    declared: set[int] = set()
    for start, end, _node in _node_spans(getattr(tree, "nodes", ()) or ()):
        declared.update(range(start, end + 1))
    spans = _node_spans(getattr(tree, "nodes", ()) or ())
    if getattr(tree, "preamble_content", None) and spans:
        declared.update(range(0, min(start for start, _e, _n in spans)))
    all_ids = {
        getattr(block, "id") for block in blocks if getattr(block, "id", None) is not None
    }
    facts["range_declaration"] = {
        "declared_blocks": len(declared & all_ids),
        "total_blocks": len(all_ids),
        "complete": bool(all_ids) and all_ids <= declared,
        "missing_block_ids": sorted(all_ids - declared)[:20],
    }

    referenced = [
        target.strip() for target in _IMAGE_REF_RE.findall(full)
    ]
    facts["referenced_resources"] = len(referenced)
    empty_refs = sum(1 for target in referenced if not target)
    if known_asset_ids is not None:
        missing = sum(
            1 for target in referenced
            if target.startswith("asset://")
            and target[len("asset://"):] not in known_asset_ids
        )
        facts["missing_resources"] = missing + empty_refs
    elif empty_refs:
        # 空目标的引用无需 store 就能判定为缺失。
        facts["missing_resources"] = empty_refs
    return facts


# ── 规范化层 ──────────────────────────────────────────────────────────────


def normalization_facts(blocks: Sequence[Any]) -> dict[str, Any]:
    """规范化层：断词/空白/连字/角色标注等变换的出现次数。

    方案 12.1 要「规范化可追溯」。provider 已经把每次变换写进 atom 的 ``provenance``，
    这里只把它数出来——这样「我们对源文字做了哪些变换」不再只存在于单个 atom 里。
    """
    counts = {label: 0 for label in _NORMALIZATION_KEYS.values()}
    atoms_seen = 0
    for block in blocks:
        for atom in (getattr(block, "metadata", None) or {}).get("atoms", []) or []:
            atoms_seen += 1
            provenance = atom.get("provenance") or {}
            for key, label in _NORMALIZATION_KEYS.items():
                if key in provenance:
                    counts[label] += 1
    return {"atoms_seen": atoms_seen, "transformations": counts}


# ── 语言标签 ──────────────────────────────────────────────────────────────


def language_facts(blocks: Sequence[Any]) -> dict[str, Any]:
    """按 CJK 字符占比给一个**派生**语言标签（供标题分桶）。

    方案要 GT 携带 ``language`` / ``domain`` 标签。语言可以从正文推出来，标签因此标
    ``derived``——它不是人工标注，读者要知道这一点。``domain``（文档域）没有任何可推
    依据，不在这里编。
    """
    cjk = 0
    letters = 0
    for block in blocks:
        for char in (getattr(block, "text", "") or ""):
            if char.isspace():
                continue
            if _is_cjk(char):
                cjk += 1
                letters += 1
            elif char.isalpha():
                letters += 1
    if not letters:
        return {"language": None, "language_source": "derived", "cjk_ratio": None}
    ratio = cjk / letters
    label = "zh" if ratio >= 0.6 else "en" if ratio <= 0.1 else "mixed"
    return {"language": label, "language_source": "derived", "cjk_ratio": ratio}


# ── 汇总 ──────────────────────────────────────────────────────────────────


def document_diagnostics(
    blocks: Sequence[Any],
    tree: Any,
    *,
    skips: Iterable[Any] = (),
    known_asset_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """一篇文档的全部可测量事实，供实验产物逐文档落盘、面板读取。

    键名与面板消费侧一一对应；面板对**缺键**继续标未测量，所以本函数不必为算不出来的
    维度造占位。
    """
    return {
        "extraction": extraction_facts(blocks, skips=skips),
        "closure": closure_facts(blocks, tree),
        "tables": table_facts(blocks),
        "delivery": delivery_facts(tree, blocks, known_asset_ids=known_asset_ids),
        "normalization": normalization_facts(blocks),
        **language_facts(blocks),
    }


def diagnostics_json(payload: dict[str, Any]) -> str:
    """稳定序列化（供落盘 / 比对）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
