"""Pydantic data models for Constellation pipeline stages.

Defines the structured schemas used across the four-stage pipeline:

- :class:`ChapterNode` — LLM output anchor (Stage 3 → Stage 4).
- :class:`LLMRouterOutput` — Complete LLM response envelope.
- :class:`DocumentNode` — Resolved document tree node (Stage 4 output).
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema


class LLMAnchorVote(BaseModel):
    """One immutable LLM observation after physical anchor alignment.

    Votes are retained when overlapping windows name the same heading so the
    global decoder can combine their confidence and level evidence instead of
    discarding one arbitrarily.  These fields are internal Stage 3-4 state and
    are excluded from the public ``ChapterNode`` serialization contract.
    """

    raw_block_id: int
    aligned_block_id: int
    title: str
    snippet: str = ""
    level: int = Field(default=1, ge=1, le=6)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    alignment_score: float = Field(default=1.0, ge=0.0, le=1.0)
    out_of_candidate: bool = False
    window_index: int = 0


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
    confidence: float = Field(
        default=1.0,
        description="Anchor confidence (0.0-1.0). Lower values trigger wider search radius in resolver.",
    )
    out_of_candidate: bool = Field(
        default=False,
        description=(
            "True if the LLM pointed outside the Stage 2.5 candidate set. "
            "Such anchors travel a low-confidence channel: physical-feature "
            "re-validation in the parser decides keep/drop instead of a "
            "hard filter in the router."
        ),
    )
    anchor_votes: SkipJsonSchema[List[LLMAnchorVote]] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
        description="Internal aligned observations merged at this anchor.",
    )
    alignment_score: SkipJsonSchema[float] = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        exclude=True,
        repr=False,
    )
    source_windows: SkipJsonSchema[List[int]] = Field(
        default_factory=list,
        exclude=True,
        repr=False,
    )
    globally_inferred: SkipJsonSchema[bool] = Field(
        default=False,
        exclude=True,
        repr=False,
        description="Internal guard: level/selection already came from global DP.",
    )
    children: List[ChapterNode] = Field(
        default_factory=list,
        description="Reserved for backward compatibility; not populated by the LLM.",
    )

    model_config = ConfigDict(populate_by_name=True)


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

    # Number of anchors dropped during validation; set by the router's
    # filter pass and consumed by its retry logic. Declared explicitly
    # (instead of an ad-hoc runtime attribute) so the contract is visible.
    _dropped_count: int = PrivateAttr(default=0)


HeadingLabel = Literal["NONE", "L1", "L2", "L3", "L4", "L5", "L6"]
EvidenceKind = Literal[
    "visible_numbering",
    "effective_numbering",
    "explicit_heading_style",
    "outline_level",
    "font_ratio",
    "bold",
    "alignment",
    "standalone_line",
    "semantic_title",
    "toc_destination",
    "run_in_pattern",
    "caption_negative",
    "list_prose_negative",
    "table_region_negative",
    "printed_toc_negative",
    "margin_negative",
]
_HEADING_LABELS: tuple[HeadingLabel, ...] = (
    "NONE", "L1", "L2", "L3", "L4", "L5", "L6",
)
_LEVEL_LABELS: tuple[HeadingLabel, ...] = _HEADING_LABELS[1:]


class TextSpan(BaseModel):
    """Half-open Unicode code-point span in the immutable Block text."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    coordinate_space: Literal["block.text.v1"] = "block.text.v1"

    @model_validator(mode="after")
    def _check_order(self) -> "TextSpan":
        if self.end < self.start:
            raise ValueError("text span end must be greater than or equal to start")
        return self


class StructuralPosition(BaseModel):
    """Atom-aware position with a stable total-order key."""

    block_id: int = Field(ge=0)
    char_offset: int = Field(default=0, ge=0)
    atom_id: str | None = None

    @property
    def order_key(self) -> tuple[int, int, str]:
        return (self.block_id, self.char_offset, self.atom_id or "")


