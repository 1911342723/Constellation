"""Document-wide heading inference for Constellation Stage 3-4.

Every possible heading site is decoded once with states ``NONE/L1..L6``.
Candidate evidence, region risk, aligned LLM votes and inverse-audit proposals
are emissions in the same dynamic program.  No heading may be inserted after
this decoder; forced closure only slices the selected physical cursor sequence.
"""
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from collections import Counter

from infrastructure.models import Block
from modules.parser.config import ParserConfig
from modules.parser.heading_candidates import (
    _SENTENCE_END_CHARS,
    _body_font_size,
    analyze_candidate_regions,
)
from modules.parser.hierarchy import hierarchy_is_legal, is_legal_level_transition
from modules.parser.schemas import (
    ChapterNode,
    HeadingCandidate,
    HeadingCandidateSet,
    LLMAnchorVote,
)

logger = logging.getLogger(__name__)
_LABELS = ("NONE", "L1", "L2", "L3", "L4", "L5", "L6")
_LEVEL_PRIOR = (0.42, 0.25, 0.14, 0.09, 0.06, 0.04)
_EPSILON = 1e-9
_STRONG_STRUCTURAL = {
    "visible_numbering",
    "effective_numbering",
    "explicit_heading_style",
    "outline_level",
    "toc_destination",
}


def _clip_probability(value: float) -> float:
    return min(max(float(value), _EPSILON), 1.0 - _EPSILON)


def _logit(value: float) -> float:
    probability = _clip_probability(value)
    return math.log(probability / (1.0 - probability))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _normalise(text: str) -> str:
    return " ".join((text or "").strip().casefold().split())


def _text_similarity(left: str, right: str) -> float:
    left_n = _normalise(left)
    right_n = _normalise(right)
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0
    if left_n in right_n or right_n in left_n:
        return 0.94
    if min(len(left_n), len(right_n)) <= 5:
        return 0.0
    return SequenceMatcher(None, left_n, right_n, autojunk=False).ratio()


@dataclass
class _Site:
    block_id: int
    block: Block
    candidate: HeadingCandidate | None = None
    anchors: list[ChapterNode] = field(default_factory=list)
    votes: list[LLMAnchorVote] = field(default_factory=list)
    inverse_proposal: bool = False


