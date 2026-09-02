"""LLM Router — Constellation Stage 3: Cursor Pointer Routing.

Routes the compressed virtual skeleton through an LLM to obtain
section boundary anchors.  The LLM acts exclusively as a *pointer
annotator*: it outputs ``(block_id, level, title, snippet)`` tuples
and is strictly forbidden from generating any body content.

Prompt templates are loaded from ``modules/parser/prompts/`` at
initialisation time so they can be edited without touching code.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import List, Optional, Set

from infrastructure.ai.llm_client import (
    LLMClient,
    estimate_messages_tokens,
    get_async_llm_client,
    get_llm_client,
)
from modules.parser.prompts import load_prompt
from modules.parser.heading_candidates import format_candidate_table
from modules.parser.config import LLMClientConfig
from modules.parser.schemas import ChapterNode, HeadingCandidate, LLMRouterOutput
from app.core.exceptions import LLMRouterError

# ── Validation & retry constants ─────────────────────────────
_VALID_LEVEL_MIN = 1
_VALID_LEVEL_MAX = 6
_MAX_ANCHOR_RETRIES = 2
_RETRY_BACKOFF_BASE = 1.0  # seconds
# Confidence assigned to out-of-candidate anchors entering the
# low-confidence channel (widens the resolver's fuzzy search radius).
_DOWNGRADE_CONFIDENCE = 0.4

# Matches both ordinary ``[42]`` lines and folded ``[42 to 51]`` ranges.
_SKELETON_ID_RE = re.compile(
    r"(?m)^[ \t]*\[(\d+)(?:\s+to\s+(\d+))?\]"
)
_BUDGET_RANGE_RE = re.compile(
    r"<!--\s*Constellation budget range:\s*(\d+)\.\.(\d+)\s*-->"
)

logger = logging.getLogger(__name__)


# ── Prompt fragment builders ─────────────────────────────────

def _build_window_hint(
    chunk_index: int,
    total_chunks: int,
    previous_tail_context: str,
) -> str:
    """Build the window-aware prompt fragment for multi-chunk routing.

    Returns a string that is appended to the user prompt to inform
    the LLM about window position, metadata expectations, and
    inherited hierarchy state from the previous window.
    """
    is_first = chunk_index == 0
    hint = f"\n\n[Window] This is chunk {chunk_index + 1}/{total_chunks}."

    if is_first:
        hint += "\nExtract doc_title and doc_authors normally."
    else:
        hint += (
            "\nThis is NOT the beginning of the document. "
            "Set doc_title and doc_authors to empty strings. "
            "Focus solely on heading detection."
        )

    if previous_tail_context:
        # Hierarchy state is advisory.  Cap it so an anomalously long LLM
        # title from the previous window cannot make the next request exceed
        # the gateway input limit after skeleton budgeting.
        inherited = previous_tail_context.strip()
        if len(inherited) > 240:
            inherited = inherited[:237] + "..."
        hint += (
            f"\n[Inherited State] Immediately preceding this window, "
            f"the heading hierarchy was:\n{inherited}\n"
            f"Continue the same level structure (Level 1-6) to "
            f"prevent level discontinuities."
        )

    return hint


def _build_candidate_prompt(
    base_prompt: str,
    candidates: Optional[List[HeadingCandidate]],
) -> tuple[str, Optional[Set[int]]]:
    """Append candidate-routing instructions and return allowed block IDs."""
    if candidates is None:
        return base_prompt, None

    allowed_ids = {candidate.block_id for candidate in candidates}
    candidate_table = format_candidate_table(candidates)
    candidate_prompt = (
        f"{base_prompt}\n\n"
        "[Heading Candidates]\n"
        "The deterministic Stage 2.5 candidate generator produced the "
        "following candidate headings. Prefer choosing headings from "
        "these block IDs. If a candidate is a caption, list item, or "
        "body text, skip it. If you find a REAL section heading in the "
        "skeleton (including inside folded regions) whose block ID is "
        "NOT in this list, you may still output it — such anchors are "
        "strictly re-validated against physical features downstream, "
        "so only do so when the line is clearly a heading. "
        "Use style=L and number=L hints before font-size hints when "
        "assigning level.\n"
        f"{candidate_table}\n"
    )
    return candidate_prompt, allowed_ids


class LLMRouter:
    """Annotate section boundaries on a virtual skeleton via LLM.

    The router sends the skeleton text together with a structured
    system prompt to the configured LLM and parses the response into
    a flat list of :class:`ChapterNode` anchors.
    """

    def __init__(
        self,
        downgrade_out_of_candidate: bool = True,
        llm_config: LLMClientConfig | None = None,
    ) -> None:
        # ``llm_config`` carries BYOK overrides (user-supplied key/model/base_url);
        # None falls back to the default pooled client from settings.
        self._llm_config = llm_config
        self._client = get_llm_client(llm_config)
        self._system_prompt = load_prompt("router_system")
        self._user_template = load_prompt("router_user")
        self._downgrade_out_of_candidate = downgrade_out_of_candidate

    # ── Input-token budgeting ─────────────────────────────────

    @property
    def input_token_budget(self) -> int:
        """Safe estimated-token budget used before reaching the hard limit."""
        limit = int(getattr(self._client, "max_input_tokens", 8192))
        margin = int(getattr(self._client, "input_token_safety_margin", 512))
        return max(1, limit - margin)

    @staticmethod
    def _candidates_for_skeleton(
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]],
    ) -> Optional[List[HeadingCandidate]]:
        """Return only candidates whose IDs are represented by a shard."""
        if candidates is None:
            return None
        budget_ranges = [
            (int(match.group(1)), int(match.group(2)))
            for match in _BUDGET_RANGE_RE.finditer(skeleton_text)
        ]
        ranges = budget_ranges
        if not ranges:
            for match in _SKELETON_ID_RE.finditer(skeleton_text):
                start = int(match.group(1))
                end = int(match.group(2) or start)
                ranges.append((min(start, end), max(start, end)))
        if not ranges:
            return []
        range_start = min(min(start, end) for start, end in ranges)
        range_end = max(max(start, end) for start, end in ranges)
        return [
            candidate
            for candidate in candidates
            if range_start <= candidate.block_id <= range_end
        ]

    def _build_user_prompt(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]],
        window_hint: str = "",
    ) -> tuple[str, Optional[Set[int]]]:
        prompt = self._user_template.format(skeleton_text=skeleton_text) + window_hint
        return _build_candidate_prompt(prompt, candidates)

    def _estimate_request_tokens(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]],
        window_hint: str = "",
    ) -> int:
        user_prompt, _ = self._build_user_prompt(
            skeleton_text, candidates, window_hint,
        )
        messages = LLMClient._build_messages(
            user_prompt, LLMRouterOutput, self._system_prompt,
        )
        return estimate_messages_tokens(messages)

    def fit_skeleton_chunks(
        self,
        skeleton_chunks: List[str],
        candidates: Optional[List[HeadingCandidate]] = None,
    ) -> List[str]:
        """Split skeletons until every final LLM request fits the safe budget.

        Compression windows are block-count based, while gateways enforce a
        token limit on the *complete* request.  This second-stage sharder
        accounts for the system prompt, JSON schema, user template, candidate
        table, and a worst-case bounded inherited-state hint.  It preserves
        every skeleton line, including a single oversized line split across
        continuation shards.
        """
        if not skeleton_chunks:
            return skeleton_chunks

        budget = self.input_token_budget
        # Reserve the largest bounded state projection used by later windows.
        reserve_hint = _build_window_hint(
            99_998,
            99_999,
            "层" * 240,
        )
        fixed_tokens = self._estimate_request_tokens("", [], reserve_hint)
        if fixed_tokens >= budget:
            raise LLMRouterError(
                "LLM input budget is too small for the fixed router prompt: "
                f"estimated {fixed_tokens} tokens with safe budget {budget}. "
                "Increase LLM_MAX_INPUT_TOKENS, reduce the safety margin, or "
                "shorten the router prompts."
            )

        def fits(text: str) -> bool:
            shard_candidates = self._candidates_for_skeleton(text, candidates)
            return self._estimate_request_tokens(
                text, shard_candidates, reserve_hint,
            ) <= budget

        fitted: List[str] = []
        for original in skeleton_chunks:
            if fits(original):
                fitted.append(original)
                continue

            shard_parts: List[str] = []
            current = ""

            def append_part(
                part: str,
                parts: List[str] = shard_parts,
            ) -> None:
                if part:
                    parts.append(part)

            for unit in original.splitlines(keepends=True):
                tentative = current + unit
                if tentative and fits(tentative):
                    current = tentative
                    continue

                append_part(current)
                current = ""

                if fits(unit):
                    current = unit
                    continue

                # A single skeleton line can itself be huge (for example an
                # embedded table or a malformed heading style).  Structural
                # ``[id]`` / ``[start to end]`` markers are atomic: the first
                # shard must contain them completely, and later shards carry a
                # non-heading HTML comment with the same candidate range.
                unit_matches = list(_SKELETON_ID_RE.finditer(unit))
                marker_end = max((match.end() for match in unit_matches), default=0)
                range_start = min(
                    (int(match.group(1)) for match in unit_matches),
                    default=-1,
                )
                range_end = max(
                    (
                        int(match.group(2) or match.group(1))
                        for match in unit_matches
                    ),
                    default=-1,
                )
                continuation_prefix = (
                    "<!-- Constellation budget range: "
                    f"{range_start}..{range_end} -->\n"
                    if range_start >= 0 else ""
                )

                remaining = unit
                first_fragment = True
                while remaining:
                    prefix = "" if first_fragment else continuation_prefix
                    minimum = marker_end if first_fragment and marker_end else 1
                    if minimum > len(remaining) or not fits(
                        prefix + remaining[:minimum]
                    ):
                        raise LLMRouterError(
                            "LLM input budget leaves no room for an atomic "
                            "skeleton block marker and its candidate table "
                            f"(safe budget {budget} tokens)."
                        )

                    low, high, best = minimum, len(remaining), minimum
                    while low <= high:
                        mid = (low + high) // 2
                        if fits(prefix + remaining[:mid]):
                            best = mid
                            low = mid + 1
                        else:
                            high = mid - 1
                    append_part(prefix + remaining[:best])
                    remaining = remaining[best:]
                    first_fragment = False
                    marker_end = 0

            append_part(current)
            if not shard_parts:
                raise LLMRouterError(
                    "Token-budget splitting produced no routable skeleton content."
                )
            fitted.extend(shard_parts)

        if len(fitted) != len(skeleton_chunks):
            logger.info(
                "[Router] Input-budget sharding: %d compressor chunks -> %d "
                "LLM-safe chunks (budget=%d estimated tokens)",
                len(skeleton_chunks), len(fitted), budget,
            )
        return fitted

    # ── Public API ─────────────────────────────────────────────

    def route(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Identify section headings in *skeleton_text*.

        Args:
            skeleton_text: Compressed skeleton produced by
                :class:`SkeletonCompressor`.

        Returns:
            Structured output containing ``doc_title``, ``doc_authors``
            and a flat ``chapters`` anchor list.

        Raises:
            LLMRouterError: On empty input or LLM call failure.
        """
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton text; cannot route.")

        user_prompt = self._user_template.format(skeleton_text=skeleton_text)
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Sending skeleton (%d chars) to LLM",
                len(skeleton_text),
            )
            result = self._call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )
            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(f"LLM routing failed: {exc}") from exc

    def route_chunk(
        self,
        skeleton_text: str,
        chunk_index: int,
        total_chunks: int,
        previous_tail_context: str = "",
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Route a single skeleton chunk (Map phase of Map-Reduce).

        For the first chunk (``chunk_index == 0``), the LLM is asked
        to extract ``doc_title`` and ``doc_authors`` as usual.  For
        subsequent chunks, those fields are explicitly set to empty
        strings so the LLM focuses solely on heading detection.
        Tail context is provided to prevent level jumps.

        Args:
            skeleton_text: One window's compressed skeleton.
            chunk_index: Zero-based window index.
            total_chunks: Total number of windows.
            previous_tail_context: Formatted string of latest
                headings from the previous chunk.

        Returns:
            :class:`LLMRouterOutput` for this chunk.

        Raises:
            LLMRouterError: On empty input or LLM call failure.
        """
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton chunk; cannot route.")

        is_first = chunk_index == 0
        window_hint = _build_window_hint(
            chunk_index, total_chunks, previous_tail_context,
        )
        user_prompt = (
            self._user_template.format(skeleton_text=skeleton_text)
            + window_hint
        )
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Sending chunk %d/%d (%d chars) to LLM",
                chunk_index + 1, total_chunks, len(skeleton_text),
            )
            result = self._call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )

            # Only the first chunk extracts document metadata
            if not is_first:
                result.doc_title = ""
                result.doc_authors = ""

            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(
                f"LLM routing failed on chunk "
                f"{chunk_index + 1}/{total_chunks}: {exc}"
            ) from exc

    # ── Async variants ─────────────────────────────────────────

    async def async_route(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Async version of :meth:`route`."""
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton text; cannot route.")

        user_prompt = self._user_template.format(skeleton_text=skeleton_text)
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Async sending skeleton (%d chars)", len(skeleton_text),
            )
            result = await self._async_call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )
            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(f"Async LLM routing failed: {exc}") from exc

    async def async_route_chunk(
        self,
        skeleton_text: str,
        chunk_index: int,
        total_chunks: int,
        previous_tail_context: str = "",
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Async version of :meth:`route_chunk`."""
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton chunk; cannot route.")

        is_first = chunk_index == 0
        window_hint = _build_window_hint(
            chunk_index, total_chunks, previous_tail_context,
        )
        user_prompt = (
            self._user_template.format(skeleton_text=skeleton_text)
            + window_hint
        )
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Async sending chunk %d/%d (%d chars)",
                chunk_index + 1, total_chunks, len(skeleton_text),
            )
            result = await self._async_call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )

            if not is_first:
                result.doc_title = ""
                result.doc_authors = ""

            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(
                f"Async LLM routing failed on chunk "
                f"{chunk_index + 1}/{total_chunks}: {exc}"
            ) from exc

    # ── Validation & retry ─────────────────────────────────────

    @staticmethod
    def _validate_and_filter_anchors(
        result: LLMRouterOutput,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
        downgrade_out_of_candidate: bool = True,
    ) -> LLMRouterOutput:
        """Validate anchors; drop invalid ones, downgrade out-of-candidate ones.

        Hard checks (anchor is dropped on failure):
        - ``level`` in ``[1, 6]``
        - ``start_block_id >= 0``
        - ``title`` non-empty after stripping
        - ``start_block_id <= max_block_id`` (when ``max_block_id >= 0``)

        Soft check: anchors pointing outside ``allowed_block_ids`` are
        NOT dropped when ``downgrade_out_of_candidate`` is True.  They
        are marked ``out_of_candidate=True`` with lowered confidence and
        re-validated against physical block features by the parser
        (the candidate generator must not hold veto power over recall).
        With ``downgrade_out_of_candidate=False`` the legacy hard filter
        applies.

        Returns the (possibly filtered) :class:`LLMRouterOutput`.
        """
        valid_chapters: List[ChapterNode] = []
        dropped = 0

        for ch in result.chapters:
            if ch.level < _VALID_LEVEL_MIN or ch.level > _VALID_LEVEL_MAX:
                logger.warning(
                    "[Router] Dropped anchor: level=%d out of range [%d,%d], "
                    "title='%s'",
                    ch.level, _VALID_LEVEL_MIN, _VALID_LEVEL_MAX,
                    ch.title[:30],
                )
                dropped += 1
                continue
            if ch.start_block_id < 0:
                logger.warning(
                    "[Router] Dropped anchor: block_id=%d < 0, title='%s'",
                    ch.start_block_id, ch.title[:30],
                )
                dropped += 1
                continue
            if max_block_id >= 0 and ch.start_block_id > max_block_id:
                logger.warning(
                    "[Router] Dropped anchor: block_id=%d > max=%d, title='%s'",
                    ch.start_block_id, max_block_id, ch.title[:30],
                )
                dropped += 1
                continue
            if (
                allowed_block_ids is not None
                and ch.start_block_id not in allowed_block_ids
            ):
                if not downgrade_out_of_candidate:
                    logger.warning(
                        "[Router] Dropped anchor: block_id=%d is not in candidate set, title='%s'",
                        ch.start_block_id,
                        ch.title[:30],
                    )
                    dropped += 1
                    continue
                ch.out_of_candidate = True
                ch.confidence = min(ch.confidence, _DOWNGRADE_CONFIDENCE)
                logger.info(
                    "[Router] Downgraded out-of-candidate anchor: "
                    "block_id=%d, title='%s' (pending physical re-validation)",
                    ch.start_block_id,
                    ch.title[:30],
                )
            if not ch.title.strip():
                logger.warning(
                    "[Router] Dropped anchor: empty title, block_id=%d",
                    ch.start_block_id,
                )
                dropped += 1
                continue
            valid_chapters.append(ch)

        if dropped > 0:
            logger.warning(
                "[Router] Dropped %d invalid anchors, kept %d",
                dropped, len(valid_chapters),
            )

        result.chapters = valid_chapters
        # Declared as a PrivateAttr on LLMRouterOutput; consumed by the
        # retry logic to distinguish "no headings" from "all filtered".
        result._dropped_count = dropped
        return result

    def _call_with_validation_retry(
        self,
        user_prompt: str,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
    ) -> LLMRouterOutput:
        """Call the LLM and retry if all anchors are invalid after filtering.

        Retries up to ``_MAX_ANCHOR_RETRIES`` times with exponential
        backoff when the LLM returns output where every anchor fails
        validation.
        """
        last_result: LLMRouterOutput | None = None

        for attempt in range(1 + _MAX_ANCHOR_RETRIES):
            result: LLMRouterOutput = self._client.structured_completion(
                prompt=user_prompt,
                response_model=LLMRouterOutput,
                system_prompt=self._system_prompt,
            )
            raw_chapter_count = len(result.chapters)
            result = self._validate_and_filter_anchors(
                result,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_block_ids,
                downgrade_out_of_candidate=self._downgrade_out_of_candidate,
            )
            last_result = result

            if result.chapters:
                return result

            # Distinguish between:
            #   (a) LLM genuinely returned no headings (correct for
            #       headingless documents) — accept immediately.
            #   (b) LLM returned headings but all were filtered out
            #       as invalid — worth retrying.
            dropped = result._dropped_count
            if raw_chapter_count == 0 and dropped == 0:
                logger.info(
                    "[Router] LLM returned no headings (likely a "
                    "headingless document), accepting as-is"
                )
                return result

            if attempt < _MAX_ANCHOR_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "[Router] All %d anchors invalid (attempt %d/%d), "
                    "retrying in %.1fs",
                    raw_chapter_count, attempt + 1,
                    1 + _MAX_ANCHOR_RETRIES, wait,
                )
                time.sleep(wait)

        logger.warning(
            "[Router] No valid anchors after %d attempts",
            1 + _MAX_ANCHOR_RETRIES,
        )
        return last_result  # type: ignore[return-value]

    async def _async_call_with_validation_retry(
        self,
        user_prompt: str,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
    ) -> LLMRouterOutput:
        """Async version of :meth:`_call_with_validation_retry`."""
        last_result: LLMRouterOutput | None = None

        for attempt in range(1 + _MAX_ANCHOR_RETRIES):
            async_client = get_async_llm_client(self._llm_config)
            result: LLMRouterOutput = await async_client.structured_completion(
                prompt=user_prompt,
                response_model=LLMRouterOutput,
                system_prompt=self._system_prompt,
            )
            raw_chapter_count = len(result.chapters)
            result = self._validate_and_filter_anchors(
                result,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_block_ids,
                downgrade_out_of_candidate=self._downgrade_out_of_candidate,
            )
            last_result = result

            if result.chapters:
                return result

            dropped = result._dropped_count
            if raw_chapter_count == 0 and dropped == 0:
                logger.info(
                    "[Router] LLM returned no headings (likely a "
                    "headingless document), accepting as-is"
                )
                return result

            if attempt < _MAX_ANCHOR_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "[Router] All %d anchors invalid (attempt %d/%d), "
                    "retrying in %.1fs",
                    raw_chapter_count, attempt + 1,
                    1 + _MAX_ANCHOR_RETRIES, wait,
                )
                await asyncio.sleep(wait)

        logger.warning(
            "[Router] No valid anchors after %d attempts",
            1 + _MAX_ANCHOR_RETRIES,
        )
        return last_result  # type: ignore[return-value]

    # ── Logging ────────────────────────────────────────────────

    @staticmethod
    def _log_result(result: LLMRouterOutput) -> None:
        """Emit a debug-level summary of the routing result."""
        logger.debug(
            "[Router] title='%s', authors='%s', chapters=%d",
            result.doc_title, result.doc_authors, len(result.chapters),
        )
        for ch in result.chapters:
            indent = "  " * (ch.level - 1)
            snippet_hint = (
                f" [snippet: {ch.snippet[:20]}…" if ch.snippet else ""
            )
            logger.debug(
                "  %s[%d] L%d: %s%s",
                indent, ch.start_block_id, ch.level, ch.title, snippet_hint,
            )
