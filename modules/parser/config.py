"""Pure-data configuration models for the Constellation pipeline.

These are intentionally decoupled from ``app.core.config.settings``
so that the parser modules can be used as a standalone library without
requiring a ``.env`` file or FastAPI application context.

The ``app`` layer is responsible for bridging ``settings`` → these
config objects at initialisation time.
"""
from __future__ import annotations

from pydantic import BaseModel, model_validator


class ParserConfig(BaseModel):
    """Top-level configuration for :class:`CaliperParser`."""

    enable_speculative_execution: bool = False
    speculative_boundary_tolerance: int = 1


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

    @model_validator(mode="after")
    def _check_window_overlap(self) -> "CompressorConfig":
        if self.window_overlap >= self.window_size:
            raise ValueError(
                f"window_overlap ({self.window_overlap}) must be strictly less "
                f"than window_size ({self.window_size}) to avoid infinite loop"
            )
        return self


class ResolverConfig(BaseModel):
    """Configuration for :class:`IntervalResolver`."""

    fuzzy_anchor_radius: int = 5
    fuzzy_min_similarity: float = 0.4
    anchor_match_min_length: int = 5
    anchor_match_levenshtein_threshold: float = 0.7
    level_jump_font_size_tolerance: float = 0.5
    dedup_id_diff: int = 3
    dedup_sim_threshold: float = 0.8
    orphan_bold_max_text_len: int = 40
    snippet_prefix_check_len: int = 20
    snippet_exact_match_len: int = 15
    snippet_extra_chars: int = 10


class CompressorConstants(BaseModel):
    """Named constants for the compressor's thread pool."""

    max_parallel_workers: int = 4


class LLMClientConfig(BaseModel):
    """Configuration for :class:`LLMClient`."""

    api_key: str = ""
    base_url: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout: float = 120.0
    connect_timeout: float = 10.0
    max_retries: int = 3
