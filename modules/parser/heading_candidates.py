"""Evidence-first heading candidate generation for Stage 2.5.

The v2 core separates four concerns that the legacy raw score conflated:
observation (located ``EvidenceAtom`` objects), local probability fusion,
region risk/admission, and the v1 route-budget projection.  The full
``HeadingCandidateSet`` is never capped; ``generate_heading_candidates`` is
an explicitly legacy wrapper that returns a budgeted view.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from infrastructure.models import Block
from modules.parser.evidence import (
    CALIBRATION_VERSION,
    PROBABILITY_QUALITY,
    deduplicate_evidence,
    evidence_diversity,
    heading_probability,
    level_probabilities,
    promotion_probability,
)
from modules.parser.schemas import (
    EvidenceAtom,
    HeadingCandidate,
    HeadingCandidateSet,
    HeadingLabel,
    RegionRisk,
    StructuralPosition,
    TextSpan,
)


_CAPTION_RE = re.compile(
    r"^(?:fig(?:ure)?|table|tbl\.?|图|表)\s*[\dA-Za-z一二三四五六七八九十]+[:：.]",
    re.IGNORECASE,
)
_NUMBERING_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)*)(?:[.)、．]|)\s+\S+")
_APPENDIX_RE = re.compile(r"^(?:appendix|annex)\s+[A-Z0-9]", re.IGNORECASE)
_ROMAN_RE = re.compile(r"^(?=[IVXLCDM]+\b)[IVXLCDM]+[.)]\s+\S+", re.IGNORECASE)

_STRONG_EVIDENCE_KINDS = {
    "explicit_heading_style",
    "outline_level",
    "visible_numbering",
    "effective_numbering",
    "semantic_title",
    "toc_destination",
}
_BASE_MIN_PROBABILITY = 0.40
_WEAK_ONLY_MIN_PROBABILITY = 0.25
# Historical names remain import-compatible, but now denote probabilities.
_BASE_MIN_SCORE = _BASE_MIN_PROBABILITY
_WEAK_ONLY_MIN_SCORE = _WEAK_ONLY_MIN_PROBABILITY
_MAX_CANDIDATE_RATIO = 0.15
_MAX_CANDIDATE_FLOOR = 30
_REGION_WINDOW_TEXT_BLOCKS = 24
_STANDALONE_LINE_MAX_CHARS = 64
_SENTENCE_END_CHARS = (
    ".", "。", "!", "?", "！", "？", ";", "；", ":", "：", ",", "，", "、",
)
_SEMANTIC_TITLES = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "methods",
    "method",
    "methodology",
    "experiments",
    "experiment",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "references",
    "bibliography",
    "appendix",
    "acknowledgments",
    "acknowledgements",
    "摘要",
    "关键词",
    "引言",
    "绪论",
    "方法",
    "实验",
    "结果",
    "讨论",
    "结论",
    "参考文献",
    "附录",
    "致谢",
}
_LEGACY_REASON_BY_KIND = {
    "explicit_heading_style": "explicit-style",
    "outline_level": "explicit-style",
    "visible_numbering": "numbering",
    "effective_numbering": "numbering",
    "semantic_title": "semantic-title",
    "bold": "bold",
    "alignment": "centered",
    "standalone_line": "standalone-line",
    "toc_destination": "toc-destination",
    "run_in_pattern": "run-in-pattern",
}


@dataclass(frozen=True)
class RegionAssessment:
    """Local region diagnostics used by candidate extraction."""

    region_id: str
    block_indices: tuple[int, ...]
    block_ids: tuple[int, ...]
    featureless: bool
    risk: RegionRisk


def _clean(text: str | None) -> str:
    return " ".join((text or "").strip().split())


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _body_font_size(blocks: Iterable[Block]) -> float:
    """Estimate body size as a character-weighted mode."""
    sizes: Counter[float] = Counter()
    fallback: Counter[float] = Counter()
    for block in blocks:
        if block.type != "text" or not block.font_size:
            continue
        size = round(block.font_size, 1)
        weight = max(len((block.text or "").strip()), 1)
        fallback[size] += weight
        if not block.is_bold and not block.is_heading_style:
            sizes[size] += weight
    if sizes:
        return sizes.most_common(1)[0][0]
    if fallback:
        return fallback.most_common(1)[0][0]
    return 12.0


def infer_numbering_level(text: str | None) -> Optional[int]:
    """Infer a level only from visible numbering syntax."""
    value = _clean(text)
    if not value:
        return None

    match = _NUMBERING_RE.match(value)
    if match:
        num = match.group("num")
        if "." not in num and int(num) > 99:
            return None
        return min(num.count(".") + 1, 6)
    if _APPENDIX_RE.match(value) or _ROMAN_RE.match(value):
        return 1
    if re.match(r"^第[一二三四五六七八九十百千\d]+章", value):
        return 1
    if re.match(r"^第[一二三四五六七八九十百千\d]+节", value):
        return 2
    if re.match(r"^[一二三四五六七八九十]+[、.．]\s*\S+", value):
        return 2
    return None


def infer_style_level(block: Block) -> Optional[int]:
    """Infer level from explicit provider style/outline metadata."""
    if block.heading_level:
        return max(1, min(int(block.heading_level), 6))
    metadata = block.metadata or {}
    outline_level = _safe_int(metadata.get("outline_level"))
    if outline_level is not None:
        return max(1, min(outline_level + 1, 6))
    style_level = _safe_int(metadata.get("style_level"))
    if style_level is not None:
        return max(1, min(style_level, 6))
    return None


def is_caption(text: str | None) -> bool:
    return bool(_CAPTION_RE.match(_clean(text)))


def _semantic_level(text: str) -> Optional[int]:
    lowered = text.strip().lower().strip(":：")
    if lowered in _SEMANTIC_TITLES:
        return 2 if lowered == "关键词" else 1
    return None


def _context(blocks: list[Block], index: int, direction: int) -> str:
    step = -1 if direction < 0 else 1
    cursor = index + step
    while 0 <= cursor < len(blocks):
        text = _clean(blocks[cursor].text)
        if text:
            return text[:120]
        cursor += step
    return ""


def _region_key(block: Block, text_ordinal: int) -> str:
    metadata = block.metadata or {}
    explicit = metadata.get("layout_region_id") or metadata.get("region_id")
    if explicit not in (None, ""):
        return f"layout:{explicit}"
    page = metadata.get("page")
    column = metadata.get("layout_column")
    if page is not None and column is not None:
        return f"page:{page}:column:{column}"
    if page is not None:
        return f"page:{page}"
    # Without provider layout metadata there is no defensible region boundary;
    # keep the sequential flow as one region rather than inventing windows
    # that can turn ordinary body-only slices into false escape regions.
    return "flow:0"


def _build_region_assessments(
    blocks: list[Block],
) -> tuple[dict[str, RegionAssessment], dict[int, str]]:
    grouped: dict[str, list[int]] = {}
    index_region: dict[int, str] = {}
    text_ordinal = 0
    for index, block in enumerate(blocks):
        if block.type != "text" or not block.text:
            continue
        key = _region_key(block, text_ordinal)
        text_ordinal += 1
        grouped.setdefault(key, []).append(index)
        index_region[index] = key

    assessments: dict[str, RegionAssessment] = {}
    for region_id, indices in grouped.items():
        region_blocks = [blocks[index] for index in indices]
        sizes = {round(block.font_size, 1) for block in region_blocks if block.font_size}
        structural_count = sum(
            bool(block.is_bold or block.is_heading_style or block.has_heading_numbering)
            for block in region_blocks
        )
        featureless = structural_count == 0 and len(sizes) <= 1

        miss = 0.08
        contamination = 0.03
        factors: list[str] = []
        if featureless:
            miss = max(miss, 0.72)
            factors.append("featureless_region")
        elif structural_count == 0:
            miss = max(miss, 0.36)
            factors.append("sparse_structural_signals")

        margin_count = 0
        table_count = 0
        printed_toc_count = 0
        run_in_count = 0
        uncertain_count = 0
        for block in region_blocks:
            metadata = block.metadata or {}
            role = str(metadata.get("role") or metadata.get("artifact_role") or "").lower()
            if role in {"margin", "margin_line", "header", "footer"}:
                margin_count += 1
            if role in {"table", "table_cell", "table_region"} or metadata.get("in_table"):
                table_count += 1
            if role in {"printed_toc", "printed_toc_line", "toc_line"}:
                printed_toc_count += 1
            if metadata.get("run_in_prefix") or metadata.get("run_in_heading"):
                run_in_count += 1
            if metadata.get("line_merge_uncertain") or metadata.get("reading_order_uncertain"):
                uncertain_count += 1

        count = max(len(region_blocks), 1)
        if run_in_count:
            miss = min(1.0, miss + 0.22)
            factors.append("run_in_pattern")
        if uncertain_count:
            miss = min(1.0, miss + 0.18 * uncertain_count / count)
            factors.append("layout_uncertainty")
        if margin_count:
            contamination += 0.80 * margin_count / count
            factors.append("margin_content")
        if table_count:
            contamination += 0.65 * table_count / count
            factors.append("table_region")
        if printed_toc_count:
            contamination += 0.85 * printed_toc_count / count
            factors.append("printed_toc_region")
        contamination = min(contamination, 1.0)

        if miss >= 0.55 and contamination < 0.70:
            band = "escape"
        elif miss >= 0.30 or contamination >= 0.30:
            band = "watch"
        else:
            band = "safe"
        risk = RegionRisk(
            region_id=region_id,
            miss_probability=miss,
            contamination_probability=contamination,
            band=band,
            factors=factors,
        )
        assessments[region_id] = RegionAssessment(
            region_id=region_id,
            block_indices=tuple(indices),
            block_ids=tuple(blocks[index].id for index in indices),
            featureless=featureless,
            risk=risk,
        )
    return assessments, index_region


def analyze_candidate_regions(blocks: list[Block]) -> dict[str, RegionAssessment]:
    """Public region-level feature/risk analysis used by candidate generation."""
    return _build_region_assessments(blocks)[0]


def is_featureless_document(blocks: Iterable[Block]) -> bool:
    """Legacy aggregate view; v2 extraction uses local region assessments.

    This helper remains for the v1 parser downgrade path.  A mixed document is
    not globally featureless, while featureless regions inside it are still
    recovered by :func:`generate_heading_candidate_set`.
    """
    block_list = list(blocks)
    assessments = analyze_candidate_regions(block_list)
    return not assessments or all(region.featureless for region in assessments.values())


def _level_likelihoods(level: int, confidence: float) -> dict[HeadingLabel, float]:
    level = max(1, min(level, 6))
    labels: tuple[HeadingLabel, ...] = ("L1", "L2", "L3", "L4", "L5", "L6")
    other_weights = {
        label: 1.0 / (1.0 + abs(int(label[1:]) - level))
        for label in labels if label != f"L{level}"
    }
    other_total = sum(other_weights.values())
    result = {
        label: (1.0 - confidence) * weight / other_total
        for label, weight in other_weights.items()
    }
    result[f"L{level}"] = confidence
    return result


def _source_for(block: Block, default: str = "rule") -> str:
    source = str((block.metadata or {}).get("source") or "").lower()
    if "pdf" in source:
        return "pdf"
    if "docx" in source or "word" in source:
        return "docx"
    return default


def _stable_evidence_id(
    block_id: int,
    char_offset: int,
    kind: str,
    observed_value: Any,
) -> str:
    payload = json.dumps(
        [block_id, char_offset, kind, observed_value],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"evidence:{block_id}:{kind}:{digest}"


def _make_evidence(
    *,
    block: Block,
    anchor: StructuralPosition,
    kind: str,
    polarity: int,
    observed_value: Any,
    reliability: float,
    correlation_group: str,
    level: int | None = None,
    level_confidence: float = 0.90,
    provenance: dict[str, Any] | None = None,
    source: str | None = None,
) -> EvidenceAtom:
    return EvidenceAtom(
        evidence_id=_stable_evidence_id(
            block.id, anchor.char_offset, kind, observed_value,
        ),
        kind=kind,
        polarity=polarity,
        observed_value=observed_value,
        reliability=reliability,
        level_likelihoods=(
            _level_likelihoods(level, level_confidence) if level is not None else {}
        ),
        anchor=anchor,
        source=source or _source_for(block),
        provenance=provenance or {},
        correlation_group=correlation_group,
    )


def _extract_evidence(
    block: Block,
    title: str,
    anchor: StructuralPosition,
    region: RegionAssessment,
    body_size: float,
) -> tuple[list[EvidenceAtom], int | None, int | None, int | None, float | None]:
    metadata = block.metadata or {}
    text_len = len(title)
    short = text_len <= 140
    medium = text_len <= 220
    evidence: list[EvidenceAtom] = []

    style_level = infer_style_level(block)
    outline_level = _safe_int(metadata.get("outline_level"))
    if block.is_heading_style or style_level is not None:
        # Heading style and outline level are two surfaces of one cause.
        # Emit one canonical atom and preserve the other values as provenance.
        kind = (
            "explicit_heading_style"
            if block.is_heading_style or metadata.get("style_level") is not None
            else "outline_level"
        )
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind=kind,
            polarity=1,
            observed_value={
                "style_level": style_level,
                "heading_style": block.is_heading_style,
                "outline_level": outline_level,
            },
            reliability=0.99,
            correlation_group="explicit_structure",
            level=style_level,
            level_confidence=0.97,
            provenance={
                "style": metadata.get("style"),
                "style_id": metadata.get("style_id"),
            },
        ))

    visible_numbering_level = infer_numbering_level(title)
    effective_numbering_level = _safe_int(metadata.get("numbering_level"))
    if effective_numbering_level is None and block.has_heading_numbering:
        effective_numbering_level = _safe_int(metadata.get("list_level"))
        if effective_numbering_level is not None:
            effective_numbering_level += 1
        else:
            effective_numbering_level = 1

    numbering_level: int | None = None
    if visible_numbering_level is not None and short:
        numbering_level = visible_numbering_level
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="visible_numbering",
            polarity=1,
            observed_value={"level": numbering_level, "text_prefix": title[:32]},
            reliability=0.95,
            correlation_group="numbering",
            level=numbering_level,
            level_confidence=0.94,
            provenance={"effective_level": effective_numbering_level},
        ))
    elif effective_numbering_level is not None and short:
        numbering_level = max(1, min(effective_numbering_level, 6))
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="effective_numbering",
            polarity=1,
            observed_value={"level": numbering_level},
            reliability=0.95,
            correlation_group="numbering",
            level=numbering_level,
            level_confidence=0.88,
            provenance={"list_level": metadata.get("list_level")},
        ))
    elif (visible_numbering_level is not None or effective_numbering_level is not None):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="list_prose_negative",
            polarity=-1,
            observed_value={"length": text_len},
            reliability=0.85,
            correlation_group="list_prose",
        ))

    semantic_level = _semantic_level(title) if short else None
    semantic_probability: float | None = None
    if semantic_level is not None:
        semantic_probability = 0.92
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="semantic_title",
            polarity=1,
            observed_value={"level": semantic_level, "normalized": title.lower()},
            reliability=semantic_probability,
            correlation_group="semantic",
            level=semantic_level,
            level_confidence=0.82,
        ))

    font_ratio: float | None = None
    if block.font_size and body_size > 0:
        font_ratio = block.font_size / body_size
        if font_ratio >= 1.10 and medium:
            evidence.append(_make_evidence(
                block=block,
                anchor=anchor,
                kind="font_ratio",
                polarity=1,
                observed_value={
                    "ratio": round(font_ratio, 4),
                    "font_size": block.font_size,
                    "body_font_size": body_size,
                },
                reliability=0.85,
                correlation_group="font_scale",
            ))

    if block.is_bold and short:
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="bold",
            polarity=1,
            observed_value=True,
            reliability=0.80,
            correlation_group="font_emphasis",
        ))
    if block.alignment and block.alignment.lower() == "center" and short:
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="alignment",
            polarity=1,
            observed_value="center",
            reliability=0.80,
            correlation_group="layout_alignment",
        ))

    if (
        region.featureless
        and short
        and text_len <= _STANDALONE_LINE_MAX_CHARS
        and not title.endswith(_SENTENCE_END_CHARS)
    ):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="standalone_line",
            polarity=1,
            observed_value={"length": text_len, "region_id": region.region_id},
            reliability=0.90,
            correlation_group="standalone_geometry",
        ))

    if is_caption(title):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="caption_negative",
            polarity=-1,
            observed_value=title[:80],
            reliability=0.99,
            correlation_group="caption_role",
        ))

    role = str(metadata.get("role") or metadata.get("artifact_role") or "").lower()
    if role in {"table", "table_cell", "table_region"} or metadata.get("in_table"):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="table_region_negative",
            polarity=-1,
            observed_value=role or "in_table",
            reliability=0.92,
            correlation_group="artifact_region",
        ))
    elif role in {"printed_toc", "printed_toc_line", "toc_line"}:
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="printed_toc_negative",
            polarity=-1,
            observed_value=role,
            reliability=0.96,
            correlation_group="artifact_region",
        ))
    elif role in {"margin", "margin_line", "header", "footer"}:
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="margin_negative",
            polarity=-1,
            observed_value=role,
            reliability=0.96,
            correlation_group="artifact_region",
        ))

    if metadata.get("toc_destination"):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="toc_destination",
            polarity=1,
            observed_value=metadata.get("toc_destination"),
            reliability=0.98,
            correlation_group="toc_destination",
            level=_safe_int(metadata.get("toc_level")),
            level_confidence=0.97,
            source="toc",
        ))
    if metadata.get("run_in_prefix") or metadata.get("run_in_heading"):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="run_in_pattern",
            polarity=1,
            observed_value=metadata.get("run_in_prefix") or True,
            reliability=0.75,
            correlation_group="run_in_geometry",
        ))

    return (
        deduplicate_evidence(evidence),
        style_level,
        numbering_level,
        semantic_level,
        semantic_probability,
    )


def _legacy_reasons(evidence: list[EvidenceAtom]) -> list[str]:
    reasons: list[str] = []
    for atom in evidence:
        if atom.kind == "font_ratio" and atom.polarity > 0:
            ratio = atom.observed_value.get("ratio", 1.0)
            reasons.append("large-font" if ratio >= 1.25 else "slightly-large-font")
            continue
        reason = _LEGACY_REASON_BY_KIND.get(atom.kind)
        if reason and atom.polarity > 0:
            reasons.append(reason)
    return sorted(set(reasons))


def _is_strong_positive(atom: EvidenceAtom) -> bool:
    if atom.polarity < 0:
        return False
    if atom.kind in _STRONG_EVIDENCE_KINDS:
        return True
    if atom.kind == "font_ratio":
        return float(atom.observed_value.get("ratio", 1.0)) >= 1.25
    return False


def _admission(evidence: list[EvidenceAtom], risk: RegionRisk) -> str:
    has_positive = any(atom.polarity > 0 for atom in evidence)
    if (
        any(_is_strong_positive(atom) for atom in evidence)
        and risk.contamination_probability < 0.65
    ):
        return "strict"
    if has_positive and risk.band == "escape":
        return "escape"
    return "audit_only"


def generate_heading_candidate_set(blocks: list[Block]) -> HeadingCandidateSet:
    """Generate the full, uncapped candidate/evidence set for decoding.

    Negative-only and audit-only sites remain present for diagnostics.  LLM
    budgets are intentionally absent from this function.
    """
    if not blocks:
        return HeadingCandidateSet()

    body_size = _body_font_size(blocks)
    regions, index_region = _build_region_assessments(blocks)
    candidates: list[HeadingCandidate] = []

    for index, block in enumerate(blocks):
        if block.type != "text" or not block.text:
            continue
        raw_text = block.text
        title = _clean(raw_text)
        if not title:
            continue

        leading = len(raw_text) - len(raw_text.lstrip())
        trailing_end = len(raw_text.rstrip())
        metadata = block.metadata or {}
        region = regions[index_region[index]]
        atom_id = str(
            metadata.get("atom_id")
            or metadata.get("structural_atom_id")
            or f"block:{block.id}:text"
        )
        anchor = StructuralPosition(
            block_id=block.id,
            char_offset=leading,
            atom_id=atom_id,
        )
        evidence, style_level, numbering_level, semantic_level, semantic_p = (
            _extract_evidence(block, title, anchor, region, body_size)
        )
        if not evidence:
            continue

        local_heading_probability = heading_probability(evidence)
        local_levels = level_probabilities(local_heading_probability, evidence)
        local_promotion = promotion_probability(
            local_heading_probability, evidence, region.risk,
        )

        page = metadata.get("page")
        bbox = metadata.get("bbox")
        if isinstance(bbox, tuple):
            bbox = list(bbox)
        candidate_id = (
            f"candidate:{block.id}:{leading}:"
            f"{hashlib.sha256((atom_id + title).encode('utf-8')).hexdigest()[:12]}"
        )
        candidates.append(HeadingCandidate(
            candidate_id=candidate_id,
            anchor=anchor,
            title_span=TextSpan(start=leading, end=trailing_end),
            title=title,
            snippet=title[:80],
            evidence=evidence,
            heading_probability=local_heading_probability,
            level_probabilities=local_levels,
            promotion_probability=local_promotion,
            semantic_level=semantic_level,
            semantic_level_probability=semantic_p,
            region_id=region.region_id,
            region_risk=region.risk,
            admission=_admission(evidence, region.risk),
            calibration_version=CALIBRATION_VERSION,
            probability_quality=PROBABILITY_QUALITY,
            # Explicit migration-only projection:
            block_id=block.id,
            source_score=local_heading_probability,
            raw_score=None,
            reasons=_legacy_reasons(evidence),
            style_level=style_level,
            numbering_level=numbering_level,
            font_size=block.font_size,
            is_bold=block.is_bold,
            alignment=block.alignment,
            page=page if isinstance(page, int) else _safe_int(page),
            bbox=bbox if isinstance(bbox, list) else None,
            context_before=_context(blocks, index, -1),
            context_after=_context(blocks, index, 1),
            metadata={
                "style": metadata.get("style"),
                "style_id": metadata.get("style_id"),
                "outline_level": metadata.get("outline_level"),
                "list_level": metadata.get("list_level"),
                "layout_column": metadata.get("layout_column"),
                "source": metadata.get("source"),
            },
        ))

    return HeadingCandidateSet(
        candidates=candidates,
        region_risks={region_id: item.risk for region_id, item in regions.items()},
        calibration_version=CALIBRATION_VERSION,
        probability_quality=PROBABILITY_QUALITY,
    )


# Short spelling used by experiments and hidden migration adapters.
generate_candidate_set = generate_heading_candidate_set


def _legacy_route_view(
    candidate_set: HeadingCandidateSet,
    *,
    base_min_probability: float,
    weak_min_probability: float,
) -> list[HeadingCandidate]:
    routed: list[HeadingCandidate] = []
    for candidate in candidate_set.candidates:
        positive = [atom for atom in candidate.evidence if atom.polarity > 0]
        if not positive:
            continue
        strong = any(_is_strong_positive(atom) for atom in positive)
        threshold = base_min_probability if strong else weak_min_probability
        if candidate.heading_probability < threshold:
            continue
        if len(candidate.title) > 220 and not (
            candidate.evidence_kinds(polarity=1)
            & {"explicit_heading_style", "outline_level"}
        ):
            continue
        routed.append(candidate)
    return routed


def select_route_candidates(
    candidate_set: HeadingCandidateSet,
    blocks: list[Block],
    *,
    base_min_probability: float = _BASE_MIN_PROBABILITY,
    weak_min_probability: float = _WEAK_ONLY_MIN_PROBABILITY,
    max_ratio: float | None = None,
) -> list[HeadingCandidate]:
    """Project one uncapped evidence set into the bounded LLM route view."""
    routed = _legacy_route_view(
        candidate_set,
        base_min_probability=base_min_probability,
        weak_min_probability=weak_min_probability,
    )
    return _cap_candidates(routed, blocks, max_ratio=max_ratio)


def generate_heading_candidates(
    blocks: list[Block],
    *,
    base_min_score: float | None = None,
    weak_only_min: float | None = None,
    max_ratio: float | None = None,
) -> list[HeadingCandidate]:
    """Legacy budgeted route view over the uncapped evidence-first set.

    The historical parameter names remain source-compatible, but thresholds
    now apply to heuristic probabilities.  ``raw_score`` is never consulted.
    """
    if not blocks:
        return []
    base_min = (
        _BASE_MIN_PROBABILITY if base_min_score is None else base_min_score
    )
    weak_min = (
        _WEAK_ONLY_MIN_PROBABILITY if weak_only_min is None else weak_only_min
    )
    candidate_set = generate_heading_candidate_set(blocks)
    return select_route_candidates(
        candidate_set,
        blocks,
        base_min_probability=base_min,
        weak_min_probability=weak_min,
        max_ratio=max_ratio,
    )


def _cap_candidates(
    candidates: list[HeadingCandidate],
    blocks: list[Block],
    *,
    max_ratio: float | None = None,
) -> list[HeadingCandidate]:
    """Apply the v1 LLM route cap without deleting the full CandidateSet.

    Ranking uses promotion/heading probabilities and independent evidence
    diversity.  The deprecated uncapped additive ``raw_score`` is ignored.
    """
    ratio = _MAX_CANDIDATE_RATIO if max_ratio is None else max_ratio
    text_block_count = sum(1 for block in blocks if block.type == "text" and block.text)
    max_candidates = max(_MAX_CANDIDATE_FLOOR, int(text_block_count * ratio))
    if len(candidates) <= max_candidates:
        return candidates

    strong = [
        candidate for candidate in candidates
        if any(_is_strong_positive(atom) for atom in candidate.evidence)
    ]
    strong_ids = {id(candidate) for candidate in strong}
    weak = [candidate for candidate in candidates if id(candidate) not in strong_ids]

    def rank(candidate: HeadingCandidate) -> tuple[float, float, int, int]:
        return (
            -candidate.promotion_probability,
            -candidate.heading_probability,
            -evidence_diversity(candidate.evidence),
            candidate.block_id,
        )

    if len(strong) >= max_candidates:
        kept = {id(candidate) for candidate in sorted(strong, key=rank)[:max_candidates]}
    else:
        kept = {id(candidate) for candidate in strong}
        weak_budget = max_candidates - len(strong)
        kept.update(id(candidate) for candidate in sorted(weak, key=rank)[:weak_budget])
    return [candidate for candidate in candidates if id(candidate) in kept]


def candidate_ids(candidates: Iterable[HeadingCandidate]) -> set[int]:
    return {candidate.block_id for candidate in candidates}


def candidates_in_range(
    candidates: Iterable[HeadingCandidate],
    start_id: int,
    end_id: int,
) -> list[HeadingCandidate]:
    return [
        candidate for candidate in candidates
        if start_id <= candidate.block_id <= end_id
    ]


def format_candidate_table(candidates: Iterable[HeadingCandidate]) -> str:
    """Render every candidate in the current shard as a compact evidence row.

    Input-budget sharding operates on the complete request, so silently hiding
    rows behind an ID range is unnecessary and makes the verifier unable to
    compare candidates.  Titles/evidence are bounded per row; candidate count
    itself is handled by :meth:`LLMRouter.fit_skeleton_chunks`.
    """
    candidate_list = sorted(
        list(candidates), key=lambda candidate: (candidate.block_id, candidate.candidate_id),
    )
    if not candidate_list:
        return "- No deterministic heading candidates in this shard."

    rows: list[str] = []
    for candidate in candidate_list:
        hints: list[str] = []
        if candidate.style_level is not None:
            hints.append(f"style=L{candidate.style_level}")
        if candidate.numbering_level is not None:
            hints.append(f"number=L{candidate.numbering_level}")
        if candidate.semantic_level is not None:
            hints.append(f"semantic=L{candidate.semantic_level}")
        if candidate.font_size is not None:
            hints.append(f"size={candidate.font_size:.1f}")
        if candidate.is_bold:
            hints.append("bold")
        if candidate.alignment:
            hints.append(f"align={candidate.alignment[:12]}")
        if candidate.page is not None:
            hints.append(f"page={candidate.page}")
        positive = sorted(candidate.evidence_kinds(polarity=1))
        negative = sorted(candidate.evidence_kinds(polarity=-1))
        evidence = "+" + ",".join(positive[:6])
        if negative:
            evidence += ";-" + ",".join(negative[:4])
        title = " ".join(candidate.title.split())
        if len(title) > 64:
            title = title[:61] + "..."
        rows.append(
            f"[{candidate.block_id}]|h={candidate.heading_probability:.2f}"
            f"|p={candidate.promotion_probability:.2f}"
            f"|r={candidate.region_risk.band[0]}"
            f"|l={candidate.numbering_level or candidate.style_level or candidate.semantic_level or 0}"
            f"|e={evidence[:48]}|{title}"
        )
    return "\n".join(rows)
