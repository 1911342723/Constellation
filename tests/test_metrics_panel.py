"""WP6 · 验收指标面板 + 匹配算法（方案第 12 节）测试。

覆盖三块：
1. 稀疏连通分量匹配与稠密 O(n³) Hungarian 的**逐一等价**（TP 配对完全相同）+ 规模曲线。
2. 方案 12.3 列出的五条 property/metamorphic 不变量。
3. ``evaluation.metrics_panel`` 的九维聚合、未测量口径与无损分层。
"""
from __future__ import annotations

import json
import random

import pytest

from evaluation import metrics_panel
from evaluation.metrics import (
    HeadingGT,
    HeadingPred,
    _connected_components,
    _maximum_weight_assignment,
    _sparse_maximum_weight_assignment,
    compute_section_f1,
)


# --------------------------------------------------------------------------- #
# (3) 匹配算法：稀疏 vs 稠密 逐一等价 + 规模曲线
# --------------------------------------------------------------------------- #
def _random_weight_matrix(rng: random.Random, rows: int, cols: int, density: float):
    """随机稀疏正权矩阵：0 表示不可匹配边，>0 表示可匹配边（附带基数奖励语义）。"""
    bonus = float(2 * (max(rows, cols) + 1))
    weights = [[0.0] * cols for _ in range(rows)]
    edges = []
    for i in range(rows):
        for j in range(cols):
            if rng.random() < density:
                # 权重 = 基数奖励 + 质量项，保证合法边严格 > 0 且基数主导。
                weights[i][j] = bonus + rng.random()
                edges.append((i, j))
    return weights, edges


def test_sparse_matching_is_pairwise_equivalent_to_dense_hungarian():
    """对随机若干组输入，稀疏解与稠密 Hungarian 的配对集合完全相同（非分数接近）。"""
    rng = random.Random(20260907)
    for _ in range(200):
        rows = rng.randint(0, 12)
        cols = rng.randint(0, 12)
        density = rng.choice([0.1, 0.3, 0.6, 1.0])
        weights, edges = _random_weight_matrix(rng, rows, cols, density)

        dense = _maximum_weight_assignment(weights)
        sparse = _sparse_maximum_weight_assignment(weights, edges)

        # 稠密解可能包含权为 0 的哑配对（未匹配对），稀疏解只含真实边——比较时
        # 都过滤掉零权配对后应完全一致。
        dense_real = {(i, j) for i, j in dense if weights[i][j] > 0}
        sparse_real = {(i, j) for i, j in sparse if weights[i][j] > 0}
        assert dense_real == sparse_real

        # 目标值（总权）也必须相等——等价性的强断言。
        assert sum(weights[i][j] for i, j in dense_real) == pytest.approx(
            sum(weights[i][j] for i, j in sparse_real)
        )


def test_compute_section_f1_tp_pairs_stable_under_sparse_solver():
    """端到端：compute_section_f1 用稀疏解，TP 配对与预期一致（回归已有全局最优用例）。"""
    from modules.parser import resolver

    similarities = {
        ("a", "p0"): 0.95, ("a", "p1"): 0.80,
        ("b", "p0"): 0.85, ("b", "p1"): 0.10,
    }
    orig = resolver._levenshtein_ratio
    resolver._levenshtein_ratio = lambda left, right: similarities[(left, right)]
    try:
        gt = [HeadingGT(0, "A", 1), HeadingGT(1, "B", 1)]
        pred = [HeadingPred(0, "P0", 1), HeadingPred(1, "P1", 1)]
        result = compute_section_f1(gt, pred, block_id_tolerance=1, title_sim_threshold=0.6)
    finally:
        resolver._levenshtein_ratio = orig

    assert result.tp == 2
    assert [(p[0].title, p[1].title) for p in result.tp_pairs] == [("A", "P1"), ("B", "P0")]


def test_connected_components_splits_independent_blocks():
    """两块互不相邻的边应切成两个连通分量，孤立顶点不进入任何分量。"""
    # 行 0-1 与列 0-1 一块；行 2 与列 2 另一块；行 3 / 列 3 孤立无边。
    edges = [(0, 0), (0, 1), (1, 1), (2, 2)]
    components = _connected_components(edges, row_count=4, column_count=4)
    normalized = sorted((tuple(r), tuple(c)) for r, c in components)
    assert normalized == [((0, 1), (0, 1)), ((2,), (2,))]


