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
    # A table-out vote is eligible only in an escape-risk region and only when
    # its aligned block passes this title/short-line gate.
    escape_vote_max_text_len: int = 140
    escape_vote_max_words: int = 12
    escape_vote_title_similarity: float = 0.5
    # Legacy physical re-validation knobs retained for compatibility helpers.
    downgrade_max_text_len: int = 300
    downgrade_candidate_proximity: int = 2
    downgrade_title_similarity: float = 0.5

    @model_validator(mode="after")
    def _check_routing_controls(self) -> "ParserConfig":
        if self.speculative_boundary_tolerance < 0:
            raise ValueError("speculative_boundary_tolerance must be >= 0")
        if self.escape_vote_max_text_len < 1:
            raise ValueError("escape_vote_max_text_len must be at least 1")
        if self.escape_vote_max_words < 1:
            raise ValueError("escape_vote_max_words must be at least 1")
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
