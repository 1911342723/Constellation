# -*- coding: utf-8 -*-
"""A01 · 先做确定性推断，再**只**复审不确定区域。

方案 A01 的五步流程，以及本模块负责哪几步：

1. 用现有候选证据跑一次**无投票**的全局推断，保留最优标签与竞争标签分数。
   —— 由 :meth:`GlobalHeadingInference.initial_decode` 给出（同一个解码器，不是第二份）。
2. 按低分差、编号/样式冲突、可疑标题密度、目录与正文不一致、阅读顺序不确定、run-in
   信号选出复审区域。 —— 本模块 :func:`select_review_regions`。
3. 给区域分配 Token / 时间预算。 —— 本模块 :func:`allocate_budget`。
4. 新增投票作为证据喂回**同一个**全局解码器，最后只闭合一次。 —— 由 parser 编排。
5. 模型不可用时输出明确标记的确定性结果；高风险未解决区域标 ``needs_confirmation``。
   —— 由 parser 编排，标记落在 :class:`ReviewReport` 上。

## 三个不确定度必须分开衡量（方案验收原文）

「不能简单设一个 ``p > 0.9`` 就宣称可靠」。所以 :class:`SiteUncertainty` 把三件事分开存：

- ``heading_probability``：**标题是否存在**的概率（NONE 与最优 Lx 之间的对比）。
- ``level_margin``：**级别**最优与次优之间的分差。存在概率很高但级别分差很小，是一种
  完全不同的不确定——它不该被一个总分掩盖。
- 文档级的 ``tree_uncertainty``（见 :class:`ReviewPlan`）：**最终树结构**的不确定性，
  用「被判为标题的站点里、级别分差低于阈值的比例」表达。它衡量的是树形，不是单点。

## 为什么区域而不是逐点

逐个不确定站点各发一次请求，请求数会接近全量路由，A01 就白做了。相邻的不确定站点合成
一个区域（``max_gap`` 内视为同一片），一个区域一次请求；区域再带上**相邻可靠锚点**，
模型才知道这段在文档结构里的位置。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "SiteUncertainty",
    "ReviewRegion",
    "ReviewPlan",
    "ReviewReport",
    "ReviewPolicy",
    "select_review_regions",
]

#: 触发复审的信号名（与方案 A01 第 2 步逐条对应）。
SIGNAL_LOW_HEADING_MARGIN = "low_heading_margin"
SIGNAL_LOW_LEVEL_MARGIN = "low_level_margin"
SIGNAL_NUMBERING_STYLE_CONFLICT = "numbering_style_conflict"
SIGNAL_HEADING_DENSITY = "suspicious_heading_density"
SIGNAL_TOC_BODY_MISMATCH = "toc_body_mismatch"
SIGNAL_READING_ORDER_UNCERTAIN = "reading_order_uncertain"
SIGNAL_RUN_IN = "run_in_signal"
SIGNAL_REGION_RISK = "region_contamination_risk"


@dataclass(frozen=True)
class SiteUncertainty:
    """一个提案站点的初判结果与**分开衡量**的不确定度。

    Attributes:
        block_id: 站点所在块。
        label: 初判标签（``NONE`` 或 ``L1``…``L6``）。
        runner_up: 次优标签。
        heading_probability: 标题**存在**的概率（初判发射里最优 Lx 的分量）。
        heading_margin: 最优标签与次优标签的分差（存在性维度）。
        level_margin: 最优级别与次优**级别**的分差（级别维度，与上面那条不是一回事）。
        contamination: 所在区域的污染概率（区域风险，不是站点自身证据）。
        signals: 触发复审的信号集合；空集表示这个站点是可靠的。
    """

    block_id: int
    label: str
    runner_up: str
    heading_probability: float
    heading_margin: float
    level_margin: float
    contamination: float
    signals: tuple[str, ...] = ()

    @property
    def uncertain(self) -> bool:
        return bool(self.signals)


@dataclass(frozen=True)
class ReviewRegion:
    """一片要交给模型复审的区域。

    ``anchor_block_ids`` 是相邻的**可靠**锚点（初判里无信号且被判为标题的站点）。
    带上它们，模型才知道这段处在文档结构的什么位置；不带的话它只能凭局部猜层级。
    """

    region_index: int
    block_ids: tuple[int, ...]
    site_block_ids: tuple[int, ...]
    anchor_block_ids: tuple[int, ...]
    signals: tuple[str, ...]
    score: float
    token_budget: int = 0
    admitted: bool = True


@dataclass(frozen=True)
class ReviewPlan:
    """初判 + 复审计划。三个不确定度分开陈述，不合成单一「可靠度」。"""

    sites: tuple[SiteUncertainty, ...]
    regions: tuple[ReviewRegion, ...]
    tree_uncertainty: float
    total_sites: int
    uncertain_sites: int
    reliable_heading_sites: int

    @property
    def admitted_regions(self) -> tuple[ReviewRegion, ...]:
        return tuple(region for region in self.regions if region.admitted)

    @property
    def deferred_regions(self) -> tuple[ReviewRegion, ...]:
        """预算不够、本轮未复审的区域——它们要被标 ``needs_confirmation``。"""
        return tuple(region for region in self.regions if not region.admitted)


@dataclass
class ReviewReport:
    """一次选择性复审的可观测记录（供交付层报 warnings / 面板统计）。

    刻意**只报可数事实**：调用了几次、估了多少 token、哪些区域没复审。质量提升不在
    这里报——方案验收明确「没有真实模型语料就不要宣称质量提升」。
    """

    mode: str = "selective"
    total_sites: int = 0
    uncertain_sites: int = 0
    regions_planned: int = 0
    regions_reviewed: int = 0
    regions_deferred: int = 0
    model_calls: int = 0
    prompt_chars: int = 0
    tree_uncertainty: float = 0.0
    model_available: bool = True
    needs_confirmation_block_ids: list[int] = field(default_factory=list)
    signals: dict[str, int] = field(default_factory=dict)

    this_run_model_calls: int = 0
    cached_model_calls: int = 0

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "total_sites": self.total_sites,
            "uncertain_sites": self.uncertain_sites,
            "regions_planned": self.regions_planned,
            "regions_reviewed": self.regions_reviewed,
            "regions_deferred": self.regions_deferred,
            "model_calls": self.model_calls,
            "this_run_model_calls": self.this_run_model_calls,
            "cached_model_calls": self.cached_model_calls,
            "prompt_chars": self.prompt_chars,
            "tree_uncertainty": self.tree_uncertainty,
            "model_available": self.model_available,
            "needs_confirmation_block_ids": list(self.needs_confirmation_block_ids),
            "signals": dict(self.signals),
        }


@dataclass(frozen=True)
class ReviewPolicy:
    """选区与预算参数。

    两条分差阈值是**按实测分布定的，不是拍的**。在合成夹具与排版扰动集上量到的分布是
    双峰的：

    - 级别分差：有真实层级证据（编号体系）的站点 ≈ 0.97；完全没有层级证据的站点
      ≈ 0.25（六个层级的条件分布近乎均匀）。两峰之间没有样本，所以阈值取 0.35——
      「没有层级证据」正是最该交给模型的一类，取 0.20 会把它整片漏掉。
    - 存在性分差：NONE 占优的正文段 0.62~0.81，带编号的真标题 0.85，而「加粗且略大字号」
      这种真正两难的站点 0.25~0.37。阈值取 0.40。

    Attributes:
        heading_margin_threshold: 存在性分差低于它即算不确定。
        level_margin_threshold: 级别分差低于它即算不确定。
        contamination_threshold: 区域污染概率高于它即算不确定。
        density_window: 判定「可疑标题密度」的滑窗**块**数（不是站点数）。
        density_threshold: 滑窗内标题数超过它即算密度可疑。
        max_gap: 相邻不确定站点在多少块以内合成同一区域。
        context_blocks: 区域两侧各带多少块局部内容。
        max_regions: 一篇文档最多复审多少个区域（文档级预算）。
        region_token_budget: 单区域 token 预算（按字符估，1 token≈2 中文字符）。
        document_token_budget: 文档级 token 总预算；0 = 不限。
    """

    heading_margin_threshold: float = 0.40
    level_margin_threshold: float = 0.35
    contamination_threshold: float = 0.35
    density_window: int = 6
    density_threshold: int = 4
    max_gap: int = 8
    context_blocks: int = 2
    max_regions: int = 8
    region_token_budget: int = 1500
    document_token_budget: int = 8000


def _margins(emission: Mapping[str, float]) -> tuple[str, str, float, float, float]:
    """从一个站点的发射分布里取出**分开衡量**的三个量。

    返回 ``(最优标签, 次优标签, 存在性分差, 级别分差, 标题存在概率)``。

    两个分差的分母都取「参与比较的那两项之和」，因此都是无标度的成对分差，可直接与
    阈值比。关键是**比谁和谁**：

    - **存在性分差**比的是「最优层级」与 ``NONE``。这是「它到底是不是标题」这一问。
    - **级别分差**比的是「最优层级」与「次优层级」，刻意不看 ``NONE``。

    第一版把存在性分差写成「最优标签 vs 次优标签」，结果正好反了：发射里
    ``L1..L6`` 都按 ``heading_probability × 相对层级支持度`` 给分，层级证据分散但存在性
    确定的站点（干净文档的标题）会有两个层级几乎同分，于是被判成「存在性不确定」；
    而 bold-everything 那种 ``NONE`` 压倒一切的站点反倒被判成「确定」。在排版扰动集上
    的表现是 clean 文档 10/10 站点全不确定、bold_everything 0/21 不确定——与常识相反，
    这正是这条定义写错的可观测症状。
    """
    ordered = sorted(emission.items(), key=lambda item: item[1], reverse=True)
    best_label = ordered[0][0]
    runner_label = ordered[1][0] if len(ordered) > 1 else "NONE"

    levels = sorted(
        ((label, value) for label, value in emission.items() if label != "NONE"),
        key=lambda item: item[1],
        reverse=True,
    )
    none_value = max(float(emission.get("NONE", 0.0)), 0.0)
    best_level_value = max(float(levels[0][1]), 0.0) if levels else 0.0
    existence_total = best_level_value + none_value
    heading_margin = (
        abs(best_level_value - none_value) / existence_total if existence_total > 0 else 0.0
    )
    if len(levels) >= 2:
        top, second = max(levels[0][1], 0.0), max(levels[1][1], 0.0)
        level_margin = (top - second) / (top + second) if (top + second) > 0 else 0.0
    else:
        level_margin = 1.0
    heading_probability = (
        best_level_value / existence_total if existence_total > 0 else 0.0
    )
    return best_label, runner_label, heading_margin, level_margin, heading_probability


def _candidate_signals(candidate, policy: ReviewPolicy) -> set[str]:
    """候选自身携带的结构性冲突信号（不依赖初判分数）。"""
    signals: set[str] = set()
    if candidate is None:
        return signals
    kinds = {atom.kind for atom in (candidate.evidence or ())}

    # 编号层级与明确样式层级同时存在且互相矛盾——这是最典型的「两种权威打架」，
    # 确定性侧只能按固定优先级夹一个，模型看局部上下文往往能判得更准。
    numbering = candidate.numbering_level
    style = candidate.style_level
    if numbering is not None and style is not None and numbering != style:
        signals.add(SIGNAL_NUMBERING_STYLE_CONFLICT)

    # 目录与正文不一致：候选被目录指向（toc_destination），却同时命中「印刷目录回声」
    # 这条负证据——说明「它是目录里的一行」和「它是正文标题」两种解释都有依据。
    if "toc_destination" in kinds and "printed_toc_negative" in kinds:
        signals.add(SIGNAL_TOC_BODY_MISMATCH)

    if "run_in_pattern" in kinds:
        signals.add(SIGNAL_RUN_IN)

    if candidate.region_risk.contamination_probability >= policy.contamination_threshold:
        signals.add(SIGNAL_REGION_RISK)
    return signals


def _block_signals(block) -> set[str]:
    metadata = getattr(block, "metadata", None) or {}
    return {SIGNAL_READING_ORDER_UNCERTAIN} if metadata.get("reading_order_uncertain") else set()


def build_uncertainties(
    site_block_ids: Sequence[int],
    emissions: Sequence[Mapping[str, float]],
    labels: Sequence[str],
    *,
    candidate_by_block: Mapping[int, object],
    block_by_id: Mapping[int, object],
    policy: ReviewPolicy,
    ordered_block_ids: Sequence[int] = (),
) -> list[SiteUncertainty]:
    """把初判的逐站点发射与标签折成 :class:`SiteUncertainty` 列表。"""
    order = list(ordered_block_ids) or sorted(block_by_id)
    position = {block_id: index for index, block_id in enumerate(order)}
    heading_block_positions = sorted(
        position.get(block_id, 0)
        for block_id, label in zip(site_block_ids, labels, strict=True)
        if label != "NONE"
    )
    # 可疑标题密度必须在**块位置**上数，不能在站点上数。站点本来就是候选位置，
    # 「连续 6 个站点里有 4 个是标题」在任何结构良好的文档里都成立——第一版正是这样
    # 写的，症状是干净文档 10/10 站点全被判不确定、过度加粗文档 0/21 不确定，与常识
    # 相反。真正可疑的是「一段正文里挤了太多标题」：正常文档标题之间隔着正文段落。
    dense_blocks: set[int] = set()
    for index, start in enumerate(heading_block_positions):
        window = [
            other for other in heading_block_positions[index:]
            if other < start + policy.density_window
        ]
        if len(window) > policy.density_threshold:
            dense_blocks.update(window)

    out: list[SiteUncertainty] = []
    for block_id, emission, label in zip(site_block_ids, emissions, labels, strict=True):
        best, runner, heading_margin, level_margin, heading_probability = _margins(emission)
        signals: set[str] = set()
        if heading_margin < policy.heading_margin_threshold:
            signals.add(SIGNAL_LOW_HEADING_MARGIN)
        # 级别分差只在「初判认为它是标题」时才有意义——判成 NONE 的站点谈级别是空谈。
        if label != "NONE" and level_margin < policy.level_margin_threshold:
            signals.add(SIGNAL_LOW_LEVEL_MARGIN)
        if position.get(block_id, -1) in dense_blocks:
            signals.add(SIGNAL_HEADING_DENSITY)
        signals |= _candidate_signals(candidate_by_block.get(block_id), policy)
        signals |= _block_signals(block_by_id.get(block_id))
        out.append(SiteUncertainty(
            block_id=block_id,
            label=label,
            runner_up=runner,
            heading_probability=heading_probability,
            heading_margin=heading_margin,
            level_margin=level_margin,
            contamination=getattr(
                getattr(candidate_by_block.get(block_id), "region_risk", None),
                "contamination_probability", 0.0,
            ) or 0.0,
            signals=tuple(sorted(signals)),
        ))
    return out


def _tree_uncertainty(sites: Sequence[SiteUncertainty], policy: ReviewPolicy) -> float:
    """最终树结构的不确定性：被判为标题的站点里级别分差偏低的比例。

    与「标题存在概率」分开报是刻意的：一篇文档可以每个标题都几乎确定存在，而层级关系
    整体模糊——那时树是不可靠的，但任何一个单点的存在概率都很高。
    """
    headings = [site for site in sites if site.label != "NONE"]
    if not headings:
        return 0.0
    shaky = sum(
        1 for site in headings if site.level_margin < policy.level_margin_threshold
    )
    return shaky / len(headings)


def _estimate_tokens(char_count: int) -> int:
    """字符 → token 的保守估算（1 token ≈ 2 个中文字符）。

    这是**估算**，不是网关计费口径；报出去时也标明是估算（与 evaluation 侧
    ``token_accounting_kind='estimated'`` 同一态度）。
    """
    return max(1, (char_count + 1) // 2)


def select_review_regions(
    sites: Sequence[SiteUncertainty],
    *,
    ordered_block_ids: Sequence[int],
    block_by_id: Mapping[int, object],
    policy: ReviewPolicy | None = None,
) -> ReviewPlan:
    """把不确定站点合成区域，并按预算准入。

    合区域的理由见模块说明：逐点各发一次请求，请求数会接近全量路由，A01 就白做了。

    预算准入是**显式**的：超出 ``max_regions`` / ``document_token_budget`` 的区域标
    ``admitted=False``，由调用方把它们的块标 ``needs_confirmation``——不是静默丢掉。
    """
    policy = policy or ReviewPolicy()
    uncertain = [site for site in sites if site.uncertain]
    reliable_headings = tuple(
        site.block_id for site in sites if site.label != "NONE" and not site.uncertain
    )
    tree_uncertainty = _tree_uncertainty(sites, policy)

    if not uncertain:
        return ReviewPlan(
            sites=tuple(sites), regions=(), tree_uncertainty=tree_uncertainty,
            total_sites=len(sites), uncertain_sites=0,
            reliable_heading_sites=len(reliable_headings),
        )

    position = {block_id: index for index, block_id in enumerate(ordered_block_ids)}
    uncertain.sort(key=lambda site: position.get(site.block_id, site.block_id))

    groups: list[list[SiteUncertainty]] = [[uncertain[0]]]
    for site in uncertain[1:]:
        previous = groups[-1][-1]
        gap = abs(position.get(site.block_id, 0) - position.get(previous.block_id, 0))
        if gap <= policy.max_gap:
            groups[-1].append(site)
        else:
            groups.append([site])

    regions: list[ReviewRegion] = []
    for index, group in enumerate(groups):
        site_ids = tuple(site.block_id for site in group)
        indices = [position.get(block_id, 0) for block_id in site_ids]
        low = max(0, min(indices) - policy.context_blocks)
        high = min(len(ordered_block_ids) - 1, max(indices) + policy.context_blocks)
        span = tuple(ordered_block_ids[low:high + 1])
        # 相邻可靠锚点：区域前后最近的各一个可靠标题站点。
        anchors: list[int] = []
        before = [b for b in reliable_headings if position.get(b, -1) < low]
        after = [b for b in reliable_headings if position.get(b, 10**9) > high]
        if before:
            anchors.append(before[-1])
        if after:
            anchors.append(after[0])
        signals = tuple(sorted({signal for site in group for signal in site.signals}))
        # 区域不确定度 = 站点数 × 平均「1 - 最小分差」。用它排序，预算优先给最模糊的。
        severity = sum(
            1.0 - min(site.heading_margin, site.level_margin) for site in group
        )
        chars = sum(
            len(getattr(block_by_id.get(block_id), "text", "") or "") for block_id in span
        )
        regions.append(ReviewRegion(
            region_index=index,
            block_ids=span,
            site_block_ids=site_ids,
            anchor_block_ids=tuple(anchors),
            signals=signals,
            score=severity,
            token_budget=min(policy.region_token_budget, _estimate_tokens(chars)),
        ))

    admitted = _admit(regions, policy)
    return ReviewPlan(
        sites=tuple(sites),
        regions=tuple(admitted),
        tree_uncertainty=tree_uncertainty,
        total_sites=len(sites),
        uncertain_sites=len(uncertain),
        reliable_heading_sites=len(reliable_headings),
    )


def _admit(regions: Sequence[ReviewRegion], policy: ReviewPolicy) -> list[ReviewRegion]:
    """按不确定度降序准入，受 ``max_regions`` 与文档级 token 预算约束。

    准入顺序按不确定度、**输出顺序按文档位置**：交付层与用例读到的区域列表始终是
    文档序，否则「第 3 个区域」这种说法在两次运行之间会漂移。
    """
    ranked = sorted(regions, key=lambda region: region.score, reverse=True)
    spent = 0
    admitted_ids: set[int] = set()
    for region in ranked:
        if len(admitted_ids) >= policy.max_regions:
            break
        if policy.document_token_budget > 0 and spent + region.token_budget > policy.document_token_budget:
            continue
        admitted_ids.add(region.region_index)
        spent += region.token_budget
    return [
        ReviewRegion(
            region_index=region.region_index,
            block_ids=region.block_ids,
            site_block_ids=region.site_block_ids,
            anchor_block_ids=region.anchor_block_ids,
            signals=region.signals,
            score=region.score,
            token_budget=region.token_budget,
            admitted=region.region_index in admitted_ids,
        )
        for region in regions
    ]


def count_signals(sites: Iterable[SiteUncertainty]) -> dict[str, int]:
    """各信号触发了多少次（供 ReviewReport / 面板统计）。"""
    counts: dict[str, int] = {}
    for site in sites:
        for signal in site.signals:
            counts[signal] = counts.get(signal, 0) + 1
    return counts
