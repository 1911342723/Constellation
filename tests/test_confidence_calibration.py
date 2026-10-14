"""A04 回归：相关重复投票降权 + 三分置信度校准入口。

两组测试：

1. **相关投票降权**——通过真实入口 ``GlobalHeadingInference`` 的发射概率
   验证「改变 window_overlap 或重复提供相同窗口不应让结论无依据变强」，
   同时「真实跨来源一致性仍有收益」。不 mock 调用方：直接构造 chapter
   的投票并读 ``_emission`` 的存在概率（``1 - NONE``），这是解码器真正
   消费投票的地方。
2. **校准入口**——可靠性曲线 / ECE / Brier / 按阈值接受后准确率的手算实现，
   存在/层级/对齐三个通道各自独立校准，签名稳定供评测面板接入。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser.evidence import (
    CalibrationReport,
    ReliabilityBin,
    VoteView,
    accuracy_after_threshold,
    brier_score,
    calibrate_channel,
    cluster_correlated_votes,
    correlated_vote_gains,
    expected_calibration_error,
    reliability_curve,
)
from modules.parser.global_inference import GlobalHeadingInference
from modules.parser.heading_candidates import generate_heading_candidate_set
from modules.parser.schemas import ChapterNode, LLMAnchorVote


# ── 通过真实解码入口读一个站点的存在概率 ─────────────────────────

def _site_existence_probability(block: Block, votes: list[LLMAnchorVote]) -> float:
    """Build one heading site with the given votes and read its emission.

    走的是解码器自己的路径：``_build_sites`` -> ``_emission``，投票经完整的
    相关性降权后累加。不 mock 任何调用方。
    """
    body = [
        Block(id=100 + i, type="text", text=f"Body sentence number {i} here.",
              font_size=11.0)
        for i in range(4)
    ]
    blocks = [block, *body]
    candidate_set = generate_heading_candidate_set(blocks)
    inference = GlobalHeadingInference(blocks, candidate_set)
    chapter = ChapterNode(
        block_id=block.id,
        title=block.text,
        snippet=block.text[:40],
        level=1,
        confidence=votes[0].confidence if votes else 0.9,
    )
    chapter.anchor_votes = votes
    sites = inference._build_sites([chapter])
    site = next(s for s in sites if s.block_id == block.id)
    emission = inference._emission(site)
    return 1.0 - emission["NONE"]


def _vote(window_index: int, confidence: float = 0.9) -> LLMAnchorVote:
    return LLMAnchorVote(
        raw_block_id=0,
        aligned_block_id=0,
        title="Shared Heading",
        snippet="Shared Heading",
        level=1,
        confidence=confidence,
        alignment_score=1.0,
        out_of_candidate=False,
        window_index=window_index,
    )


def _heading_block() -> Block:
    return Block(id=0, type="text", text="Shared Heading", font_size=18.0, is_bold=True)


def _neutral_block() -> Block:
    """A block with no strong physical heading features.

    Its base activation is low, so the votes' log-odds dominate the emission
    and the effect of correlation down-weighting is visible in probability
    space rather than being masked by a saturated physical prior.
    """
    return Block(id=0, type="text", text="Shared Heading", font_size=11.0)


def test_repeating_the_same_window_does_not_strengthen_the_conclusion():
    """重复提供相同窗口（同 window_index）不应让存在置信度无依据变强。

    A04 验收原文：重复提供相同窗口不应让结论无依据变强。同 window_index
    归入同一相关簇，降增益后其等效独立票数有硬上限——在弱物理先验的块上
    直接体现为存在概率几乎不随重复次数增长。
    """
    one = _site_existence_probability(_neutral_block(), [_vote(0)])
    five_same = _site_existence_probability(
        _neutral_block(), [_vote(0) for _ in range(5)],
    )
    # 重复同窗最多贡献几何级数上限（<2 张等效票）的极小增量。
    assert five_same <= one + 0.05, (one, five_same)


def test_increasing_window_overlap_does_not_strengthen_the_conclusion():
    """把同一观察当作多个完全重叠窗口反复投，也不应无依据抬升置信度。"""
    baseline = _site_existence_probability(_neutral_block(), [_vote(0)])
    overlapped = _site_existence_probability(
        _neutral_block(), [_vote(0), _vote(0), _vote(0)],
    )
    assert overlapped <= baseline + 0.05, (baseline, overlapped)


def test_independent_cross_source_agreement_still_adds_confidence():
    """真实跨来源一致性仍有收益：不同窗口的独立投票各自成簇、满额累加。"""
    one = _site_existence_probability(_neutral_block(), [_vote(0)])
    three_independent = _site_existence_probability(
        _neutral_block(), [_vote(0), _vote(1), _vote(2)],
    )
    assert three_independent > one + 1e-6, (one, three_independent)


def test_repeated_votes_stay_below_independent_votes():
    """同一收敛面上：三张重复票的置信度必须低于三张独立票。

    这是「相关不当独立」的判决性对照——重复不应达到独立同等的收益。
    """
    three_same = _site_existence_probability(
        _neutral_block(), [_vote(0), _vote(0), _vote(0)],
    )
    three_independent = _site_existence_probability(
        _neutral_block(), [_vote(0), _vote(1), _vote(2)],
    )
    assert three_same < three_independent, (three_same, three_independent)


def test_cluster_and_gains_semantics():
    """降增益核心性质：同簇增益几何衰减且和有上限；独立票恒为满额。"""
    same = [VoteView(key=i, confidence=0.9, window_index=0) for i in range(4)]
    clusters = cluster_correlated_votes(same)
    assert len(clusters) == 1 and sorted(clusters[0]) == [0, 1, 2, 3]
    gains = correlated_vote_gains(same)
    assert gains == [1.0, 0.5, 0.25, 0.125]
    assert sum(gains) < 2.0

    independent = [VoteView(key=i, confidence=0.9, window_index=i) for i in range(4)]
    assert len(cluster_correlated_votes(independent)) == 4
    assert correlated_vote_gains(independent) == [1.0, 1.0, 1.0, 1.0]


def test_different_model_or_prompt_version_are_independent():
    """不同模型 / 不同提示词版本即使同窗口也算独立来源，不降权。"""
    votes = [
        VoteView(key=0, confidence=0.9, window_index=0, model="A"),
        VoteView(key=1, confidence=0.9, window_index=0, model="B"),
        VoteView(key=2, confidence=0.9, window_index=0, model="A", prompt_version="v2"),
    ]
    assert len(cluster_correlated_votes(votes)) == 3
    assert correlated_vote_gains(votes) == [1.0, 1.0, 1.0]


# ── 校准入口：可靠性曲线 / ECE / Brier / 阈值后准确率 ─────────────

def test_perfect_calibration_has_zero_ece():
    """预测概率与实际频率完全一致时 ECE 为 0。"""
    confidences = [0.9] * 10
    correct = [True] * 9 + [False]
    assert expected_calibration_error(confidences, correct, n_bins=10) < 1e-9


def test_overconfident_predictions_have_positive_ece():
    """系统性过度自信（全说 0.95 却全错）ECE 接近 0.95。"""
    confidences = [0.95] * 20
    correct = [False] * 20
    ece = expected_calibration_error(confidences, correct, n_bins=10)
    assert math.isclose(ece, 0.95, abs_tol=1e-6)


def test_brier_score_matches_manual_mean_squared_error():
    confidences = [0.8, 0.2, 1.0, 0.0]
    correct = [True, False, True, False]
    expected = ((0.8 - 1) ** 2 + (0.2 - 0) ** 2 + 0 + 0) / 4
    assert math.isclose(brier_score(confidences, correct), expected)


def test_reliability_curve_bins_and_skips_empties():
    confidences = [0.05, 0.15, 0.95]
    correct = [False, True, True]
    curve = reliability_curve(confidences, correct, n_bins=10)
    assert [b.count for b in curve] == [1, 1, 1]
    assert all(isinstance(b, ReliabilityBin) for b in curve)
    lows = [b for b in curve if b.lower == 0.0]
    assert lows and lows[0].mean_accuracy == 0.0


def test_accuracy_after_threshold_filters_by_confidence():
    confidences = [0.4, 0.6, 0.8, 0.95]
    correct = [False, True, True, True]
    result = accuracy_after_threshold(confidences, correct, [0.5, 0.7, 0.99])
    assert result[0.5] == 1.0
    assert result[0.7] == 1.0
    assert result[0.99] == 0.0


def test_three_channels_are_calibrated_independently():
    """存在 / 层级 / 对齐三通道各自独立校准，返回稳定签名的报告。"""
    existence = calibrate_channel(
        "existence", [0.9, 0.8, 0.3], [True, True, False],
    )
    level = calibrate_channel(
        "level", [0.7, 0.7, 0.7], [True, False, True],
    )
    alignment = calibrate_channel(
        "alignment", [1.0, 1.0], [True, True],
    )
    for report in (existence, level, alignment):
        assert isinstance(report, CalibrationReport)
        assert report.sample_count > 0
        assert 0.0 <= report.ece <= 1.0
        assert 0.0 <= report.brier <= 1.0
        assert set(report.accuracy_at_threshold) == {0.5, 0.7, 0.9}
    assert existence.channel == "existence"
    assert level.channel == "level"
    assert alignment.channel == "alignment"
    assert (existence.sample_count, level.sample_count, alignment.sample_count) == (3, 3, 2)