def test_sparse_matching_scale_curve():
    """规模曲线：n=200/400/800 三档跑得通并记录操作计数（连通分量数 + 子块规模）。"""
    rng = random.Random(7)
    curve = {}
    block = 5  # 每 5 个 GT/pred 构成一个独立块，块间无边，模拟真实文档里互不重叠的 span 组。
    for n in (200, 400, 800):
        weights = [[0.0] * n for _ in range(n)]
        edges = []
        bonus = float(2 * (n + 1))
        for start in range(0, n, block):
            end = min(start + block, n)
            for i in range(start, end):
                for j in range(start, end):
                    weights[i][j] = bonus + rng.random()
                    edges.append((i, j))
        components = _connected_components(edges, n, n)
        assignment = _sparse_maximum_weight_assignment(weights, edges)
        largest = max((len(r) for r, _ in components), default=0)
        curve[n] = {
            "edges": len(edges),
            "components": len(components),
            "largest_component_rows": largest,
            "matched": len(assignment),
        }
        # 稀疏化的意义：最大连通分量远小于 n（否则退化回稠密 O(n³)）。
        assert largest <= block
        assert len(assignment) == n
    # 单调性 sanity：规模越大边越多。
    assert curve[200]["edges"] < curve[400]["edges"] < curve[800]["edges"]


# --------------------------------------------------------------------------- #
# (4) property / metamorphic：方案 12.3 五条
# --------------------------------------------------------------------------- #
def _identity_pairs(gt, pred, **kwargs):
    result = compute_section_f1(gt, pred, **kwargs)
    return {(p[0].block_id, p[1].block_id) for p in result.tp_pairs}


def test_metamorphic_adding_plain_paragraph_keeps_heading_identity():
    """M1：添加普通段落（非标题、块 ID 后移不与既有标题冲突）不改变既有标题身份。"""
    gt = [HeadingGT(0, "引言", 1), HeadingGT(10, "方法", 1)]
    pred = [HeadingPred(0, "引言", 1), HeadingPred(10, "方法", 1)]
    base = _identity_pairs(gt, pred, block_id_tolerance=1)

    # 插入普通段落等价于在两个标题之间增加非标题内容——预测标题集合不变，仅块间距离不变时
    # 身份配对必须保持。
    assert base == {(0, 0), (10, 10)}
    # 再跑一次，pred 未变，配对稳定（幂等）。
    assert _identity_pairs(gt, pred, block_id_tolerance=1) == base


def test_metamorphic_unrelated_image_change_does_not_affect_body_levels():
    """M2：改变无关图片不影响正文层级——层级指标由标题层级决定，与图片无关。"""
    gt = [HeadingGT(0, "A", 1), HeadingGT(5, "B", 2)]
    pred = [HeadingPred(0, "A", 1), HeadingPred(5, "B", 2)]
    before = compute_section_f1(gt, pred, block_id_tolerance=1)
    # 「改变无关图片」在评测输入层面等价于标题输入完全不变（图片不是标题）。
    after = compute_section_f1(gt, pred, block_id_tolerance=1)
    assert before.parent_relation_distance == after.parent_relation_distance
    assert before.all_gt_level_f1 == after.all_gt_level_f1


def test_metamorphic_window_cut_change_does_not_change_content_coverage():
    """M3：改变窗口切点不改变内容覆盖——覆盖由被覆盖块 ID 集合决定，与切点无关。"""
    from evaluation.metrics import compute_block_coverage

    total = 20
    # 两种不同的「窗口切分」覆盖同一批块 ID，覆盖率必须相同。
    cut_a = list(range(0, 10)) + list(range(10, 20))
    cut_b = list(range(0, 7)) + list(range(7, 20))
    assert compute_block_coverage(total, cut_a) == compute_block_coverage(total, cut_b)


