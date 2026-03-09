"""Test: Constellation pure semantic anchoring for zero-feature documents.

Validates the algorithm's ability to identify headings in documents
that have NO physical formatting (no bold, no heading styles, no
large fonts, no center alignment). The LLM should rely solely on
semantic cues (numbering patterns, keywords).
"""

import sys
import asyncio
import logging
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser.compressor import SkeletonCompressor
from modules.parser.parser import CaliperParser

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("test_semantic_anchor")


def build_zero_feature_blocks() -> list[Block]:
    """Build a document with ZERO physical formatting features.

    All blocks use default font (12pt), no bold, no heading style,
    no center alignment. Headings are only identifiable by their
    semantic content (numbering, keywords).
    """
    paragraphs = [
        # block 0: title-like (short, numbered)
        "Constellation 纯语义锚定能力测试",
        # block 1: body
        "本文档没有任何物理排版特征，所有段落使用相同字号、无加粗、无默认标题样式。",
        # block 2: body
        "这是一个用来验证 Constellation 算法在零特征文档下能力的测试文档。",
        # block 3: heading-like (numbered)
        "1. 核心大纲提取能力",
        # block 4: body
        "大纲提取是文档结构化解析的核心任务，传统方法依赖物理排版特征如加粗、大字号或内嵌 Heading 样式来判断标题。然而实际中存在大量文档的标题并未使用标准排版。",
        # block 5: heading-like (numbered)
        "2. 极端场景表现",
        # block 6: body
        "在完全没有物理标签指引的条件下，算法仅凭语义推断从纯文本流中识别章节锚点。测试覆盖了中文学术文献、技术报告和一般性文档等多种类型。",
        # block 7: heading-like (sub-numbered)
        "2.1 嵌套层级识别",
        # block 8: body
        "子章节的识别需要算法同时理解编号的层级关系，例如 2.1 应属于 2 的子节，而不是一个独立的顶级章节。",
        # block 9: heading-like (keyword)
        "结论",
        # block 10: body
        "实验表明，即使在完全缺乏物理排版特征的条件下，Constellation 架构仍能通过纯语义推断完成基本结构识别。",
    ]

    blocks = []
    for i, text in enumerate(paragraphs):
        blocks.append(Block(
            id=i,
            type="text",
            text=text,
            is_bold=False,
            font_size=12.0,
            alignment=None,
            is_heading_style=False,
            heading_level=None,
        ))
    return blocks


def test_compression_classification():
    """Verify that all blocks are classified as P-frames (no I-frames)."""
    blocks = build_zero_feature_blocks()
    compressor = SkeletonCompressor()

    items = compressor._classify_and_compress(blocks)

    iframe_count = sum(1 for item in items if item["type"] == "iframe")
    pframe_count = sum(1 for item in items if item["type"] == "pframe")

    logger.info("=== Phase 1: I-frame/P-frame Classification ===")
    logger.info("Total blocks: %d", len(blocks))
    logger.info("I-frames (with physical features): %d", iframe_count)
    logger.info("P-frames (no physical features): %d", pframe_count)

    # In a zero-feature doc, ALL blocks should be P-frames
    assert iframe_count == 0, f"Expected 0 I-frames, got {iframe_count}"
    assert pframe_count == len(blocks), f"Expected {len(blocks)} P-frames, got {pframe_count}"
    logger.info("[PASS] All blocks correctly classified as P-frames")
    return True


def test_skeleton_output():
    """Check that the skeleton preserves enough semantic info for the LLM."""
    blocks = build_zero_feature_blocks()
    compressor = SkeletonCompressor()

    skeleton_chunks = compressor.compress(blocks)
    skeleton_text = skeleton_chunks[0]

    logger.info("\n=== Skeleton Output ===")
    logger.info("\n%s", skeleton_text)

    # Check that heading-like lines are visible in the skeleton
    heading_markers = ["1. 核心大纲提取能力", "2. 极端场景表现", "2.1 嵌套层级识别", "结论"]
    for marker in heading_markers:
        # Allow partial match (truncated to 35 chars)
        prefix = marker[:35]
        if prefix in skeleton_text:
            logger.info("[VISIBLE] '%s' found in skeleton", marker)
        else:
            logger.warning("[HIDDEN] '%s' might be truncated/hidden in skeleton", marker)

    return skeleton_text


async def test_full_pipeline():
    """End-to-end test: zero-feature doc → full parse → check headings."""
    blocks = build_zero_feature_blocks()

    logger.info("\n=== Full Pipeline Test (requires LLM) ===")
    logger.info("Parsing %d zero-feature blocks...", len(blocks))

    parser = CaliperParser()
    try:
        doc_tree = await parser.async_parse(blocks)
    except Exception as e:
        logger.error("Pipeline failed: %s", e)
        return False

    markdown = doc_tree.to_markdown()
    sections = doc_tree.to_markdown_sections()
    stats = doc_tree.get_stats()

    logger.info("\n--- Document Tree Stats ---")
    logger.info("Title: %s", stats.get("doc_title", "(none)"))
    logger.info("Total sections: %d", stats.get("total_sections", 0))
    logger.info("Max depth: %d", stats.get("max_depth", 0))

    logger.info("\n--- Sections Found ---")
    for i, sec in enumerate(sections):
        logger.info("  [%d] Level %s: %s", i, sec.get("level", "?"), sec.get("title", "(untitled)"))

    logger.info("\n--- Full Markdown Output ---")
    logger.info("\n%s", markdown[:2000])

    # Verify expected headings were found
    expected_titles = ["核心大纲提取能力", "极端场景表现", "嵌套层级识别", "结论"]
    found = 0
    for title in expected_titles:
        if any(title in sec.get("title", "") for sec in sections):
            logger.info("[FOUND] Heading '%s' correctly identified", title)
            found += 1
        else:
            logger.warning("[MISSED] Heading '%s' was NOT identified", title)

    logger.info("\n=== Result: %d/%d expected headings found ===", found, len(expected_titles))

    if found == len(expected_titles):
        logger.info("[PASS] All headings identified via pure semantic anchoring")
    elif found > 0:
        logger.warning("[PARTIAL] Some headings missed — LLM semantic inference incomplete")
    else:
        logger.error("[FAIL] No headings found — algorithm cannot handle zero-feature documents")

    return found == len(expected_titles)


if __name__ == "__main__":
    print("=" * 60)
    print("Constellation Zero-Feature Document Test")
    print("=" * 60)

    # Test 1: Compression classification (no LLM needed)
    test_compression_classification()

    # Test 2: Skeleton output inspection (no LLM needed)
    test_skeleton_output()

    # Test 3: Full pipeline (requires LLM)
    print("\n" + "=" * 60)
    print("Running full pipeline test (requires LLM connection)...")
    print("=" * 60)
    result = asyncio.run(test_full_pipeline())

    sys.exit(0 if result else 1)
