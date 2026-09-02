from __future__ import annotations

from infrastructure.models import Block
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig, ParserConfig
from modules.parser.global_inference import GlobalHeadingInference
from modules.parser.heading_candidates import (
    format_candidate_table,
    generate_heading_candidate_set,
    select_route_candidates,
)
from modules.parser.schemas import ChapterNode, HeadingCandidateSet


def _body(block_id: int, text: str, **kwargs) -> Block:
    return Block(id=block_id, type="text", text=text, **kwargs)


def test_sparse_skeleton_expands_candidate_context_and_uses_atomic_gaps():
    blocks = [_body(i, f"ordinary body paragraph {i}. " + "x" * 80) for i in range(30)]
    blocks[15] = _body(
        15, "3.2 Global Decoder", is_bold=True, font_size=16,
        has_heading_numbering=True,
    )
    candidate_set = generate_heading_candidate_set(blocks)
    routed = select_route_candidates(candidate_set, blocks)

    skeleton = SkeletonCompressor(CompressorConfig(
        candidate_context_blocks=1,
        sparse_preamble_blocks=2,
    )).compress(
        blocks,
        candidates=routed,
        region_risks=candidate_set.region_risks,
    )[0]

    assert "[15]" in skeleton
    assert "[14]" in skeleton and "[16]" in skeleton
    assert "<Gap:" in skeleton
    assert "ordinary body paragraph 8" not in skeleton
    assert "[2 to 13]" in skeleton


def test_featureless_escape_region_is_broad_scanned_not_hidden():
    blocks = [
        _body(i, text, font_size=10, metadata={"page": 1, "layout_column": 0})
        for i, text in enumerate([
            "A plain body sentence.",
            "Unusual Local Heading",
            "Another plain body sentence.",
        ])
    ]
    candidate_set = generate_heading_candidate_set(blocks)
    skeleton = SkeletonCompressor(CompressorConfig(sparse_preamble_blocks=0)).compress(
        blocks,
        candidates=select_route_candidates(candidate_set, blocks),
        region_risks=candidate_set.region_risks,
    )[0]

    assert "Unusual Local Heading" in skeleton
    assert "<Gap:" not in skeleton


def test_candidate_table_contains_every_shard_candidate():
    candidates = []
    for index in range(35):
        candidate_set = generate_heading_candidate_set([
            _body(
                index,
                f"{index + 1}. Section {index}",
                has_heading_numbering=True,
            )
        ])
        candidates.extend(candidate_set.candidates)

    table = format_candidate_table(candidates)

    assert "Additional candidate" not in table
    assert sum(1 for line in table.splitlines() if line.startswith("[")) == 35
    for index in range(35):
        assert f"[{index}]" in table


def test_strict_first_rejects_safe_table_out_vote():
    blocks = [
        _body(0, "1 Introduction", is_bold=True, has_heading_numbering=True),
        _body(1, "ordinary emphasized prose", is_bold=True),
    ]
    inference = GlobalHeadingInference(
        blocks,
        HeadingCandidateSet(),
        parser_config=ParserConfig(strict_first_routing=True),
    )
    decoded = inference.decode([
        ChapterNode(
            block_id=1,
            title="ordinary emphasized prose",
            snippet="ordinary emphasized prose",
            level=1,
            confidence=0.95,
            out_of_candidate=True,
        )
    ])

    assert decoded == []


def test_strict_first_allows_aligned_vote_only_in_escape_region():
    blocks = [
        _body(
            0,
            "Unusual Local Heading",
            font_size=10,
            metadata={"page": 1, "layout_column": 0},
        ),
        _body(
            1,
            "body sentence with no formatting.",
            font_size=10,
            metadata={"page": 1, "layout_column": 0},
        ),
    ]
    inference = GlobalHeadingInference(
        blocks,
        HeadingCandidateSet(),
        parser_config=ParserConfig(strict_first_routing=True),
    )
    decoded = inference.decode([
        ChapterNode(
            block_id=0,
            title="Unusual Local Heading",
            snippet="Unusual Local Heading",
            level=1,
            confidence=0.99,
            out_of_candidate=True,
        )
    ])

    assert [chapter.start_block_id for chapter in decoded] == [0]
