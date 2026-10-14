"""Monotonic Stage-3 anchor alignment.

The router emits observations, not physical cursors.  This module aligns each
window's observation sequence to immutable Blocks with a monotonic dynamic
program, then merges overlap-window votes at their aligned positions.  Fuzzy
correction and deduplication therefore cannot disagree or run in the wrong
order.
"""
from __future__ import annotations

import copy
import bisect
import logging
import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from infrastructure.models import Block
from modules.parser.analysis_context import block_index
from modules.parser.config import ResolverConfig
from modules.parser.schemas import ChapterNode, HeadingCandidate, LLMAnchorVote
from modules.parser.evidence import VoteView, correlated_vote_gains
from modules.parser.titles import structural_text

logger = logging.getLogger(__name__)
_SPACE_RE = re.compile(r"\s+")
_TRUNCATION_RE = re.compile(r"\[(?:省略|omitted)[^\]]*\]|\.\.\.", re.IGNORECASE)


def _normalise(text: str) -> str:
    # Emphasis markers are transport encoding, not part of the name: without
    # stripping them the exact-text index never matches an LLM title against a
    # bold heading block, silently downgrading every anchor to fuzzy search.
    return _SPACE_RE.sub(" ", structural_text(text)).casefold()


# 短文本保护阈值：短于等于此长度的一方一律要求精确匹配,不接受裸子串。
# 定为 5 覆盖了实测会误锚的全部形态——首字母缩写(``AI`` 2、``API`` 3)、
# 单字符编号(``1``)、双字中文词(``结论``)——同时不误伤 6 字以上的真标题
# (``Methods`` 这类仍走覆盖比例加权的包含分支)。
_SHORT_TEXT_LEN = 5

# 短文本唯一放行的例外：两侧只差首尾标点 / 空白。标题原文常带尾随冒号或句号
# (``结论。``、``Intro:``),模型回抄 snippet 时通常不带,若一律判 0 会把
# ``结论`` 这类两字中文标题(中文文档里极常见:摘要/引言/结论/前言/致谢)
# 全部挡在锚定之外——那是拿 B07 的修复换来一批新的漏锚。剥掉首尾标点后仍
# 必须完全相等才放行,所以 ``AI`` vs ``details of training``(剩余部分含字母)、
# ``1`` vs ``1 introduction``(剩余部分含字母)照旧判 0,保护没有被削弱。
# 这正是方案 B07 里「使用完整匹配**或明确词边界**」的后半句。
_EDGE_PUNCT = " \t\u3000:：。.、,，;；!！?？-—–_()（）[]【】<>《》\"'“”‘’/\\|*#~`"


def _punct_core(text: str) -> str:
    """剥掉首尾标点 / 空白后的核心文本(短文本例外判定用)。"""
    return text.strip(_EDGE_PUNCT)


def _similarity(left: str, right: str) -> float:
    """Heading-oriented text similarity with short-text protection applied FIRST.

    修复 B07:早期实现把"子串高分"分支放在短文本保护之前,于是
    ``_similarity("AI", "Details of training")`` 返回 0.9263——``ai`` 恰好是
    ``training`` 里的子串,却被当成近乎相同的标题,把锚点错钉到正文段。
    ``API``/``Capital expenditure``、单字符编号 ``1``、双字中文 ``结论`` 同理。

    正确顺序:先按较短一方判定。很短的编号 / 缩写 / 中文词必须精确匹配,
    唯一例外是两侧只差首尾标点(见 :data:`_EDGE_PUNCT`);较长标题才允许
    前缀 / 包含匹配,且分数按覆盖比例与长度比缩放——这样
    ``Introduction to Machine`` 被模型轻微截断成 ``...Learning Methods``
    仍高分召回,而短查询落进大标题不再虚高。
    """
    left_n = _normalise(left)
    right_n = _normalise(right)
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0

    shorter, longer = sorted((left_n, right_n), key=len)
    short_len, long_len = len(shorter), len(longer)

    # 1) 短文本保护先行。较短一方很短时,裸子串太廉价(``ai`` in ``training``),
    #    也不能只凭"作为独立词出现"就承认——``1`` 是 ``1 Introduction`` 的独立
    #    token、``AI`` 可能是任意正文里的独立单词,词边界命中对这么短的串仍是
    #    偶合。故很短一方要求精确匹配,只放行"只差首尾标点"这一种形态。
    #    这直接堵住 B07 的 AI/Details、API/Capital、单字符编号、双字中文碰撞。
    if short_len <= _SHORT_TEXT_LEN:
        core_short, core_long = _punct_core(shorter), _punct_core(longer)
        if core_short and core_short == core_long:
            # 只差标点算同一个标题,但不给满分:留出与真正逐字相等的区分度。
            return 0.97
        return 0.0

    # 2) 较长标题:保留前缀 / 包含匹配的召回,但按覆盖比例(shorter/longer)
    #    加权,避免"一个词落在长句里"拿到和真截断同级的高分。前缀命中
    #    (真截断的典型形态:模型少抄了尾巴)额外加成,盖过纯中段包含。
    if shorter in longer:
        coverage = short_len / long_len
        prefix_bonus = 0.06 if longer.startswith(shorter) else 0.0
        return min(1.0, 0.72 + 0.22 * coverage + prefix_bonus)

    # 3) 无包含关系:用受限窗口的序列比,召回"单复数 / 编号 / 连接词"这类
    #    非包含改写(如 ``Ablation Study`` vs ``4.2 Ablation Studies ...``)。
    span = max(len(left_n) + 12, len(right_n))
    return SequenceMatcher(None, left_n, right_n[:span], autojunk=False).ratio()


