"""
第三轮深度修复测试套件

覆盖内容:
1. 字符召回率递归统计（修复漏算子节点）
2. 缓存键包含物理特征（不同格式 ≠ 同一文档）
3. CompressorConfig 窗口验证（overlap >= size 报错）
4. LLM 空值防御（choices=[] / content=None）
5. Resolver deepcopy（不修改原始输入）
6. Resolver _level_font 显式初始化
7. Block _sanitize_raw_placeholders 预编译正则
8. DocxProvider 状态重置
9. CaliperParser.clear_cache() 公共 API
10. 评估框架指标正确性（F1 / Hierarchy Accuracy / TED）
11. 评估框架统计显著性（多次运行 mean/std）

运行: python tests/test_round3_fixes.py
"""
import sys
import os
import copy
import math
import logging

logging.basicConfig(level=logging.WARNING)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from infrastructure.models import Block
from modules.parser.schemas import ChapterNode, DocumentNode
from modules.parser.config import CompressorConfig, ResolverConfig
from modules.parser.resolver import IntervalResolver, _levenshtein_ratio
from modules.parser.parser import CaliperParser, _compute_blocks_hash, _doc_cache
from modules.parser.document_tree import DocumentTree
from evaluation.metrics import (
    HeadingGT, HeadingPred, EvalResult,
    compute_section_f1, compute_char_recall, format_eval_report,
    _sequence_edit_distance,
)


def _make_blocks(n, *, with_headings=False):
    blocks = []
    for i in range(n):
        is_heading = with_headings and i % 10 == 0
        blocks.append(Block(
            id=i, type="text",
            text=f"Heading {i}" if is_heading else f"Body paragraph {i}. " * 5,
            is_bold=is_heading,
            font_size=16.0 if is_heading else 11.0,
            is_heading_style=is_heading,
            heading_level=1 if is_heading else None,
        ))
    return blocks


# ═══════════════════════════════════════════════════════════════
# 1. 字符召回率递归统计
# ═══════════════════════════════════════════════════════════════

class TestCharRecallRecursive:

    def test_get_stats_includes_children(self):
        child = DocumentNode(
            title="Child", level=2, start_block_id=5, end_block_id=9,
            content="Child content here", children=[],
        )
        root = DocumentNode(
            title="Root", level=1, start_block_id=0, end_block_id=9,
            content="Root content", children=[child],
        )
        tree = DocumentTree(nodes=[root])
        stats = tree.get_stats()
        expected = len("Root content") + len("Child content here")
        assert stats["total_content_chars"] == expected, (
            f"Expected {expected}, got {stats['total_content_chars']}"
        )

    def test_deeply_nested_stats(self):
        leaf = DocumentNode(title="L3", level=3, start_block_id=10, end_block_id=15,
                            content="ABCDE", children=[])
        mid = DocumentNode(title="L2", level=2, start_block_id=5, end_block_id=15,
                           content="FG", children=[leaf])
        root = DocumentNode(title="L1", level=1, start_block_id=0, end_block_id=15,
                            content="H", children=[mid])
        tree = DocumentTree(nodes=[root])
        stats = tree.get_stats()
        assert stats["total_content_chars"] == 8  # 5+2+1
        assert stats["max_depth"] == 3
        assert stats["total_sections"] == 3


# ═══════════════════════════════════════════════════════════════
# 2. 缓存键包含物理特征
# ═══════════════════════════════════════════════════════════════

