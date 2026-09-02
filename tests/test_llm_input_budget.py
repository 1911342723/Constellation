"""Regression tests for the upstream LLM input-length limit."""

from __future__ import annotations

import asyncio
import random
import re
import string
from unittest.mock import MagicMock

import pytest
import tiktoken

from app.core.exceptions import LLMRouterError
from infrastructure.ai.llm_client import AsyncLLMClient, LLMClient
from infrastructure.models import Block
from modules.parser.config import LLMClientConfig, ParserConfig
from modules.parser.parser import CaliperParser
from modules.parser.router import LLMRouter, _build_window_hint
from modules.parser.schemas import LLMRouterOutput


def _config(*, limit: int = 8192, margin: int = 512) -> LLMClientConfig:
    return LLMClientConfig(
        api_key="test-key",
        base_url="http://localhost:9999/v1",
        max_input_tokens=limit,
        input_token_safety_margin=margin,
    )


def _reserve_hint(chunk_count: int = 2) -> str:
    del chunk_count
    return _build_window_hint(99_998, 99_999, "层" * 240)


_BUDGET_RANGE_COMMENT = re.compile(
    r"<!--\s*Constellation budget range:\s*\d+\.\.\d+\s*-->\n"
)


def _without_budget_metadata(chunks: list[str]) -> str:
    return _BUDGET_RANGE_COMMENT.sub("", "".join(chunks))


def _cl100k_request_tokens(
    router: LLMRouter,
    chunk: str,
    candidates,
    hint: str,
) -> int:
    prompt, _ = router._build_user_prompt(chunk, candidates, hint)
    messages = LLMClient._build_messages(
        prompt, LLMRouterOutput, router._system_prompt,
    )
    encoding = tiktoken.get_encoding("cl100k_base")
    return 6 + sum(
        len(encoding.encode(str(message.get("role", "")), disallowed_special=()))
        + len(encoding.encode(str(message.get("content", "")), disallowed_special=()))
        for message in messages
    )


def test_sync_client_rejects_oversized_final_messages_before_transport():
    client = LLMClient(config=_config(limit=100, margin=0))
    transport = MagicMock()
    client.client = transport

    with pytest.raises(LLMRouterError, match="input is too long before request"):
        client.structured_completion(
            "x" * 1000,
            LLMRouterOutput,
            system_prompt="route headings",
        )

    transport.chat.completions.create.assert_not_called()


def test_async_client_rejects_oversized_final_messages_before_transport():
    client = AsyncLLMClient(config=_config(limit=100, margin=0))
    transport = MagicMock()
    client.client = transport

    async def run() -> None:
        with pytest.raises(LLMRouterError, match="input is too long before request"):
            await client.structured_completion(
                "x" * 1000,
                LLMRouterOutput,
                system_prompt="route headings",
            )

    asyncio.run(run())
    transport.chat.completions.create.assert_not_called()


def test_budget_sharding_preserves_all_skeleton_text_and_fits_requests():
    router = LLMRouter(llm_config=_config())
    skeleton = "".join(
        f"[{block_id}] " + ("中文骨架内容" * 120) + "\n"
        for block_id in range(80)
    )

    chunks = router.fit_skeleton_chunks([skeleton], candidates=[])

    assert len(chunks) > 1
    assert "".join(chunks) == skeleton
    hint = _reserve_hint(len(chunks))
    assert all(
        router._estimate_request_tokens(
            chunk,
            router._candidates_for_skeleton(chunk, []),
            hint,
        )
        <= router.input_token_budget
        for chunk in chunks
    )


@pytest.mark.parametrize(
    "payload",
    [
        "".join(
            random.Random(20260716).choices(
                string.ascii_letters + string.digits,
                k=50_000,
            )
        ),
        "\U00020BB7" * 5_000,
    ],
    ids=["high_entropy_ascii", "four_byte_unicode"],
)
def test_pathological_text_is_split_by_actual_tokenizer_count(payload: str):
    router = LLMRouter(llm_config=_config())
    skeleton = f"[0] {payload}\n"

    chunks = router.fit_skeleton_chunks([skeleton], [])

    assert len(chunks) > 1
    assert _without_budget_metadata(chunks) == skeleton
    hint = _reserve_hint(len(chunks))
    for chunk in chunks:
        shard_candidates = router._candidates_for_skeleton(chunk, [])
        assert router._estimate_request_tokens(
            chunk, shard_candidates, hint,
        ) <= router.input_token_budget
        assert _cl100k_request_tokens(
            router, chunk, shard_candidates, hint,
        ) <= 8192


def test_inline_bracket_reference_does_not_expand_structural_range():
    from modules.parser.schemas import HeadingCandidate

    router = LLMRouter(llm_config=_config())
    skeleton = "[0] 正文引用[999] " + ("内容" * 5_000) + "\n"
    candidates = [
        HeadingCandidate(block_id=0, title="Real", source_score=1.0),
        HeadingCandidate(block_id=999, title="Reference", source_score=1.0),
    ]

    chunks = router.fit_skeleton_chunks([skeleton], candidates)

    assert len(chunks) > 1
    assert _without_budget_metadata(chunks) == skeleton
    for chunk in chunks:
        assert CaliperParser._chunk_id_range(chunk, 999) == (0, 0)
        assert [
            candidate.block_id
            for candidate in router._candidates_for_skeleton(chunk, candidates) or []
        ] == [0]


