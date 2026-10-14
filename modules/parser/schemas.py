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
    context_span: tuple[int, int] | None = None
    model: str = "default"
    prompt_version: str = "default"


# ── A08 · R 层（ReviewVerdict）已按方案验收**删除** ─────────────────────────
#
# 这里曾有 ``ReviewAction`` 与 ``ReviewVerdict`` 两个类型：一个「复审只输出
# accept/reject/adjust_level 并作为观测进解码器」的接口。它从定义之日起就**没有生产者、
# 也没有消费者**——``GlobalHeadingInference`` 收下 ``review_verdicts`` 存进
# ``_verdict_by_block`` 就再没读过。
#
# 方案 A08 的验收原文是「对同一批失败样本比较『确定性修复』『局部 R 层』『换模型』
# 三种代价与收益；**没有独立收益时关闭或删除未使用的半截接口**」。删除的依据有三条，
# 都是可查的事实而不是省事：
#
# 1. **按它自己的契约，它碰不到记录在案的失败。** 该接口只能重判「解码器已经产出的
#    标题」，不能提名新块、不能改标题。而 experiment_results 里记录的失败是候选层漏检
#    （round4 诊断：候选召回 96.7% / 100% / 96.8%，漏的那几个标题从未成为候选）与
#    Stage 1/2 的负压缩——两类都在它的权限之外。
# 2. **它要服务的那个失败集已经空了。** 排版扰动集 24 篇 × 三档（含最贴近真实模型行为的
#    flat_votes：模型对无深度排版一律回 L1）现为 72 行全 F1=100% / level=100% / exact=Y。
#    确定性修复已经把这些失败吸收掉了，没有可比的失败样本，第三种治法（换模型）也就无从比较。
# 3. **接通它会造出第二个权威。** A01 的选择性复审已经落地，它的产出是 ``ChapterNode``
#    投票，走既有的证据仲裁通道。再加一条 ReviewVerdict 通道，等于让「复审」有两种影响
#    解码的方式——本仓库在 Artifact vs content.result 上正是这样吃过双权威的亏。
#
# **重新引入的判据**（不要凭「感觉有用」加回来）：拿到一个可复现的失败集，其中
# 标题**已被解码出来**、只是层级判错，且确定性修复与 A01 的局部投票都解决不了它。
# 那时再加回来，并同时给出三种治法的代价/收益对照。


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
    out_of_candidate: SkipJsonSchema[bool] = Field(
        default=False,
        description=(
            "True if the LLM pointed outside the Stage 2.5 candidate set. "
            "Such anchors travel a low-confidence channel: physical-feature "
            "re-validation in the parser decides keep/drop instead of a "
            "hard filter in the router. "
            "A07：这是**服务端判定**的结果（router 比对候选集合后写入），模型无从知道也"
            "不该被问，所以从对外 schema 里剥掉。刻意**不加** exclude=True——检查点存的是"
            "model_dump 后的 JSON，排除它会让缓存命中后这个标记丢失，"
            "从而跳过 out-of-candidate 锚点该走的物理复核。"
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
    # A07：曾有一个 ``children`` 字段，注释写着「Reserved for backward compatibility;
    # not populated by the LLM」——既没有任何读取方，又出现在模型看到的 schema 里，等于
    # 让模型多认一个它永远不该填的字段。层级由 ``level`` 表达（扁平数组，LLM 生成更可靠），
    # 嵌套 children 是 DocumentNode 的事，与本 DTO 无关。已删除。

    model_config = ConfigDict(populate_by_name=True)
    # Reconstructed from the actual request even after a checkpoint hit.
    _observation_context: tuple[int, int] | None = PrivateAttr(default=None)
    _observation_model: str = PrivateAttr(default="default")
    _observation_prompt: str = PrivateAttr(default="default")
    _confidence_details: dict = PrivateAttr(default_factory=dict)


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
    _observed_block_ids: set[int] | None = PrivateAttr(default=None)


HeadingLabel = Literal["NONE", "L1", "L2", "L3", "L4", "L5", "L6"]
EvidenceKind = Literal[
    "document_metadata_negative",
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
    "non_lexical_negative",
    "bibliography_negative",
    "list_prose_negative",
    "table_region_negative",
    "printed_toc_negative",
    "margin_negative",
    "styled_prose_negative",
    "body_signature_negative",
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
    confidence_details: dict[str, Any] = Field(default_factory=dict)
    content_relations: list[dict[str, Any]] = Field(default_factory=list)
