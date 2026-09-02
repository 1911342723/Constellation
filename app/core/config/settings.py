"""Configuration for the Constellation parsing pipeline.

Only the parser-facing knobs live here.  The hosted service that this
research release was extracted from carried additional deployment,
authentication and billing settings; those are intentionally absent.

``env_file`` is anchored to the repository root rather than resolved
against the process CWD, so a launcher that changes directories cannot
silently drop the whole configuration back to defaults.  A ``.env`` in
the current directory is still layered on top for local overrides.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Pipeline settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM (Stage 3 router only; offline metrics never read these) ----
    llm_model: str = "deepseek-chat"
    # Empty by default so that importing the package, running the test
    # suite and reproducing the offline metrics all work without a key.
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096
    # Upstream gateway input limit.  Routing splits oversized skeletons
    # before sending and retains a margin for tokenizer differences.
    llm_max_input_tokens: int = 8192
    llm_input_token_safety_margin: int = 512

    # ---- Stage 2: skeleton compression ----
    skeleton_head_chars: int = 40
    skeleton_tail_chars: int = 30
    skeleton_enable_rle: bool = True
    skeleton_rle_threshold: int = 3
    skeleton_max_rle_group: int = 10
    # Candidate-aware sparse skeleton controls.
    skeleton_enable_candidate_sparse: bool = True
    skeleton_candidate_context_blocks: int = 1
    skeleton_sparse_preamble_blocks: int = 8

    # ---- Map-Reduce sliding window for very long documents ----
    sliding_window_threshold: int = 500
    window_size: int = 300
    window_overlap: int = 50

    # ---- Stage 4: fuzzy anchoring ----
    fuzzy_anchor_radius: int = 5
    fuzzy_min_similarity: float = 0.4

    # ---- Strict-first escape-risk controls.  These gates only affect
    # aligned LLM votes outside the deterministic candidate table.
    strict_first_routing: bool = True
    escape_vote_max_text_len: int = 140
    escape_vote_max_words: int = 12
    escape_vote_title_similarity: float = 0.5

    log_level: str = "INFO"

    app_name: str = "Constellation"
    app_version: str = "1.1.0"


settings = Settings()
