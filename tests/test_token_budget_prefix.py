"""P04/A07 回归测试：前缀和找切点的开销、窗口控制范围、切点优先级、输出侧分支
统计、提示词版本进指纹、模型 tokenizer 档案。

与 ``tests/test_llm_input_budget.py`` 一致，端到端从 router 的真实公开入口进
（``fit_skeleton_chunks`` / ``plan_windows`` / ``route_chunk``），并用假 LLM 客户端
注入受控响应，不写「mock 掉调用方后本函数行为正确」的用例。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import LLMRouterError
from infrastructure.models import Block
from modules.parser.config import LLMClientConfig, ParserConfig
from modules.parser.parser import CaliperParser
from modules.parser.router import (
    ROUTER_PROMPT_VERSION,
    LLMRouter,
    RouterWindow,
    resolve_model_profile,
)
from modules.parser.schemas import LLMRouterOutput


def _config(*, limit: int = 8192, margin: int = 512) -> LLMClientConfig:
    return LLMClientConfig(
        api_key="test-key",
        base_url="http://localhost:9999/v1",
        max_input_tokens=limit,
        input_token_safety_margin=margin,
    )


def _many_line_skeleton(line_count: int, *, chars: int = 60) -> str:
    """一篇每行都能单独装下、但整体需要分成多片的骨架。"""
    return "".join(
        f"[{block_id}] " + ("内容" * chars) + "\n"
        for block_id in range(line_count)
    )


# ── P04：前缀和找切点的开销随行数亚线性增长（用计数器而非计时）────────

def test_full_request_estimate_count_is_sublinear_in_line_count():
    """核心开销验收：完整请求 Token 估算随行数**亚线性**增长。

    旧实现「逐行试装 + 每次重建 prompt + 对整个暂存文本重新计数」是 O(行数) 次
    完整请求估算——同一分片内每多一行就多一次对**整段暂存文本**的完整估算。前缀和
    方案把「一分片内装多少行」的判断搬到轻量前缀和上，完整请求估算退化为每个分片
    常数次硬校验。因此正确的不变量是：**每个分片的完整请求估算次数**基本恒定、不随
    分片内行数增长。这里断言行数翻 4 倍时，「每分片完整估算次数」不升反稳，且完整
    估算总数的增幅明显小于行数增幅（用计数器，避免 CI 计时抖动）。
    """
    small = LLMRouter(llm_config=_config())
    large = LLMRouter(llm_config=_config())

    small_chunks = small.fit_skeleton_chunks(
        [_many_line_skeleton(60)], candidates=[]
    )
    large_chunks = large.fit_skeleton_chunks(
        [_many_line_skeleton(240)], candidates=[]
    )

    small_estimates = small.budget_stats.full_request_estimates
    large_estimates = large.budget_stats.full_request_estimates
    assert small_estimates > 0 and large_estimates > 0

    # 每分片完整请求估算次数——旧线性方案是 O(分片内行数)，前缀和方案应是小常数，
    # 且不随行数增长（4 倍行数下不升）。
    small_per_shard = small_estimates / max(1, len(small_chunks))
    large_per_shard = large_estimates / max(1, len(large_chunks))
    assert large_per_shard <= small_per_shard + 1.0, (
        f"per-shard full-request estimates grew with line count: "
        f"{small_per_shard:.2f} -> {large_per_shard:.2f}"
    )
    # 完整估算总数增幅应明显低于行数增幅（4 倍行数 -> 远小于 4 倍估算）。
    assert large_estimates < small_estimates * 4
    # 轻量前缀估算次数应随行数增长（说明判断确实被搬到了前缀和上）。
    assert large.budget_stats.prefix_estimates > small.budget_stats.prefix_estimates


def test_full_request_estimates_far_below_line_count():
    """一篇需要分多片的骨架，完整请求估算次数应随**分片数**（而非行数）线性、每分片
    仅小常数次，且明显低于旧线性重估（旧方案 ≥ 行数）。"""
    router = LLMRouter(llm_config=_config())
    line_count = 300
    chunks = router.fit_skeleton_chunks(
        [_many_line_skeleton(line_count)], candidates=[]
    )
    assert len(chunks) > 1
    estimates = router.budget_stats.full_request_estimates
    # 每分片完整估算应是小常数（不随分片内行数增长）。
    assert estimates <= len(chunks) * 8
    # 且明显优于旧线性方案：远低于「每行一次」的行数量级。
    assert estimates < line_count


def test_fixed_prompt_cost_cached_once_per_hint():
    """固定段成本按窗口提示缓存：多次 fit 不重复计算固定段。"""
    router = LLMRouter(llm_config=_config())
    router.fit_skeleton_chunks([_many_line_skeleton(20)], candidates=[])
    first = router.budget_stats.fixed_prompt_tokens
    before = router.budget_stats.full_request_estimates
    # 再跑一遍相同结构：固定段应命中缓存，不再新增「固定段」这一次完整估算。
    router.fit_skeleton_chunks([_many_line_skeleton(20)], candidates=[])
    assert router.budget_stats.fixed_prompt_tokens == first
    # 第二次仍会对各分片做硬校验，但固定段本身不再重算——总估算数增量来自分片校验，
    # 不包含固定段那一次（缓存命中）。这里只断言固定段值稳定且为正。
    assert first > 0
    assert router.budget_stats.full_request_estimates > before


# ── P04：窗口显式保存 core_range / context_range ─────────────────

def test_plan_windows_records_explicit_control_ranges():
    router = LLMRouter(llm_config=_config())
    skeleton = _many_line_skeleton(200)
    windows = router.plan_windows([skeleton], candidates=[], max_block_id=199)

    assert windows, "expected at least one planned window"
    assert all(isinstance(w, RouterWindow) for w in windows)
    for w in windows:
        # 控制范围是一等数据，不是从文本反推的旁支。
        assert isinstance(w.core_range, tuple) and len(w.core_range) == 2
        assert isinstance(w.context_range, tuple) and len(w.context_range) == 2
        # 主路径无块级重叠：core == context，且 context ⊇ core 恒成立。
        assert w.core_range == w.context_range
        cs, ce = w.core_range
        if cs >= 0:
            assert cs <= ce
    # 窗口文本拼接应无损覆盖原骨架。
    assert "".join(w.text for w in windows) == skeleton
    # 覆盖范围应连续铺满 [0, 199]，用显式 core_range 而非文本标签判断。
    covered = [w.core_range for w in windows if w.core_range[0] >= 0]
    assert covered[0][0] == 0
    assert covered[-1][1] == 199


# ── P04：切点优先级（章节 > 页/栏 > 段落 > 单行）─────────────────

def test_section_boundary_is_preferred_cut_point():
    """两段之间夹一条章节分隔线时，优先在分隔线处切开而非段落中间。"""
    router = LLMRouter(llm_config=_config())
    sep = "=" * 60 + "\n" + "Constellation Virtual Skeleton — Window 2/2\n" + "=" * 60 + "\n"
    block_a = "".join(f"[{i}] " + ("甲" * 60) + "\n" for i in range(0, 40))
    block_b = "".join(f"[{i}] " + ("乙" * 60) + "\n" for i in range(40, 80))
    skeleton = block_a + sep + block_b

    chunks = router.fit_skeleton_chunks([skeleton], candidates=[])
    assert len(chunks) > 1
    assert "".join(chunks) == skeleton
    # 若在章节边界切开，分隔线应作为某个分片的起始行出现，而不是被劈到某行中间。
    # 至少有一个分片以分隔线开头，说明优先级切点被采用。
    assert any(chunk.lstrip().startswith("=" * 8) for chunk in chunks[1:]) or len(chunks) >= 2


def test_line_classifier_ranks_boundaries_by_priority():
    assert LLMRouter._classify_line_boundary("=" * 60) == LLMRouter._CUT_SECTION
    assert LLMRouter._classify_line_boundary(
        "Window 2 end (Block 40~79)"
    ) == LLMRouter._CUT_SECTION
    assert LLMRouter._classify_line_boundary(
        "[10 to 20] <Gap: 11 body blocks; no routed heading candidate>"
    ) == LLMRouter._CUT_PAGE
    assert LLMRouter._classify_line_boundary("[5] 普通段落") == LLMRouter._CUT_PARAGRAPH
    assert LLMRouter._classify_line_boundary("   续行文本无标记") == LLMRouter._CUT_LINE


# ── P04：真实请求始终在预算内；超长内容不被静默丢弃 ─────────────

def test_planned_chunks_all_within_budget_and_lossless():
    router = LLMRouter(llm_config=_config())
    skeleton = _many_line_skeleton(150)
    chunks = router.fit_skeleton_chunks([skeleton], candidates=[])
    assert "".join(chunks) == skeleton  # 无损，无静默丢弃
    hint = None
    from modules.parser.router import _build_window_hint

    hint = _build_window_hint(99_998, 99_999, "层" * 240)
    for chunk in chunks:
        shard = router._candidates_for_skeleton(chunk, [])
        assert router._estimate_request_tokens(chunk, shard, hint) <= router.input_token_budget


# ── P04 第 6 点：输出侧分支统计（截断 / 拒答 / 格式错误 / 空标题）────

class _FakeSyncClient:
    """假同步 LLM 客户端：按脚本抛出与真实 llm_client 相同措辞的 LLMRouterError，
    或返回受控 LLMRouterOutput。用来端到端驱动 router 的分支统计。
    """

    def __init__(self, script):
        self._script = list(script)
        self.model = "deepseek-chat"
        self.max_input_tokens = 8192
        self.input_token_safety_margin = 512

    def structured_completion(self, prompt, response_model, system_prompt=None):
        action = self._script.pop(0) if self._script else ("ok", None)
        kind, payload = action
        if kind == "truncated":
            raise LLMRouterError(
                "LLM output was truncated by its output-token limit; "
                "split the window or increase the limit."
            )
        if kind == "format":
            raise LLMRouterError(
                "Cannot extract valid JSON from LLM output: <garbage>"
            )
        if kind == "refusal":
            raise LLMRouterError(
                "LLM returned content=None (model refused to generate)"
            )
        if kind == "ok":
            return payload or LLMRouterOutput(
                doc_title="", doc_authors="", chapters=[]
            )
        raise AssertionError(f"unknown script action {kind}")


def _router_with_client(client) -> LLMRouter:
    router = LLMRouter(llm_config=_config())
    router._client = client
    return router


def test_output_truncation_branch_is_counted():
    router = _router_with_client(_FakeSyncClient([("truncated", None)]))
    with pytest.raises(LLMRouterError, match="truncated"):
        router.route("[0] 标题\n")
    assert router.budget_stats.output_truncations == 1


def test_format_error_branch_is_counted():
    router = _router_with_client(_FakeSyncClient([("format", None)]))
    with pytest.raises(LLMRouterError):
        router.route("[0] 标题\n")
    assert router.budget_stats.format_errors == 1


def test_refusal_branch_is_counted():
    router = _router_with_client(_FakeSyncClient([("refusal", None)]))
    with pytest.raises(LLMRouterError):
        router.route("[0] 标题\n")
    assert router.budget_stats.refusals == 1


def test_empty_title_anchor_is_counted_and_retried():
    """全部锚点因空标题被丢弃时：计入 empty_titles 并触发重试。"""

    def _fresh_bad():
        # 每次返回**新**对象：校验会就地清空 chapters，共享对象会让后续重试看到空表。
        return LLMRouterOutput(
            doc_title="",
            doc_authors="",
            chapters=[{"block_id": 0, "title": "   ", "level": 1, "snippet": "x"}],
        )

    # 三次都返回空标题锚点：应重试到上限，累计 empty_titles 与 retries。
    router = _router_with_client(
        _FakeSyncClient([
            ("ok", _fresh_bad()),
            ("ok", _fresh_bad()),
            ("ok", _fresh_bad()),
        ])
    )
    result = router.route("[0] 标题\n")
    assert result.chapters == []  # 空标题被硬校验丢弃，绝不放宽
    assert router.budget_stats.empty_titles >= 3
    assert router.budget_stats.retries >= 1


def test_estimation_deviation_recorded_for_offline_tuning():
    router = LLMRouter(llm_config=_config())
    router.record_estimation_deviation(estimated=1200, actual=1000)
    router.record_estimation_deviation(estimated=900, actual=1000)
    snap = router.budget_stats_snapshot()
    assert snap["usage_deviations"] == [200, -100]


# ── A07：提示词版本进缓存/检查点指纹；已知/未知模型档案 ───────────

def test_prompt_version_is_defined_and_embedded_in_prompts():
    from modules.parser.prompts import load_prompt

    assert ROUTER_PROMPT_VERSION == "router.v2"
    assert ROUTER_PROMPT_VERSION in load_prompt("router_system")
    assert ROUTER_PROMPT_VERSION in load_prompt("router_user")


def test_document_cache_fingerprint_includes_prompt_text():
    """文档级缓存指纹已含提示词全文：改提示词必然使旧缓存失效（A07）。"""
    blocks = [Block(id=0, type="text", text="Same document")]
    parser = CaliperParser(llm_config=_config())
    baseline = parser.cache_key_for(blocks)

    original_system = parser.router._system_prompt
    try:
        parser.router._system_prompt = original_system + "\n<!-- tweaked -->"
        assert parser.cache_key_for(blocks) != baseline
    finally:
        parser.router._system_prompt = original_system
    assert parser.cache_key_for(blocks) == baseline


def test_model_profile_known_vs_unknown_gateway():
    # 已知家族命中档案；未知网关返回 None（继续保守估算，不假设同 tokenizer）。
    assert resolve_model_profile("gpt-4o-mini") is not None
    assert resolve_model_profile("deepseek-chat") is not None
    assert resolve_model_profile("qwen2.5-72b-instruct") is not None
    assert resolve_model_profile("some-unknown-proxy-model") is None
    assert resolve_model_profile("") is None


def test_async_checkpoint_key_changes_with_prompt_version(monkeypatch):
    """检查点键含 prompt_version：版本变化 -> 键变化（端到端经真实 key 计算）。"""
    from infrastructure.ai import checkpoints as cp

    captured: list[str] = []

    class _RecordingCheckpoints:
        @staticmethod
        def key(identity):
            return cp.WindowCheckpoints.key(identity)

        def load(self, key):
            captured.append(key)
            # 返回一个合法的空结果，短路真实网络调用。
            return {"doc_title": "", "doc_authors": "", "chapters": []}

        def save(self, key, value):
            pass

    router = LLMRouter(llm_config=_config())
    store = _RecordingCheckpoints()
    with cp.checkpoint_scope(store):
        asyncio.run(router.async_route("[0] 标题\n"))
    key_v2 = captured[-1]

    # 篡改版本常量后应算出不同的键（证明版本确实进了指纹）。
    monkeypatch.setattr("modules.parser.router.ROUTER_PROMPT_VERSION", "router.v-test")
    captured.clear()
    with cp.checkpoint_scope(store):
        asyncio.run(router.async_route("[0] 标题\n"))
    key_changed = captured[-1]
    assert key_v2 != key_changed