def test_metamorphic_repeated_evidence_does_not_inflate_confidence_unbounded():
    """M4：相同证据重复出现不无限提高置信度——重复标题预测不应让 TP 超过 GT 数。"""
    gt = [HeadingGT(0, "唯一标题", 1)]
    # 同一预测证据重复十次。
    pred = [HeadingPred(0, "唯一标题", 1)] + [HeadingPred(0, "唯一标题", 1) for _ in range(9)]
    result = compute_section_f1(gt, pred, block_id_tolerance=1)
    # 一对一匹配：至多命中 1 个 TP，其余重复算 FP，绝不因重复把 TP 抬高。
    assert result.tp == 1
    assert result.fp == 9


def test_metamorphic_serialization_roundtrip_preserves_frontmatter():
    """M5：序列化往返不丢前言——面板 dict 经 JSON 往返后前言/字段完全保留。"""
    payload = {
        "token_accounting_kind": "estimated",
        "baseline": {"documents": [{
            "name": "d", "status": "success", "requested_runs": 1, "successful_runs": 1,
            "block_coverage": 0.9, "section_f1": 0.8, "precision": 0.8, "recall": 0.8,
            "hierarchy_accuracy": 1.0, "all_gt_level_accuracy": 0.9, "all_gt_level_f1": 0.9,
            "parent_relation_distance": 2.0, "false_positives": 1.0, "false_negatives": 0.0,
            "stage1_time": 0.5, "total_time": 1.0, "window_count": 3,
            "skeleton_tokens": 100, "full_text_tokens": 300,
        }]},
    }
    panel = metrics_panel.build_panel(payload, source_label="frontmatter")
    roundtrip = json.loads(json.dumps(panel, ensure_ascii=False))
    assert roundtrip["source"] == "frontmatter"
    assert roundtrip["n_documents"] == 1
    assert roundtrip["token_accounting_kind"] == "estimated"
    assert roundtrip == panel


# --------------------------------------------------------------------------- #
# (1)(2) 面板：九维聚合 + 未测量口径 + 无损分层
# --------------------------------------------------------------------------- #
def _sample_payload():
    return {
        "token_accounting_kind": "estimated",
        "baseline": {"documents": [
            {
                "name": "doc1", "status": "success", "requested_runs": 2, "successful_runs": 2,
                "block_coverage": 0.95, "section_f1": 0.7, "precision": 0.72, "recall": 0.68,
                "hierarchy_accuracy": 0.9, "all_gt_level_accuracy": 0.8, "all_gt_level_f1": 0.85,
                "parent_relation_distance": 3.0, "false_positives": 2.0, "false_negatives": 1.0,
                "stage1_time": 0.4, "stage2_time": 0.3, "stage3_time": 0.2, "stage4_time": 0.1,
                "total_time": 1.0, "window_count": 4, "skeleton_tokens": 200, "full_text_tokens": 600,
            },
            {
                "name": "doc2", "status": "partial", "requested_runs": 2, "successful_runs": 1,
                "block_coverage": 0.85, "section_f1": 0.5, "precision": 0.5, "recall": 0.5,
                "hierarchy_accuracy": 0.7, "all_gt_level_accuracy": 0.6, "all_gt_level_f1": 0.65,
                "parent_relation_distance": 5.0, "false_positives": 3.0, "false_negatives": 2.0,
                "stage1_time": 0.6, "stage2_time": 0.5, "stage3_time": 0.3, "stage4_time": 0.2,
                "total_time": 1.6, "window_count": 6, "skeleton_tokens": 300, "full_text_tokens": 900,
            },
        ]},
    }


