"""Production parser contracts for candidate-first compression and fallback."""
from __future__ import annotations

import asyncio

import pytest

from infrastructure.models import Block
from modules.parser.config import CompressorConfig, ParserConfig
from modules.parser.parser import CaliperParser
from modules.parser.schemas import LLMRouterOutput


def _blocks(tag: str) -> list[Block]:
    blocks = [
        Block(
            id=index,
            type="text",
            text=f"{tag} ordinary body paragraph {index}. " + "x" * 90,
            font_size=10.0,
        )
        for index in range(18)
    ]
    blocks[9] = Block(
        id=9,
        type="text",
        text=f"2.1 {tag} Decoder",
        font_size=15.0,
        is_bold=True,
        has_heading_numbering=True,
    )
    return blocks


def _run_entry(parser: CaliperParser, entry: str, blocks: list[Block]):
    empty = LLMRouterOutput(doc_title="", doc_authors="", chapters=[])
    if entry == "async_parse":
        async def route(*_args, **_kwargs):
            return empty

        parser.router.async_route = route
        return asyncio.run(parser.async_parse(blocks))

    parser.router.route = lambda *_args, **_kwargs: empty
    result = getattr(parser, entry)(blocks)
    return result[0] if entry == "parse_with_timing" else result


@pytest.mark.parametrize("entry", ["parse", "parse_with_timing", "async_parse"])
def test_candidate_set_is_built_once_before_compression(monkeypatch, entry):
    """All public parser entries share one candidate-first Stage 2 contract."""
    import modules.parser.parser as parser_module

    CaliperParser.clear_cache()
    events: list[str] = []
    original_generate = parser_module.generate_heading_candidate_set

    def generate_once(blocks):
        events.append("candidate-set")
        return original_generate(blocks)

    monkeypatch.setattr(parser_module, "generate_heading_candidate_set", generate_once)
    parser = CaliperParser(compressor_config=CompressorConfig(enable_rle=False))
    original_compress = parser.compressor.compress

    def compress_after_candidates(blocks, *args, **kwargs):
        events.append("compress")
        return original_compress(blocks, *args, **kwargs)

    monkeypatch.setattr(parser.compressor, "compress", compress_after_candidates)
    tree = _run_entry(parser, entry, _blocks(f"once-{entry}"))

    assert tree is not None
    assert events.count("candidate-set") == 1
    assert events.count("compress") == 1
    assert events.index("candidate-set") < events.index("compress")
    CaliperParser.clear_cache()


@pytest.mark.parametrize("entry", ["parse", "parse_with_timing", "async_parse"])
def test_disabled_candidates_use_complete_standard_skeleton(monkeypatch, entry):
    """The no-candidate ablation must not collapse into an empty sparse view."""
    import modules.parser.parser as parser_module

    CaliperParser.clear_cache()

    def forbidden_generation(_blocks):
        raise AssertionError("disabled candidates must not build a CandidateSet")

    monkeypatch.setattr(
        parser_module,
        "generate_heading_candidate_set",
        forbidden_generation,
    )
    parser = CaliperParser(
        compressor_config=CompressorConfig(enable_rle=False),
        parser_config=ParserConfig(enable_heading_candidates=False),
    )
    captured: dict[str, object] = {}
    empty = LLMRouterOutput(doc_title="", doc_authors="", chapters=[])

    def capture(skeleton, *, candidates=None, **_kwargs):
        captured["skeleton"] = skeleton
        captured["candidates"] = candidates
        return empty

    if entry == "async_parse":
        async def async_capture(skeleton, *, candidates=None, **_kwargs):
            return capture(skeleton, candidates=candidates)

        parser.router.async_route = async_capture
        tree = asyncio.run(parser.async_parse(_blocks(f"fallback-{entry}")))
    else:
        parser.router.route = capture
        result = getattr(parser, entry)(_blocks(f"fallback-{entry}"))
        tree = result[0] if entry == "parse_with_timing" else result

    skeleton = str(captured["skeleton"])
    assert tree is not None
    assert captured["candidates"] is None
    assert "Constellation Virtual Skeleton" in skeleton
    assert "Candidate-Aware Sparse Skeleton" not in skeleton
    assert "<Gap:" not in skeleton
    assert f"fallback-{entry}" in skeleton
    for block_id in range(18):
        assert f"[{block_id}]" in skeleton
    CaliperParser.clear_cache()