class EvidenceAtom(BaseModel):
    """One typed, located observation; it is evidence, not a decision."""

    evidence_id: str
    kind: EvidenceKind
    polarity: Literal[-1, 1]
    observed_value: Any
    reliability: float = Field(ge=0.0, le=1.0)
    level_likelihoods: dict[HeadingLabel, float] = Field(default_factory=dict)
    anchor: StructuralPosition
    source: Literal["pdf", "docx", "rule", "toc", "llm"] = "rule"
    provenance: dict[str, Any] = Field(default_factory=dict)
    correlation_group: str = Field(
        description=(
            "Mutually correlated observations share a group and contribute "
            "at most once to probability fusion."
        )
    )

    @field_validator("level_likelihoods")
    @classmethod
    def _check_level_likelihoods(
        cls, values: dict[HeadingLabel, float],
    ) -> dict[HeadingLabel, float]:
        for label, value in values.items():
            if label == "NONE":
                raise ValueError("evidence level_likelihoods must not include NONE")
            if not 0.0 <= value <= 1.0:
                raise ValueError("evidence level likelihoods must be in [0, 1]")
        return values


class RegionRisk(BaseModel):
    """Heuristic miss/contamination risk for one local layout region."""

    region_id: str = "document:0"
    miss_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    contamination_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    band: Literal["safe", "watch", "escape"] = "safe"
    factors: list[str] = Field(default_factory=list)
    calibration_version: str = "candidate-risk.heuristic.v1"

    @field_validator("factors")
    @classmethod
    def _deduplicate_factors(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


def _legacy_level_probabilities(
    heading_probability: float,
    level: int | None,
) -> dict[HeadingLabel, float]:
    """Build a valid emission for legacy constructors lacking v2 fields."""
    selected = max(1, min(level or 1, 6))
    result = {label: 0.0 for label in _HEADING_LABELS}
    result["NONE"] = 1.0 - heading_probability
    result[f"L{selected}"] = heading_probability
    return result


class HeadingCandidateV2(BaseModel):
    """Evidence-first local heading hypothesis.

    ``heading_probability`` and ``level_probabilities`` are heuristic local
    emissions, not global decoder posteriors.  Correlated evidence is fused by
    correlation group before this model is built.  The legacy fields at the
    bottom exist only as a v1 transport projection; candidate logic must use
    typed evidence and probabilities instead.
    """

    candidate_id: str = ""
    anchor: StructuralPosition = Field(
        default_factory=lambda: StructuralPosition(block_id=0)
    )
    title_span: TextSpan | None = None
    title: str
    snippet: str = ""
    evidence: list[EvidenceAtom] = Field(default_factory=list)
    heading_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    level_probabilities: dict[HeadingLabel, float] = Field(default_factory=dict)
    promotion_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    semantic_level: Literal[1, 2, 3, 4, 5, 6] | None = None
    semantic_level_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    region_id: str = "document:0"
    region_risk: RegionRisk = Field(default_factory=RegionRisk)
    admission: Literal["strict", "escape", "audit_only"] = "audit_only"
    calibration_version: str = "heading-evidence.heuristic.v1"
    probability_quality: Literal["calibrated", "heuristic"] = "heuristic"

    # ---- v1 migration projection (never consumed by v2 scoring) ----------
    block_id: int = Field(
        default=0,
        description="DEPRECATED v1 projection of anchor.block_id.",
    )
    source_score: float = Field(
        default=0.0,
        description="DEPRECATED v1 projection of heading_probability.",
    )
    raw_score: Optional[float] = Field(
        default=None,
        description="DEPRECATED raw additive score; v2 generators always leave it None.",
    )
    reasons: List[str] = Field(
        default_factory=list,
        description="DEPRECATED display projection of typed evidence kinds.",
    )
    style_level: Optional[int] = Field(
        default=None,
        description="DEPRECATED v1 projection; explicit style only.",
    )
    numbering_level: Optional[int] = Field(
        default=None,
        description="DEPRECATED v1 projection; numbering evidence only.",
    )
    font_size: Optional[float] = None
    is_bold: bool = False
    alignment: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    context_before: str = ""
    context_after: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _adapt_legacy_input(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        anchor = data.get("anchor")
        if anchor is None:
            data["anchor"] = {
                "block_id": int(data.get("block_id", 0)),
                "char_offset": 0,
            }
        elif "block_id" not in data:
            data["block_id"] = (
                anchor.block_id if isinstance(anchor, StructuralPosition)
                else int(anchor.get("block_id", 0))
            )

        if "heading_probability" not in data:
            data["heading_probability"] = float(data.get("source_score", 0.0))
        heading_probability = max(
            0.0, min(float(data.get("heading_probability", 0.0)), 1.0)
        )
        data.setdefault("source_score", heading_probability)
        data.setdefault("promotion_probability", heading_probability)
        if "level_probabilities" not in data or not data["level_probabilities"]:
            level = (
                data.get("numbering_level")
                or data.get("style_level")
                or data.get("semantic_level")
            )
            data["level_probabilities"] = _legacy_level_probabilities(
                heading_probability, level,
            )
        if data.get("semantic_level") is not None:
            data.setdefault("semantic_level_probability", 1.0)
        return data

    @model_validator(mode="after")
    def _check_and_complete(self) -> "HeadingCandidateV2":
        if self.anchor.block_id != self.block_id:
            # ``anchor`` is authoritative in v2; block_id is only a projection.
            self.block_id = self.anchor.block_id
        if not self.candidate_id:
            self.candidate_id = (
                f"candidate:{self.anchor.block_id}:"
                f"{self.anchor.char_offset}:{self.anchor.atom_id or 'block'}"
            )
        if self.title_span is None:
            self.title_span = TextSpan(
                start=self.anchor.char_offset,
                end=self.anchor.char_offset + len(self.title),
            )
        if not self.snippet:
            self.snippet = self.title[:80]
        if self.region_risk.region_id != self.region_id:
            self.region_id = self.region_risk.region_id

        missing = set(_HEADING_LABELS) - set(self.level_probabilities)
        extra = set(self.level_probabilities) - set(_HEADING_LABELS)
        if missing or extra:
            raise ValueError(
                f"level_probabilities must contain exactly {_HEADING_LABELS}; "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )
        total = sum(self.level_probabilities.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"level_probabilities must sum to 1.0 (got {total:.9f})"
            )
        if abs(
            (1.0 - self.level_probabilities["NONE"])
            - self.heading_probability
        ) > 1e-6:
            raise ValueError(
                "heading_probability must equal 1 - level_probabilities['NONE']"
            )
        return self

    @property
    def label_probabilities(self) -> dict[HeadingLabel, float]:
        """Architect-spec spelling retained as a read-only compatibility view."""
        return self.level_probabilities

    def evidence_kinds(self, *, polarity: int | None = None) -> set[str]:
        return {
            atom.kind for atom in self.evidence
            if polarity is None or atom.polarity == polarity
        }

    def legacy_projection(self) -> dict[str, Any]:
        """Return the explicit v1 transport shape for legacy consumers."""
        return {
            "block_id": self.block_id,
            "title": self.title,
            "snippet": self.snippet,
            "source_score": self.source_score,
            "raw_score": self.raw_score,
            "style_level": self.style_level,
            "numbering_level": self.numbering_level,
            "font_size": self.font_size,
            "is_bold": self.is_bold,
            "alignment": self.alignment,
            "page": self.page,
            "bbox": self.bbox,
            "reasons": list(self.reasons),
            "context_before": self.context_before,
            "context_after": self.context_after,
            "metadata": dict(self.metadata),
        }


# Existing imports keep working, but now resolve to the evidence-first model.
HeadingCandidate = HeadingCandidateV2


class HeadingCandidateSet(BaseModel):
    """Uncapped document-wide candidates plus region diagnostics."""

    candidates: list[HeadingCandidateV2] = Field(default_factory=list)
    region_risks: dict[str, RegionRisk] = Field(default_factory=dict)
    calibration_version: str = "heading-evidence.heuristic.v1"
    probability_quality: Literal["calibrated", "heuristic"] = "heuristic"

    @property
    def strict_candidates(self) -> list[HeadingCandidateV2]:
        return [candidate for candidate in self.candidates if candidate.admission == "strict"]

    @property
    def escape_candidates(self) -> list[HeadingCandidateV2]:
        return [candidate for candidate in self.candidates if candidate.admission == "escape"]

    @property
    def audit_only_candidates(self) -> list[HeadingCandidateV2]:
        return [candidate for candidate in self.candidates if candidate.admission == "audit_only"]


CandidateSet = HeadingCandidateSet


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
