# -*- coding: utf-8 -*-
"""A01 · 确定性初判 + 只复审不确定区域。

验收重点（方案 A01 原文）：

- 三个不确定度**分开**衡量：标题存在概率、级别分差、树结构不确定性。不能设一个
  ``p > 0.9`` 就宣称可靠，所以这里有专门的用例证明它们互不替代。
- 复审前后是显式的初判/终判，**最终决策只有一个出口**。所以有一条用例数
  ``GlobalHeadingInference.decode`` 被调了两次、且是**同一个实例**——两个实例意味着
  初判据以选区的权重与终判实际用的权重可能不同，选区就与最终决策无关了。
- 复审只能产出**投票**，绝不直接改正文、绝不直接插标题（旧式「闭合完了再到处插标题」
  是方案点名禁止的）。
- 模型不可用时输出明确标记的确定性结果，高风险未解决区域标 needs_confirmation。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser import global_inference as gi
from modules.parser.parser import CaliperParser
from modules.parser.review_regions import (
    SIGNAL_LOW_HEADING_MARGIN,
    SIGNAL_LOW_LEVEL_MARGIN,
    ReviewPolicy,
    SiteUncertainty,
    _margins,
    select_review_regions,
)
from modules.parser.schemas import ChapterNode, LLMRouterOutput


@pytest.fixture(autouse=True)
def _clear_cache():
    CaliperParser.clear_cache()
    yield
    CaliperParser.clear_cache()


def _clean_doc() -> list[Block]:
    """编号规范 + 明确标题样式：A01 的目标就是这种文档不必调模型。"""
    blocks: list[Block] = []
    for index, (title, level) in enumerate([
        ("第一章 概述", 1), ("1.1 背景", 2), ("1.2 范围", 2),
        ("第二章 设计", 1), ("2.1 架构", 2),
    ]):
        blocks.append(Block(
            id=len(blocks), type="text", text=title,
            is_heading_style=True, heading_level=level, font_size=16.0 - level,
        ))
        blocks.append(Block(
            id=len(blocks), type="text",
            text=f"这是第 {index} 节的正文内容，用于把标题隔开。" * 6,
            font_size=10.5,
        ))
    return blocks


def _ambiguous_doc() -> list[Block]:
    """加粗 + 仅略大于正文的字号 + 无编号无明确样式。

    夹具形态是**实测**挑出来的，不是想当然：同字号加粗会被解码器自信地判成正文
    （存在性分差 0.81），字号明显更大则自信地判成标题；只有「略大一点」这一档落在
    存在性分差 0.25 / 级别分差 0.25 的两难区间——那才是 A01 要交给模型的那一类。
    """
    blocks: list[Block] = []
    for index, title in enumerate(["总体说明", "实现细节", "边界条件", "后续工作"]):
        blocks.append(Block(
            id=len(blocks), type="text", text=title, is_bold=True, font_size=12.0,
        ))
        blocks.append(Block(
            id=len(blocks), type="text",
            text=f"第 {index} 段正文，字号是正文字号，没有任何编号或样式线索。" * 6,
            font_size=10.5,
        ))
    return blocks


class _CountingRouter:
    """记录调用与 prompt 长度；按需回答区域内的标题投票。"""

    def __init__(self, votes_for: dict[int, tuple[str, int]] | None = None, boom: bool = False):
        self.calls: list[str] = []
        self._votes_for = votes_for or {}
        self._boom = boom

    def __call__(self, chunk: str, *_args, **_kwargs) -> LLMRouterOutput:
        if self._boom:
            raise RuntimeError("模型不可用")
        self.calls.append(chunk)
        chapters = [
            ChapterNode(block_id=block_id, title=title, snippet=title[:20],
                        level=level, confidence=0.9)
            for block_id, (title, level) in self._votes_for.items()
            if f"[{block_id}]" in chunk
        ]
        return LLMRouterOutput(doc_title="", doc_authors="", chapters=chapters)


def _install(parser: CaliperParser, router: _CountingRouter) -> None:
    parser.router.route = router
    parser.router.route_chunk = lambda chunk, *a, **k: router(chunk)


# ── 分差定义：三个不确定度互不替代 ────────────────────────────────────────


class Test三个不确定度分开衡量:
    def test_存在性确定但级别模糊(self) -> None:
        """几乎肯定是标题、L2/L3 难分：存在性分差大、级别分差小。

        第一版把存在性分差写成「最优标签 vs 次优标签」，这种站点会被误判成
        「存在性不确定」——因为次优标签是另一个层级。
        """
        emission = {"NONE": 0.02, "L1": 0.10, "L2": 0.98, "L3": 0.97,
                    "L4": 0.05, "L5": 0.01, "L6": 0.01}
        _best, _runner, heading_margin, level_margin, probability = _margins(emission)
        assert heading_margin > 0.9, "存在性应当很确定"
        assert level_margin < 0.05, "级别应当很模糊"
        assert probability > 0.9

    def test_存在性模糊但级别确定(self) -> None:
        """是不是标题说不清，但如果是就一定是 L1：存在性分差小、级别分差大。"""
        emission = {"NONE": 0.50, "L1": 0.52, "L2": 0.03, "L3": 0.01,
                    "L4": 0.01, "L5": 0.01, "L6": 0.01}
        _best, _runner, heading_margin, level_margin, _p = _margins(emission)
        assert heading_margin < 0.1
        assert level_margin > 0.8

    def test_两条阈值各自触发各自的信号(self) -> None:
        policy = ReviewPolicy()
        certain_level_vague = SiteUncertainty(
            block_id=0, label="L2", runner_up="L3",
            heading_probability=0.98, heading_margin=0.95, level_margin=0.01,
            contamination=0.0, signals=(SIGNAL_LOW_LEVEL_MARGIN,),
        )
        assert certain_level_vague.uncertain
        plan = select_review_regions(
            [certain_level_vague], ordered_block_ids=[0], block_by_id={}, policy=policy,
        )
        assert plan.uncertain_sites == 1
        # 树结构不确定度只看「被判为标题且级别分差低」的比例——它不是前两者的平均。
        assert plan.tree_uncertainty == 1.0

    def test_树结构不确定度与单点存在概率无关(self) -> None:
        """每个标题都几乎肯定存在，但层级整体模糊——树是不可靠的，单点却都很确定。"""
        sites = [
            SiteUncertainty(block_id=index, label="L2", runner_up="L3",
                            heading_probability=0.99, heading_margin=0.98,
                            level_margin=0.01, contamination=0.0,
                            signals=(SIGNAL_LOW_LEVEL_MARGIN,))
            for index in range(4)
        ]
        plan = select_review_regions(
            sites, ordered_block_ids=list(range(4)), block_by_id={},
        )
        assert plan.tree_uncertainty == 1.0
        assert all(site.heading_probability > 0.9 for site in plan.sites)


# ── 端到端：干净文档零调用 ────────────────────────────────────────────────


def test_clean_document_needs_no_model_call() -> None:
    """A01 的目标场景：编号规范 + 明确样式的文档一次模型都不调。"""
    router = _CountingRouter()
    parser = CaliperParser()
    _install(parser, router)
    tree, report = parser.parse_selective_with_report(_clean_doc())

    assert router.calls == [], f"干净文档不该调模型，实际调了 {len(router.calls)} 次"
    assert report.uncertain_sites == 0
    assert report.model_calls == 0
    assert report.regions_planned == 0
    assert [node.title for node in tree.nodes][:1] == ["第一章 概述"]


def test_clean_document_matches_the_deterministic_result() -> None:
    """零调用时的结果必须与显式零模型模式逐节一致——两条路走的是同一个解码器。"""
    parser = CaliperParser()
    _install(parser, _CountingRouter())
    selective, _report = parser.parse_selective_with_report(_clean_doc())
    CaliperParser.clear_cache()
    fast = CaliperParser().parse_fast(_clean_doc())

    def shape(tree):
        out: list[tuple[str, int]] = []

        def walk(nodes):
            for node in nodes:
                out.append((node.title, node.level))
                walk(node.children)

        walk(tree.nodes)
        return out

    assert shape(selective) == shape(fast)


# ── 端到端：不确定文档只复审那一片 ────────────────────────────────────────


def test_ambiguous_document_reviews_only_the_uncertain_region() -> None:
    router = _CountingRouter()
    parser = CaliperParser()
    _install(parser, router)
    _tree, report = parser.parse_selective_with_report(_ambiguous_doc())

    assert report.uncertain_sites > 0, "无编号无样式的同字号标题应当被判为不确定"
    assert report.regions_planned >= 1
    # 一个区域一次请求：逐点各发一次会让请求数接近全量路由，A01 就白做了。
    assert len(router.calls) == report.model_calls == len(
        [r for r in range(report.regions_reviewed)]
    )
    assert report.model_calls <= report.regions_planned


def test_review_votes_reach_the_final_decode() -> None:
    """复审的产出是投票，必须影响终判——否则复审就是白烧钱。"""
    blocks = _ambiguous_doc()
    baseline = CaliperParser().parse_fast(list(blocks))
    CaliperParser.clear_cache()

    def levels(tree):
        out: list[int] = []

        def walk(nodes):
            for node in nodes:
                out.append(node.level)
                walk(node.children)

        walk(tree.nodes)
        return out

    # 让模型把后三个标题投成 L2：终判应当采纳（层级由投票 + 全局约束共同决定）。
    router = _CountingRouter(votes_for={
        0: ("总体说明", 1), 2: ("实现细节", 2), 4: ("边界条件", 2), 6: ("后续工作", 2),
    })
    parser = CaliperParser()
    _install(parser, router)
    reviewed, report = parser.parse_selective_with_report(list(blocks))

    assert report.model_calls >= 1
    assert levels(reviewed) != levels(baseline) or reviewed.to_markdown() != baseline.to_markdown()


def test_review_never_edits_body_text_directly() -> None:
    """复审只输出投票；正文由 Stage 4 唯一出口装配，字符总量不因复审而变。"""
    blocks = _ambiguous_doc()
    baseline = CaliperParser().parse_fast(list(blocks))
    CaliperParser.clear_cache()
    router = _CountingRouter(votes_for={0: ("总体说明", 1), 4: ("边界条件", 2)})
    parser = CaliperParser()
    _install(parser, router)
    reviewed, _report = parser.parse_selective_with_report(list(blocks))

    def body_chars(tree) -> int:
        return tree.get_stats()["total_content_chars"]

    # 强制闭合是无遗漏无重复的，所以两条路的正文字符总量必须一致；变的只是标题/层级。
    assert body_chars(reviewed) == body_chars(baseline)


# ── 初判与终判必须是同一个解码器 ──────────────────────────────────────────


def test_initial_and_final_decode_share_one_decoder(monkeypatch) -> None:
    """两次解码、一个实例。两个实例意味着选区权重与最终决策权重可能不同。"""
    instances: list[int] = []
    calls: list[int] = []
    real_decode = gi.GlobalHeadingInference.decode

    def counting(self, chapters):
        instances.append(id(self))
        calls.append(len(chapters))
        return real_decode(self, chapters)

    monkeypatch.setattr(gi.GlobalHeadingInference, "decode", counting)
    parser = CaliperParser()
    _install(parser, _CountingRouter())
    parser.parse_selective_with_report(_ambiguous_doc())

    assert len(calls) == 2, f"应当恰好两次解码（初判 + 终判），实际 {len(calls)}"
    assert calls[0] == 0, "初判必须是无投票的"
    assert len(set(instances)) == 1, "初判与终判必须落在同一个解码器实例上"


# ── 模型不可用 / 预算不足 ─────────────────────────────────────────────────


def test_model_unavailable_falls_back_to_marked_deterministic_result() -> None:
    blocks = _ambiguous_doc()
    baseline = CaliperParser().parse_fast(list(blocks))
    CaliperParser.clear_cache()
    parser = CaliperParser()
    _install(parser, _CountingRouter(boom=True))
    tree, report = parser.parse_selective_with_report(list(blocks))

    assert report.model_available is False, "模型不可用必须被如实标出"
    assert report.model_calls == 0
    assert report.needs_confirmation_block_ids, "未解决的区域必须标 needs_confirmation"
    assert tree.to_markdown() == baseline.to_markdown(), "应当退回确定性结果"


def test_region_budget_defers_and_marks_the_rest() -> None:
    """预算不够时超出的区域标 needs_confirmation，不是静默丢掉。"""
    parser = CaliperParser(review_policy=ReviewPolicy(max_regions=0))
    router = _CountingRouter()
    _install(parser, router)
    _tree, report = parser.parse_selective_with_report(_ambiguous_doc())

    assert report.model_calls == 0
    assert report.regions_planned >= 1
    assert report.regions_deferred == report.regions_planned
    assert report.needs_confirmation_block_ids


def test_report_only_states_countable_facts() -> None:
    """报告里没有「质量提升」这类字段——没有真实模型语料就不宣称质量变好。"""
    parser = CaliperParser()
    _install(parser, _CountingRouter())
    _tree, report = parser.parse_selective_with_report(_clean_doc())
    payload = report.as_dict()
    assert set(payload) == {
        "mode", "total_sites", "uncertain_sites", "regions_planned", "regions_reviewed",
        "regions_deferred", "model_calls", "this_run_model_calls", "cached_model_calls",
        "prompt_chars", "tree_uncertainty",
        "model_available", "needs_confirmation_block_ids", "signals",
    }
    assert not any("quality" in key or "improve" in key for key in payload)


def test_region_request_carries_adjacent_reliable_anchors() -> None:
    """区域请求必须带相邻可靠锚点，否则模型无从判断这段的结构深度。"""
    sites = [
        SiteUncertainty(block_id=0, label="L1", runner_up="NONE", heading_probability=0.99,
                        heading_margin=0.99, level_margin=0.99, contamination=0.0),
        SiteUncertainty(block_id=4, label="L2", runner_up="L3", heading_probability=0.9,
                        heading_margin=0.9, level_margin=0.01, contamination=0.0,
                        signals=(SIGNAL_LOW_LEVEL_MARGIN,)),
        SiteUncertainty(block_id=8, label="L1", runner_up="NONE", heading_probability=0.99,
                        heading_margin=0.99, level_margin=0.99, contamination=0.0),
    ]
    plan = select_review_regions(
        sites,
        ordered_block_ids=list(range(10)),
        block_by_id={index: Block(id=index, type="text", text="x") for index in range(10)},
        policy=ReviewPolicy(context_blocks=1),
    )
    assert len(plan.regions) == 1
    region = plan.regions[0]
    assert region.site_block_ids == (4,)
    # 前后各一个可靠锚点。
    assert set(region.anchor_block_ids) == {0, 8}


# ── A08 · R 层已删除，钉住这个结论 ────────────────────────────────────────


class TestA08半截接口已删除:
    """A08 的验收是「没有独立收益就关闭或**删除**未使用的半截接口」。

    删除依据（都是可查事实，不是省事）：R 层按自己的契约只能重判**已解码出的**标题，
    而记录在案的失败是候选层漏检与 Stage 1/2 负压缩，都在它权限之外；排版扰动集
    72 行已全 100%，失败集是空的；而且 A01 的复审已经走既有投票通道，再加一条
    ReviewVerdict 通道等于给「复审」造第二个权威。

    这几条用例的作用是防止它被「感觉有用」重新加回来：真要加回来，得先拿到
    「标题已解码出来、只是层级判错，且确定性修复与 A01 局部投票都解决不了」的失败集。
    """

    def test_类型确实不存在了(self) -> None:
        import modules.parser.schemas as schemas

        assert not hasattr(schemas, "ReviewVerdict")
        assert not hasattr(schemas, "ReviewAction")

    def test_解码器不再接受只写不读的参数(self) -> None:
        import inspect

        signature = inspect.signature(gi.GlobalHeadingInference.__init__)
        assert "review_verdicts" not in signature.parameters

    def test_证据种类里没有只有它会产的那一类(self) -> None:
        """``llm_review_negative`` 只有 R 层会产；权重表里那条注释还指向一个从不存在的
        ``modules/parser/review.py``——半截接口的典型痕迹，一并删掉。"""
        from modules.parser import evidence
        from modules.parser.schemas import EvidenceKind

        assert "llm_review_negative" not in getattr(EvidenceKind, "__args__", ())
        weights = getattr(evidence, "_EVIDENCE_WEIGHTS", {})
        assert "llm_review_negative" not in weights

    def test_删除理由与重新引入判据写在源码里(self) -> None:
        """结论必须留在代码旁边。写在提交说明里，下一个人读源码时看不到。"""
        source = Path(__file__).parent.parent / "modules" / "parser" / "schemas.py"
        text = source.read_text(encoding="utf-8")
        assert "A08" in text
        assert "重新引入的判据" in text
