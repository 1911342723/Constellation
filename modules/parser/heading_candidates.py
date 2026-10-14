"""Evidence-first heading candidate generation for Stage 2.5.

The v2 core separates four concerns that the legacy raw score conflated:
observation (located ``EvidenceAtom`` objects), local probability fusion,
region risk/admission, and the v1 route-budget projection.  The full
``HeadingCandidateSet`` is never capped; ``generate_heading_candidates`` is
an explicitly legacy wrapper that returns a budgeted view.
"""
from __future__ import annotations

from modules.parser.analysis_context import measured_once

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
from modules.parser.titles import (
    SECTION_NUMBERING_RE,
    has_dot_leader,
    is_metadata_line,
    is_non_lexical,
    structural_text,
)
from modules.parser.typography import (
    TypographyProfile,
    analyze_typography,
    is_prose_shaped,
)


_CAPTION_RE = re.compile(
    r"^(?:fig(?:ure)?\.?|table|tbl\.?|图|表)\s*(?:[\dA-Za-z一二三四五六七八九十]+[:：.]|(?:\d+|[IVXLC]+)\s*$)",
    re.IGNORECASE,
)
_NUMBERING_RE = SECTION_NUMBERING_RE
_LEADING_NUMBER_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+)[.)]?\s+")
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
# Level confidence floors: even fully distrusted typography still points at its
# own level, it just stops drowning out every other level.
_STYLE_LEVEL_CONFIDENCE_FLOOR = 0.35
_NUMBERING_LEVEL_CONFIDENCE_FLOOR = 0.30
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




@measured_once
def _body_font_size(blocks: Iterable[Block]) -> float:
    """Estimate body size as a character-weighted mode."""
    sizes: Counter[float] = Counter()
    fallback: Counter[float] = Counter()
    for block in blocks:
        if block.type != "text" or not block.font_size:
            continue
        size = round(block.font_size, 1)
        weight = max(int((block.metadata or {}).get("char_count", len((block.text or "").strip()))), 1)
        fallback[size] += weight
        if not block.is_bold and not block.is_heading_style:
            sizes[size] += weight
    if sizes:
        return sizes.most_common(1)[0][0]
    if fallback:
        return fallback.most_common(1)[0][0]
    return 12.0


#: 段首片段的角色（A02）。决定「标题跨度」到底覆盖整块还是只覆盖段首那一截。
FRAGMENT_WHOLE_BLOCK = "whole_block"
FRAGMENT_RUN_IN = "run_in"
FRAGMENT_LEAD_IN_LABEL = "lead_in_label"

#: 段首片段 + 分隔 + 后续正文。两条分支的分隔要求不同，刻意分开写：
#: **加粗**片段本身就是强信号，允许仅用空白分隔（``**Implementation.** We first…``）；
#: 不加粗的片段必须以句读收尾才可能是标题，否则任何一句正文都能被空格切成"标题+正文"。
_RUN_IN_RE = re.compile(
    r"^(?:"
    r"\*\*(?P<bold>[^*\n]{1,60})\*\*[.。．:：]?\s+"
    r"|(?P<plain>[0-9A-Za-z\u4e00-\u9fff][^\n。．:：]{0,48})[.。．:：]\s*"
    r")(?P<rest>\S[\s\S]*)$"
)

#: 只是引导词、不是标题的段首片段。命中即判 lead_in_label——把它当标题会凭空造出
#: 一堆「注」「例如」层级节点，而正文反而被吞掉。
_LEAD_IN_WORDS = frozenset({
    "注", "备注", "说明", "提示", "例如", "举例", "例", "如", "其中", "另外", "此外",
    "note", "notes", "tip", "example", "e.g", "i.e", "warning", "caution", "see",
})

#: run-in 判定要求「后续正文」至少这么长。太短说明整块本来就是一个带句读的标题
#: （如「4.2 静态信息：」），切开只会把标题自己劈成两半。
_RUN_IN_MIN_REST_CHARS = 24
#: 段首片段超过这个长度就不像标签/标题，而像正文首句。
_RUN_IN_MAX_LEAD_CHARS = 48