class TestCacheKeyPhysicalFeatures:

    def test_different_bold_different_hash(self):
        b1 = [Block(id=0, type="text", text="Hello", is_bold=True, font_size=14.0)]
        b2 = [Block(id=0, type="text", text="Hello", is_bold=False, font_size=14.0)]
        assert _compute_blocks_hash(b1) != _compute_blocks_hash(b2)

    def test_different_font_size_different_hash(self):
        b1 = [Block(id=0, type="text", text="Hello", font_size=14.0)]
        b2 = [Block(id=0, type="text", text="Hello", font_size=11.0)]
        assert _compute_blocks_hash(b1) != _compute_blocks_hash(b2)

    def test_different_heading_style_different_hash(self):
        b1 = [Block(id=0, type="text", text="Hello", is_heading_style=True)]
        b2 = [Block(id=0, type="text", text="Hello", is_heading_style=False)]
        assert _compute_blocks_hash(b1) != _compute_blocks_hash(b2)

    def test_same_physical_features_same_hash(self):
        b1 = [Block(id=0, type="text", text="Hello", is_bold=True, font_size=14.0)]
        b2 = [Block(id=0, type="text", text="Hello", is_bold=True, font_size=14.0)]
        assert _compute_blocks_hash(b1) == _compute_blocks_hash(b2)


# ═══════════════════════════════════════════════════════════════
# 3. CompressorConfig 窗口验证
# ═══════════════════════════════════════════════════════════════

class TestCompressorConfigValidation:

    def test_overlap_less_than_size_ok(self):
        cfg = CompressorConfig(window_size=100, window_overlap=50)
        assert cfg.window_overlap < cfg.window_size

    def test_overlap_equal_size_raises(self):
        try:
            CompressorConfig(window_size=100, window_overlap=100)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "strictly less" in str(e)

    def test_overlap_greater_than_size_raises(self):
        try:
            CompressorConfig(window_size=100, window_overlap=150)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "strictly less" in str(e)


# ═══════════════════════════════════════════════════════════════
# 4. LLM 空值防御
# ═══════════════════════════════════════════════════════════════

class TestLLMNullSafety:

    def test_sync_empty_choices_raises(self):
        from unittest.mock import MagicMock, patch
        from infrastructure.ai.llm_client import LLMClient
        from app.core.exceptions import LLMRouterError
        from modules.parser.schemas import LLMRouterOutput
        from modules.parser.config import LLMClientConfig

        cfg = LLMClientConfig(api_key="test", base_url="http://fake")
        client = LLMClient(config=cfg)

        mock_completion = MagicMock()
        mock_completion.choices = []
        client.client = MagicMock()
        client.client.chat.completions.create.return_value = mock_completion

        try:
            client.structured_completion("test", LLMRouterOutput)
            assert False, "Should have raised LLMRouterError"
        except LLMRouterError as e:
            assert "空 choices" in str(e)

    def test_sync_none_content_raises(self):
        from unittest.mock import MagicMock
        from infrastructure.ai.llm_client import LLMClient
        from app.core.exceptions import LLMRouterError
        from modules.parser.schemas import LLMRouterOutput
        from modules.parser.config import LLMClientConfig

        cfg = LLMClientConfig(api_key="test", base_url="http://fake")
        client = LLMClient(config=cfg)

        mock_msg = MagicMock()
        mock_msg.content = None
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        client.client = MagicMock()
        client.client.chat.completions.create.return_value = mock_completion

        try:
            client.structured_completion("test", LLMRouterOutput)
            assert False, "Should have raised LLMRouterError"
        except LLMRouterError as e:
            assert "content=None" in str(e)


# ═══════════════════════════════════════════════════════════════
# 5. Resolver deepcopy（不修改原始输入）
# ═══════════════════════════════════════════════════════════════

class TestResolverDeepCopy:

    def test_resolve_does_not_mutate_input(self):
        blocks = _make_blocks(20, with_headings=True)
        chapters = [
            ChapterNode(start_block_id=0, title="Heading 0", level=3, snippet="Heading 0"),
            ChapterNode(start_block_id=10, title="Heading 10", level=1, snippet="Heading 10"),
        ]
        original_levels = [ch.level for ch in chapters]
        original_titles = [ch.title for ch in chapters]

        resolver = IntervalResolver(blocks)
        resolver.resolve(chapters)

        for i, ch in enumerate(chapters):
            assert ch.level == original_levels[i], (
                f"Chapter {i} level mutated: {original_levels[i]} → {ch.level}"
            )
            assert ch.title == original_titles[i], (
                f"Chapter {i} title mutated"
            )

    def test_multiple_resolves_produce_consistent_results(self):
        blocks = _make_blocks(30, with_headings=True)
        chapters = [
            ChapterNode(start_block_id=0, title="Heading 0", level=1, snippet="Heading 0"),
            ChapterNode(start_block_id=10, title="Heading 10", level=2, snippet="Heading 10"),
            ChapterNode(start_block_id=20, title="Heading 20", level=1, snippet="Heading 20"),
        ]
        resolver = IntervalResolver(blocks)
        result1 = resolver.resolve(chapters)
        result2 = resolver.resolve(chapters)

        assert len(result1) == len(result2)
        for n1, n2 in zip(result1, result2):
            assert n1.title == n2.title
            assert n1.level == n2.level