def test_candidate_table_is_budgeted_with_actual_parser_ranges():
    from modules.parser.heading_candidates import candidates_in_range
    from modules.parser.schemas import HeadingCandidate

    router = LLMRouter(llm_config=_config())
    skeleton = "".join(
        f"[{block_id}] Section {block_id}\n" for block_id in range(120)
    )
    candidates = [
        HeadingCandidate(
            block_id=block_id,
            title=(f"Section {block_id} " + ("candidate title " * 20)),
            source_score=1.0,
            reasons=["heading_style", "numbering"],
        )
        for block_id in range(120)
    ]

    chunks = router.fit_skeleton_chunks([skeleton], candidates)

    assert chunks
    assert "".join(chunks) == skeleton
    hint = _reserve_hint(len(chunks))
    for chunk in chunks:
        start_id, end_id = CaliperParser._chunk_id_range(chunk, 119)
        actual_candidates = candidates_in_range(candidates, start_id, end_id)
        assert router._estimate_request_tokens(
            chunk, actual_candidates, hint,
        ) <= router.input_token_budget



def test_rle_range_remains_atomic_across_oversized_line_shards():
    from modules.parser.heading_candidates import candidates_in_range
    from modules.parser.schemas import HeadingCandidate

    router = LLMRouter(llm_config=_config())
    skeleton = "[0 to 99] " + ("超长折叠区域内容" * 2000) + "\n"
    candidates = [
        HeadingCandidate(
            block_id=block_id,
            title=f"Heading {block_id}",
            source_score=1.0,
            reasons=["heading_style"],
        )
        for block_id in range(100)
    ]

    chunks = router.fit_skeleton_chunks([skeleton], candidates)

    assert len(chunks) > 1
    assert _without_budget_metadata(chunks) == skeleton
    assert chunks[0].startswith("[0 to 99]")
    hint = _reserve_hint(len(chunks))
    for chunk in chunks:
        start_id, end_id = CaliperParser._chunk_id_range(chunk, 99)
        assert (start_id, end_id) == (0, 99)
        actual_candidates = candidates_in_range(candidates, start_id, end_id)
        assert len(router._candidates_for_skeleton(chunk, candidates) or []) == 100
        assert router._estimate_request_tokens(
            chunk, actual_candidates, hint,
        ) <= router.input_token_budget


def test_budget_boundary_keeps_fitting_input_and_splits_first_overflow():
    router = LLMRouter(llm_config=_config())
    hint = _reserve_hint()

    def fits(length: int) -> bool:
        text = "[0] " + ("a" * length)
        return router._estimate_request_tokens(text, [], hint) <= router.input_token_budget

    low, high = 1, 100_000
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    boundary = low

    fitting = "[0] " + ("a" * boundary)
    overflowing = "[0] " + ("a" * (boundary + 32))
    assert len(router.fit_skeleton_chunks([fitting], [])) == 1

    split = router.fit_skeleton_chunks([overflowing], [])
    assert len(split) > 1
    assert _without_budget_metadata(split) == overflowing


def test_sync_parse_routes_only_budget_safe_chunks():
    CaliperParser.clear_cache()
    parser = CaliperParser(llm_config=_config())
    blocks = [Block(id=0, type="text", text="Document body")]
    skeleton = "".join(
        f"[{block_id}] " + ("同步解析骨架" * 120) + "\n"
        for block_id in range(60)
    )
    parser.compressor.compress = lambda _: [skeleton]
    observed: list[int] = []

    def fake_route_chunk(
        chunk,
        chunk_index,
        total_chunks,
        previous_tail_context="",
        candidates=None,
        max_block_id=-1,
    ):
        hint = _build_window_hint(
            chunk_index, total_chunks, previous_tail_context,
        )
        observed.append(
            parser.router._estimate_request_tokens(chunk, candidates, hint)
        )
        return LLMRouterOutput(doc_title="", doc_authors="", chapters=[])

    parser.router.route_chunk = fake_route_chunk
    tree = parser.parse(blocks)

    assert tree is not None
    assert len(observed) > 1
    assert max(observed) <= parser.router.input_token_budget
    CaliperParser.clear_cache()


def test_async_parse_routes_only_budget_safe_chunks():
    CaliperParser.clear_cache()
    parser = CaliperParser(
        parser_config=ParserConfig(enable_speculative_execution=True),
        llm_config=_config(),
    )
    blocks = [Block(id=0, type="text", text="Document body")]
    skeleton = "".join(
        f"[{block_id}] " + ("异步解析骨架" * 120) + "\n"
        for block_id in range(60)
    )
    parser.compressor.compress = lambda _: [skeleton]
    observed: list[int] = []

    async def fake_route_chunk(
        chunk,
        chunk_index,
        total_chunks,
        previous_tail_context="",
        candidates=None,
        max_block_id=-1,
    ):
        hint = _build_window_hint(
            chunk_index, total_chunks, previous_tail_context,
        )
        observed.append(
            parser.router._estimate_request_tokens(chunk, candidates, hint)
        )
        return LLMRouterOutput(doc_title="", doc_authors="", chapters=[])

    parser.router.async_route_chunk = fake_route_chunk
    tree = asyncio.run(parser.async_parse(blocks))

    assert tree is not None
    assert len(observed) > 1
    assert max(observed) <= parser.router.input_token_budget
    CaliperParser.clear_cache()


def test_document_cache_key_isolated_by_input_budget():
    blocks = [Block(id=0, type="text", text="Same document")]
    parser_a = CaliperParser(llm_config=_config(limit=8192, margin=512))
    parser_b = CaliperParser(llm_config=_config(limit=4096, margin=256))

    assert parser_a.cache_key_for(blocks) != parser_b.cache_key_for(blocks)


def test_input_budget_configuration_rejects_invalid_margin():
    with pytest.raises(ValueError, match="strictly less"):
        _config(limit=8192, margin=8192)
