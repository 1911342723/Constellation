"""Pure-data configuration models for the Constellation pipeline.

These are intentionally decoupled from ``app.core.config.settings`` so that
parser modules can be used as a standalone library.  The application layer is
responsible for bridging environment-backed settings into these models.
"""
from __future__ import annotations

import math
from urllib.parse import urlparse

from pydantic import BaseModel, model_validator


class ParserConfig(BaseModel):
    """Top-level configuration for :class:`CaliperParser`."""

    enable_speculative_execution: bool = True
    speculative_boundary_tolerance: int = 1
    enable_heading_candidates: bool = True
    # Out-of-candidate LLM anchors must survive the router so physical
    # alignment can place them before strict-first region validation.
    enable_anchor_downgrade: bool = True
    strict_first_routing: bool = True
    hierarchy_beam_width: int = 32
    hierarchy_relation_weight: float = 2.8
    confidence_calibration_path: str = ""
    # A table-out vote is eligible only in an escape-risk region and only when
    # its aligned block passes this title/short-line gate.
    escape_vote_max_text_len: int = 140
    escape_vote_max_words: int = 12
    escape_vote_title_similarity: float = 0.5
    # Likelihood ratio of a model's *silence* on a routed candidate it was
    # shown: P(no vote | heading) / P(no vote | not heading).  With the 18%
    # false-vote baseline used for positive votes and an assumed 90% recall of
    # the reader, (1 - 0.90) / (1 - 0.18) ~= 0.12.  1.0 disables the channel.
    llm_silence_likelihood_ratio: float = 0.12
    # Legacy physical re-validation knobs retained for compatibility helpers.
    downgrade_max_text_len: int = 300
    downgrade_candidate_proximity: int = 2
    downgrade_title_similarity: float = 0.5

    # ── R02 · 单文档 LLM 请求扇出上限 ────────────────────────────
    # 上游的文档级准入只限制**并发文档数**；
    # 一篇大文档在 _async_speculative_route 里把 N 个窗口一次性 gather，
    # N 篇同时进来就放大成 (文档数 × 窗口数) 个在途模型请求——document 准入
    # 完全看不见这一层。这里补两道闸：
    #
    # - per_document：一篇文档同时在途的窗口请求数上限。默认 8 —— 足够让
    #   推测并行拿到多窗口吞吐，又不至于一篇 500 窗口的文档瞬间打出 500 个
    #   请求把网关配额和本机 socket 打满。
    # - global：整个进程所有文档合计的在途窗口请求数上限。默认 32。它是
    #   跨文档的总闸：即便每篇只占 8，4 篇以上并发也会在这里排队而非叠加。
    #   全局闸用 FIFO 唤醒，配合 per_document 上限，保证一篇巨型文档占不满
    #   全部全局槽位，小文档的窗口能穿插进来不被饿死（见验收「不饥饿」）。
    #
    # 0 = 不限制该层（仅用于测试对照 / 明确关闭；生产默认给正数）。
    max_inflight_requests_per_document: int = 8
    max_inflight_requests_global: int = 32
    # 单个窗口请求的软超时（秒）。0 = 不额外设超时，完全交给 LLM 客户端自身
    # 的 timeout。设正数时，超时窗口按所选失败策略（取消整组）处理——这是让
    # 「一个窗口卡死不能拖垮/泄漏整组」可被观测验证的入口。
    window_request_timeout: float = 0.0

    @model_validator(mode="after")
    def _check_routing_controls(self) -> "ParserConfig":
        if not 1 <= self.hierarchy_beam_width <= 256:
            raise ValueError("hierarchy_beam_width must be in [1, 256]")
        if not math.isfinite(self.hierarchy_relation_weight) or self.hierarchy_relation_weight < 0:
            raise ValueError("hierarchy_relation_weight must be finite and >= 0")
        if self.speculative_boundary_tolerance < 0:
            raise ValueError("speculative_boundary_tolerance must be >= 0")
        if self.max_inflight_requests_per_document < 0:
            raise ValueError("max_inflight_requests_per_document must be >= 0 (0 = unbounded)")
        if self.max_inflight_requests_global < 0:
            raise ValueError("max_inflight_requests_global must be >= 0 (0 = unbounded)")
        if not math.isfinite(self.window_request_timeout) or self.window_request_timeout < 0:
            raise ValueError("window_request_timeout must be finite and >= 0 (0 = no timeout)")
        if self.escape_vote_max_text_len < 1:
            raise ValueError("escape_vote_max_text_len must be at least 1")
        if self.escape_vote_max_words < 1:
            raise ValueError("escape_vote_max_words must be at least 1")
        if not 0.0 < self.llm_silence_likelihood_ratio <= 1.0:
            raise ValueError("llm_silence_likelihood_ratio must be in (0, 1]")
        if not 0.0 <= self.escape_vote_title_similarity <= 1.0:
            raise ValueError("escape_vote_title_similarity must be in [0, 1]")
        if self.downgrade_max_text_len < 1:
            raise ValueError("downgrade_max_text_len must be at least 1")
        if self.downgrade_candidate_proximity < 0:
            raise ValueError("downgrade_candidate_proximity must be >= 0")
        if not 0.0 <= self.downgrade_title_similarity <= 1.0:
            raise ValueError("downgrade_title_similarity must be in [0, 1]")
        return self