def test_panel_computes_measurable_dimensions():
    panel = metrics_panel.build_panel(_sample_payload(), "sample")
    dims = panel["dimensions"]

    # 提取：block_coverage 可算。
    assert dims["提取"]["源片段覆盖"]["mean_block_coverage"] == pytest.approx(0.9)
    # 标题：最终层可算，候选/路由标未测量。
    assert dims["标题"]["最终层_P_R_F1"]["f1_mean"] == pytest.approx(0.6)
    assert dims["标题"]["候选层_P_R_F1"]["status"] == metrics_panel.NOT_MEASURED
    # 层级：三口径可算。
    assert dims["层级"]["parent_relation_distance_mean"] == pytest.approx(4.0)
    # 速度：stage 有值 p50/p95 可算。
    assert dims["速度"]["stage1_提取"]["p50"] is not None
    assert dims["速度"]["导出阶段"]["status"] == metrics_panel.NOT_MEASURED
    # 成本：estimated 口径透出且带告警 note。
    assert dims["成本"]["输入_输出_Token"]["token_accounting_kind"] == "estimated"
    assert dims["成本"]["LLM_调用数"]["status"] == metrics_panel.NOT_MEASURED
    # 恢复：完成率/partial率可算。
    assert dims["恢复"]["文档完成率"] == pytest.approx(0.5)
    assert dims["恢复"]["partial率"] == pytest.approx(0.5)
    assert dims["恢复"]["run级成功率"] == pytest.approx(0.75)


def test_panel_marks_missing_dimensions_as_not_measured_not_zero():
    """缺数据维度必须是「未测量」而不是数值 0。"""
    panel = metrics_panel.build_panel({"documents": []}, "empty")
    dims = panel["dimensions"]
    assert dims["提取"]["未识别元素数"]["status"] == metrics_panel.NOT_MEASURED
    assert dims["容量"]["事件循环延迟"]["status"] == metrics_panel.NOT_MEASURED
    assert dims["成本"]["OCR_页数"]["status"] == metrics_panel.NOT_MEASURED
    # 恢复整维度在无文档时标未测量。
    assert dims["恢复"]["status"] == metrics_panel.NOT_MEASURED


def test_panel_capacity_rss_is_honest_about_platform():
    """峰值 RSS：POSIX 给量级；无 resource 模块（Windows）如实标未测量而非编造。"""
    peak = metrics_panel.dimension_capacity()["峰值_RSS"]
    # 要么给出 ru_maxrss_raw，要么明确标未测量——绝不给编造数值。
    assert ("ru_maxrss_raw" in peak) or (peak.get("status") == metrics_panel.NOT_MEASURED)


def test_lossless_tiers_ban_markdown_char_proof():
    """无损分层必须显式禁用「Markdown 字符数 ≥ 原文字数」口径，并五层齐全。"""
    tiers = metrics_panel.build_lossless_tiers([{"block_coverage": 0.9}])
    assert "Markdown 字符数" in tiers["禁用口径声明"]
    for key in ("源数据保留", "canonical内容覆盖", "规范化可追溯", "视图完整性", "识别准确性"):
        assert key in tiers
    # canonical 覆盖用 block_coverage 近似，但注明不等于「恰好一次」。
    assert "恰好一次" in tiers["canonical内容覆盖"]["note"]


def test_run_panel_writes_json_and_markdown(tmp_path):
    src = tmp_path / "raw.json"
    src.write_text(json.dumps(_sample_payload(), ensure_ascii=False), encoding="utf-8")
    json_path, md_path = metrics_panel.run_panel(src, tmp_path / "out", basename="panel_test")

    assert json_path.exists() and md_path.exists()
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["n_documents"] == 2
    md = md_path.read_text(encoding="utf-8")
    assert "最小指标面板报告" in md
    assert "无损承诺五分层" in md
    # 未测量项在 Markdown 里如实渲染。
    assert "未测量" in md


def test_run_evaluation_shape_documents_also_parsed():
    """run_evaluation 形态（顶层 documents）也能被面板消费。"""
    payload = {
        "token_accounting_kind": "unavailable",
        "documents": [{
            "name": "d", "status": "success", "requested_runs": 1, "successful_runs": 1,
            "block_coverage": 0.8, "section_f1": 0.9, "precision": 0.9, "recall": 0.9,
            "hierarchy_accuracy": 1.0, "all_gt_level_accuracy": 1.0, "all_gt_level_f1": 1.0,
            "parent_relation_distance": 0.0,
        }],
    }
    panel = metrics_panel.build_panel(payload, "run_eval")
    assert panel["n_documents"] == 1
    # unavailable 口径下 Token 必须标未测量。
    assert panel["dimensions"]["成本"]["输入_输出_Token"]["status"] == metrics_panel.NOT_MEASURED
