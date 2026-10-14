import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser.compressor import SkeletonCompressor


def build_zero_feature_blocks() -> list[Block]:
    paragraphs = [
        "Semantic Anchoring Demo",
        "This document has no bold text, no heading styles, and no centered titles.",
        "1. Core Outline Extraction",
        "The parser must rely on numbering and short semantic cues.",
        "2. Extreme Case Handling",
        "2.1 Nested Heading Recognition",
        "Conclusion",
    ]
    return [
        Block(
            id=i,
            type="text",
            text=text,
            is_bold=False,
            font_size=12.0,
            alignment=None,
            is_heading_style=False,
            heading_level=None,
        )
        for i, text in enumerate(paragraphs)
    ]


def test_zero_feature_blocks_stay_pframes():
    blocks = build_zero_feature_blocks()
    items = SkeletonCompressor()._classify_and_compress(blocks)

    assert sum(1 for item in items if item["type"] == "iframe") == 0
    assert sum(1 for item in items if item["type"] == "pframe") == len(blocks)


def test_zero_feature_skeleton_keeps_semantic_markers_visible():
    blocks = build_zero_feature_blocks()
    skeleton = SkeletonCompressor().compress(blocks)[0]

    for marker in [
        "1. Core Outline Extraction",
        "2. Extreme Case Handling",
        "2.1 Nested Heading Recognition",
        "Conclusion",
    ]:
        assert marker[:35] in skeleton
