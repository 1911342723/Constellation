"""Quantitative evaluation metrics for the Constellation pipeline.

The module deliberately distinguishes three concepts:
* heading detection (maximum-weight one-to-one matching),
* TP-only level accuracy versus all-GT hierarchy quality, and
* heading-sequence edit distance versus parent-relation structure distance.

The structure metric is explicitly parent-relation based; no full ordered-tree
edit algorithm is claimed.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import List


@dataclass
class HeadingGT:
    """Ground-truth heading entry."""

    block_id: int
    title: str
    level: int


@dataclass
class HeadingPred:
    """Predicted heading entry."""

    block_id: int
    title: str
    level: int


@dataclass
class EvalResult:
    """Aggregated evaluation result."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0

    # Backward-compatible TP-only level accuracy.
    level_correct: int = 0
    level_total: int = 0
    hierarchy_accuracy: float = 0.0
    hierarchy_accuracy_tp_only: float = 0.0

    # All-GT hierarchy metrics: unmatched GT headings count as incorrect.
    all_gt_level_accuracy: float = 0.0
    all_gt_level_precision: float = 0.0
    all_gt_level_recall: float = 0.0
    all_gt_level_f1: float = 0.0
    hierarchy_accuracy_all_gt: float = 0.0
    hierarchy_f1_all_gt: float = 0.0

    # Explicitly named distances.  ``tree_edit_distance`` is retained only as
    # a compatibility alias for ``parent_relation_distance``.
    heading_sequence_edit_distance: float = 0.0
    parent_relation_distance: float = 0.0
    tree_edit_distance: float = 0.0

    char_recall: float = 0.0
    block_coverage: float = 0.0
    markdown_char_coverage: float = 0.0

    tp_pairs: list = field(default_factory=list)
    fp_preds: list = field(default_factory=list)
    fn_gts: list = field(default_factory=list)
    matching_details: list[dict] = field(default_factory=list)