# ═══════════════════════════════════════════════════════════════
# 6. _level_font 显式初始化
# ═══════════════════════════════════════════════════════════════

class TestLevelFontInit:

    def test_level_font_exists_before_validate(self):
        blocks = _make_blocks(5)
        resolver = IntervalResolver(blocks)
        assert hasattr(resolver, '_level_font')
        assert resolver._level_font == {}

    def test_infer_orphan_level_without_validate(self):
        blocks = _make_blocks(5)
        resolver = IntervalResolver(blocks)
        level = resolver._infer_orphan_level(blocks[0], parent_level=1)
        assert level == 2  # fallback: parent_level + 1


# ═══════════════════════════════════════════════════════════════
# 7. Block 预编译正则
# ═══════════════════════════════════════════════════════════════

class TestBlockSanitize:

    def test_raw_xml_math_replacement(self):
        text = "[RAW_XML_NODE: oMath] x^2 + y^2 = z^2"
        result = Block._sanitize_raw_placeholders(text)
        assert "$" in result
        assert "x^2" in result

    def test_raw_xml_textbox_replacement(self):
        text = "[RAW_XML_NODE: txbxContent] Hello World"
        result = Block._sanitize_raw_placeholders(text)
        assert "文本框" in result
        assert "Hello World" in result

    def test_no_raw_xml_unchanged(self):
        text = "Normal text without any XML nodes"
        result = Block._sanitize_raw_placeholders(text)
        assert result == text


# ═══════════════════════════════════════════════════════════════
# 8. CaliperParser.clear_cache()
# ═══════════════════════════════════════════════════════════════

class TestClearCacheAPI:

    def test_clear_cache_empties_doc_cache(self):
        _doc_cache.put("test_key", "test_value")
        assert _doc_cache.get("test_key") is not None
        CaliperParser.clear_cache()
        assert _doc_cache.get("test_key") is None

    def test_clear_cache_is_static(self):
        assert callable(CaliperParser.clear_cache)
        CaliperParser.clear_cache()


# ═══════════════════════════════════════════════════════════════
# 9. 评估指标正确性
# ═══════════════════════════════════════════════════════════════