class CompressorConfig(BaseModel):
    """Configuration for :class:`SkeletonCompressor`."""

    head_chars: int = 40
    tail_chars: int = 30
    enable_rle: bool = True
    rle_threshold: int = 3
    max_rle_group: int = 10
    sliding_window_threshold: int = 500
    window_size: int = 300
    window_overlap: int = 50
    rle_dynamic_prefix_min_length: int = 35
    rle_dynamic_prefix_extra: int = 25
    compress_max_workers: int = 0
    enable_candidate_sparse: bool = True
    candidate_context_blocks: int = 1
    sparse_preamble_blocks: int = 8

    @model_validator(mode="after")
    def _check_compressor_controls(self) -> "CompressorConfig":
        positive_fields = {
            "head_chars": self.head_chars,
            "tail_chars": self.tail_chars,
            "rle_threshold": self.rle_threshold,
            "max_rle_group": self.max_rle_group,
            "sliding_window_threshold": self.sliding_window_threshold,
            "window_size": self.window_size,
            "rle_dynamic_prefix_min_length": self.rle_dynamic_prefix_min_length,
            "rle_dynamic_prefix_extra": self.rle_dynamic_prefix_extra,
        }
        for name, value in positive_fields.items():
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.window_overlap < 0:
            raise ValueError("window_overlap must be >= 0")
        if self.window_overlap >= self.window_size:
            raise ValueError(
                f"window_overlap ({self.window_overlap}) must be strictly less "
                f"than window_size ({self.window_size}) to avoid infinite loop"
            )
        if self.rle_threshold > self.max_rle_group:
            raise ValueError(
                "rle_threshold must be <= max_rle_group; otherwise folding can "
                "never activate before the buffer is flushed"
            )
        if self.compress_max_workers < 0:
            raise ValueError("compress_max_workers must be >= 0 (0 = auto-detect)")
        if self.candidate_context_blocks < 0:
            raise ValueError("candidate_context_blocks must be >= 0")
        if self.sparse_preamble_blocks < 0:
            raise ValueError("sparse_preamble_blocks must be >= 0")
        return self


class ResolverConfig(BaseModel):
    """Configuration for :class:`IntervalResolver`."""

    fuzzy_anchor_radius: int = 5
    fuzzy_min_similarity: float = 0.4
    # 远距离救援（anchor_alignment）：本地窗口最佳相似度低于 trigger 时，
    # 放开距离限制在全部标题候选里找 ≥ min_similarity 的明显更优匹配。
    rescue_trigger_similarity: float = 0.75
    rescue_min_similarity: float = 0.60
    anchor_match_min_length: int = 5
    anchor_match_levenshtein_threshold: float = 0.7
    level_jump_font_size_tolerance: float = 0.5
    dedup_id_diff: int = 3
    dedup_sim_threshold: float = 0.8
    orphan_bold_max_text_len: int = 40
    max_orphan_promotions: int = 10
    snippet_prefix_check_len: int = 20
    snippet_exact_match_len: int = 15
    snippet_extra_chars: int = 10

    @model_validator(mode="after")
    def _check_resolver_controls(self) -> "ResolverConfig":
        non_negative_fields = {
            "fuzzy_anchor_radius": self.fuzzy_anchor_radius,
            "level_jump_font_size_tolerance": self.level_jump_font_size_tolerance,
            "dedup_id_diff": self.dedup_id_diff,
            "max_orphan_promotions": self.max_orphan_promotions,
            "snippet_extra_chars": self.snippet_extra_chars,
        }
        for name, value in non_negative_fields.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and >= 0")
        positive_fields = {
            "anchor_match_min_length": self.anchor_match_min_length,
            "orphan_bold_max_text_len": self.orphan_bold_max_text_len,
            "snippet_prefix_check_len": self.snippet_prefix_check_len,
            "snippet_exact_match_len": self.snippet_exact_match_len,
        }
        for name, value in positive_fields.items():
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        bounded_fields = {
            "fuzzy_min_similarity": self.fuzzy_min_similarity,
            "anchor_match_levenshtein_threshold": self.anchor_match_levenshtein_threshold,
            "dedup_sim_threshold": self.dedup_sim_threshold,
            "rescue_trigger_similarity": self.rescue_trigger_similarity,
            "rescue_min_similarity": self.rescue_min_similarity,
        }
        for name, value in bounded_fields.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        return self


class LLMClientConfig(BaseModel):
    """Configuration for :class:`LLMClient`."""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.1
    max_tokens: int = 4096
    max_input_tokens: int = 8192
    input_token_safety_margin: int = 512
    timeout: float = 120.0
    connect_timeout: float = 10.0
    max_retries: int = 3

    @model_validator(mode="after")
    def _check_client_controls(self) -> "LLMClientConfig":
        self.base_url = self.base_url.strip()
        self.model = self.model.strip()
        parsed_base_url = urlparse(self.base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.netloc:
            raise ValueError("base_url must be a valid non-empty http(s) URL")
        if not self.model:
            raise ValueError("model must not be empty")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be in [0, 2]")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        if self.max_input_tokens < 1:
            raise ValueError("max_input_tokens must be at least 1")
        if not 0 <= self.input_token_safety_margin < self.max_input_tokens:
            raise ValueError(
                "input_token_safety_margin must be >= 0 and strictly less "
                "than max_input_tokens"
            )
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("timeout must be finite and > 0")
        if not math.isfinite(self.connect_timeout) or self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be finite and > 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        return self