def _maximum_weight_assignment(weights: list[list[float]]) -> list[tuple[int, int]]:
    """Return a maximum-weight one-to-one assignment using Hungarian DP.

    The matrix is padded with zero-weight dummy vertices, so rows and columns
    may remain unmatched.  The implementation is O(n^3) and dependency-free.
    """
    if not weights or not weights[0]:
        return []
    row_count = len(weights)
    column_count = len(weights[0])
    size = max(row_count, column_count)
    padded = [[0.0] * size for _ in range(size)]
    for i, row in enumerate(weights):
        padded[i][:column_count] = row

    max_weight = max((max(row) for row in padded), default=0.0)
    costs = [[max_weight - value for value in row] for row in padded]

    # Standard shortest-augmenting-path Hungarian algorithm for min cost.
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        minv = [float("inf")] * (size + 1)
        used = [False] * (size + 1)
        j0 = 0
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = float("inf")
            j1 = 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                cur = costs[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assignment: list[tuple[int, int]] = []
    for j in range(1, size + 1):
        i = p[j] - 1
        column = j - 1
        if 0 <= i < row_count and column < column_count:
            assignment.append((i, column))
    return assignment


def _connected_components(
    edges: list[tuple[int, int]], row_count: int, column_count: int,
) -> list[tuple[list[int], list[int]]]:
    """把稀疏可匹配图按连通分量切开。

    顶点是 GT 行与 pred 列两侧，边是 ``edges`` 中出现的可匹配对。返回每个连通分量
    的 ``(该分量涉及的行索引, 该分量涉及的列索引)``。只包含至少落在一条边上的顶点，
    孤立顶点（无任何可匹配边）不进入任何分量——它们在任何解里都必然未匹配，单独处理
    不影响最优解。
    """
    # 并查集：行编号 [0, row_count)，列编号 [row_count, row_count+column_count)。
    parent = list(range(row_count + column_count))

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    touched_rows: set[int] = set()
    touched_cols: set[int] = set()
    for gt_index, pred_index in edges:
        union(gt_index, row_count + pred_index)
        touched_rows.add(gt_index)
        touched_cols.add(pred_index)

    groups: dict[int, tuple[list[int], list[int]]] = {}
    for gt_index in touched_rows:
        groups.setdefault(find(gt_index), ([], []))[0].append(gt_index)
    for pred_index in touched_cols:
        groups.setdefault(find(row_count + pred_index), ([], []))[1].append(pred_index)

    components: list[tuple[list[int], list[int]]] = []
    for rows, cols in groups.values():
        components.append((sorted(rows), sorted(cols)))
    return components


def _sparse_maximum_weight_assignment(
    weights: list[list[float]],
    edges: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """在稀疏可匹配图上求最大权一对一匹配，结果与稠密 Hungarian 逐一等价。

    做法：先按 ``edges``（严格正权可匹配边）把二分图切成互不相邻的连通分量，再对每个
    分量抽取其行列子矩阵、单独跑 :func:`_maximum_weight_assignment`，最后把各分量的配对
    映射回全局索引后合并。

    等价性依据：不同连通分量之间没有任何可匹配边，任一分量的配对选择都不影响其它分量的
    可行域与目标值，因此各分量分别取最优即为全局最优；权为 0 的不可匹配边在带基数奖励
    （每条合法边严格 > 0）的目标下永远不会被选入最大权匹配，切分时丢弃它们不改变最优解。
    """
    if not weights or not weights[0]:
        return []
    row_count = len(weights)
    column_count = len(weights[0])
    components = _connected_components(edges, row_count, column_count)

    assignment: list[tuple[int, int]] = []
    for rows, cols in components:
        if not rows or not cols:
            continue
        sub_weights = [
            [weights[gt_index][pred_index] for pred_index in cols]
            for gt_index in rows
        ]
        for sub_gt, sub_pred in _maximum_weight_assignment(sub_weights):
            assignment.append((rows[sub_gt], cols[sub_pred]))
    return assignment


def compute_section_f1(
    gt_headings: List[HeadingGT],
    pred_headings: List[HeadingPred],
    block_id_tolerance: int = 3,
    title_sim_threshold: float = 0.6,
) -> EvalResult:
    """Compute heading, hierarchy and structural metrics.

    Eligible edges retain the historical block-distance and title-similarity
    gates.  A global maximum-weight one-to-one assignment replaces GT-order
    greedy matching.  A cardinality bonus larger than all quality tie-breakers
    makes the objective maximize TP count first, then title similarity and
    block proximity.
    """
    from modules.parser.resolver import _levenshtein_ratio

    if block_id_tolerance < 0:
        raise ValueError("block_id_tolerance must be >= 0")
    if not 0.0 <= title_sim_threshold <= 1.0:
        raise ValueError("title_sim_threshold must be in [0, 1]")

    result = EvalResult()
    edge_details: dict[tuple[int, int], dict] = {}
    eligible_edges: list[tuple[int, int]] = []
    weights = [[0.0 for _ in pred_headings] for _ in gt_headings]
    cardinality_bonus = float(2 * (max(len(gt_headings), len(pred_headings)) + 1))

    for gt_index, gt in enumerate(gt_headings):
        for pred_index, pred in enumerate(pred_headings):
            distance = abs(pred.block_id - gt.block_id)
            if distance > block_id_tolerance:
                continue
            similarity = _levenshtein_ratio(
                gt.title.strip().lower(), pred.title.strip().lower(),
            )
            if similarity < title_sim_threshold:
                continue
            proximity = (
                (block_id_tolerance - distance) / (block_id_tolerance + 1)
                if block_id_tolerance >= 0 else 0.0
            )
            # Proximity is a deterministic tie-breaker and cannot dominate a
            # title-similarity difference.
            quality = similarity + proximity * 1e-3
            weight = cardinality_bonus + quality
            weights[gt_index][pred_index] = weight
            eligible_edges.append((gt_index, pred_index))
            edge_details[(gt_index, pred_index)] = {
                "title_similarity": similarity,
                "block_distance": distance,
                "quality_weight": quality,
                "assignment_weight": weight,
            }

    assignments = _sparse_maximum_weight_assignment(weights, eligible_edges)
    matched_gt: set[int] = set()
    matched_pred: set[int] = set()
    for gt_index, pred_index in sorted(assignments):
        detail = edge_details.get((gt_index, pred_index))
        if detail is None:
            continue
        gt = gt_headings[gt_index]
        pred = pred_headings[pred_index]
        matched_gt.add(gt_index)
        matched_pred.add(pred_index)
        result.tp += 1
        result.tp_pairs.append((gt, pred))
        level_correct = gt.level == pred.level
        if level_correct:
            result.level_correct += 1
        result.level_total += 1
        result.matching_details.append({
            "gt_index": gt_index,
            "pred_index": pred_index,
            "gt": asdict(gt),
            "pred": asdict(pred),
            "level_correct": level_correct,
            **detail,
        })

    for gt_index, gt in enumerate(gt_headings):
        if gt_index not in matched_gt:
            result.fn += 1
            result.fn_gts.append(gt)
    for pred_index, pred in enumerate(pred_headings):
        if pred_index not in matched_pred:
            result.fp += 1
            result.fp_preds.append(pred)

    result.precision = result.tp / max(result.tp + result.fp, 1)
    result.recall = result.tp / max(result.tp + result.fn, 1)
    if result.precision + result.recall:
        result.f1 = 2 * result.precision * result.recall / (result.precision + result.recall)

    result.hierarchy_accuracy = result.level_correct / max(result.level_total, 1)
    result.hierarchy_accuracy_tp_only = result.hierarchy_accuracy
    result.all_gt_level_accuracy = result.level_correct / max(len(gt_headings), 1)
    result.all_gt_level_precision = result.level_correct / max(len(pred_headings), 1)
    result.all_gt_level_recall = result.all_gt_level_accuracy
    if result.all_gt_level_precision + result.all_gt_level_recall:
        result.all_gt_level_f1 = (
            2 * result.all_gt_level_precision * result.all_gt_level_recall
            / (result.all_gt_level_precision + result.all_gt_level_recall)
        )
    result.hierarchy_accuracy_all_gt = result.all_gt_level_accuracy
    result.hierarchy_f1_all_gt = result.all_gt_level_f1

    result.heading_sequence_edit_distance = _compute_heading_sequence_edit_distance(
        gt_headings, pred_headings,
    )
    result.parent_relation_distance = _compute_parent_relation_distance(
        gt_headings, pred_headings,
    )
    result.tree_edit_distance = result.parent_relation_distance
    return result


def compute_char_recall(original_chars: int, extracted_chars: int) -> float:
    """Compute character-level recall (coverage)."""
    if original_chars == 0:
        return 1.0
    return extracted_chars / original_chars


def compute_block_coverage(total_blocks: int, covered_block_ids: set[int] | list[int]) -> float:
    """Compute block-id interval coverage."""
    if total_blocks == 0:
        return 1.0
    return len(set(covered_block_ids)) / total_blocks


def compute_markdown_char_coverage(original_markdown_chars: int, rendered_markdown_chars: int) -> float:
    """Compute Markdown character coverage with insertion guard."""
    if original_markdown_chars == 0:
        return 1.0
    return min(rendered_markdown_chars / original_markdown_chars, 1.0)


def _normalise_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


def _compute_heading_sequence_edit_distance(
    gt_headings: List[HeadingGT], pred_headings: List[HeadingPred],
) -> float:
    """Sequence edit distance over ``(level, normalised title)`` tuples."""
    seq_gt = [(h.level, _normalise_title(h.title)) for h in gt_headings]
    seq_pr = [(h.level, _normalise_title(h.title)) for h in pred_headings]
    return _sequence_edit_distance(seq_gt, seq_pr)


def _parent_relations(headings: List[HeadingGT] | List[HeadingPred]) -> Counter:
    """Build a multiset of labelled ``parent -> child`` heading relations."""
    relations: Counter = Counter()
    stack: list[HeadingGT | HeadingPred] = []
    for heading in headings:
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        parent_title = _normalise_title(stack[-1].title) if stack else "<root>"
        relations[(parent_title, _normalise_title(heading.title))] += 1
        stack.append(heading)
    return relations


def _compute_parent_relation_distance(
    gt_headings: List[HeadingGT], pred_headings: List[HeadingPred],
) -> float:
    """Labelled parent-relation edge edit distance.

    The score is the multiset insertion/deletion count required to transform
    GT parent-child relations into predicted relations.  Re-parenting one node
    therefore costs one deletion plus one insertion.
    """
    gt_relations = _parent_relations(gt_headings)
    pred_relations = _parent_relations(pred_headings)
    keys = set(gt_relations) | set(pred_relations)
    return float(sum(abs(gt_relations[key] - pred_relations[key]) for key in keys))


def _compute_tree_edit_distance(
    gt_headings: List[HeadingGT], pred_headings: List[HeadingPred],
) -> float:
    """Compatibility wrapper for the parent-relation structure distance."""
    return _compute_parent_relation_distance(gt_headings, pred_headings)


def _sequence_edit_distance(seq_a: list[tuple], seq_b: list[tuple]) -> float:
    """Classic two-row DP edit distance over tuples."""
    m, n = len(seq_a), len(seq_b)
    prev = list(range(n + 1))
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        curr[0] = i
        for j in range(1, n + 1):
            cost = 0 if seq_a[i - 1] == seq_b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev, curr = curr, prev
    return float(prev[n])


def format_eval_report(result: EvalResult, doc_name: str = "") -> str:
    """Format an evaluation result as a Markdown report fragment."""
    lines = []
    if doc_name:
        lines.extend([f"### {doc_name}", ""])
    lines.extend([
        "| Metric | Value |",
        "|:-------|------:|",
        f"| Precision | {result.precision:.4f} |",
        f"| Recall | {result.recall:.4f} |",
        f"| **F1** | **{result.f1:.4f}** |",
        f"| TP / FP / FN | {result.tp} / {result.fp} / {result.fn} |",
        f"| Hierarchy Accuracy (TP-only) | {result.hierarchy_accuracy:.4f} |",
        f"| All-GT Level Accuracy | {result.all_gt_level_accuracy:.4f} |",
        f"| All-GT Level F1 | {result.all_gt_level_f1:.4f} |",
        f"| Heading Sequence Edit Distance | {result.heading_sequence_edit_distance:.1f} |",
        f"| Parent-Relation Distance | {result.parent_relation_distance:.1f} |",
    ])
    if result.block_coverage > 0:
        lines.append(f"| Block Coverage | {result.block_coverage:.4f} |")
    if result.markdown_char_coverage > 0:
        lines.append(f"| Markdown Char Coverage | {result.markdown_char_coverage:.4f} |")
    if result.char_recall > 0:
        lines.append(f"| Character Recall | {result.char_recall:.4f} |")
    if result.fn_gts:
        lines.extend(["", "**Missed headings (FN):**"])
        lines.extend(f"- [ID={gt.block_id}] L{gt.level}: {gt.title}" for gt in result.fn_gts)
    if result.fp_preds:
        lines.extend(["", "**False positives (FP):**"])
        lines.extend(f"- [ID={pred.block_id}] L{pred.level}: {pred.title}" for pred in result.fp_preds)
    return "\n".join(lines)