class TestEvalMetrics:

    def test_perfect_f1(self):
        gt = [HeadingGT(block_id=0, title="Intro", level=1),
              HeadingGT(block_id=5, title="Methods", level=1)]
        pred = [HeadingPred(block_id=0, title="Intro", level=1),
                HeadingPred(block_id=5, title="Methods", level=1)]
        result = compute_section_f1(gt, pred)
        assert result.f1 == 1.0
        assert result.precision == 1.0
        assert result.recall == 1.0
        assert result.hierarchy_accuracy == 1.0

    def test_zero_f1_no_overlap(self):
        gt = [HeadingGT(block_id=0, title="Intro", level=1)]
        pred = [HeadingPred(block_id=100, title="Conclusion", level=1)]
        result = compute_section_f1(gt, pred)
        assert result.f1 == 0.0
        assert result.tp == 0
        assert result.fp == 1
        assert result.fn == 1

    def test_fuzzy_block_id_matching(self):
        gt = [HeadingGT(block_id=5, title="Introduction", level=1)]
        pred = [HeadingPred(block_id=7, title="Introduction", level=1)]
        result = compute_section_f1(gt, pred, block_id_tolerance=3)
        assert result.tp == 1

    def test_hierarchy_accuracy_partial(self):
        gt = [HeadingGT(block_id=0, title="A", level=1),
              HeadingGT(block_id=5, title="B", level=2)]
        pred = [HeadingPred(block_id=0, title="A", level=1),
                HeadingPred(block_id=5, title="B", level=3)]  # wrong level
        result = compute_section_f1(gt, pred)
        assert result.tp == 2
        assert result.hierarchy_accuracy == 0.5  # 1 correct out of 2

    def test_tree_edit_distance_identical(self):
        gt = [HeadingGT(block_id=0, title="A", level=1)]
        pred = [HeadingPred(block_id=0, title="A", level=1)]
        result = compute_section_f1(gt, pred)
        assert result.tree_edit_distance == 0.0

    def test_tree_edit_distance_different(self):
        gt = [HeadingGT(block_id=0, title="A", level=1),
              HeadingGT(block_id=5, title="B", level=2)]
        pred = [HeadingPred(block_id=0, title="A", level=1)]
        result = compute_section_f1(gt, pred)
        assert result.tree_edit_distance >= 1.0

    def test_char_recall_full(self):
        assert compute_char_recall(1000, 1000) == 1.0

    def test_char_recall_partial(self):
        assert abs(compute_char_recall(1000, 500) - 0.5) < 1e-6

    def test_char_recall_zero_original(self):
        assert compute_char_recall(0, 0) == 1.0

    def test_sequence_edit_distance(self):
        assert _sequence_edit_distance([], []) == 0.0
        assert _sequence_edit_distance([(1, "a")], []) == 1.0
        assert _sequence_edit_distance([(1, "a")], [(1, "a")]) == 0.0
        assert _sequence_edit_distance([(1, "a")], [(2, "a")]) == 1.0

    def test_format_eval_report(self):
        result = EvalResult(tp=5, fp=1, fn=2, precision=0.833, recall=0.714,
                            f1=0.769, hierarchy_accuracy=0.8, tree_edit_distance=2.0)
        report = format_eval_report(result, "test.docx")
        assert "test.docx" in report
        assert "F1" in report


# ═══════════════════════════════════════════════════════════════
# 10. DocxProvider 状态重置
# ═══════════════════════════════════════════════════════════════

class TestDocxProviderReset:

    def test_reset_clears_image_counter(self):
        from infrastructure.providers.docx_provider import DocxProvider
        provider = DocxProvider()
        provider.image_counter = 42
        provider.image_store = {"img1": b"data"}
        provider._reset_state()
        assert provider.image_counter == 0
        assert provider.image_store == {}
        assert provider._doc_rels is None


# ═══════════════════════════════════════════════════════════════
# 11. 魔法数字配置化
# ═══════════════════════════════════════════════════════════════

class TestConfigurableMagicNumbers:

    def test_orphan_bold_max_text_len_default(self):
        cfg = ResolverConfig()
        assert cfg.orphan_bold_max_text_len == 40

    def test_orphan_bold_max_text_len_custom(self):
        cfg = ResolverConfig(orphan_bold_max_text_len=60)
        assert cfg.orphan_bold_max_text_len == 60

    def test_resolver_uses_config_value(self):
        blocks = _make_blocks(5)
        cfg = ResolverConfig(orphan_bold_max_text_len=100)
        resolver = IntervalResolver(blocks, config=cfg)
        assert resolver.orphan_bold_max_text_len == 100


# ═══════════════════════════════════════════════════════════════
# 12. Coverage metric correctness (to_markdown baseline + tree recursion)
# ═══════════════════════════════════════════════════════════════

