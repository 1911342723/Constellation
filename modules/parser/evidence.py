"""Evidence fusion for heading candidates.

This module deliberately knows nothing about routing budgets or legacy scores.
It consumes typed :class:`EvidenceAtom` observations, collapses correlated
signals, and emits heuristic local probabilities.  The values are versioned
heuristics until a held-out calibration artifact is supplied; callers must not
report them as calibrated posteriors.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


from modules.parser.schemas import EvidenceAtom, HeadingLabel, RegionRisk


CALIBRATION_VERSION = "heading-evidence.heuristic.v1"
PROBABILITY_QUALITY = "heuristic"

# ── A04：相关投票聚类的降增益上限 ──────────────────────────────
# 同一模型 / 同一提示词版本 / 高度重叠上下文的投票是相关的，不能当独立
# 证据一票一累加。给一个聚类内的第 k 张票以递减增益 GAIN_DECAY**(k-1)：
# 首票拿全额，重复票的边际收益按几何级数衰减，其无穷和收敛到
# 1/(1-GAIN_DECAY)，即一个聚类无论投多少次，其等效独立票数有硬上限。
# 取 0.5 使上限为 2.0：真正的“再看一眼”值半票，第三票起近乎归零，
# 而“重复同一个窗口”永远突破不了这个上限——这正是验收要求的
#「重复提供相同窗口不应让结论无依据变强」。
_CORRELATED_VOTE_GAIN_DECAY = 0.5
# 上下文重叠超过此比例即视为“高度重叠”，归入同一聚类。低于它的两次
# 观察被当作真正独立的跨来源证据，继续全额累加（验收：真实跨来源一致
# 性仍有收益）。
_CONTEXT_OVERLAP_CORRELATED = 0.5

# Likelihood ratios express evidence strength without reviving the old
# additive raw-score model.  Reliability tempers each ratio exponentially.
_POSITIVE_LR: dict[str, float] = {
    "visible_numbering": 30.0,
    "effective_numbering": 18.0,
    "explicit_heading_style": 120.0,
    "outline_level": 90.0,
    "bold": 4.0,
    "alignment": 3.0,
    "standalone_line": 30.0,
    "semantic_title": 30.0,
    "toc_destination": 150.0,
    "run_in_pattern": 10.0,
    # Heading region reported by an external layout detector (e.g. Docling);
    # as strong as visible numbering, but not structural: the reader's
    # silence can still overrule it.
    "layout_detector": 30.0,
}
_NEGATIVE_LR: dict[str, float] = {
    "document_metadata_negative": 0.0001,
    "caption_negative": 0.01,
    "non_lexical_negative": 0.02,
    "bibliography_negative": 0.03,
    "list_prose_negative": 0.12,
    "table_region_negative": 0.18,
    "printed_toc_negative": 0.08,
    "margin_negative": 0.05,
    # Strong enough to overturn an explicit heading style on its own: a styled
    # paragraph that reads as a sentence is a styling accident, and the style
    # atom (LR 120) is exactly what has to lose here.
    "styled_prose_negative": 0.05,
    # Same strength, for the failure shape analysis cannot reach: a short line
    # wearing a heading style but the body's own size and weight.  It must lose
    # to a style that is *corroborated* -- numbering or a font step alongside
    # the style still clears the bar -- and win against a style standing alone.
    "body_signature_negative": 0.05,
    # The layout detector read the page and did not report this block as a
    # heading: its silence, (1 - recall) / (1 - false-detection rate) with
    # recall ~0.93 on the development split, rounded up to 0.1.
    "layout_detector_negative": 0.10,
}
_DEFAULT_LEVEL_PRIOR: dict[HeadingLabel, float] = {
    "L1": 0.42,
    "L2": 0.25,
    "L3": 0.14,
    "L4": 0.09,
    "L5": 0.06,
    "L6": 0.04,
}
_LEVEL_LABELS: tuple[HeadingLabel, ...] = (
    "L1", "L2", "L3", "L4", "L5", "L6",
)
_DETERMINISTIC_PROMOTION_KINDS = {
    "visible_numbering",
    "effective_numbering",
    "explicit_heading_style",
    "outline_level",
    "toc_destination",
    "font_ratio",
}


def _font_likelihood_ratio(atom: EvidenceAtom) -> float:
    observed = atom.observed_value
    ratio = observed.get("ratio", 1.0) if isinstance(observed, dict) else observed
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return 1.0
    if value >= 1.5:
        return 35.0
    if value >= 1.25:
        return 25.0
    if value >= 1.10:
        return 4.0
    if value <= 0.75:
        return 0.65
    return 1.0


def evidence_likelihood_ratio(atom: EvidenceAtom) -> float:
    """Return the reliability-tempered likelihood ratio for one atom."""
    if atom.kind == "font_ratio":
        base = _font_likelihood_ratio(atom)
    elif atom.polarity > 0:
        base = _POSITIVE_LR.get(atom.kind, 1.0)
    else:
        base = _NEGATIVE_LR.get(atom.kind, 1.0)
    return base ** atom.reliability


def deduplicate_evidence(evidence: Iterable[EvidenceAtom]) -> list[EvidenceAtom]:
    """Keep one strongest observation per correlation group.

    Provider metadata often exposes the same fact through multiple surfaces
    (for example heading style *and* outline level, or visible numbering and
    effective numbering).  Keeping both atoms would double-count a single
    cause.  Ties are deterministic by evidence ID.
    """
    strongest: dict[str, tuple[float, EvidenceAtom]] = {}
    for atom in evidence:
        lr = evidence_likelihood_ratio(atom)
        impact = abs(math.log(max(lr, 1e-12)))
        previous = strongest.get(atom.correlation_group)
        if previous is None or (impact, atom.evidence_id) > (
            previous[0], previous[1].evidence_id,
        ):
            strongest[atom.correlation_group] = (impact, atom)
    return sorted(
        (entry[1] for entry in strongest.values()),
        key=lambda atom: (atom.anchor.order_key, atom.kind, atom.evidence_id),
    )


def heading_probability(
    evidence: Iterable[EvidenceAtom],
    *,
    prior: float = 0.05,
) -> float:
    """Fuse independent evidence families into a local heading probability."""
    atoms = deduplicate_evidence(evidence)
    prior = min(max(prior, 1e-6), 1.0 - 1e-6)
    log_odds = math.log(prior / (1.0 - prior))
    log_odds += math.fsum(
        math.log(max(evidence_likelihood_ratio(atom), 1e-12))
        for atom in atoms
    )
    if log_odds >= 0:
        probability = 1.0 / (1.0 + math.exp(-log_odds))
    else:
        exp_value = math.exp(log_odds)
        probability = exp_value / (1.0 + exp_value)
    return min(max(probability, 0.0), 1.0)


def level_probabilities(
    probability: float,
    evidence: Iterable[EvidenceAtom],
) -> dict[HeadingLabel, float]:
    """Build a normalized ``NONE/L1..L6`` local emission distribution."""
    atoms = deduplicate_evidence(evidence)
    log_scores = {
        label: math.log(_DEFAULT_LEVEL_PRIOR[label]) for label in _LEVEL_LABELS
    }
    for atom in atoms:
        if atom.polarity < 0 or not atom.level_likelihoods:
            continue
        for label in _LEVEL_LABELS:
            likelihood = atom.level_likelihoods.get(label, 1e-4)
            log_scores[label] += atom.reliability * math.log(max(likelihood, 1e-8))

    maximum = max(log_scores.values())
    weights = {label: math.exp(value - maximum) for label, value in log_scores.items()}
    total = math.fsum(weights.values())
    conditional = {label: weights[label] / total for label in _LEVEL_LABELS}

    result: dict[HeadingLabel, float] = {"NONE": 1.0 - probability}
    for label in _LEVEL_LABELS:
        result[label] = probability * conditional[label]
    # Eliminate the tiny floating drift rejected by the schema invariant.
    result["L6"] += 1.0 - math.fsum(result.values())
    return result


def promotion_probability(
    probability: float,
    evidence: Iterable[EvidenceAtom],
    region_risk: RegionRisk,
) -> float:
    """Estimate deterministic promotion support when no LLM vote exists.

    Promotion is a proposal emission, never a post-hoc insertion.  In
    particular, font scale and bold are independent physical causes: a short
    bold line that is only moderately larger than body text can be a genuine
    swallowed heading even when it has the same size as a confirmed sibling.
    The previous multiplicative projection drove that compound signal below
    0.5 and made the global decoder incapable of selecting it.
    """
    atoms = deduplicate_evidence(evidence)
    deterministic = [
        atom.reliability for atom in atoms
        if atom.polarity > 0 and atom.kind in _DETERMINISTIC_PROMOTION_KINDS
    ]
    support = max(deterministic, default=0.35)
    support_factor = 0.55 + 0.45 * support
    contamination_factor = 1.0 - 0.75 * region_risk.contamination_probability
    projected = probability * support_factor * contamination_factor

    # Structural floors are allowed only in the absence of explicit negative
    # evidence.  This prevents bold/large captions, TOC echoes and table or
    # margin artifacts from bypassing their typed rejection evidence.
    has_negative = any(atom.polarity < 0 for atom in atoms)
    if not has_negative:
        font_ratios = []
        positive_kinds = {
            atom.kind for atom in atoms if atom.polarity > 0
        }
        for atom in atoms:
            if atom.polarity <= 0 or atom.kind != "font_ratio":
                continue
            observed = atom.observed_value
            value = observed.get("ratio", 1.0) if isinstance(observed, dict) else observed
            try:
                font_ratios.append(float(value))
            except (TypeError, ValueError):
                continue
        largest_ratio = max(font_ratios, default=1.0)
        structural_floor = 0.0
        if largest_ratio >= 1.25:
            structural_floor = 0.70
        elif largest_ratio >= 1.10 and "bold" in positive_kinds:
            structural_floor = 0.64
        projected = max(
            projected,
            structural_floor * contamination_factor,
        )

    return min(max(projected, 0.0), 1.0)


def evidence_diversity(evidence: Iterable[EvidenceAtom]) -> int:
    """Count independent positive evidence families for deterministic ties."""
    return len({
        atom.correlation_group for atom in deduplicate_evidence(evidence)
        if atom.polarity > 0
    })


# ══════════════════════════════════════════════════════════════════════
# A04 · 相关重复投票降权（control repeated-vote inflation）
# ══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class VoteView:
    """A model vote reduced to just what decides whether two votes correlate.

    这是 `global_inference` 里 `LLMAnchorVote` 与评测面板的最小公共视图：
    只保留“同不同源”所需的字段，聚类逻辑因此可被两侧共享、被单独测试，
    而不必依赖具体投票类型。``model`` / ``prompt_version`` 缺省相同 —— 现网
    单模型单提示词，重叠窗口默认全部相关；一旦接入多模型/多提示词，只需
    在构造 VoteView 时填入真实标识即可自动分簇。
    """

    key: object
    confidence: float
    model: str = "default"
    prompt_version: str = "default"
    window_index: int = 0
    context_span: tuple[int, int] | None = None


def _context_overlap(left: tuple[int, int] | None, right: tuple[int, int] | None) -> float:
    """Jaccard overlap of two half-open context spans (0 when either is absent)."""
    if left is None or right is None:
        return 0.0
    lo = max(left[0], right[0])
    hi = min(left[1], right[1])
    intersection = max(0, hi - lo)
    if intersection == 0:
        return 0.0
    union = (left[1] - left[0]) + (right[1] - right[0]) - intersection
    return intersection / union if union > 0 else 0.0


def cluster_correlated_votes(votes: Sequence[VoteView]) -> list[list[int]]:
    """Group vote indices that are NOT mutually independent evidence.

    两张票同源，当且仅当同模型且同提示词版本，且要么同窗口、要么上下文
    重叠超过阈值。返回的每个子列表是一个相关簇的原始下标，按簇内首次
    出现顺序排列（供降增益按次序施加）。真正独立的跨来源票各自成簇，
    因此本函数对“完全独立的证据集”是恒等分组，不改变既有行为。

    并查集实现，O(n^2) 足够——单文档投票数以十计，且此处清晰性优先。
    """
    parent = list(range(len(votes)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(len(votes)):
        for j in range(i + 1, len(votes)):
            left, right = votes[i], votes[j]
            if left.model != right.model or left.prompt_version != right.prompt_version:
                continue
            same_window = left.window_index == right.window_index
            overlap = _context_overlap(left.context_span, right.context_span)
            if same_window or overlap >= _CONTEXT_OVERLAP_CORRELATED:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for index in range(len(votes)):
        clusters.setdefault(find(index), []).append(index)
    # 按每簇最小下标排序，稳定可复现。
    return [clusters[root] for root in sorted(clusters)]


def correlated_vote_gains(votes: Sequence[VoteView]) -> list[float]:
    """Per-vote gain multiplier in [0, 1] after correlation down-weighting.

    保留每一张原始投票（用于解释），但对同一相关簇内的票按置信度降序
    施加几何衰减增益 ``GAIN_DECAY**rank``：簇内最强的一票拿满额 1.0，其后
    每张按 rank 递减。独立票单独成簇、rank=0、增益恒为 1.0。

    关键性质（A04 验收）：一个簇的增益之和 <= 1/(1-decay)，与簇内票数无关
    ——**重复提供相同窗口无法让结论无依据变强**；而不同源的票分属不同簇，
    各自满额，**真实跨来源一致性仍有收益**。
    """
    gains = [1.0] * len(votes)
    for cluster in cluster_correlated_votes(votes):
        ordered = sorted(cluster, key=lambda idx: -votes[idx].confidence)
        for rank, index in enumerate(ordered):
            gains[index] = _CORRELATED_VOTE_GAIN_DECAY ** rank
    return gains


# ══════════════════════════════════════════════════════════════════════
# A04 · 置信度可靠性度量（reliability curve / ECE / Brier）
# ══════════════════════════════════════════════════════════════════════
#
# 算法实现落在这里；评测面板（另一个 agent 接）只需调用下列函数并读取
# 其 dataclass 字段。全部为纯函数，无第三方依赖（ECE/Brier 手算），可脱离
# 管线单独单测。三类置信度（存在 / 层级 / 对齐）用同一套函数分别校准 ——
# 传入各自的 (confidence, correct) 序列即可，不共享分箱状态。


@dataclass(frozen=True)
class ReliabilityBin:
    """One bin of a reliability (calibration) curve."""

    lower: float          # 分箱下界（含）
    upper: float          # 分箱上界（分箱右开，末箱含 1.0）
    count: int            # 落入本箱的样本数
    mean_confidence: float  # 本箱预测置信度均值（曲线横坐标）
    mean_accuracy: float    # 本箱实际正确率（曲线纵坐标）


@dataclass(frozen=True)
class CalibrationReport:
    """Calibration quality of one confidence channel.

    ``bins`` 直接画可靠性曲线（对角线 = 完美校准）；``ece`` 是各箱
    |confidence-accuracy| 按样本量加权的期望校准误差；``brier`` 是均方
    概率误差；``accuracy_at_threshold`` 是“按阈值接受后”的实测正确率，
    对应验收里“阈值在训练/开发集选择，测试集只验收”的那条曲线。
    """

    channel: str
    sample_count: int
    ece: float
    brier: float
    mean_confidence: float
    mean_accuracy: float
    bins: tuple[ReliabilityBin, ...] = ()
    accuracy_at_threshold: dict[float, float] = field(default_factory=dict)


def reliability_curve(
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """Bin (confidence, correctness) pairs into an equal-width reliability curve.

    等宽分箱于 [0, 1]；空箱被跳过（不产出零样本箱）。``correct[i]`` 为该
    预测是否命中真值（存在：是否真为标题；层级：层级是否正确；对齐：是否
    锚到真实块）。
    """
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for probability, hit in zip(confidences, correct):
        clamped = min(max(float(probability), 0.0), 1.0)
        index = min(int(clamped * n_bins), n_bins - 1)
        buckets[index].append((clamped, bool(hit)))

    curve: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        confidence_values = [value for value, _hit in bucket]
        hits = [1.0 if hit else 0.0 for _value, hit in bucket]
        curve.append(ReliabilityBin(
            lower=index / n_bins,
            upper=(index + 1) / n_bins,
            count=len(bucket),
            mean_confidence=math.fsum(confidence_values) / len(bucket),
            mean_accuracy=math.fsum(hits) / len(bucket),
        ))
    return curve


def expected_calibration_error(
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    n_bins: int = 10,
) -> float:
    """Sample-weighted mean gap between confidence and accuracy across bins."""
    total = len(confidences)
    if total == 0:
        return 0.0
    return math.fsum(
        (bin_.count / total) * abs(bin_.mean_confidence - bin_.mean_accuracy)
        for bin_ in reliability_curve(confidences, correct, n_bins=n_bins)
    )


def brier_score(
    confidences: Sequence[float],
    correct: Sequence[bool],
) -> float:
    """Mean squared error between confidence and the 0/1 outcome."""
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    if not confidences:
        return 0.0
    return math.fsum(
        (min(max(float(p), 0.0), 1.0) - (1.0 if hit else 0.0)) ** 2
        for p, hit in zip(confidences, correct)
    ) / len(confidences)


def accuracy_after_threshold(
    confidences: Sequence[float],
    correct: Sequence[bool],
    thresholds: Sequence[float],
) -> dict[float, float]:
    """Accuracy among predictions accepted at each threshold (>= threshold).

    验收面板用它画“按阈值接受后的错误率”。无样本越过阈值时该阈值记 0.0。
    """
    result: dict[float, float] = {}
    for threshold in thresholds:
        accepted = [
            (1.0 if hit else 0.0)
            for p, hit in zip(confidences, correct)
            if float(p) >= threshold
        ]
        result[float(threshold)] = (
            math.fsum(accepted) / len(accepted) if accepted else 0.0
        )
    return result


def calibrate_channel(
    channel: str,
    confidences: Sequence[float],
    correct: Sequence[bool],
    *,
    n_bins: int = 10,
    thresholds: Sequence[float] = (0.5, 0.7, 0.9),
) -> CalibrationReport:
    """Full calibration report for ONE confidence channel.

    存在 / 层级 / 对齐三类置信度各调用一次本函数，互不共享分箱 —— 这就是
    验收要求的“分别校准”。返回值全部为可序列化字段，评测面板直接接。
    """
    bins = reliability_curve(confidences, correct, n_bins=n_bins)
    n = len(confidences)
    mean_conf = (
        math.fsum(min(max(float(p), 0.0), 1.0) for p in confidences) / n
        if n else 0.0
    )
    mean_acc = (
        math.fsum(1.0 for hit in correct if hit) / n if n else 0.0
    )
    return CalibrationReport(
        channel=channel,
        sample_count=n,
        ece=expected_calibration_error(confidences, correct, n_bins=n_bins),
        brier=brier_score(confidences, correct),
        mean_confidence=mean_conf,
        mean_accuracy=mean_acc,
        bins=tuple(bins),
        accuracy_at_threshold=accuracy_after_threshold(
            confidences, correct, thresholds,
        ),
    )


__all__ = [
    "CALIBRATION_VERSION",
    "PROBABILITY_QUALITY",
    "evidence_likelihood_ratio",
    "deduplicate_evidence",
    "heading_probability",
    "level_probabilities",
    "promotion_probability",
    "evidence_diversity",
    # A04 vote-correlation
    "VoteView",
    "cluster_correlated_votes",
    "correlated_vote_gains",
    # A04 calibration
    "ReliabilityBin",
    "CalibrationReport",
    "reliability_curve",
    "expected_calibration_error",
    "brier_score",
    "accuracy_after_threshold",
    "calibrate_channel",
]