class GlobalHeadingInference:
    """Viterbi decoder over physical proposal sites in document order."""

    def __init__(
        self,
        blocks: Sequence[Block],
        candidates: HeadingCandidateSet | Iterable[HeadingCandidate] | None = None,
        parser_config: ParserConfig | None = None,
    ) -> None:
        self.blocks = list(blocks)
        self.block_map = {block.id: block for block in self.blocks}
        self.body_font_size = _body_font_size(self.blocks)
        self.parser_config = parser_config or ParserConfig()
        if isinstance(candidates, HeadingCandidateSet):
            self.candidate_set = candidates
        else:
            self.candidate_set = HeadingCandidateSet(candidates=list(candidates or []))
        self._risk_by_block_id = {}
        for assessment in analyze_candidate_regions(self.blocks).values():
            risk = self.candidate_set.region_risks.get(
                assessment.region_id, assessment.risk,
            )
            for block_id in assessment.block_ids:
                self._risk_by_block_id[block_id] = risk
        # block_id -> 确认兄弟的众数层级（样式签名放大，见 _compute_sibling_boosts）。
        self._sibling_boost: dict[int, int] = {}

    def inverse_audit_proposals(
        self,
        aligned_chapters: Sequence[ChapterNode],
    ) -> list[HeadingCandidate]:
        """Return unconfirmed candidates as proposals, never as inserted nodes."""
        llm_ids = {chapter.start_block_id for chapter in aligned_chapters}
        return [
            candidate for candidate in self.candidate_set.candidates
            if candidate.block_id not in llm_ids
        ]

    def decode(self, aligned_chapters: Sequence[ChapterNode]) -> list[ChapterNode]:
        """Select headings and levels jointly under hard hierarchy legality."""
        sites = self._build_sites(aligned_chapters)
        if not sites:
            return []

        self._sibling_boost = self._compute_sibling_boosts(sites)
        emissions = [self._emission(site) for site in sites]
        labels = self._viterbi(emissions)
        decoded = [
            self._chapter_for(site, label, emission)
            for site, label, emission in zip(sites, labels, emissions, strict=True)
            if label != "NONE"
        ]

        if not hierarchy_is_legal(decoded):  # defensive invariant, not repair
            raise RuntimeError("global heading decoder produced an illegal hierarchy")

        logger.info(
            "[GlobalInference] %d sites (%d inverse proposals) -> %d headings",
            len(sites),
            sum(site.inverse_proposal for site in sites),
            len(decoded),
        )
        return decoded

    def _build_sites(self, aligned_chapters: Sequence[ChapterNode]) -> list[_Site]:
        sites: dict[int, _Site] = {}
        llm_ids = {chapter.start_block_id for chapter in aligned_chapters}

        # Full uncapped candidates are the inverse-audit proposal channel.  A
        # proposal is merely a site/emission; only Viterbi may promote it.
        candidate_by_id: dict[int, HeadingCandidate] = {}
        for candidate in self.candidate_set.candidates:
            if candidate.block_id not in self.block_map:
                continue
            previous = candidate_by_id.get(candidate.block_id)
            if previous is None or (
                candidate.promotion_probability,
                candidate.heading_probability,
                candidate.candidate_id,
            ) > (
                previous.promotion_probability,
                previous.heading_probability,
                previous.candidate_id,
            ):
                candidate_by_id[candidate.block_id] = candidate

        for block_id, candidate in candidate_by_id.items():
            block = self.block_map[block_id]
            sites[block_id] = _Site(
                block_id=block_id,
                block=block,
                candidate=candidate,
                inverse_proposal=block_id not in llm_ids,
            )

        for chapter in aligned_chapters:
            block = self.block_map.get(chapter.start_block_id)
            if block is None:
                continue
            site = sites.setdefault(
                chapter.start_block_id,
                _Site(block_id=chapter.start_block_id, block=block),
            )
            site.anchors.append(copy.deepcopy(chapter))
            raw_votes = chapter.anchor_votes or [LLMAnchorVote(
                raw_block_id=chapter.start_block_id,
                aligned_block_id=chapter.start_block_id,
                title=chapter.title,
                snippet=chapter.snippet,
                level=max(1, min(chapter.level, 6)),
                confidence=max(0.0, min(chapter.confidence, 1.0)),
                alignment_score=chapter.alignment_score,
                out_of_candidate=chapter.out_of_candidate,
                window_index=0,
            )]
            site.votes.extend(
                vote for vote in raw_votes if self._vote_passes_post_alignment_checks(vote, site)
            )

        # Sites without either viable candidate support or a physically valid
        # aligned vote cannot affect the DP and are removed here.
        return [
            sites[block_id] for block_id in sorted(sites)
            if sites[block_id].candidate is not None or sites[block_id].votes
        ]

    def _vote_passes_post_alignment_checks(self, vote: LLMAnchorVote, site: _Site) -> bool:
        """Validate a vote only after its physical position is known.

        Strict-first is defined here, not in the router: an observation that
        was outside a shard's candidate table may align onto a real document
        candidate and must then be treated normally.  A genuinely table-out
        site is admitted only inside a locally assessed escape region and only
        after conservative title/shape gates.
        """
        block = site.block
        if block.type != "text" or not block.text:
            return False
        text = " ".join(block.text.strip().split())
        query = vote.snippet or vote.title
        similarity = _text_similarity(query, text)
        if similarity < 0.30 and vote.alignment_score < 0.40:
            logger.warning(
                "[GlobalInference] Rejected misaligned LLM vote raw=%d aligned=%d",
                vote.raw_block_id,
                vote.aligned_block_id,
            )
            return False

        candidate = site.candidate
        if vote.out_of_candidate and candidate is None and self.parser_config.strict_first_routing:
            risk = self._risk_by_block_id.get(site.block_id)
            if risk is None or risk.band != "escape":
                return False
            if len(text) > self.parser_config.escape_vote_max_text_len:
                return False
            words = text.split()
            if len(words) > self.parser_config.escape_vote_max_words:
                return False
            if len(words) <= 1 and len(text) > 64:
                return False
            if text.endswith((",", ";", ":", "，", "；", "：")):
                return False
            if text.endswith((".", "。", "!", "?", "！", "？")) and len(words) > 6:
                return False
            if similarity < self.parser_config.escape_vote_title_similarity:
                return False

        if candidate and candidate.region_risk.contamination_probability >= 0.90:
            positive = candidate.evidence_kinds(polarity=1)
            if not (positive & _STRONG_STRUCTURAL) and similarity < 0.85:
                return False
        return True

    def _style_signature(self, block: Block) -> tuple[float | None, bool] | None:
        """Discriminative (font_size, bold) fingerprint, or None when useless.

        Featureless blocks (no size, no bold) carry zero style information —
        every plain line would share the signature, so it is rejected here.
        """
        if block.type != "text":
            return None
        size = round(block.font_size, 1) if block.font_size else None
        bold = bool(block.is_bold)
        if size is None and not bold:
            return None
        return (size, bold)

    def _compute_sibling_boosts(self, sites: Sequence[_Site]) -> dict[int, int]:
        """Map unconfirmed candidate sites to confirmed same-style sibling levels.

        既有恢复通道覆盖"字号更大"（结构地板需 ratio>=1.1）；**与正文同字号、
        仅加粗**的标题款式字号比为 1.0，一旦 LLM 漏标就永久丢失。当 LLM 已
        确认 >=2 个同签名（同字号 + 加粗状态）锚点时，"同款式"本身就是强
        物理证据：给同签名的未确认候选一个激活地板，是否选中仍由全局 DP
        在层级合法性约束下裁决。

        精度防线：签名必须区别于正文签名；候选无任何负证据；短行（<=80
        字符）且不以句读结尾；污染区（>=0.65）不放大。
        """
        body_signature = (
            round(self.body_font_size, 1) if self.body_font_size > 0 else None,
            False,
        )
        confirmed_levels: dict[tuple, list[int]] = {}
        confirmed_count: dict[tuple, int] = {}
        for site in sites:
            if not site.votes:
                continue
            signature = self._style_signature(site.block)
            if signature is None or signature == body_signature:
                continue
            confirmed_count[signature] = confirmed_count.get(signature, 0) + 1
            confirmed_levels.setdefault(signature, []).extend(
                vote.level for vote in site.votes
            )

        boosts: dict[int, int] = {}
        for site in sites:
            if site.votes or site.candidate is None:
                continue
            if site.candidate.evidence_kinds(polarity=-1):
                continue
            if site.candidate.region_risk.contamination_probability >= 0.65:
                continue
            signature = self._style_signature(site.block)
            if signature is None or confirmed_count.get(signature, 0) < 2:
                continue
            text = " ".join((site.block.text or "").strip().split())
            if not text or len(text) > 80:
                continue
            if text.endswith(_SENTENCE_END_CHARS):
                continue
            modal_level = Counter(confirmed_levels[signature]).most_common(1)[0][0]
            boosts[site.block_id] = max(1, min(int(modal_level), 6))
        if boosts:
            logger.info(
                "[GlobalInference] Style-signature boost for %d unconfirmed sibling(s): %s",
                len(boosts), sorted(boosts),
            )
        return boosts

    def _physical_activation(self, block: Block) -> float:
        """Conservative prior for an aligned LLM site absent from candidates."""
        text = (block.text or "").strip()
        if not text:
            return 0.02
        if block.is_heading_style:
            return 0.85
        if block.is_potential_title(min_body_size=self.body_font_size):
            return 0.62
        if block.is_bold and len(text) <= 80 and not text.endswith((".", "。")):
            return 0.48
        if block.font_size and self.body_font_size > 0 and block.font_size >= self.body_font_size * 1.2:
            return 0.52
        return 0.04

    def _candidate_activation(self, site: _Site) -> float:
        candidate = site.candidate
        if candidate is None:
            return self._physical_activation(site.block)
        # Without an LLM vote this is precisely the inverse-audit proposal
        # probability.  Confirmed sites may use the broader heading emission.
        activation = (
            candidate.heading_probability
            if site.votes else candidate.promotion_probability
        )
        risk = candidate.region_risk
        positive = candidate.evidence_kinds(polarity=1)
        if risk.contamination_probability >= 0.65 and not (positive & _STRONG_STRUCTURAL):
            activation *= max(0.1, 1.0 - risk.contamination_probability)
        if candidate.admission == "audit_only" and not site.votes:
            activation = min(activation, candidate.promotion_probability)
        # 样式签名放大：与 >=2 个已确认锚点同款（同字号+加粗状态）的未确认
        # 候选获得激活地板（准入门槛见 _compute_sibling_boosts）。
        if not site.votes and site.block_id in self._sibling_boost:
            activation = max(activation, 0.62)
        return min(max(activation, 0.001), 0.999)

    def _emission(self, site: _Site) -> dict[str, float]:
        activation = self._candidate_activation(site)
        log_odds = _logit(activation)

        candidate = site.candidate
        contamination = candidate.region_risk.contamination_probability if candidate else 0.0
        valid_votes: list[tuple[LLMAnchorVote, float]] = []
        for vote in site.votes:
            effective = vote.confidence * max(vote.alignment_score, 0.35)
            effective *= 1.0 - 0.45 * contamination
            effective = min(max(effective, 0.05), 0.995)
            # LLM votes are compared to a conservative 18% false-positive
            # baseline; duplicate overlap votes add evidence rather than being
            # deleted before inference.
            log_odds += 0.90 * (_logit(effective) - _logit(0.18))
            valid_votes.append((vote, effective))
        heading_probability = _sigmoid(log_odds)

        if candidate and candidate.heading_probability > _EPSILON:
            conditional = [
                max(candidate.level_probabilities[f"L{level}"] / candidate.heading_probability, _EPSILON)
                for level in range(1, 7)
            ]
        else:
            conditional = list(_LEVEL_PRIOR)
        level_scores = [math.log(value) for value in conditional]

        for vote, effective in valid_votes:
            for level in range(1, 7):
                distance = abs(level - vote.level)
                if distance == 0:
                    level_scores[level - 1] += 3.0 * effective
                elif distance == 1:
                    level_scores[level - 1] += 0.35 * effective

        # 签名放大站点：层级向确认兄弟的众数层级看齐（弱于真实投票的 3.0）。
        sibling_level = self._sibling_boost.get(site.block_id)
        if sibling_level is not None and not site.votes:
            level_scores[sibling_level - 1] += 1.2

        maximum = max(level_scores)
        weights = [math.exp(score - maximum) for score in level_scores]
        total = math.fsum(weights)
        conditional = [weight / total for weight in weights]

        # Factor heading existence from conditional level choice.  These are
        # Viterbi potentials, not seven mutually exclusive calibrated class
        # posteriors: scaling each Lx by its normalized conditional probability
        # made an otherwise strong heading lose to NONE merely because its
        # level evidence was diffuse (the label-bias root cause of swallowed
        # large-font headings).  The best supported level now carries the
        # heading activation; relative conditional support still chooses the
        # level and hierarchy transitions still constrain the path globally.
        best_conditional = max(conditional)
        emission = {"NONE": 1.0 - heading_probability}
        for level, probability in enumerate(conditional, start=1):
            relative_level_support = probability / best_conditional
            emission[f"L{level}"] = heading_probability * relative_level_support
        return emission

    @staticmethod
    def _viterbi(emissions: Sequence[dict[str, float]]) -> list[str]:
        # State is the most recently selected level (0 means none selected).
        previous_scores: dict[int, float] = {0: 0.0}
        layers: list[dict[int, tuple[float, int, str]]] = []

        for emission in emissions:
            current: dict[int, tuple[float, int, str]] = {}
            for previous_level in sorted(previous_scores):
                previous_score = previous_scores[previous_level]
                none_score = previous_score + math.log(max(emission["NONE"], _EPSILON))
                existing = current.get(previous_level)
                if existing is None or none_score > existing[0]:
                    current[previous_level] = (none_score, previous_level, "NONE")

                for level in range(1, 7):
                    if not is_legal_level_transition(previous_level, level):
                        continue
                    score = previous_score + math.log(max(emission[f"L{level}"], _EPSILON))
                    existing = current.get(level)
                    if existing is None or score > existing[0]:
                        current[level] = (score, previous_level, f"L{level}")
            layers.append(current)
            previous_scores = {state: entry[0] for state, entry in current.items()}

        state = max(previous_scores, key=lambda item: previous_scores[item])
        labels = ["NONE"] * len(emissions)
        for index in range(len(emissions) - 1, -1, -1):
            _score, previous_state, label = layers[index][state]
            labels[index] = label
            state = previous_state
        return labels

    def _chapter_for(
        self,
        site: _Site,
        label: str,
        emission: dict[str, float],
    ) -> ChapterNode:
        level = int(label[1:])
        if site.anchors:
            chapter = max(
                site.anchors,
                key=lambda anchor: (
                    anchor.confidence * max(anchor.alignment_score, 0.2),
                    len(anchor.anchor_votes),
                ),
            )
            result = copy.deepcopy(chapter)
        else:
            candidate = site.candidate
            title = candidate.title if candidate else (site.block.text or "").strip()
            snippet = candidate.snippet if candidate else title[:80]
            result = ChapterNode(
                block_id=site.block_id,
                title=title,
                level=level,
                snippet=snippet,
                confidence=emission[label],
            )

        if site.candidate and site.candidate.title:
            result.title = site.candidate.title
            result.snippet = site.candidate.snippet
        elif site.block.type == "text" and site.block.text:
            result.title = site.block.text.strip()
            result.snippet = result.title[:80]
        result.start_block_id = site.block_id
        result.level = level
        result.confidence = min(max(1.0 - emission["NONE"], 0.0), 1.0)
        result.anchor_votes = list(site.votes)
        result.source_windows = sorted({vote.window_index for vote in site.votes})
        result.alignment_score = max(
            (vote.alignment_score for vote in site.votes),
            default=1.0 if site.candidate else 0.0,
        )
        result.out_of_candidate = bool(site.votes) and all(
            vote.out_of_candidate for vote in site.votes
        )
        result.globally_inferred = True
        return result


__all__ = ["GlobalHeadingInference"]