def detect_title_fragment(block: Block) -> tuple[int, int, str, str]:
    """判定块内**标题跨度**与段首片段角色，返回 ``(start, end, title, role)``。

    A02 要修的现状：候选生成对整块 text 只建一个候选、``title_span`` 就是整段去首尾
    空白的范围；闭合遇到标题则跳过**整个**起始 block。于是
    ``**Implementation.** We first ...`` 这种段落只有两种下场——整段被当标题（正文
    凭空消失），或整段被当正文（标题丢失）。两者都是静默的内容错位。

    这里把「标题跨度」与「块」解耦：确认是 run-in 标题时，跨度只覆盖段首那一截，
    块内其余字符仍然归正文（半开区间，字符去向可逐一核对）。

    判定只用确定性物理/句法证据，宁可判成整块也不乱切：
      * 段首必须是加粗片段或短的编号/词组，且以句读或多空格与后文分隔；
      * 后续正文必须够长（``_RUN_IN_MIN_REST_CHARS``），否则整块本来就是带句读的标题；
      * 段首命中引导词表（注/说明/例如/Note/e.g.）判 ``lead_in_label``——它不是标题；
      * 后文以 ``$`` 开头（公式）或 ``|`` 开头（表格行）时不判 run-in：那是公式前标签
        或表格字段名，方案明确要求这四类不得相互混淆。
    """
    raw = block.text or ""
    leading = len(raw) - len(raw.lstrip())
    trailing_end = len(raw.rstrip())
    whole = (leading, trailing_end, _clean(raw), FRAGMENT_WHOLE_BLOCK)

    match = _RUN_IN_RE.match(raw.strip())
    if not match:
        return whole

    rest = match.group("rest")
    lead_text = (match.group("bold") or match.group("plain") or "").strip()
    if not lead_text or len(lead_text) > _RUN_IN_MAX_LEAD_CHARS:
        return whole
    if len(rest) < _RUN_IN_MIN_REST_CHARS:
        return whole
    # 公式前标签 / 表格字段名：不是章节标题。
    if rest.lstrip().startswith(("$", "|")):
        return whole

    normalized_lead = lead_text.strip().rstrip(".:：。").lower()
    if normalized_lead in _LEAD_IN_WORDS:
        return leading, trailing_end, _clean(raw), FRAGMENT_LEAD_IN_LABEL

    is_bold_lead = match.group("bold") is not None
    has_numbering = infer_numbering_level(lead_text) is not None
    if not (is_bold_lead or has_numbering):
        # 既不加粗也无编号的段首短语只是正文首句，不足以判成标题。
        return whole

    # 跨度覆盖段首片段**连同它的记号与分隔符**——用 rest 的起点作终点，与走了哪条
    # 分支无关。闭合时按这个区间把标题从正文视图里摘掉，剩下的字符原样留在内容里，
    # 不会残留一个孤零零的 ``**`` 或前导句号。
    return leading, leading + match.start("rest"), lead_text, FRAGMENT_RUN_IN


_BIBLIOGRAPHY_TITLES = {
    "references", "reference", "bibliography", "literaturecited", "referencesandnotes",
    "works cited".replace(" ", ""), "参考文献",
}
_BIBLIOGRAPHY_END_RE = re.compile(
    r"^(?:appendix|appendices|supplementary|supplemental|附录)", re.IGNORECASE,
)
_LETTER_SECTION_RE = re.compile(r"^[A-Z](?:\.\d+)*\.?\s+[A-Z][a-z]")


