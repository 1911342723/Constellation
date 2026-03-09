"""Pydantic data models for Constellation pipeline stages.

Defines the structured schemas used across the four-stage pipeline:

- :class:`ChapterNode` — LLM output anchor (Stage 3 → Stage 4).
- :class:`LLMRouterOutput` — Complete LLM response envelope.
- :class:`DocumentNode` — Resolved document tree node (Stage 4 output).
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ChapterNode(BaseModel):
    """A single section heading anchor produced by the LLM router.

    The anchor is *flat* — hierarchy is encoded via the ``level`` field
    rather than nested ``children``, because LLMs are far more reliable
    when generating flat arrays than deeply nested JSON.

    The ``snippet`` field enables *fuzzy anchoring*: the resolver
    cross-validates ``block_id`` against the snippet text and
    auto-corrects off-by-one errors using Levenshtein distance.
    """

    title: str = Field(..., description="Section heading text.")
    start_block_id: int = Field(
        ..., alias="block_id", description="Block ID where this heading starts."
    )
    level: int = Field(
        default=1, description="Heading depth: 1 = top-level, 2 = sub-section, etc."
    )
    snippet: str = Field(
        default="",
        description="First ~30 chars of the block's original text for fuzzy anchor verification.",
    )
    children: List[ChapterNode] = Field(
        default_factory=list,
        description="Reserved for backward compatibility; not populated by the LLM.",
    )

    class Config:
        populate_by_name = True


class LLMRouterOutput(BaseModel):
    """Envelope for the complete LLM router response.

    Contains document-level metadata (title, authors) and a flat,
    ``block_id``-ascending list of :class:`ChapterNode` anchors.
    """

    doc_title: str = Field(default="", description="Document title extracted from the skeleton.")
    doc_authors: str = Field(default="", description="Author information extracted from the skeleton.")
    chapters: List[ChapterNode] = Field(
        ..., description="Flat anchor list sorted by block_id ascending."
    )


class DocumentNode(BaseModel):
    """A fully resolved document tree node (Stage 4 output).

    Each node owns a contiguous ``[start_block_id, end_block_id]``
    interval of the original Block array.  The ``content`` field holds
    the lossless Markdown rendering of all blocks in that interval
    (excluding the heading block itself, which is rendered as ``#``).
    """

    title: str = Field(..., description="Section title.")
    level: int = Field(..., description="Heading depth.")
    start_block_id: int = Field(..., description="First Block ID in this section (inclusive).")
    end_block_id: int = Field(..., description="Last Block ID in this section (inclusive).")
    content: str = Field(default="", description="Lossless Markdown content of this section.")
    children: List[DocumentNode] = Field(default_factory=list, description="Child sections.")
    section_type: str = Field(
        default="section",
        description="Semantic type: section | abstract | reference | appendix | acknowledgment.",
    )
