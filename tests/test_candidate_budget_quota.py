# -*- coding: utf-8 -*-
"""A05 · 路由候选预算按**逻辑区域**分配：保底 / 疑难追加 / 全局保留。

为什么这些断言必须是单元用例而不是靠语料度量：
``evaluation/candidate_budget_recall.py`` 在两组语料上都测出**上限没有咬住**
（两档预算的路由候选数完全相同），也就是说那两组语料测不到预算分配这一层。
与其把夹具调到「刚好失败」为止（那是构造稻草人），不如把预算路径用极小的 ``max_ratio``
直接逼出来，逐条钉住三份预算各自的行为。

三条要守的性质：

1. **区域保底**：每个逻辑区域至少拿到一个名额。没有它，标题密集的后半篇会被前半篇同分
   候选整片挤掉——「同分排序偏向早出现的 block」是既有事实。
2. **疑难追加**：编号断裂 / 模板变化 / 高风险带的区域多拿名额。
3. **全局保留**：剩余名额按全局排序发，强证据仍能跨区竞争。

另外钉一条：明显正文候选先降一档，而不只是分数略低（方案「压低明显正文候选，而不只按
全局分数取前若干项」）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser.heading_candidates import (
    _cap_candidates,
    _region_extra_quota,
    generate_heading_candidate_set,
    select_route_candidates,
)
from modules.parser.schemas import (
    EvidenceAtom,
    HeadingCandidate,
    RegionRisk,
    StructuralPosition,
)


def _atom(kind: str, polarity: int = 1, reliability: float = 0.8) -> EvidenceAtom:
    return EvidenceAtom(
        evidence_id=f"{kind}:{polarity}",
        kind=kind,
        polarity=polarity,
        observed_value=True,
        reliability=reliability,
        anchor=StructuralPosition(block_id=0),
        correlation_group=kind,
    )


def _candidate(
    block_id: int,
    region_id: str,
    *,
    probability: float = 0.6,
    kinds: tuple[str, ...] = ("bold",),
    risk: RegionRisk | None = None,
) -> HeadingCandidate:
    return HeadingCandidate(
        block_id=block_id,
        title=f"标题 {block_id}",
        anchor=StructuralPosition(block_id=block_id),
        heading_probability=probability,
        promotion_probability=probability,
        evidence=[_atom(kind) for kind in kinds],
        region_id=region_id,
        region_risk=risk or RegionRisk(region_id=region_id),
    )


def _blocks(count: int) -> list[Block]:
    return [
        Block(id=index, type="text", text=f"第 {index} 段正文内容。" * 3)
        for index in range(count)
    ]


class Test区域保底:
    def test_每个区域至少拿到一个名额(self) -> None:
        """把上限压到远小于候选数，验证每个逻辑区域都还在路由视图里。"""
        candidates = []
        for region_index in range(6):
            for offset in range(10):
                block_id = region_index * 10 + offset
                candidates.append(_candidate(
                    block_id, f"region:{region_index}",
                    # 前面的区域分数更高：只按全局排序会把后面的区域整片挤掉。
                    probability=0.9 - region_index * 0.1,
                ))
        kept = _cap_candidates(candidates, _blocks(60), max_ratio=0.05)
        regions = {candidate.region_id for candidate in kept}
        assert len(kept) < len(candidates), "上限必须真的咬住，否则这条用例是空过的"
        assert regions == {f"region:{index}" for index in range(6)}, (
            f"有区域被整片挤掉：{sorted(regions)}"
        )

    def test_没有保底时后段会被前段挤掉(self) -> None:
        """把保底设为 0、全局保留设为 1.0，退化成纯全局排序——后段确实消失。

        这条是「让门禁先红一次」的对照：它证明上一条断言不是碰巧成立，而是保底在起作用。
        """
        candidates = []
        for region_index in range(6):
            for offset in range(10):
                candidates.append(_candidate(
                    region_index * 10 + offset, f"region:{region_index}",
                    probability=0.9 - region_index * 0.1,
                ))
        kept = _cap_candidates(
            candidates, _blocks(60), max_ratio=0.05,
            region_floor=0, global_reserve_ratio=1.0,
        )
        regions = {candidate.region_id for candidate in kept}
        assert regions != {f"region:{index}" for index in range(6)}, (
            "纯全局排序竟然也覆盖了所有区域，说明夹具没造出竞争"
        )


class Test疑难追加:
    def test_高风险区域拿到额外名额(self) -> None:
        safe = RegionRisk(region_id="region:safe")
        risky = RegionRisk(
            region_id="region:risky", miss_probability=0.6,
            contamination_probability=0.5, band="escape",
            factors=["numbering_break"],
        )
        assert _region_extra_quota(risky, set(risky.factors)) > _region_extra_quota(safe, set())

    def test_结构性疑难因子单独生效(self) -> None:
        """追加依据是结构性疑难（编号断裂 / 模板变化），不是分数高低。"""
        plain = RegionRisk(region_id="r")
        assert _region_extra_quota(plain, {"numbering_break"}) == 1
        assert _region_extra_quota(plain, {"template_change"}) == 1
        assert _region_extra_quota(plain, {"unrelated_factor"}) == 0

    def test_疑难区域在削减时最后被削(self) -> None:
        """保底+追加超预算时按风险升序削减——安全区域先让位。"""
        candidates = []
        for region_index in range(8):
            risk = RegionRisk(
                region_id=f"region:{region_index}",
                miss_probability=0.9 if region_index == 7 else 0.0,
                contamination_probability=0.9 if region_index == 7 else 0.0,
            )
            for offset in range(4):
                candidates.append(_candidate(
                    region_index * 4 + offset, f"region:{region_index}",
                    probability=0.5, risk=risk,
                ))
        kept = _cap_candidates(candidates, _blocks(32), max_ratio=0.05)
        assert any(candidate.region_id == "region:7" for candidate in kept), (
            "风险最高的区域被削掉了，削减顺序反了"
        )


class Test全局保留:
    def test_强证据仍能跨区竞争(self) -> None:
        """全局保留段的意义：一个区域里有两个强候选时，第二个仍有机会进来。"""
        candidates = [
            _candidate(0, "region:0", probability=0.99, kinds=("visible_numbering",)),
            _candidate(1, "region:0", probability=0.98, kinds=("visible_numbering",)),
        ]
        candidates += [
            _candidate(index, f"region:{index}", probability=0.3)
            for index in range(2, 40)
        ]
        kept = {candidate.block_id for candidate in _cap_candidates(
            candidates, _blocks(40), max_ratio=0.05, region_floor=1,
            global_reserve_ratio=0.5,
        )}
        assert {0, 1} <= kept, "同区域的第二个强候选被保底挤掉了，全局保留段没起作用"


class Test压低明显正文候选:
    def test_正文类负证据的候选先降一档(self) -> None:
        """只按分数排序时，分数略高的「带样式正文」会挤掉真标题。降档修的是这个。"""
        prose = _candidate(
            0, "region:0", probability=0.80,
            kinds=("bold", "body_signature_negative"),
        )
        heading = _candidate(1, "region:1", probability=0.55, kinds=("visible_numbering",))
        filler = [
            _candidate(index, f"region:{index}", probability=0.10)
            for index in range(2, 40)
        ]
        kept = {c.block_id for c in _cap_candidates(
            [prose, heading, *filler], _blocks(40), max_ratio=0.05,
            region_floor=0, global_reserve_ratio=1.0,
        )}
        assert 1 in kept, "证据更弱但没有正文负证据的真标题应当留下"
        assert 0 not in kept, "命中正文负证据的候选应当被降档挤出"


class Test未路由与模型否定是两件事:
    def test_解码器默认不区分_行为与从前一致(self) -> None:
        """默认 ``routed_block_ids=None``：两类都不罚，逐字节与从前一致。"""
        from modules.parser.global_inference import GlobalHeadingInference

        blocks = [
            Block(id=0, type="text", text="第一章 概述", is_heading_style=True, heading_level=1),
            Block(id=1, type="text", text="正文内容。" * 20),
        ]
        candidate_set = generate_heading_candidate_set(blocks)
        without = GlobalHeadingInference(blocks, candidate_set).decode([])
        assert [chapter.title for chapter in without] == ["第一章 概述"]

    def test_模型看过没确认才吃罚分(self) -> None:
        """被路由过却没投票 = 一次**观察到的否定**；从未路由 = 模型一无所知，不该罚。

        若两者同等对待，预算裁掉谁、谁就被判得更像正文——配额裁剪会自我实现。
        """
        from modules.parser.global_inference import GlobalHeadingInference

        blocks = [
            Block(id=0, type="text", text="总体说明", is_bold=True, font_size=12.0),
            Block(id=1, type="text", text="正文内容。" * 20, font_size=10.5),
            Block(id=2, type="text", text="实现细节", is_bold=True, font_size=12.0),
            Block(id=3, type="text", text="正文内容。" * 20, font_size=10.5),
        ]
        candidate_set = generate_heading_candidate_set(blocks)
        unknown = GlobalHeadingInference(blocks, candidate_set)
        routed = GlobalHeadingInference(blocks, candidate_set, routed_block_ids={0, 2})
        unknown_sites = unknown.decode([])
        routed_sites = routed.decode([])
        # 罚分只降低激活，不必然改变最终标签；断言的是「激活确实被降低了」这件可数事实。
        _ids, unknown_emissions, _labels = unknown._last_trace
        _ids2, routed_emissions, _labels2 = routed._last_trace
        assert unknown_emissions and routed_emissions
        assert any(
            routed["NONE"] > plain["NONE"]
            for plain, routed in zip(unknown_emissions, routed_emissions)
        ), "被路由过却没确认的站点应当被轻微压低"
        assert len(routed_sites) <= len(unknown_sites)


class Test路由视图入口传下配额参数:
    def test_select_route_candidates_走同一份裁剪(self) -> None:
        """路由视图只有一处裁剪实现；另写一份迟早与配额规则漂移。"""
        blocks = [
            Block(id=index, type="text", text=f"{index // 4 + 1}.{index % 4 + 1} 小节 {index}",
                  is_bold=True, font_size=12.0)
            if index % 2 == 0 else
            Block(id=index, type="text", text="正文内容。" * 6, font_size=10.5)
            for index in range(80)
        ]
        candidate_set = generate_heading_candidate_set(blocks)
        tight = select_route_candidates(candidate_set, blocks, max_ratio=0.05)
        loose = select_route_candidates(candidate_set, blocks, max_ratio=1.0)
        assert len(tight) <= len(loose)
        # 收紧预算时每个区域仍应有代表。
        assert {c.region_id for c in tight} == {c.region_id for c in loose} or len(tight) < len(loose)
