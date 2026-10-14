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
from modules.parser.analysis_context import block_index
from modules.parser.config import ParserConfig
from modules.parser.heading_candidates import (
    _SENTENCE_END_CHARS,
    _body_font_size,
    analyze_candidate_regions,
)
from modules.parser.hierarchy import (
    hierarchy_is_legal, is_legal_level_transition, _numbering_series,
    evaluate_soft_constraints,
)
from modules.parser.titles import structural_text
from modules.parser.typography import TypographyProfile, analyze_typography
from modules.parser.evidence import VoteView, correlated_vote_gains
from modules.parser.calibration import calibrated_score, load_calibration, confidence_domain
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
# Above this share of bold text blocks, boldness stops discriminating.
_BOLD_SATURATION_RATIO = 0.60
# Level-bearing evidence, split by the trust family that governs it.
_NUMBERING_LEVEL_KINDS = {"visible_numbering", "effective_numbering"}
_STYLE_LEVEL_KINDS = {"explicit_heading_style", "outline_level", "toc_destination"}
# Weight of the measured style-class level in level log-space.  Sized to
# outrank the generic depth prior over its whole range (log(0.42/0.04) = 2.35)
# while staying below a confident model vote (3.0): a document's own repetition
# pattern beats an assumption about documents in general, but not a reader.
_CLASS_LEVEL_WEIGHT = 2.8


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
        profile: TypographyProfile | None = None,
        routed_block_ids: Iterable[int] | None = None,
    ) -> None:
        # 索引由本次解析共享的 BlockIndex 提供（L01），不再自建第三份。
        self._index = block_index(blocks)
        self.blocks = list(self._index.blocks)
        self.block_map = self._index.by_id
        self.body_font_size = _body_font_size(self.blocks)
        text_blocks = [
            block for block in self.blocks if block.type == "text" and block.text
        ]
        self._bold_ratio = (
            sum(1 for block in text_blocks if block.is_bold) / len(text_blocks)
            if text_blocks else 0.0
        )
        self.parser_config = parser_config or ParserConfig()
        self.profile = profile or analyze_typography(self.blocks)
        # A05：被送进路由视图（模型真的看过）的块 id。``None`` = 路由集合未知，
        # 此时「未请求」与「模型否定」不作区分，行为与从前一致。
        self._routed_block_ids = (
            None if routed_block_ids is None else set(routed_block_ids)
        )
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
        # 本文档的投票是否说出了深度差别（见 _votes_resolve_depth）。
        self._votes_resolve_depth = True
        # A01：上一次 decode 的逐站点留痕 ``(block_ids, emissions, labels)``。
        self._last_trace: tuple[tuple[int, ...], tuple[dict, ...], tuple[str, ...]] = ((), (), ())
        self.quality_report: dict = {}
        self._calibration = load_calibration(self.parser_config.confidence_calibration_path, self.parser_config)

    def initial_decode(self, policy=None):
        """A01 第 1 步：**无投票**的初判，返回 ``(章节, 复审计划)``。

        「无投票」是字面意思：不传任何 LLM 锚点，只吃确定性候选证据。走的是与终判
        完全相同的 :meth:`decode`，因此初判选出的「不确定站点」与终判用的是同一套权重；
        另写一个打分函数会让两者迟早漂移，那时选区就与最终决策无关了。

        返回的 :class:`~modules.parser.review_regions.ReviewPlan` 里三个不确定度是分开
        的（标题存在概率 / 级别分差 / 树结构不确定性），不合成单一「可靠度」——方案验收
        明确禁止「设一个 p>0.9 就宣称可靠」。
        """
        from modules.parser.review_regions import (
            ReviewPolicy,
            build_uncertainties,
            select_review_regions,
        )

        chapters = self.decode([])
        block_ids, emissions, labels = self._last_trace
        policy = policy or ReviewPolicy()
        candidate_by_block = {
            candidate.block_id: candidate for candidate in self.candidate_set.candidates
        }
        sites = build_uncertainties(
            block_ids, emissions, labels,
            candidate_by_block=candidate_by_block,
            block_by_id=self.block_map,
            policy=policy,
            ordered_block_ids=[block.id for block in self.blocks],
        )
        plan = select_review_regions(
            sites,
            ordered_block_ids=[block.id for block in self.blocks],
            block_by_id=self.block_map,
            policy=policy,
        )
        return chapters, plan

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
            self._last_trace = ((), (), ())
            return []

        self._sibling_boost = self._compute_sibling_boosts(sites)
        self._votes_resolve_depth = self._compute_votes_resolve_depth(sites)
        emissions = [self._emission(site) for site in sites]
        labels = self._decode_with_ancestors(sites, emissions)
        # A01：把本次解码的逐站点发射与标签留下，供「初判」读出竞争标签分数。
        # 留痕而不是另写一个打分函数——A01 的硬要求是复审前后走**同一个**解码器，
        # 两份实现迟早在某个权重上漂移，那时初判选出的不确定区域就与终判无关了。
        self._last_trace = (
            tuple(site.block_id for site in sites), tuple(emissions), tuple(labels),
        )
        decoded = [
            self._chapter_for(site, label, emission)
            for site, label, emission in zip(sites, labels, emissions, strict=True)
            if label != "NONE"
        ]

        if not hierarchy_is_legal(decoded):  # defensive invariant, not repair
            raise RuntimeError("global heading decoder produced an illegal hierarchy")

        report = evaluate_soft_constraints(decoded, self.blocks, list(self.candidate_set.candidates))
        self.quality_report = {
            "confidence_quality": "heuristic",
            "structure_verified": False,
            "soft_constraint_violations": [
                {"kind": v.kind, "block_ids": list(v.block_ids), "explanation": v.explanation}
                for v in report.violations if v.penalty > 0
            ],
            "virtual_containers": [
                {"number": list(v.number), "level": v.level, "child_block_id": v.child_block_id,
                 "is_virtual": True} for v in report.virtual_containers
            ],
        }
        for chapter in decoded:
            findings = [v.kind for v in report.violations
                        if v.penalty > 0 and chapter.start_block_id in v.block_ids]
            chapter._confidence_details["structure_findings"] = findings
            if findings:
                chapter._confidence_details["needs_confirmation"] = True

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
        if (block.metadata or {}).get("semantic_role") == "front_matter":
            return False
        if block.type != "text" or not block.text:
            return False
        # Shape gates and title comparison run on the structural form: the raw
        # text carries transport-level ``**`` emphasis that would defeat every
        # sentence-punctuation check below (see titles.structural_text).
        text = structural_text(block.text)
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

    def _bold_is_uninformative(self, signature: tuple[float | None, bool]) -> bool:
        """Whether a signature owes its distinctiveness to a saturated bold flag.

        In a document that is bold nearly everywhere, ``bold`` separates nothing:
        body paragraphs share the confirmed headings' signature and the boost
        would promote the whole document.  Only the *size* dimension may carry
        the signature in that case.
        """
        size, bold = signature
        if not bold or self._bold_ratio <= _BOLD_SATURATION_RATIO:
            return False
        return size is None or abs(size - self.body_font_size) < 0.05

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
            if self._bold_is_uninformative(signature):
                continue
            text = structural_text(site.block.text)
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

    def _compute_votes_resolve_depth(self, sites: Sequence[_Site]) -> bool:
        """Whether the votes tell depths apart where the document itself does.

        A vote asserting a level is evidence *about depth* only if the votes as
        a body separate depths.  Compare them against the style classes on the
        very blocks they landed on: when every vote says ``L1`` while those
        blocks belong to classes the document places at different depths, the
        reader has confirmed that headings exist and has said nothing at all
        about their hierarchy.  Such a vote must abstain on level rather than
        outrank the only signal that does speak — the same discipline as
        ``_bold_is_uninformative``: a signal that separates nothing in this
        document does not get to decide it.

        The comparison is deliberately made on shared blocks.  A document that
        really is flat has flat classes too, so the votes and the classes agree
        and nothing here fires.
        """
        vote_levels: set[int] = set()
        class_levels: set[int] = set()
        for site in sites:
            if not site.votes:
                continue
            class_level = self.profile.class_level_for(site.block_id)
            if class_level is None:
                continue
            vote_levels.update(vote.level for vote in site.votes)
            class_levels.add(class_level)

        if len(class_levels) < 2 or len(vote_levels) > 1:
            return True
        logger.info(
            "[GlobalInference] Votes assert a single level across %d style-class "
            "depth(s); their level claim abstains where a class level exists",
            len(class_levels),
        )
        return False

    def _class_level_prior(self, site: _Site) -> tuple[int, float] | None:
        """Depth implied by the block's style class, and how far it may speak.

        The class level is read off the document's own class sequence under the
        constraint this decoder already enforces, so it survives typography
        that carries no depth signal at all — a document whose deeper headings
        are simply rendered *larger* has nothing else to go on.  Where style or
        numbering do state a level, this prior yields to them in proportion to
        how much that family is trusted on this particular document.
        """
        level = self.profile.class_level_for(site.block_id)
        if level is None:
            return None
        positive = (
            site.candidate.evidence_kinds(polarity=1) if site.candidate else set()
        )
        if positive & _NUMBERING_LEVEL_KINDS:
            weight = _CLASS_LEVEL_WEIGHT * (1.0 - self.profile.numbering_trust)
        elif positive & _STYLE_LEVEL_KINDS:
            weight = _CLASS_LEVEL_WEIGHT * (1.0 - self.profile.style_trust)
        else:
            weight = _CLASS_LEVEL_WEIGHT
        return level, weight

    def _physical_activation(self, block: Block) -> float:
        """Conservative prior for an aligned LLM site absent from candidates."""
        text = structural_text(block.text)
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
        if (site.block.metadata or {}).get("semantic_role") == "front_matter":
            return 0.001
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
        # A05：「未请求模型」与「模型否定」是两件事，此前一律按同一档处理。
        # 候选被送进过路由视图、模型看过却没确认它 —— 那是一次**观察到的否定**，
        # 应当轻微压低；而根本没被路由的候选，模型对它一无所知，不该因此吃罚分
        # （否则预算裁掉谁，谁就被判得更像正文，配额裁剪会自我实现）。
        # 默认 routed_block_ids=None 表示「路由集合未知」，两类都不罚——行为与从前逐字一致。
        if (
            self._routed_block_ids is not None
            and not site.votes
            and site.block_id in self._routed_block_ids
        ):
            # A reader that was shown the candidate and did not mark it has
            # voted NONE.  Apply that observation in odds space, symmetric to
            # the positive-vote update in _emission: strong deterministic
            # evidence (outline destination, explicit style) still survives a
            # single silence, a borderline proposal does not.
            activation = _sigmoid(
                _logit(activation)
                + math.log(self.parser_config.llm_silence_likelihood_ratio)
            )
        return min(max(activation, 0.001), 0.999)

    def _emission(self, site: _Site) -> dict[str, float]:
        activation = self._candidate_activation(site)
        log_odds = _logit(activation)

        candidate = site.candidate
        contamination = candidate.region_risk.contamination_probability if candidate else 0.0
        # A04：同一站点上的多张投票按“模型/提示词版本/上下文重叠”聚类，
        # 相关簇内按置信度降序施加几何衰减增益（correlated_vote_gains）。
        # 重复提供同一窗口（相同 window_index）归入同簇、增益快速衰减，
        # 因此不会让结论无依据地变强；真正独立的窗口各自成簇、满额累加。
        # window_index 相同即视为同一次观察；缺省单模型单提示词，多模型/
        # 多提示词接入时只需在此填入真实标识即可自动分簇。
        vote_views = [
            VoteView(
                key=id(vote),
                confidence=vote.confidence,
                window_index=vote.window_index,
                context_span=vote.context_span,
                model=vote.model,
                prompt_version=vote.prompt_version,
            )
            for vote in site.votes
        ]
        vote_gains = correlated_vote_gains(vote_views)
        valid_votes: list[tuple[LLMAnchorVote, float, float]] = []
        for vote, gain in zip(site.votes, vote_gains, strict=True):
            effective = vote.confidence * max(vote.alignment_score, 0.35)
            effective *= 1.0 - 0.45 * contamination
            effective = min(max(effective, 0.05), 0.995)
            # LLM votes are compared to a conservative 18% false-positive
            # baseline; correlated (duplicate/overlap) votes contribute a
            # geometrically decayed share rather than a full independent vote.
            log_odds += gain * 0.90 * (_logit(effective) - _logit(0.18))
            valid_votes.append((vote, effective, gain))
        heading_probability = _sigmoid(log_odds)

        if candidate and candidate.heading_probability > _EPSILON:
            conditional = [
                max(candidate.level_probabilities[f"L{level}"] / candidate.heading_probability, _EPSILON)
                for level in range(1, 7)
            ]
        else:
            conditional = list(_LEVEL_PRIOR)
        level_scores = [math.log(value) for value in conditional]

        class_prior = self._class_level_prior(site)
        # The vote's level claim is heard unless it turned out to separate
        # nothing (see _compute_votes_resolve_depth).  Even then it keeps its
        # full say over heading *existence* above, and it still speaks wherever
        # no class level exists — there it is the only reader in the room.
        if self._votes_resolve_depth or class_prior is None:
            for vote, effective, gain in valid_votes:
                for level in range(1, 7):
                    distance = abs(level - vote.level)
                    if distance == 0:
                        level_scores[level - 1] += gain * 3.0 * effective
                    elif distance == 1:
                        level_scores[level - 1] += gain * 0.35 * effective

        if class_prior is not None:
            class_level, class_weight = class_prior
            level_scores[class_level - 1] += class_weight

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

    def _decode_with_ancestors(self, sites: Sequence[_Site], emissions: Sequence[dict]) -> list[str]:
        """Bounded ancestor-aware search; numbering relations are soft evidence.

        Unlike a last-level state, each state retains the actual active parent
        sites. Missing/inconsistent numbering never fabricates a heading or
        forces a content deletion. With no useful relation evidence retain the
        exact historical decoder, avoiding an approximate-search regression.
        """
        numbers = [_numbering_series(structural_text(site.block.text)) for site in sites]
        if sum(number is not None for number in numbers) < 2 or not self.parser_config.hierarchy_relation_weight:
            return self._viterbi(emissions)
        states = {(): (0.0, None)}
        layers = []
        for index, emission in enumerate(emissions):
            layer = {}
            for stack, (score, _unused) in states.items():
                options = [(stack, "NONE", 0.0)]
                for level in range(1, min(len(stack) + 1, 6) + 1):
                    penalty = 0.0
                    number = numbers[index]
                    parent = numbers[stack[level - 2]] if level > 1 else None
                    sibling = numbers[stack[level - 1]] if level <= len(stack) else None
                    if number and parent:
                        if len(number) <= len(parent) or number[:len(parent)] != parent:
                            penalty += 1.0
                        elif len(number) != len(parent) + 1:
                            penalty += 0.5
                    if number and sibling:
                        if number[:-1] == sibling[:-1] and len(number) == len(sibling):
                            penalty -= 0.15
                        elif number[:len(sibling)] == sibling:
                            penalty += 0.75
                    options.append((stack[:level - 1] + (index,), f"L{level}", penalty))
                for next_stack, label, penalty in options:
                    next_score = score + math.log(max(emission[label], _EPSILON))
                    next_score -= self.parser_config.hierarchy_relation_weight * penalty
                    old = layer.get(next_stack)
                    if old is None or next_score > old[0]:
                        layer[next_stack] = (next_score, stack, label)
            ordered = sorted(layer, key=lambda key: (-layer[key][0], key))
            layer = {key: layer[key] for key in ordered[:self.parser_config.hierarchy_beam_width]}
            layers.append(layer)
            states = {key: (value[0], None) for key, value in layer.items()}
        state = max(states, key=lambda key: states[key][0])
        labels = []
        for layer in reversed(layers):
            _score, state, label = layer[state]
            labels.append(label)
        return list(reversed(labels))

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
        domain = confidence_domain(site.block, bool(site.votes))
        level_total = sum(emission[f"L{level}"] for level in range(1, 7))
        level_supports = sorted((emission[f"L{level}"] for level in range(1, 7)), reverse=True)
        level_margin = (level_supports[0] - level_supports[1]) / max(level_supports[0] + level_supports[1], _EPSILON)
        result._confidence_details = {
            "heading": calibrated_score(self._calibration, "heading", result.confidence, domain),
            "level": calibrated_score(self._calibration, "level", emission[label] / max(level_total, _EPSILON), domain),
            "level_margin": level_margin,
            "needs_confirmation": emission[label] < max(emission.values()) or self.profile.flat_view or level_margin < 0.35,
        }
        result.anchor_votes = list(site.votes)
        result.source_windows = sorted({vote.window_index for vote in site.votes})
        result.alignment_score = max(
            (vote.alignment_score for vote in site.votes),
            default=1.0 if site.candidate else 0.0,
        )
        result._confidence_details["alignment"] = (
            calibrated_score(self._calibration, "alignment", result.alignment_score, domain)
            if site.votes else {"score": None, "probability": None, "quality": "not_observed"}
        )
        result.out_of_candidate = bool(site.votes) and all(
            vote.out_of_candidate for vote in site.votes
        )
        result.globally_inferred = True
        return result


__all__ = ["GlobalHeadingInference"]