def bibliography_zone(blocks: list[Block]) -> set[int]:
    """Indices of blocks inside the (last) bibliography section.

    The zone opens after a block titled "References"/"Bibliography" and closes
    at the first block that opens an appendix or reads as a short bold title.
    Only the indices are returned; the zone adds negative evidence and never
    removes content.
    """
    start = None
    body_size = _body_font_size(blocks)
    for index, block in enumerate(blocks):
        if block.type != "text" or not block.text:
            continue
        key = re.sub(r"[^\w]", "", _LEADING_NUMBER_RE.sub("", structural_text(block.text)).casefold())
        if key in _BIBLIOGRAPHY_TITLES:
            start = index
    if start is None:
        return set()
    zone: set[int] = set()
    for index in range(start + 1, len(blocks)):
        block = blocks[index]
        if block.type != "text" or not block.text:
            continue
        shape = structural_text(block.text)
        if len(shape) <= 120:
            if _BIBLIOGRAPHY_END_RE.match(shape):
                break
            # "A Proofs" / "B.1 Setup" open an appendix only when typeset as a
            # title; "A. Smith, B. Jones ..." is an author-initial reference.
            emphasized = block.is_bold or bool(
                block.font_size and body_size > 0 and block.font_size >= 1.1 * body_size)
            if emphasized and _LETTER_SECTION_RE.match(shape):
                break
            if block.is_bold and len(shape) <= 80 and not shape.endswith((".", ",")):
                break
        zone.add(index)
    return zone


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
    # IEEE-style subsections: "A. Zero Collision MAC" (L2).
    if len(value) <= 120 and re.match(r"^[A-Z]\.\s+[A-Z][A-Za-z]", value):
        return 2
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
        figure_count = 0
        printed_toc_count = 0
        run_in_count = 0
        uncertain_count = 0
        for block in region_blocks:
            metadata = block.metadata or {}
            role = str(metadata.get("role") or metadata.get("artifact_role") or metadata.get("source_role") or "").lower()
            if role in {"margin", "margin_line", "header", "footer", "footnote", "marginal", "sidebar"}:
                margin_count += 1
            # grid_like：provider 判出的无框线表格区（find_tables 没抓成表，但几何
            # 上是网格）。它和 in_table 一样是"这里是表不是正文"，按表格区算污染。
            if (
                role in {"table", "table_cell", "table_region"}
                or metadata.get("in_table")
                or metadata.get("grid_like")
            ):
                table_count += 1
            if metadata.get("figure_like") or role in {"figure", "figure_label"}:
                figure_count += 1
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
        if figure_count:
            contamination += 0.65 * figure_count / count
            factors.append("figure_region")
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


@measured_once
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
    profile: TypographyProfile,
    in_bibliography: bool = False,
) -> tuple[list[EvidenceAtom], int | None, int | None, int | None, float | None]:
    metadata = block.metadata or {}
    # ``title`` keeps the transport form (``**bold**``) because it is also the
    # candidate's title; every *shape* decision uses the structural form.
    shape = structural_text(title)
    text_len = len(title)
    short = text_len <= 140
    medium = text_len <= 220
    evidence: list[EvidenceAtom] = []
    # Measured typography scales how much a signal may say about the *level*.
    # Existence stays a per-block judgement (see ``styled_prose_negative``):
    # a document-wide average must not veto an individual heading.
    style_level_confidence = _STYLE_LEVEL_CONFIDENCE_FLOOR + (
        0.97 - _STYLE_LEVEL_CONFIDENCE_FLOOR
    ) * profile.style_trust
    numbering_level_confidence = _NUMBERING_LEVEL_CONFIDENCE_FLOOR + (
        0.94 - _NUMBERING_LEVEL_CONFIDENCE_FLOOR
    ) * profile.numbering_trust

    style_level = infer_style_level(block)
    outline_level = _safe_int(metadata.get("outline_level"))
    # A PDF has no paragraph styles: its ``is_heading_style`` flag is the
    # provider's own font-size heuristic, already represented by ``font_ratio``.
    # Counting it again as an *explicit* author style (LR 120) let enlarged
    # figure labels outrank real headings.
    inferred_pdf_style = _source_for(block) == "pdf" and style_level is None
    if (block.is_heading_style and not inferred_pdf_style) or style_level is not None:
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
            level_confidence=style_level_confidence,
            provenance={
                "style": metadata.get("style"),
                "style_id": metadata.get("style_id"),
            },
        ))

    # Numbering depth is relative to the document's own numbering root: a
    # document whose top sections are all "1.1" is not a document of level-2
    # sections (see typography._analyse_numbering).
    visible_numbering_level = profile.numbering_level_for(block.id)
    if visible_numbering_level is None:
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
            level_confidence=numbering_level_confidence,
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
        and not shape.endswith(_SENTENCE_END_CHARS)
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

    if (
        (block.is_heading_style or block.is_bold or (font_ratio or 0.0) >= 1.10)
        and is_prose_shaped(shape)
    ):
        # An author can apply a heading style, bold, or a larger font to a whole
        # paragraph; the shape of the text is then the only honest witness.
        # This is a per-block veto, deliberately not a document-wide reliability
        # cut — over-styling one paragraph says nothing about the next heading.
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="styled_prose_negative",
            polarity=-1,
            observed_value={"length": text_len, "shape": shape[:60]},
            reliability=0.90,
            correlation_group="prose_shape",
        ))

    if profile.wears_body_signature(block.id):
        # The document distinguishes headings from running text by size or
        # weight somewhere, and this block sits on the text side of that line
        # while claiming to be a heading.  Shape cannot see this: "Status: In
        # review" is short and carries no sentence punctuation.
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="body_signature_negative",
            polarity=-1,
            observed_value={
                "font_size": block.font_size,
                "is_bold": bool(block.is_bold),
                "body_signature": list(profile.body_signature or ()),
            },
            reliability=0.90,
            correlation_group="body_signature",
        ))

    if in_bibliography:
        # Numbered reference entries ("12. A. Author. Title. In Proc. ...") share
        # the shape of numbered headings; inside the bibliography they are list
        # items, not sections.
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="bibliography_negative",
            polarity=-1,
            observed_value=shape[:40],
            reliability=0.95,
            correlation_group="artifact_region",
        ))

    if is_non_lexical(title):
        # Page numbers, equation numbers, numeric table rows and formula
        # fragments are short standalone lines too; without words they cannot
        # name a section, whatever their geometry says.
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="non_lexical_negative",
            polarity=-1,
            observed_value=shape[:40],
            reliability=0.97,
            correlation_group="lexical_shape",
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

    role = str(metadata.get("role") or metadata.get("artifact_role") or metadata.get("source_role") or "").lower()
    if metadata.get("semantic_role") == "front_matter" or is_metadata_line(title):
        evidence.append(_make_evidence(block=block, anchor=anchor,
            kind="document_metadata_negative", polarity=-1, observed_value="front_matter",
            reliability=0.99, correlation_group="document_metadata"))
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
    elif role in {"printed_toc", "printed_toc_line", "toc_line"} or has_dot_leader(title):
        evidence.append(_make_evidence(
            block=block,
            anchor=anchor,
            kind="printed_toc_negative",
            polarity=-1,
            observed_value=role or "dot_leader",
            reliability=0.96,
            correlation_group="artifact_region",
        ))
    elif role in {"margin", "margin_line", "header", "footer", "footnote", "marginal", "sidebar"}:
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


