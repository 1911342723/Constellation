"""方案第 12 节的最小指标面板统一入口。

本模块**只做离线聚合**：吃现有实验产物（``evaluation.paper_experiments`` /
``evaluation.run_evaluation`` 写出的 raw JSON），把九个验收维度尽可能算出来，算不出来
的维度输出明确的 ``"未测量"`` 标记而不是 0；并把「无损」承诺按 12.1 拆成五个分层分别
陈述。产出结构化报告（JSON + Markdown），落到 ``experiment_results/``。

设计约束（严格遵守方案与仓库纪律）：
* 不依赖第三方库（无 numpy/pandas）；峰值 RSS 只在标准库能拿到时才报，拿不到标记未测量。
* 不调用真实模型、不发网络请求；缺数据的维度一律 :data:`NOT_MEASURED`。
* 沿用 ``evaluation.metrics`` 的标题匹配 / 全 GT / 父关系距离口径，不另起一套。
* **绝不**用「Markdown 字符数 ≥ 原文字数」证明无损——见 :func:`build_lossless_tiers`。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 缺数据维度的统一占位。任何维度取不到证据时，其值必须是这个对象序列化后的结构，
# 而不是数值 0——0 会被误读成「测到了且为零」。
NOT_MEASURED = "未测量"


def _unmeasured(reason: str) -> dict[str, Any]:
    """构造一个「未测量」条目，附上为什么测不到。"""
    return {"status": NOT_MEASURED, "reason": reason}


def _percentile(values: list[float], pct: float) -> float | None:
    """最近秩法百分位；空序列返回 ``None``（交给调用方标记未测量）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = pct / 100.0 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _collect_documents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从两种 raw JSON 形态里取出 per-document 列表。

    * ``paper_experiments`` 形态：``payload["baseline"]["documents"]``。
    * ``run_evaluation`` 形态：``payload["documents"]``。
    两者都取不到时返回空列表。
    """
    if isinstance(payload.get("baseline"), dict):
        docs = payload["baseline"].get("documents")
        if isinstance(docs, list):
            return docs
    docs = payload.get("documents")
    return docs if isinstance(docs, list) else []


def _diagnostics(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """取出逐文档的 ``diagnostics`` 块（12.2 生产者的产物）。

    没有这一块说明本份产物是**旧生产者**跑出来的——面板据此继续标未测量，而不是补 0。
    这一条很重要：读者必须能区分「测了，值是 0」和「这份产物根本没测」。
    """
    return [
        doc["diagnostics"] for doc in documents
        if isinstance(doc.get("diagnostics"), dict)
    ]


def _sum_field(entries: list[dict[str, Any]], section: str, key: str) -> int | None:
    values = [
        entry[section][key] for entry in entries
        if isinstance(entry.get(section), dict)
        and isinstance(entry[section].get(key), (int, float))
    ]
    return int(sum(values)) if values else None


def _mean_field(entries: list[dict[str, Any]], section: str, key: str) -> float | None:
    values = [
        float(entry[section][key]) for entry in entries
        if isinstance(entry.get(section), dict)
        and isinstance(entry[section].get(key), (int, float))
    ]
    return _mean(values)


# --------------------------------------------------------------------------- #
# 九个维度
# --------------------------------------------------------------------------- #
def dimension_extraction(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """提取维度：源片段覆盖 / 未识别元素数 / 乱码页率 / 阅读顺序边。

    未识别元素数、乱码页率、OCR 页数、阅读顺序不确定边由
    :mod:`modules.parser.diagnostics` 在评测链路里产出（12.2 补的生产者）。
    阅读顺序**准确率**仍未测量——它要拿预测边与 GT 顺序边比，而 GT 数据集没有标顺序；
    报一个没有对照的「准确率」是编数字。
    """
    coverages = [
        doc["block_coverage"]
        for doc in documents
        if isinstance(doc.get("block_coverage"), (int, float)) and doc["block_coverage"] > 0
    ]
    diag = _diagnostics(documents)
    unrecognized = _sum_field(diag, "extraction", "unrecognized_element_count")
    garbled = _mean_field(diag, "extraction", "garbled_page_ratio")
    uncertain = _sum_field(diag, "extraction", "reading_order_uncertain_blocks")
    edges = _sum_field(diag, "extraction", "reading_order_edges")
    return {
        "源片段覆盖": {
            "mean_block_coverage": _mean(coverages),
            "n_docs": len(coverages),
        } if coverages else _unmeasured("实验产物未记录 block_coverage（>0）"),
        "未识别元素数": {"sum": unrecognized, "n_docs": len(diag)}
        if unrecognized is not None
        else _unmeasured("本份产物由旧生产者生成（无 diagnostics.extraction）"),
        "乱码页率": {"mean": garbled, "n_docs": len(diag)}
        if garbled is not None
        else _unmeasured("无页码信息的来源（DOCX 无可靠分页）或本份产物无 diagnostics"),
        "阅读顺序不确定边": {"uncertain_blocks": uncertain, "total_edges": edges}
        if uncertain is not None
        else _unmeasured("本份产物由旧生产者生成（无 diagnostics.extraction）"),
        "阅读顺序边准确率": _unmeasured(
            "GT 数据集未标注阅读顺序边，无对照可比；仅预测侧不确定边可测（见上一行）"
        ),
    }


def dimension_title(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """标题维度：候选层 / 路由层 / 最终层各自的 P/R/F1，以及按语言分桶。

    三层此前只有最终层，于是「漏检发生在候选层还是路由层」看不出来——而这两种漏检的
    修法完全不同（补候选信号 vs 改提示词/窗口）。候选层与路由层的 TP/FP/FN 由
    ``paper_experiments`` 用**同一个** ``compute_section_f1`` 口径产出，三层数字可比。
    """
    finals = [
        doc for doc in documents
        if isinstance(doc.get("section_f1"), (int, float))
        and isinstance(doc.get("precision"), (int, float))
        and isinstance(doc.get("recall"), (int, float))
    ]
    final_layer: dict[str, Any]
    if finals:
        final_layer = {
            "precision_mean": _mean([d["precision"] for d in finals]),
            "recall_mean": _mean([d["recall"] for d in finals]),
            "f1_mean": _mean([d["section_f1"] for d in finals]),
            "n_docs": len(finals),
        }
    else:
        final_layer = _unmeasured("实验产物未记录最终层 precision/recall/section_f1")

    def layer(key: str, label: str) -> dict[str, Any]:
        entries = [doc[key] for doc in documents if isinstance(doc.get(key), dict)]
        if not entries:
            return _unmeasured(
                f"本份产物未记录{label} TP/FP/FN"
                "（旧生产者，或该 run 未走路由：缓存命中 / 零特征兜底）"
            )
        return {
            "tp_sum": sum(int(e.get("tp", 0)) for e in entries),
            "fp_sum": sum(int(e.get("fp", 0)) for e in entries),
            "fn_sum": sum(int(e.get("fn", 0)) for e in entries),
            "precision_mean": _mean([float(e.get("precision", 0.0)) for e in entries]),
            "recall_mean": _mean([float(e.get("recall", 0.0)) for e in entries]),
            "f1_mean": _mean([float(e.get("f1", 0.0)) for e in entries]),
            "n_docs": len(entries),
        }

    buckets: dict[str, list[float]] = {}
    derived = False
    for doc in documents:
        language = doc.get("language")
        if not language or not isinstance(doc.get("section_f1"), (int, float)):
            continue
        derived = derived or doc.get("language_source") == "derived"
        buckets.setdefault(str(language), []).append(float(doc["section_f1"]))

    return {
        "候选层_P_R_F1": layer("candidate_layer", "候选层"),
        "路由层_P_R_F1": layer("router_layer", "路由层"),
        "最终层_P_R_F1": final_layer,
        "按语言分桶": {
            "note": "language 为按 CJK 字符占比**派生**的标签，不是人工标注"
            if derived else "language 来自产物标注",
            "buckets": {
                name: {"f1_mean": _mean(values), "n_docs": len(values)}
                for name, values in sorted(buckets.items())
            },
        } if buckets else _unmeasured("产物未携带 language 标签"),
        "按文档域分桶": _unmeasured(
            "domain（文档域）没有任何可推依据，需要人工在 GT 里标注；不在此推测"
        ),
    }


def dimension_hierarchy(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """层级维度：TP-only 与 all-GT level accuracy/F1、parent-relation distance。"""
    tp_only = [
        doc["hierarchy_accuracy"] for doc in documents
        if isinstance(doc.get("hierarchy_accuracy"), (int, float))
    ]
    all_gt_acc = [
        doc["all_gt_level_accuracy"] for doc in documents
        if isinstance(doc.get("all_gt_level_accuracy"), (int, float))
    ]
    all_gt_f1 = [
        doc["all_gt_level_f1"] for doc in documents
        if isinstance(doc.get("all_gt_level_f1"), (int, float))
    ]
    prd = [
        doc["parent_relation_distance"] for doc in documents
        if isinstance(doc.get("parent_relation_distance"), (int, float))
    ]
    if not documents:
        return _unmeasured("无 document 记录")
    return {
        "level_accuracy_tp_only_mean": _mean(tp_only),
        "all_gt_level_accuracy_mean": _mean(all_gt_acc),
        "all_gt_level_f1_mean": _mean(all_gt_f1),
        "parent_relation_distance_mean": _mean(prd),
        "n_docs": len(documents),
    }


def dimension_content(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """内容维度：重复/遗漏 atom 数、重复/遗漏 cell 数、资源缺失数。

    atom 与 cell 的归属账本由 12.2 补的生产者产出：
    ``diagnostics.closure``（每个来源单位的直接归属计数 + 未归属清单）与
    ``diagnostics.tables``（复用 E04 的 ``coverage_is_exact``，不另造判定）。
    """
    fps = [
        doc["false_positives"] for doc in documents
        if isinstance(doc.get("false_positives"), (int, float))
    ]
    fns = [
        doc["false_negatives"] for doc in documents
        if isinstance(doc.get("false_negatives"), (int, float))
    ]
    diag = _diagnostics(documents)
    stale = _unmeasured("本份产物由旧生产者生成（无 diagnostics 块）")
    duplicated = _sum_field(diag, "closure", "duplicated_units")
    unattributed = _sum_field(diag, "closure", "unattributed_units")
    total_units = _sum_field(diag, "closure", "source_units_total")
    dup_cells = _sum_field(diag, "tables", "tables_with_overlapping_cells")
    missing_cells = _sum_field(diag, "tables", "unattributed_cells")
    missing_resources = _sum_field(diag, "delivery", "missing_resources")
    return {
        "标题层_多余数_mean": _mean(fps) if fps else None,
        "标题层_遗漏数_mean": _mean(fns) if fns else None,
        "重复_atom_数": {"sum": duplicated, "source_units_total": total_units}
        if duplicated is not None else stale,
        "遗漏_atom_数": {"sum": unattributed, "source_units_total": total_units}
        if unattributed is not None else stale,
        "恰好一次": all(
            entry.get("closure", {}).get("exactly_once") is True for entry in diag
        ) if diag else stale,
        "重复_cell_表数": {"sum": dup_cells} if dup_cells is not None else stale,
        "遗漏_cell_数": {"sum": missing_cells} if missing_cells is not None else stale,
        "资源缺失数": {"sum": missing_resources} if missing_resources is not None
        else _unmeasured(
            "本次未挂 AssetStore（资源按引用外置未启用），缺失与否不可判——不报 0"
        ),
    }


def dimension_speed(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """速度维度：各阶段 p50/p95。

    实验产物提供 stage1/stage2/stage3/stage4/total 的秒数。方案要求的
    分片/排队/闭合/导出细分阶段当前未单独计时——把能对上的报出来，对不上的标未测量。
    """
    stage_field_map = {
        "stage1_提取": "stage1_time",
        "stage2_分析": "stage2_time",
        "stage3_路由": "stage3_time",
        "stage4_闭合": "stage4_time",
        "total": "total_time",
    }
    out: dict[str, Any] = {}
    for label, key in stage_field_map.items():
        samples = [
            doc[key] for doc in documents
            if isinstance(doc.get(key), (int, float)) and doc[key] > 0
        ]
        if samples:
            out[label] = {"p50": _percentile(samples, 50), "p95": _percentile(samples, 95), "n": len(samples)}
        else:
            out[label] = _unmeasured(f"实验产物未记录 {key}（>0）")
    out["分片阶段"] = _unmeasured("分片未单独计时，含在 stage2/stage3 内，需要拆分计时点")
    out["排队阶段"] = _unmeasured("排队等待未单独计时，需要运行时导出队列驻留时长")
    out["导出阶段"] = _unmeasured("导出未单独计时，评测链路不含导出阶段")
    return out


def dimension_cost(documents: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    """成本维度：LLM 调用数、输入/输出 Token、重复窗口数、OCR 页数。

    关键：区分 token 记账口径。``token_accounting_kind == "estimated"`` 是本地估算而非
    真实调用统计，必须原样透出；``"unavailable"`` 时 Token 一律标未测量。
    """
    kind = payload.get("token_accounting_kind")
    skeleton_tokens = [
        doc["skeleton_tokens"] for doc in documents
        if isinstance(doc.get("skeleton_tokens"), (int, float))
    ]
    full_tokens = [
        doc["full_text_tokens"] for doc in documents
        if isinstance(doc.get("full_text_tokens"), (int, float))
    ]
    windows = [
        doc["window_count"] for doc in documents
        if isinstance(doc.get("window_count"), (int, float))
    ]
    token_block: dict[str, Any]
    if kind == "estimated" and (skeleton_tokens or full_tokens):
        token_block = {
            "token_accounting_kind": "estimated",
            "note": "离线估算，非真实调用统计；仅可用于相对压缩率，不可当真实计费",
            "skeleton_tokens_sum": sum(skeleton_tokens),
            "full_text_tokens_sum": sum(full_tokens),
        }
    else:
        token_block = _unmeasured(
            f"token_accounting_kind={kind!r}，离线无真实 Token 计数（未调用模型）"
        )
    diag = _diagnostics(documents)
    ocr_pages = _sum_field(diag, "extraction", "ocr_pages")
    return {
        "输入_输出_Token": token_block,
        "LLM_调用数": _unmeasured("离线未调用模型；真实调用数需网关侧记账"),
        "窗口数_sum": sum(windows) if windows else None,
        "OCR_页数": {"sum": ocr_pages}
        if ocr_pages is not None
        else _unmeasured("本份产物由旧生产者生成（无 diagnostics.extraction）"),
    }


def dimension_capacity(documents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """容量维度：峰值 RSS、事件循环延迟、在途请求、输出体积。

    峰值 RSS 用标准库 :mod:`resource`（POSIX）尝试；Windows 无该模块，如实标未测量而不是
    编造。事件循环延迟 / 在途请求需运行时探针，离线聚合拿不到。
    输出体积由 12.2 的交付层生产者（``diagnostics.delivery.view_bytes``）产出。
    """
    peak_rss: dict[str, Any]
    try:  # POSIX 上标准库可取；Windows 无 resource 模块。
        import resource  # type: ignore

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux 单位是 KB，macOS 是字节；两处都换算成 MB 并注明口径不精确。
        peak_rss = {
            "ru_maxrss_raw": usage.ru_maxrss,
            "note": "ru_maxrss 单位随平台不同（Linux=KB/macOS=bytes），仅作量级参考",
        }
    except (ImportError, OSError) as exc:  # pragma: no cover - 平台相关
        peak_rss = _unmeasured(f"标准库 resource 不可用（本环境不可测）：{exc}")

    diag = _diagnostics(documents or [])
    view_bytes: dict[str, Any] = {}
    for view in ("full_markdown", "sections_markdown", "json"):
        values = [
            float(entry["delivery"]["view_bytes"][view])
            for entry in diag
            if isinstance(entry.get("delivery"), dict)
            and isinstance(entry["delivery"].get("view_bytes"), dict)
            and isinstance(entry["delivery"]["view_bytes"].get(view), (int, float))
        ]
        if values:
            view_bytes[view] = {"sum": int(sum(values)), "mean": _mean(values)}
    return {
        "峰值_RSS": peak_rss,
        "事件循环延迟": _unmeasured("需要运行时事件循环探针，离线聚合不可测"),
        "在途请求": _unmeasured("需要运行时并发计数器快照，离线聚合不可测"),
        "输出体积": view_bytes if view_bytes
        else _unmeasured("本份产物由旧生产者生成（无 diagnostics.delivery.view_bytes）"),
    }


def dimension_recovery(documents: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    """恢复维度：任务完成率、partial 率、断点复用率、取消确认时长。

    离线可得近似：从 per-document 的 status（success/partial/error）与 successful_runs /
    requested_runs 折算完成率与 partial 率。断点复用率 / 取消确认时长需运行时事件，标未测量。
    """
    if not documents:
        return _unmeasured("无 document 记录")
    total = len(documents)
    success = sum(1 for d in documents if d.get("status") == "success")
    partial = sum(1 for d in documents if d.get("status") == "partial")
    requested = sum(
        d["requested_runs"] for d in documents
        if isinstance(d.get("requested_runs"), (int, float))
    )
    succeeded = sum(
        d["successful_runs"] for d in documents
        if isinstance(d.get("successful_runs"), (int, float))
    )
    return {
        "文档完成率": success / total if total else None,
        "partial率": partial / total if total else None,
        "run级成功率": (succeeded / requested) if requested else None,
        "断点复用率": _unmeasured("需要运行时导出检查点命中事件（L03），离线不可测"),
        "取消确认时长": _unmeasured("需要运行时导出取消请求到确认的时延，离线不可测"),
    }


# --------------------------------------------------------------------------- #
# 12.1 无损承诺五分层
# --------------------------------------------------------------------------- #
def build_lossless_tiers(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """把「无损」承诺按 12.1 拆成五个分层分别陈述。

    **绝不**用「Markdown 字符数 ≥ 原文字数」证明无损：标记和重复内容都能抬高字符数，
    这是方案明确点名的错口径。这里显式声明该口径被禁用。
    """
    coverages = [
        doc["block_coverage"] for doc in documents
        if isinstance(doc.get("block_coverage"), (int, float)) and doc["block_coverage"] > 0
    ]
    diag = _diagnostics(documents)
    stale = _unmeasured("本份产物由旧生产者生成（无 diagnostics 块）")

    exactly_once = None
    if diag:
        exactly_once = all(
            entry.get("closure", {}).get("exactly_once") is True for entry in diag
        )
    normalization = {}
    for entry in diag:
        for label, count in (entry.get("normalization", {}).get("transformations") or {}).items():
            normalization[label] = normalization.get(label, 0) + int(count)
    ranges_complete = None
    if diag:
        ranges_complete = all(
            entry.get("delivery", {}).get("range_declaration", {}).get("complete") is True
            for entry in diag
        )
    return {
        "禁用口径声明": (
            "不使用 Markdown 字符数 ≥ 原文字数 作为无损证明（方案 12.1 明确点名该口径错误）"
        ),
        "源数据保留": {
            "已读来源单位数": _sum_field(diag, "closure", "source_units_total"),
            "未识别元素数": _sum_field(diag, "extraction", "unrecognized_element_count"),
            "需 OCR 页数": _sum_field(diag, "extraction", "ocr_pages"),
            "note": "登记的是「已读到并进账的来源单位」与「明确失败/未识别的单位」两侧，"
                    "两者相加才是被处理过的全部来源",
        } if diag else stale,
        "canonical内容覆盖": {
            "度量": "每个来源单位在树中恰好直接归属一次",
            "mean_block_coverage": _mean(coverages),
            "n_docs": len(coverages),
            "恰好一次": exactly_once,
            "重复归属单位数": _sum_field(diag, "closure", "duplicated_units"),
            "未归属单位数": _sum_field(diag, "closure", "unattributed_units"),
            "note": "block_coverage 只证明区间被覆盖；「恰好一次」由闭合层归属账本判定"
                    "（按**直接归属**算，因为父节点区间天然覆盖子节点）",
        } if coverages or diag else _unmeasured("实验产物未记录 block_coverage"),
        "规范化可追溯": {"变换出现次数": normalization} if normalization else (
            stale if not diag
            else _unmeasured("本批文档的 atom 上没有任何规范化痕迹（未发生变换）")
        ),
        "视图完整性": {
            "各视图字节数": [
                entry.get("delivery", {}).get("view_bytes") for entry in diag
            ][:1],
            "范围声明齐全": ranges_complete,
            "note": "范围齐全 = 树声明的块区间并集覆盖全部块 id；字节数只取首篇作形状示例",
        } if diag else stale,
        "识别准确性": {
            "说明": "OCR/公式转换/表格重建属推断，单独报准确率，不计入强制闭合的无遗漏证明",
            "OCR准确率": _unmeasured("离线无 OCR GT 对齐，未测量"),
            "公式转换准确率": _unmeasured("离线无公式 GT 对齐，未测量"),
            "表格重建准确率": {
                "coverage_exact_ratio_mean": _mean_field(diag, "tables", "coverage_exact_ratio"),
                "note": "这是**覆盖不变量**通过率（无重叠无空洞），不是与 GT 对齐的准确率",
            } if _mean_field(diag, "tables", "coverage_exact_ratio") is not None
            else _unmeasured("离线无表格 GT 对齐；覆盖不变量需 diagnostics.tables"),
        },
    }


# --------------------------------------------------------------------------- #
# 面板入口
# --------------------------------------------------------------------------- #
def build_panel(payload: dict[str, Any], source_label: str = "") -> dict[str, Any]:
    """吃一份实验产物 payload，产出结构化面板报告（dict）。"""
    documents = _collect_documents(payload)
    return {
        "source": source_label,
        "n_documents": len(documents),
        "token_accounting_kind": payload.get("token_accounting_kind", NOT_MEASURED),
        "dimensions": {
            "提取": dimension_extraction(documents),
            "标题": dimension_title(documents),
            "层级": dimension_hierarchy(documents),
            "内容": dimension_content(documents),
            "速度": dimension_speed(documents),
            "成本": dimension_cost(documents, payload),
            "容量": dimension_capacity(documents),
            "恢复": dimension_recovery(documents, payload),
        },
        "lossless_tiers": build_lossless_tiers(documents),
    }


def _render_value(value: Any) -> str:
    if isinstance(value, dict) and value.get("status") == NOT_MEASURED:
        return f"未测量（{value.get('reason', '')}）"
    if isinstance(value, float):
        return f"{value:.4f}"
    if value is None:
        return "未测量（无样本）"
    return str(value)


def render_markdown(panel: dict[str, Any]) -> str:
    """把面板 dict 渲染成 Markdown 报告。"""
    lines = [
        "# 最小指标面板报告（方案第 12 节）",
        "",
        f"- 数据源：`{panel.get('source', '')}`",
        f"- 文档数：{panel.get('n_documents', 0)}",
        f"- Token 记账口径：{panel.get('token_accounting_kind')}",
        "",
        "## 九个维度",
        "",
    ]
    for dim_name, dim_value in panel["dimensions"].items():
        lines.append(f"### {dim_name}")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|:-----|:---|")
        if isinstance(dim_value, dict) and dim_value.get("status") == NOT_MEASURED:
            lines.append(f"| （整维度） | {_render_value(dim_value)} |")
        else:
            for key, value in dim_value.items():
                lines.append(f"| {key} | {_render_value(value)} |")
        lines.append("")
    lines.append("## 无损承诺五分层（12.1）")
    lines.append("")
    lines.append(f"> {panel['lossless_tiers']['禁用口径声明']}")
    lines.append("")
    lines.append("| 分层 | 陈述 |")
    lines.append("|:-----|:-----|")
    for key, value in panel["lossless_tiers"].items():
        if key == "禁用口径声明":
            continue
        lines.append(f"| {key} | {_render_value(value)} |")
    lines.append("")
    return "\n".join(lines)


def run_panel(
    input_json: str | Path,
    output_dir: str | Path = "experiment_results",
    basename: str = "metrics_panel",
) -> tuple[Path, Path]:
    """从一份实验产物 JSON 生成面板报告（JSON + Markdown），返回两个落盘路径。"""
    input_path = Path(input_json)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    panel = build_panel(payload, source_label=str(input_path))

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{basename}.json"
    md_path = out_dir / f"{basename}.md"
    json_path.write_text(json.dumps(panel, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(panel), encoding="utf-8")
    return json_path, md_path


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m evaluation.metrics_panel <experiment_raw.json> [output_dir]")
        raise SystemExit(2)
    out = sys.argv[2] if len(sys.argv) > 2 else "experiment_results"
    json_out, md_out = run_panel(sys.argv[1], out)
    print(f"面板 JSON: {json_out}")
    print(f"面板 Markdown: {md_out}")