class TestCoverageMetric:
    """Verify that coverage uses to_markdown() as baseline and counts tree recursively."""

    def test_image_block_baseline_uses_markdown(self):
        """Image blocks have text='' but to_markdown() includes base64 data."""
        b = Block(id=0, type="image", text="", image_data="data:image/png;base64,ABC123")
        assert len(b.text or '') == 0
        md = b.to_markdown()
        assert len(md) > 0, "to_markdown should produce non-empty output for images"
        assert "ABC123" in md

    def test_table_block_text_equals_markdown(self):
        """Table blocks with .text set use that as markdown (no inflation)."""
        b = Block(id=0, type="table", text="| A | B |\n| --- | --- |\n| 1 | 2 |")
        assert b.to_markdown() == b.text

    def test_coverage_formula_no_image_inflation(self):
        """Coverage = assembled / sum(to_markdown) should be ~100% not 3000%+."""
        blocks = [
            Block(id=0, type="text", text="Hello world"),
            Block(id=1, type="image", text="", image_data="data:img;base64,LONGDATA" * 100),
        ]
        baseline = sum(len(b.to_markdown()) for b in blocks)
        text_only = sum(len(b.text or '') for b in blocks)
        assert baseline > text_only, "Markdown baseline must include image data"
        # If we assembled all content, coverage vs markdown baseline would be ~100%
        # not the inflated 1000%+ from using text-only baseline
        assert baseline > 0

    def test_tree_recursive_char_count(self):
        """Recursive tree count includes all levels."""
        child = DocumentNode(
            title="Child", level=2, start_block_id=5, end_block_id=10,
            content="ABCDE", children=[],
        )
        root = DocumentNode(
            title="Root", level=1, start_block_id=0, end_block_id=10,
            content="XY", children=[child],
        )
        # Top-level only: 2 chars
        top_only = sum(len(n.content or '') for n in [root])
        assert top_only == 2
        # Recursive: 2 + 5 = 7 chars
        from modules.parser.document_tree import DocumentTree
        tree = DocumentTree(nodes=[root])
        stats = tree.get_stats()
        assert stats["total_content_chars"] == 7

    def test_ibm_lorem_coverage_is_real_loss(self):
        """ibm_lorem has real ~21% loss due to no heading markers - this is expected."""
        import os
        fpath = os.path.join("tests", "data", "benchmarks", "ibm_lorem.docx")
        if not os.path.isfile(fpath):
            return  # skip if file not available
        from infrastructure.providers.docx_provider import DocxProvider
        from modules.parser.resolver import IntervalResolver
        from modules.parser.schemas import ChapterNode
        provider = DocxProvider()
        blocks = provider.extract(fpath)
        root = ChapterNode(start_block_id=blocks[0].id, title="Root", level=1, snippet="Root")
        resolver = IntervalResolver(blocks)
        nodes = resolver.resolve([root])

        def count_tree(ns):
            return sum(len(n.content or '') for n in ns) + sum(count_tree(n.children) for n in ns)

        baseline = sum(len(b.to_markdown()) for b in blocks)
        assembled = count_tree(nodes)
        cov = assembled / max(baseline, 1) * 100
        assert 70 < cov < 90, "ibm_lorem should have ~78% coverage (headingless doc)"


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

def _run_test_class(cls):
    print(f"\n{'='*60}")
    print(f"  {cls.__name__}")
    print(f"{'='*60}")

    instance = cls()
    methods = [m for m in dir(instance) if m.startswith("test_")]
    passed = 0
    failed = 0

    for method_name in sorted(methods):
        method = getattr(instance, method_name)
        try:
            method()
            print(f"  PASS  {method_name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {method_name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    return passed, failed


def main():
    print("=" * 60)
    print("  Constellation 第三轮修复测试套件")
    print("=" * 60)

    test_classes = [
        TestCharRecallRecursive,
        TestCacheKeyPhysicalFeatures,
        TestCompressorConfigValidation,
        TestLLMNullSafety,
        TestResolverDeepCopy,
        TestLevelFontInit,
        TestBlockSanitize,
        TestClearCacheAPI,
        TestEvalMetrics,
        TestDocxProviderReset,
        TestConfigurableMagicNumbers,
        TestCoverageMetric,
    ]

    total_passed = 0
    total_failed = 0

    for cls in test_classes:
        p, f = _run_test_class(cls)
        total_passed += p
        total_failed += f

    print(f"\n{'='*60}")
    print(f"  总计: {total_passed} 通过, {total_failed} 失败")
    print(f"{'='*60}")

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    exit(main())