def generate_heading_candidate_set(
    blocks: list[Block],
    profile: TypographyProfile | None = None,
) -> HeadingCandidateSet:
    """Generate the full, uncapped candidate/evidence set for decoding.

    Negative-only and audit-only sites remain present for diagnostics.  LLM
    budgets are intentionally absent from this function.  *profile* is the
    measured typography (computed here when the caller has none); it decides how
    much authority style and numbering carry over heading *levels*.
    """
    if not blocks:
        return HeadingCandidateSet()

    profile = profile or analyze_typography(blocks)
    body_size = _body_font_size(blocks)
    regions, index_region = _build_region_assessments(blocks)
    bibliography = bibliography_zone(blocks)
    candidates: list[HeadingCandidate] = []

    for index, block in enumerate(blocks):
        if block.type != "text" or not block.text:
            continue
        # A02：标题跨度与块解耦。run-in 标题只占段首那一截，块内其余字符仍归正文；
        # 引导词 / 公式前标签 / 表格字段名一律不切（detect_title_fragment 负责区分）。
        leading, trailing_end, title, fragment_role = detect_title_fragment(block)
        if not title:
            continue
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
            _extract_evidence(block, title, anchor, region, body_size, profile,
                              in_bibliography=index in bibliography)
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


def _prose_penalty(candidate: HeadingCandidate) -> int:
    """明显正文候选的降档位（A05：「压低明显正文候选，而不只按全局分数取前若干项」）。

    只按全局分数排序时，一个「带样式的正文段」只要分数略高，就能挤掉一个证据中等但
    所在区域缺观察的真标题。所以命中正文类负证据的候选先降一档，再参与区域内排序——
    这是**排序降权**，不是删除：它仍在全量 CandidateSet 里，仍进确定性全局推断。
    """
    negatives = {
        "body_signature_negative",
        "list_prose_negative",
        "table_region_negative",
        "printed_toc_negative",
        "styled_prose_negative",
        "caption_negative",
        "margin_negative",
        "non_lexical_negative",
        "bibliography_negative",
    }
    return 1 if any(atom.kind in negatives for atom in candidate.evidence) else 0


