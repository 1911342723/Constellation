"""Serializable physical structure atoms carried inside ``Block.metadata``.

Providers keep the public :class:`Block` schema unchanged.  They create
``StructuralAtom`` instances while extracting physical source elements and
store only ``to_metadata()`` dictionaries on blocks.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class StructuralAtom(BaseModel):
    """Smallest auditable physical text unit emitted by a provider.

    ``char_start``/``char_end`` are half-open offsets in the canonical parent
    block text.  ``text`` always retains the physical source text; when a
    provider normalises a join (for example PDF soft hyphenation), the exact
    transformation is recorded in ``provenance`` rather than overwriting it.
    """

    model_config = ConfigDict(extra="forbid")

    atom_id: str = Field(..., description="Deterministic provider/source-span identifier.")
    block_id: Optional[int] = Field(None, description="Final containing Block id.")
    source: str = Field(..., description="Provider/source kind, e.g. pdf_line or docx_run.")
    source_span: dict[str, Any] = Field(default_factory=dict)
    char_start: int = Field(default=0, ge=0)
    char_end: int = Field(default=0, ge=0)
    text: str = ""
    page: Optional[int] = Field(None, ge=1)
    bbox: Optional[list[float]] = None
    font_family: Optional[str] = None
    font_size: Optional[float] = None
    is_bold: Optional[bool] = None
    is_italic: Optional[bool] = None
    is_underline: Optional[bool] = None
    is_strike: Optional[bool] = None
    is_superscript: Optional[bool] = None
    is_subscript: Optional[bool] = None
    is_code: Optional[bool] = None
    alignment: Optional[str] = None
    region: Optional[str] = None
    vertical_gap_before: Optional[float] = None
    vertical_gap_after: Optional[float] = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def stable_id(source: str, source_span: dict[str, Any]) -> str:
        """Return a deterministic id independent of final block ordering."""
        payload = json.dumps(
            {"source": source, "span": source_span},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        return f"{source}:{digest}"

    @classmethod
    def create(cls, *, source: str, source_span: dict[str, Any], **values: Any) -> "StructuralAtom":
        """Construct an atom with a stable id derived from its source span."""
        return cls(
            atom_id=cls.stable_id(source, source_span),
            source=source,
            source_span=source_span,
            **values,
        )

    def to_metadata(self) -> dict[str, Any]:
        """Return the lightweight JSON-compatible representation for metadata."""
        return self.model_dump(mode="json", exclude_none=True)
