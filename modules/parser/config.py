"""Pure-data configuration models for the Cursor-Caliper pipeline.

These are intentionally decoupled from ``app.core.config.settings``
so that the parser modules can be used as a standalone library without
requiring a ``.env`` file or FastAPI application context.

The ``app`` layer is responsible for bridging ``settings`` → these
config objects at initialisation time.
"""
from __future__ import annotations

from pydantic import BaseModel


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


class ResolverConfig(BaseModel):
    """Configuration for :class:`IntervalResolver`."""

    fuzzy_anchor_radius: int = 5
    fuzzy_min_similarity: float = 0.4
    anchor_match_min_length: int = 5
    anchor_match_levenshtein_threshold: float = 0.7
    level_jump_font_size_tolerance: float = 0.5
    dedup_id_diff: int = 3
    dedup_sim_threshold: float = 0.8


class LLMClientConfig(BaseModel):
    """Configuration for :class:`LLMClient`."""

    api_key: str = ""
    base_url: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.1
    max_tokens: int = 4096