def _region_extra_quota(risk, factors: set[str]) -> int:
    """疑难区域的追加配额（A05 的「疑难追加」那一份）。

    追加依据是**结构性疑难**而不是分数高低：编号断裂、模板/样式变化、置信分歧。
    这些区域里确定性证据本身就不可信，正是最需要模型多看几眼的地方。
    """
    extra = 0
    if risk is not None:
        if risk.miss_probability >= 0.35:
            extra += 1
        if risk.contamination_probability >= 0.35:
            extra += 1
        if getattr(risk, "band", "safe") == "escape":
            extra += 1
    hard = {
        "numbering_break", "numbering_gap", "template_change", "style_shift",
        "mixed_template", "confidence_divergence",
    }
    if factors & hard:
        extra += 1
    return extra


def _cap_candidates(
    candidates: list[HeadingCandidate],
    blocks: list[Block],
    *,
    max_ratio: float | None = None,
    region_floor: int = 1,
    global_reserve_ratio: float = 0.25,
) -> list[HeadingCandidate]:
    """Apply the v1 LLM route cap without deleting the full CandidateSet.

    A05：预算分三份，且按**逻辑区域**分配而不是按位置切伪区域。

    - **区域保底**（``region_floor``）：每个逻辑区域至少拿到这么多名额。没有它，标题密集
      的后半篇会被前半篇同分候选整片挤掉——同分排序偏向早出现的 block 是既有事实。
    - **疑难追加**：编号断裂 / 模板变化 / 置信分歧 / 高风险带的区域按
      :func:`_region_extra_quota` 再拿名额。
    - **全局保留**（``global_reserve_ratio``）：剩下的按全局排序发，让强证据跨区竞争。

    此前这里按 ``index * region_count // len(ordered)`` 把候选切成最多 16 个**位置**伪区域。
    位置均衡解决了「前半篇吃满名额」，但它对「哪个区域真的疑难」一无所知：排版正常的长章节
    与编号断裂的短章节拿到同样名额。改按逻辑区域（``candidate.region_id``，与区域风险同一
    口径）分配后，配额才与风险对齐。

    未被路由的候选**继续进入确定性全局推断**——本函数只裁剪「给模型看哪些」，不裁剪证据。
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

    def rank(candidate: HeadingCandidate) -> tuple[int, float, float, int, int]:
        return (
            _prose_penalty(candidate),
            -candidate.promotion_probability,
            -candidate.heading_probability,
            -evidence_diversity(candidate.evidence),
            candidate.block_id,
        )

    def balanced(pool, budget):
        """按逻辑区域分配 ``budget``：保底 → 疑难追加 → 全局保留。"""
        if not pool or budget <= 0:
            return []
        by_region: dict[str, list[HeadingCandidate]] = {}
        for candidate in pool:
            by_region.setdefault(candidate.region_id or "document:0", []).append(candidate)
        for region_id in by_region:
            by_region[region_id].sort(key=rank)

        reserve = int(budget * max(0.0, min(global_reserve_ratio, 1.0)))
        quota_budget = max(budget - reserve, 0)
        quotas: dict[str, int] = {}
        for region_id, members in by_region.items():
            risk = members[0].region_risk
            factors = set(getattr(risk, "factors", ()) or ())
            quotas[region_id] = min(
                len(members), region_floor + _region_extra_quota(risk, factors),
            )
        # 保底 + 追加超出可分配额度时，按区域风险升序削减（疑难区域最后被削）。
        while sum(quotas.values()) > quota_budget and any(quotas.values()):
            ordered_regions = sorted(
                (region for region, count in quotas.items() if count > 0),
                key=lambda region: (
                    by_region[region][0].region_risk.miss_probability
                    + by_region[region][0].region_risk.contamination_probability
                ),
            )
            quotas[ordered_regions[0]] -= 1

        chosen: list[HeadingCandidate] = []
        taken: set[int] = set()
        for region_id, count in quotas.items():
            for candidate in by_region[region_id][:count]:
                chosen.append(candidate)
                taken.add(id(candidate))
        remaining = budget - len(chosen)
        if remaining > 0:
            for candidate in sorted(pool, key=rank):
                if remaining <= 0:
                    break
                if id(candidate) in taken:
                    continue
                chosen.append(candidate)
                taken.add(id(candidate))
                remaining -= 1
        return chosen

    if len(strong) >= max_candidates:
        kept = {id(candidate) for candidate in balanced(strong, max_candidates)}
    else:
        kept = {id(candidate) for candidate in strong}
        weak_budget = max_candidates - len(strong)
        kept.update(id(candidate) for candidate in balanced(weak, weak_budget))
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