@dataclass(frozen=True)
class _Option:
    block_id: int
    score: float
    text_similarity: float


@dataclass
class _AlignedItem:
    chapter: ChapterNode
    option: _Option
    window_index: int


class MonotonicAnchorAligner:
    """Align LLM anchor observations before any physical/risk validation."""

    def __init__(
        self,
        blocks: Sequence[Block],
        config: ResolverConfig | None = None,
    ) -> None:
        self.config = config or ResolverConfig()
        # 索引由本次解析共享的 BlockIndex 提供（L01）：此前本类自建一份
        # list(blocks) + {b.id: b}，与 resolver / global_inference / hierarchy
        # 各自那份完全重复，同一份不变的 blocks 被重新物化多次。
        self._index = block_index(blocks)
        self.blocks = list(self._index.blocks)
        self.block_map = self._index.by_id
        self._ordered_ids = list(self._index.ordered_ids)
        self._exact_text_ids: dict[str, list[int]] = {}
        for block in self.blocks:
            if block.type != "text" or not block.text:
                continue
            self._exact_text_ids.setdefault(_normalise(block.text), []).append(block.id)

    def align(
        self,
        chapters: Sequence[ChapterNode],
        candidates: Iterable[HeadingCandidate] | None = None,
    ) -> list[ChapterNode]:
        """Align one observation sequence and return document-ordered anchors."""
        return self.align_windows([chapters], candidates=candidates)

    def align_windows(
        self,
        windows: Sequence[Sequence[ChapterNode]],
        candidates: Iterable[HeadingCandidate] | None = None,
    ) -> list[ChapterNode]:
        """Align each window monotonically, then merge overlap-window votes."""
        candidate_list = list(candidates or [])
        candidate_by_id: dict[int, HeadingCandidate] = {}
        for candidate in candidate_list:
            previous = candidate_by_id.get(candidate.block_id)
            if previous is None or candidate.heading_probability > previous.heading_probability:
                candidate_by_id[candidate.block_id] = candidate
        candidate_exact: dict[str, list[int]] = {}
        for candidate in candidate_by_id.values():
            candidate_exact.setdefault(_normalise(candidate.title), []).append(candidate.block_id)
        self._candidate_exact_text = candidate_exact

        aligned: list[_AlignedItem] = []
        for window_index, chapters in enumerate(windows):
            if not chapters:
                continue
            # A second pass (for example resolver safety alignment after parser
            # merge) must preserve the original votes rather than manufacture
            # stronger duplicate evidence.
            if all(chapter.anchor_votes for chapter in chapters):
                for chapter in chapters:
                    option = _Option(
                        chapter.start_block_id,
                        chapter.alignment_score,
                        chapter.alignment_score,
                    )
                    aligned.append(_AlignedItem(copy.deepcopy(chapter), option, window_index))
                continue
            aligned.extend(
                self._align_window(chapters, window_index, candidate_by_id)
            )

        return self._merge_aligned(aligned, candidate_by_id)

    def deduplicate(self, chapters: Sequence[ChapterNode]) -> list[ChapterNode]:
        """Compatibility adapter when Blocks are unavailable.

        It uses the same overlap clustering as physical alignment, with claimed
        IDs as already-aligned positions.  Parser callers should normally pass
        Blocks and use :meth:`align_windows`.
        """
        items: list[_AlignedItem] = []
        for index, chapter in enumerate(sorted(
            (copy.deepcopy(item) for item in chapters),
            key=lambda item: item.start_block_id,
        )):
            option = _Option(chapter.start_block_id, 1.0, 1.0)
            vote = LLMAnchorVote(
                raw_block_id=chapter.start_block_id,
                aligned_block_id=chapter.start_block_id,
                title=chapter.title,
                snippet=chapter.snippet,
                level=max(1, min(chapter.level, 6)),
                confidence=max(0.0, min(chapter.confidence, 1.0)),
                alignment_score=1.0,
                out_of_candidate=chapter.out_of_candidate,
                window_index=index,
            )
            chapter.anchor_votes = [vote]
            chapter.source_windows = [index]
            items.append(_AlignedItem(chapter, option, index))
        return self._merge_aligned(items, {})

    def _align_window(
        self,
        chapters: Sequence[ChapterNode],
        window_index: int,
        candidate_by_id: dict[int, HeadingCandidate],
    ) -> list[_AlignedItem]:
        if not self._ordered_ids:
            return []

        prepared: list[tuple[int, ChapterNode, list[_Option]]] = []
        for raw_index, original in enumerate(chapters):
            chapter = copy.deepcopy(original)
            options = self._options(chapter, candidate_by_id)
            prepared.append((raw_index, chapter, options))

        # Claimed IDs can cross after an off-by-N router error.  A strong text
        # match is a better provisional order key; sorting by it lets the
        # sequence DP recover physical order instead of preserving bad claims.
        def provisional_key(item: tuple[int, ChapterNode, list[_Option]]) -> tuple[int, int]:
            raw_index, chapter, options = item
            best = max(options, key=lambda option: option.score)
            key = best.block_id if best.text_similarity >= self.config.fuzzy_min_similarity else chapter.start_block_id
            return key, raw_index

        prepared.sort(key=provisional_key)

        # Viterbi over candidate physical positions.  Equal positions remain
        # possible (duplicate observations); overlap merge resolves them using
        # all votes rather than first/last-wins deletion.
        states: dict[int, tuple[float, _Option, int | None]] = {}
        history: list[dict[int, tuple[float, _Option, int | None]]] = []
        previous_title = ""
        for index, (_raw_index, chapter, options) in enumerate(prepared):
            next_states: dict[int, tuple[float, _Option, int | None]] = {}
            current_title = chapter.snippet or chapter.title
            same_title = _similarity(previous_title, current_title) >= self.config.dedup_sim_threshold
            for option in options:
                if index == 0:
                    next_states[option.block_id] = (option.score, option, None)
                    continue
                best_transition: tuple[float, _Option, int | None] | None = None
                for previous_id, (previous_score, _previous_option, _parent) in states.items():
                    if option.block_id < previous_id:
                        continue
                    duplicate_penalty = 0.0
                    if option.block_id == previous_id:
                        duplicate_penalty = 0.0 if same_title else -1.5
                    proposal = (
                        previous_score + option.score + duplicate_penalty,
                        option,
                        previous_id,
                    )
                    if best_transition is None or proposal[0] > best_transition[0]:
                        best_transition = proposal
                if best_transition is not None:
                    current = next_states.get(option.block_id)
                    if current is None or best_transition[0] > current[0]:
                        next_states[option.block_id] = best_transition
            if not next_states:
                # This is only reachable for malformed sparse/non-monotonic IDs.
                # Preserve evidence at independently best positions; the final
                # sorted merge is still monotonic and never clamps to a wrong ID.
                logger.warning(
                    "[AnchorAlignment] No monotonic path in window %d; using "
                    "best independent physical matches",
                    window_index,
                )
                chosen = [max(entry[2], key=lambda option: option.score) for entry in prepared]
                return self._materialise(prepared, chosen, window_index)
            states = next_states
            history.append(states)
            previous_title = current_title

        if not states:
            return []
        position = max(states, key=lambda key: states[key][0])
        path: list[_Option] = []
        for layer in reversed(history):
            _score, option, parent = layer[position]
            path.append(option)
            if parent is None:
                break
            position = parent
        path.reverse()
        return self._materialise(prepared, path, window_index)

    def _materialise(
        self,
        prepared: Sequence[tuple[int, ChapterNode, list[_Option]]],
        path: Sequence[_Option],
        window_index: int,
    ) -> list[_AlignedItem]:
        result: list[_AlignedItem] = []
        for (_raw_index, chapter, _options), option in zip(prepared, path, strict=True):
            raw_id = chapter.start_block_id
            chapter.start_block_id = option.block_id
            chapter.alignment_score = max(0.0, min(option.text_similarity, 1.0))
            chapter.source_windows = [window_index]
            chapter.anchor_votes = [LLMAnchorVote(
                raw_block_id=raw_id,
                aligned_block_id=option.block_id,
                title=chapter.title,
                snippet=chapter.snippet,
                level=max(1, min(chapter.level, 6)),
                confidence=max(0.0, min(chapter.confidence, 1.0)),
                alignment_score=chapter.alignment_score,
                out_of_candidate=chapter.out_of_candidate,
                window_index=window_index,
                context_span=chapter._observation_context,
                model=chapter._observation_model,
                prompt_version=chapter._observation_prompt,
            )]
            result.append(_AlignedItem(chapter, option, window_index))
        return result

    def _options(
        self,
        chapter: ChapterNode,
        candidate_by_id: dict[int, HeadingCandidate],
    ) -> list[_Option]:
        query = (chapter.snippet or chapter.title).strip()
        claimed = chapter.start_block_id
        confidence = max(0.0, min(chapter.confidence, 1.0))
        radius = max(
            1,
            math.ceil(self.config.fuzzy_anchor_radius * (2.0 - 0.5 * confidence)),
        )

        left = bisect.bisect_left(self._ordered_ids, claimed - radius)
        right = bisect.bisect_right(self._ordered_ids, claimed + radius)
        ids: set[int] = set(self._ordered_ids[left:right])
        if claimed in self.block_map:
            ids.add(claimed)
        query_n = _normalise(query)
        ids.update(self._exact_text_ids.get(query_n, []))
        ids.update(getattr(self, "_candidate_exact_text", {}).get(query_n, []))
        candidate_ids = sorted(candidate_by_id)
        c_left = bisect.bisect_left(candidate_ids, claimed - radius * 2)
        c_right = bisect.bisect_right(candidate_ids, claimed + radius * 2)
        # Deterministic heading candidates close to the claimed position are
        # always viable options.  Their structural evidence is useful even when
        # the router lightly rewrites the title, and the final score/monotonic
        # DP still decides whether they beat the claimed/local blocks.
        ids.update(candidate_ids[c_left:c_right])

        if not ids:
            ids.add(min(self._ordered_ids, key=lambda block_id: abs(block_id - claimed)))

        distance_scale = max(radius, 1)

        def build_option(block_id: int) -> _Option | None:
            block = self.block_map.get(block_id)
            if block is None:
                return None
            similarity = _similarity(query, block.text or "") if query else 0.5
            position = max(0.0, 1.0 - abs(block_id - claimed) / (distance_scale * 2.0))
            candidate = candidate_by_id.get(block_id)
            candidate_bonus = 0.12 * candidate.heading_probability if candidate else 0.0
            exact_bonus = 0.18 if similarity >= 0.999 else 0.0
            score = 2.8 * similarity + 0.35 * position + candidate_bonus + exact_bonus
            return _Option(block_id, score, similarity)

        options: list[_Option] = []
        for block_id in ids:
            option = build_option(block_id)
            if option is not None:
                options.append(option)

        # 远距离救援：本地窗口 + 全文精确匹配都拿不出可信文本匹配时
        # （路由器报的 block id 偏移超出模糊窗口、且标题被截断 / 改写导致
        # 精确匹配失败），放开距离限制在全部确定性标题候选里找明显更优的
        # 标题。候选集受路由预算上限约束，全局扫描代价有界；仅在局部失配
        # 时触发，常规路径零开销。
        #
        # 接纳条件是"绝对下限 + 相对优势"双门槛：绝对下限保证不放过
        # 单复数 / 编号 / 连接词差异这类 0.6-0.9 相似度的真标题（早期 0.92
        # 硬门槛只覆盖包含关系级别的匹配，召回只修了一半）；相对优势
        # （须比本地最佳高出 0.2）保证弱相似的远端候选不会劫持本地尚可
        # 的匹配。只取头部若干个，最终仍由单调 DP 与重叠合并裁决，
        # 不破坏物理顺序保证。
        best_similarity = max((option.text_similarity for option in options), default=0.0)
        if query_n and best_similarity < self.config.rescue_trigger_similarity:
            acceptance = max(
                self.config.rescue_min_similarity,
                best_similarity + 0.2,
            )
            scored: list[tuple[float, int]] = []
            for candidate in candidate_by_id.values():
                if candidate.block_id in ids:
                    continue
                similarity = _similarity(query, candidate.title)
                if similarity >= acceptance:
                    scored.append((similarity, candidate.block_id))
            scored.sort(key=lambda item: (-item[0], item[1]))
            for _rescue_similarity, block_id in scored[:8]:
                option = build_option(block_id)
                if option is not None:
                    options.append(option)

        options.sort(key=lambda option: (-option.score, abs(option.block_id - claimed), option.block_id))
        return options[:24]

    def _merge_aligned(
        self,
        items: Sequence[_AlignedItem],
        candidate_by_id: dict[int, HeadingCandidate],
    ) -> list[ChapterNode]:
        if not items:
            return []

        clusters: list[list[_AlignedItem]] = []
        for item in sorted(items, key=lambda entry: entry.chapter.start_block_id):
            target: list[_AlignedItem] | None = None
            for cluster in reversed(clusters):
                representative = cluster[0]
                delta = item.chapter.start_block_id - representative.chapter.start_block_id
                if delta > self.config.dedup_id_diff:
                    break
                same_window = any(entry.window_index == item.window_index for entry in cluster)
                same_position = item.chapter.start_block_id == representative.chapter.start_block_id
                title_match = _similarity(
                    item.chapter.title or item.chapter.snippet,
                    representative.chapter.title or representative.chapter.snippet,
                ) >= self.config.dedup_sim_threshold
                if title_match and (not same_window or same_position):
                    target = cluster
                    break
            if target is None:
                clusters.append([item])
            else:
                target.append(item)

        merged: list[ChapterNode] = []
        for cluster in clusters:
            def canonical_score(entry: _AlignedItem) -> tuple[float, float, int]:
                candidate = candidate_by_id.get(entry.chapter.start_block_id)
                candidate_support = candidate.heading_probability if candidate else 0.0
                return (
                    entry.option.text_similarity + 0.15 * candidate_support,
                    entry.chapter.confidence,
                    -entry.chapter.start_block_id,
                )

            representative = max(cluster, key=canonical_score)
            canonical_id = representative.chapter.start_block_id
            chapter = copy.deepcopy(representative.chapter)
            votes: list[LLMAnchorVote] = []
            for entry in cluster:
                source_votes = entry.chapter.anchor_votes or [LLMAnchorVote(
                    raw_block_id=entry.chapter.start_block_id,
                    aligned_block_id=entry.chapter.start_block_id,
                    title=entry.chapter.title,
                    snippet=entry.chapter.snippet,
                    level=entry.chapter.level,
                    confidence=entry.chapter.confidence,
                    alignment_score=entry.chapter.alignment_score,
                    out_of_candidate=entry.chapter.out_of_candidate,
                    window_index=entry.window_index,
                )]
                votes.extend(
                    vote.model_copy(update={"aligned_block_id": canonical_id})
                    for vote in source_votes
                )

            gains = correlated_vote_gains([
                VoteView(key=i, confidence=v.confidence, window_index=v.window_index,
                         context_span=v.context_span, model=v.model, prompt_version=v.prompt_version)
                for i, v in enumerate(votes)
            ])
            weights = [gain * max(1e-6, vote.confidence * max(vote.alignment_score, 0.2))
                       for vote, gain in zip(votes, gains, strict=True)]
            level_weight: dict[int, float] = {}
            for vote, weight in zip(votes, weights, strict=True):
                level_weight[vote.level] = level_weight.get(vote.level, 0.0) + weight
            chapter.start_block_id = canonical_id
            chapter.level = min(level_weight, key=lambda level: (-level_weight[level], level))
            chapter.anchor_votes = votes
            chapter.alignment_score = max((vote.alignment_score for vote in votes), default=0.0)
            chapter.source_windows = sorted({vote.window_index for vote in votes})
            chapter.confidence = 1.0 - math.prod(1.0 - min(weight, 0.999) for weight in weights)
            chapter.out_of_candidate = all(vote.out_of_candidate for vote in votes)

            block = self.block_map.get(canonical_id)
            if block and block.type == "text" and block.text:
                physical_title = block.text.strip()
                if (
                    _TRUNCATION_RE.search(chapter.title or "")
                    or _similarity(chapter.title, physical_title) >= 0.72
                ):
                    chapter.title = physical_title
                    chapter.snippet = physical_title[:80]
            merged.append(chapter)

        merged.sort(key=lambda chapter: chapter.start_block_id)
        logger.info(
            "[AnchorAlignment] %d observations -> %d monotonic aligned anchors",
            sum(len(item.chapter.anchor_votes) or 1 for item in items),
            len(merged),
        )
        return merged


__all__ = ["MonotonicAnchorAligner"]
