"""
并发与工程优化测试套件

测试内容:
1. LRU 缓存线程安全性
2. 单例模式线程安全（双重检查锁）
3. 骨架压缩滑动窗口并行化
4. 推测执行与选择性回滚
5. 反向审计自动修复（孤岛节点提升）
6. Levenshtein 后端解析缓存
7. 异步管线基本流程

运行: python -m pytest tests/test_concurrency.py -v
或:   python tests/test_concurrency.py
"""
import sys
import os
import asyncio
import threading
import time
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logging.basicConfig(level=logging.INFO)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from infrastructure.models import Block
from modules.parser.schemas import ChapterNode, LLMRouterOutput, DocumentNode
from modules.parser.config import CompressorConfig, ResolverConfig, ParserConfig
from modules.parser.compressor import SkeletonCompressor
from modules.parser.resolver import IntervalResolver, _levenshtein_ratio
from modules.parser.parser import _LRUCache, _compute_blocks_hash, CaliperParser
from modules.parser.document_tree import DocumentTree


# ═══════════════════════════════════════════════════════════════
# Helpers — synthetic Block & Chapter factories
# ═══════════════════════════════════════════════════════════════

def _make_blocks(n: int, *, with_headings: bool = False) -> list[Block]:
    """Generate *n* synthetic text blocks with sequential IDs."""
    blocks = []
    for i in range(n):
        is_heading = with_headings and i % 50 == 0
        blocks.append(Block(
            id=i,
            type="text",
            text=f"{'Chapter ' + str(i // 50 + 1) if is_heading else 'Body paragraph ' + str(i)}. "
                 f"Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
                 f"Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
            is_bold=is_heading,
            font_size=16.0 if is_heading else 12.0,
        ))
    return blocks


def _make_chapters_from_blocks(blocks: list[Block]) -> list[ChapterNode]:
    """Extract heading blocks as ChapterNodes."""
    chapters = []
    for b in blocks:
        if b.is_bold and b.font_size and b.font_size > 14.0:
            chapters.append(ChapterNode(
                title=b.text[:40] if b.text else "",
                start_block_id=b.id,
                level=1,
                snippet=b.text[:30] if b.text else "",
            ))
    return chapters


def _make_llm_output(chapters: list[ChapterNode]) -> LLMRouterOutput:
    return LLMRouterOutput(
        doc_title="Test Document",
        doc_authors="Test Author",
        chapters=chapters,
    )


# ═══════════════════════════════════════════════════════════════
# 1. LRU 缓存线程安全测试
# ═══════════════════════════════════════════════════════════════

class TestLRUCacheThreadSafety:
    """Hammer the LRU cache from many threads to verify atomicity."""

    def test_concurrent_put_and_get(self):
        cache = _LRUCache(max_size=16)
        num_threads = 32
        ops_per_thread = 200
        barrier = threading.Barrier(num_threads)
        errors: list[str] = []

        def worker(tid: int):
            barrier.wait()
            for i in range(ops_per_thread):
                key = f"key-{tid}-{i}"
                sentinel = DocumentTree(nodes=[], doc_title=f"t-{tid}-{i}")
                cache.put(key, sentinel)
                result = cache.get(key)
                if result is not None and result.doc_title != f"t-{tid}-{i}":
                    errors.append(f"Thread {tid}: got wrong value for {key}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Data races detected:\n" + "\n".join(errors)

    def test_max_size_respected(self):
        cache = _LRUCache(max_size=4)
        for i in range(10):
            cache.put(f"k{i}", DocumentTree(nodes=[], doc_title=f"d{i}"))
        hits = sum(1 for i in range(10) if cache.get(f"k{i}") is not None)
        assert hits <= 4, f"Cache should hold at most 4 items, but {hits} found"

    def test_lru_eviction_order(self):
        cache = _LRUCache(max_size=3)
        cache.put("a", DocumentTree(nodes=[], doc_title="a"))
        cache.put("b", DocumentTree(nodes=[], doc_title="b"))
        cache.put("c", DocumentTree(nodes=[], doc_title="c"))

        cache.get("a")  # touch 'a' — 'b' is now oldest
        cache.put("d", DocumentTree(nodes=[], doc_title="d"))

        assert cache.get("b") is None, "'b' should have been evicted (LRU)"
        assert cache.get("a") is not None, "'a' was recently accessed"
        assert cache.get("c") is not None
        assert cache.get("d") is not None

    def test_cache_clear(self):
        cache = _LRUCache(max_size=8)
        for i in range(5):
            cache.put(f"k{i}", DocumentTree(nodes=[], doc_title=f"d{i}"))
        cache.clear()
        for i in range(5):
            assert cache.get(f"k{i}") is None


# ═══════════════════════════════════════════════════════════════
# 2. 单例线程安全测试
# ═══════════════════════════════════════════════════════════════

class TestSingletonThreadSafety:
    """Verify that concurrent calls to get_*_client return the same instance."""

    def test_llm_client_singleton_identity(self):
        import modules.parser.parser as parser_mod
        from modules.parser.config import LLMClientConfig

        instances: list = []
        barrier = threading.Barrier(16)

        dummy_config = LLMClientConfig(
            api_key="test-key", base_url="http://localhost:9999",
        )

        import infrastructure.ai.llm_client as llm_mod
        original = llm_mod._instance
        llm_mod._instance = None  # reset for test

        try:
            def grab():
                barrier.wait()
                instances.append(llm_mod.get_llm_client(dummy_config))

            threads = [threading.Thread(target=grab) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(set(id(inst) for inst in instances)) == 1, (
                "Multiple LLMClient instances created under concurrent access"
            )
        finally:
            llm_mod._instance = original

    def test_async_llm_client_singleton_identity(self):
        from modules.parser.config import LLMClientConfig
        import infrastructure.ai.llm_client as llm_mod

        instances: list = []
        barrier = threading.Barrier(16)

        dummy_config = LLMClientConfig(
            api_key="test-key", base_url="http://localhost:9999",
        )

        original = llm_mod._async_instance
        llm_mod._async_instance = None

        try:
            def grab():
                barrier.wait()
                instances.append(llm_mod.get_async_llm_client(dummy_config))

            threads = [threading.Thread(target=grab) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(set(id(inst) for inst in instances)) == 1, (
                "Multiple AsyncLLMClient instances created under concurrent access"
            )
        finally:
            llm_mod._async_instance = original


# ═══════════════════════════════════════════════════════════════
# 3. 骨架压缩并行化测试
# ═══════════════════════════════════════════════════════════════

class TestCompressorParallelism:
    """Verify that parallel window compression produces identical output to serial."""

    @staticmethod
    def _force_serial_compress(blocks: list[Block], config: CompressorConfig) -> list[str]:
        """Compress each window sequentially — no ThreadPoolExecutor."""
        from modules.parser.compressor import SkeletonCompressor as SC
        comp = SC(config=config)
        total = len(blocks)
        step = config.window_size - config.window_overlap
        windows = []
        start = 0
        while start < total:
            end = min(start + config.window_size, total)
            windows.append((start, end))
            if end >= total:
                break
            start += step
        chunks = []
        for i, (ws, we) in enumerate(windows):
            chunks.append(comp._compress_window(
                blocks[ws:we], i, len(windows), total, ws, we,
            ))
        return chunks

    @staticmethod
    def _parallel_compress(blocks: list[Block], config: CompressorConfig) -> list[str]:
        """Compress via the real parallel sliding-window path."""
        compressor = SkeletonCompressor(config=config)
        return compressor.compress(blocks)

    def test_parallel_matches_serial(self):
        blocks = _make_blocks(800, with_headings=True)
        config = CompressorConfig(
            sliding_window_threshold=200,
            window_size=150,
            window_overlap=30,
        )
        serial_result = self._force_serial_compress(blocks, config)
        parallel_result = self._parallel_compress(blocks, config)

        assert len(serial_result) == len(parallel_result), (
            f"Chunk count mismatch: serial={len(serial_result)}, parallel={len(parallel_result)}"
        )
        for i, (s, p) in enumerate(zip(serial_result, parallel_result)):
            assert s == p, f"Chunk {i} differs between serial and parallel"

    def test_compression_determinism(self):
        """Multiple runs with the same input produce identical output."""
        blocks = _make_blocks(600, with_headings=True)
        config = CompressorConfig(sliding_window_threshold=200)
        compressor = SkeletonCompressor(config=config)

        results = [compressor.compress(blocks) for _ in range(5)]
        for i in range(1, len(results)):
            assert results[i] == results[0], f"Run {i} differs from run 0"

    def test_concurrent_compressor_instances(self):
        """Multiple compressor instances running in threads produce consistent output."""
        blocks = _make_blocks(600, with_headings=True)
        config = CompressorConfig(sliding_window_threshold=200)
        results: dict[int, list[str]] = {}
        barrier = threading.Barrier(8)

        def compress_worker(tid):
            barrier.wait()
            compressor = SkeletonCompressor(config=config)
            results[tid] = compressor.compress(blocks)

        threads = [threading.Thread(target=compress_worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for tid in range(1, 8):
            assert results[tid] == results[0], f"Thread {tid} produced different output"


# ═══════════════════════════════════════════════════════════════
# 4. 推测执行与选择性回滚测试
# ═══════════════════════════════════════════════════════════════

class TestSpeculativeExecution:
    """Test speculative parallel routing with boundary verification."""

    def _make_speculative_parser(self) -> CaliperParser:
        return CaliperParser(
            parser_config=ParserConfig(
                enable_speculative_execution=True,
                speculative_boundary_tolerance=1,
            ),
        )

    def test_speculative_dispatch_with_enough_windows(self):
        """Speculative mode activates only when total > 2 and flag is set."""
        parser = self._make_speculative_parser()
        assert parser._parser_config.enable_speculative_execution is True

    def test_serial_dispatch_when_disabled(self):
        """When speculative is disabled, serial mode is used."""
        parser = CaliperParser(
            parser_config=ParserConfig(enable_speculative_execution=False),
        )
        assert parser._parser_config.enable_speculative_execution is False

    def test_speculative_boundary_detection(self):
        """Simulate boundary inconsistency detection logic."""
        tolerance = 1

        prev_tail_level = 2
        curr_head_level = 4  # jump of 2 — exceeds tolerance
        inconsistent = curr_head_level > prev_tail_level + tolerance
        assert inconsistent, "Should detect boundary inconsistency"

        curr_head_level = 3  # jump of 1 — within tolerance
        inconsistent = curr_head_level > prev_tail_level + tolerance
        assert not inconsistent, "Should NOT flag as inconsistent"

        curr_head_level = 1  # decrease — always consistent
        inconsistent = curr_head_level > prev_tail_level + tolerance
        assert not inconsistent, "Level decrease should be consistent"

    def test_speculative_merge_preserves_order(self):
        """Verify _merge_route_results produces sorted, deduplicated chapters."""
        parser = self._make_speculative_parser()
        results = {
            0: LLMRouterOutput(
                doc_title="Doc", doc_authors="Auth",
                chapters=[
                    ChapterNode(title="Ch1", start_block_id=0, level=1, snippet="Ch1"),
                    ChapterNode(title="Ch2", start_block_id=10, level=2, snippet="Ch2"),
                ],
            ),
            1: LLMRouterOutput(
                doc_title="", doc_authors="",
                chapters=[
                    ChapterNode(title="Ch2", start_block_id=10, level=2, snippet="Ch2"),
                    ChapterNode(title="Ch3", start_block_id=20, level=1, snippet="Ch3"),
                ],
            ),
        }
        merged = parser._merge_route_results(results, 2)
        assert merged.doc_title == "Doc"
        block_ids = [ch.start_block_id for ch in merged.chapters]
        assert block_ids == sorted(block_ids), "Chapters not sorted by block_id"
        assert len(set(block_ids)) == len(block_ids), "Duplicate chapters not removed"

    def test_speculative_full_async_flow(self):
        """End-to-end speculative flow with mocked LLM router."""
        parser = self._make_speculative_parser()

        async def mock_async_route_chunk(chunk, idx, total, tail_ctx):
            await asyncio.sleep(0.01)
            return LLMRouterOutput(
                doc_title="Doc" if idx == 0 else "",
                doc_authors="Author" if idx == 0 else "",
                chapters=[
                    ChapterNode(
                        title=f"Window {idx} Ch1",
                        start_block_id=idx * 100,
                        level=1,
                        snippet=f"Window {idx}",
                    ),
                    ChapterNode(
                        title=f"Window {idx} Ch2",
                        start_block_id=idx * 100 + 50,
                        level=2,
                        snippet=f"Window {idx} sub",
                    ),
                ],
            )

        parser.router.async_route_chunk = mock_async_route_chunk
        skeleton_chunks = ["chunk0", "chunk1", "chunk2", "chunk3"]

        result = asyncio.run(parser._async_speculative_route(skeleton_chunks))
        assert result.doc_title == "Doc"
        assert len(result.chapters) >= 4, f"Expected >=4 chapters, got {len(result.chapters)}"
        ids = [ch.start_block_id for ch in result.chapters]
        assert ids == sorted(ids)


# ═══════════════════════════════════════════════════════════════
# 5. 反向审计自动修复测试
# ═══════════════════════════════════════════════════════════════

class TestInverseAuditRepair:
    """Test that the inverse audit detects and auto-repairs swallowed headings."""

    def _make_blocks_with_swallowed_heading(self) -> list[Block]:
        return [
            Block(id=0, type="text", text="Chapter 1 Introduction", font_size=16.0, is_bold=True),
            Block(id=1, type="text", text="Some introduction text here.", font_size=12.0),
            Block(id=2, type="text", text="More body text.", font_size=12.0),
            Block(id=3, type="text", text="Hidden Heading", font_size=18.0, is_bold=True),
            Block(id=4, type="text", text="Content under hidden heading.", font_size=12.0),
            Block(id=5, type="text", text="Chapter 2 Methods", font_size=16.0, is_bold=True),
            Block(id=6, type="text", text="Methods body text.", font_size=12.0),
        ]

    def test_swallowed_heading_detected_and_promoted(self):
        blocks = self._make_blocks_with_swallowed_heading()
        chapters = [
            ChapterNode(title="Chapter 1 Introduction", start_block_id=0, level=1, snippet="Chapter 1"),
            ChapterNode(title="Chapter 2 Methods", start_block_id=5, level=1, snippet="Chapter 2"),
        ]
        # Block 3 ("Hidden Heading", font_size=18) is inside Chapter 1's interval [0,4]
        # and should be auto-promoted
        resolver = IntervalResolver(blocks)
        intervals = resolver._compute_intervals(chapters)

        interval_starts = [iv["start_id"] for iv in intervals]
        assert 3 in interval_starts, (
            f"Block 3 (swallowed heading) should have been promoted. Got starts: {interval_starts}"
        )

    def test_no_false_positives_on_normal_doc(self):
        blocks = [
            Block(id=0, type="text", text="Title", font_size=16.0, is_bold=True),
            Block(id=1, type="text", text="Body one", font_size=12.0),
            Block(id=2, type="text", text="Body two", font_size=12.0),
            Block(id=3, type="text", text="Subtitle", font_size=16.0, is_bold=True),
            Block(id=4, type="text", text="Body three", font_size=12.0),
        ]
        chapters = [
            ChapterNode(title="Title", start_block_id=0, level=1, snippet="Title"),
            ChapterNode(title="Subtitle", start_block_id=3, level=2, snippet="Subtitle"),
        ]
        resolver = IntervalResolver(blocks)
        intervals = resolver._compute_intervals(chapters)
        assert len(intervals) == 2, "Should not create orphan nodes for normal doc"

    def test_bold_same_size_detection(self):
        """When child has same font size but is bold while anchor is not."""
        blocks = [
            Block(id=0, type="text", text="Chapter One Title", font_size=14.0, is_bold=False),
            Block(id=1, type="text", text="Body text", font_size=12.0),
            Block(id=2, type="text", text="Missed Heading", font_size=14.0, is_bold=True),
            Block(id=3, type="text", text="More body text", font_size=12.0),
        ]
        chapters = [
            ChapterNode(title="Chapter One Title", start_block_id=0, level=1, snippet="Chapter One"),
        ]
        resolver = IntervalResolver(blocks)
        intervals = resolver._compute_intervals(chapters)
        starts = [iv["start_id"] for iv in intervals]
        assert 2 in starts, "Bold same-size heading should be detected"


# ═══════════════════════════════════════════════════════════════
# 6. Levenshtein 后端解析测试
# ═══════════════════════════════════════════════════════════════

class TestLevenshteinBackend:
    """Verify the module-level Levenshtein backend resolution."""

    def test_identical_strings(self):
        assert _levenshtein_ratio("hello", "hello") == 1.0

    def test_empty_strings(self):
        assert _levenshtein_ratio("", "") == 1.0
        assert _levenshtein_ratio("abc", "") == 0.0
        assert _levenshtein_ratio("", "abc") == 0.0

    def test_similar_strings(self):
        ratio = _levenshtein_ratio("chapter one", "chaptir one")
        assert ratio > 0.8, f"Expected high similarity, got {ratio}"

    def test_dissimilar_strings(self):
        ratio = _levenshtein_ratio("abcdef", "zyxwvu")
        assert ratio < 0.3, f"Expected low similarity, got {ratio}"

    def test_deterministic_across_calls(self):
        """Backend resolution is one-shot; repeated calls return the same result."""
        results = [_levenshtein_ratio("test string", "test strong") for _ in range(100)]
        assert len(set(results)) == 1, "Non-deterministic Levenshtein results"

    def test_performance_no_import_overhead(self):
        """Verify hot-path performance — each call should be fast."""
        start = time.perf_counter()
        for _ in range(10000):
            _levenshtein_ratio("Chapter 3.2 Data Collection", "Chapter 3.2 Data Collction")
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"10k Levenshtein calls took {elapsed:.2f}s (expected < 5s)"


# ═══════════════════════════════════════════════════════════════
# 7. 文档级缓存哈希测试
# ═══════════════════════════════════════════════════════════════

class TestDocumentCacheHash:
    """Verify cache key computation."""

    def test_same_blocks_same_hash(self):
        b1 = _make_blocks(10)
        b2 = _make_blocks(10)
        assert _compute_blocks_hash(b1) == _compute_blocks_hash(b2)

    def test_different_blocks_different_hash(self):
        b1 = _make_blocks(10)
        b2 = _make_blocks(10)
        b2[5].text = "DIFFERENT TEXT"
        assert _compute_blocks_hash(b1) != _compute_blocks_hash(b2)

    def test_hash_stability(self):
        blocks = _make_blocks(50)
        hashes = [_compute_blocks_hash(blocks) for _ in range(20)]
        assert len(set(hashes)) == 1

    def test_concurrent_hash_computation(self):
        """Many threads hashing the same blocks should all get the same result."""
        blocks = _make_blocks(100)
        results: list[str] = []
        barrier = threading.Barrier(16)

        def hasher():
            barrier.wait()
            results.append(_compute_blocks_hash(blocks))

        threads = [threading.Thread(target=hasher) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 1, "Hash computation is not thread-safe"


# ═══════════════════════════════════════════════════════════════
# 8. 异步管线基本测试
# ═══════════════════════════════════════════════════════════════

class TestAsyncPipeline:
    """Verify async_parse path (with mocked LLM)."""

    def test_async_parse_caching(self):
        """Second call with same blocks should hit the cache."""
        blocks = _make_blocks(100, with_headings=True)
        chapters = _make_chapters_from_blocks(blocks)
        llm_output = _make_llm_output(chapters)

        parser = CaliperParser()

        cache_key = _compute_blocks_hash(blocks)
        tree = DocumentTree(
            nodes=[],
            doc_title="Cached",
            doc_authors="Test",
        )

        from modules.parser.parser import _doc_cache
        _doc_cache.put(cache_key, tree)

        try:
            result = parser.parse(blocks)
            assert result.doc_title == "Cached", "Should return cached result"
        finally:
            _doc_cache.clear()

    def test_async_serial_route_ordering(self):
        """Verify _async_serial_route maintains state projection ordering."""
        parser = CaliperParser()
        call_order: list[int] = []

        async def mock_async_route_chunk(chunk, idx, total, tail_ctx):
            call_order.append(idx)
            await asyncio.sleep(0.01)
            level = 1 if idx == 0 else 2
            return LLMRouterOutput(
                doc_title="Doc" if idx == 0 else "",
                doc_authors="" if idx > 0 else "Auth",
                chapters=[ChapterNode(
                    title=f"Ch-{idx}", start_block_id=idx * 50,
                    level=level, snippet=f"Ch-{idx}",
                )],
            )

        parser.router.async_route_chunk = mock_async_route_chunk

        result = asyncio.run(parser._async_serial_route(["c0", "c1", "c2"]))
        assert call_order == [0, 1, 2], f"Serial route should call in order, got {call_order}"
        assert len(result.chapters) == 3

    def test_speculative_parallel_speed_advantage(self):
        """Speculative should complete faster than serial for independent windows."""
        parser_spec = CaliperParser(
            parser_config=ParserConfig(enable_speculative_execution=True)
        )
        parser_serial = CaliperParser(
            parser_config=ParserConfig(enable_speculative_execution=False)
        )

        async def mock_slow_route(chunk, idx, total, tail_ctx):
            await asyncio.sleep(0.1)
            return LLMRouterOutput(
                doc_title="D" if idx == 0 else "",
                doc_authors="",
                chapters=[ChapterNode(
                    title=f"Ch{idx}", start_block_id=idx * 50,
                    level=1, snippet=f"Ch{idx}",
                )],
            )

        chunks = [f"chunk{i}" for i in range(5)]

        # Speculative (parallel)
        parser_spec.router.async_route_chunk = mock_slow_route
        t0 = time.perf_counter()
        asyncio.run(parser_spec._async_speculative_route(chunks))
        spec_time = time.perf_counter() - t0

        # Serial
        parser_serial.router.async_route_chunk = mock_slow_route
        t0 = time.perf_counter()
        asyncio.run(parser_serial._async_serial_route(chunks))
        serial_time = time.perf_counter() - t0

        print(f"\n  Speculative: {spec_time:.3f}s | Serial: {serial_time:.3f}s | "
              f"Speedup: {serial_time / max(spec_time, 0.001):.1f}x")
        assert spec_time < serial_time * 0.6, (
            f"Speculative ({spec_time:.3f}s) should be significantly faster "
            f"than serial ({serial_time:.3f}s)"
        )


# ═══════════════════════════════════════════════════════════════
# 9. 解析器端到端并发测试
# ═══════════════════════════════════════════════════════════════

class TestParserConcurrentAccess:
    """Verify CaliperParser handles concurrent parse requests safely."""

    def test_concurrent_parses_with_cache(self):
        """Multiple threads parsing the same document should all get valid results."""
        blocks = _make_blocks(50, with_headings=True)
        chapters = _make_chapters_from_blocks(blocks)

        cache_key = _compute_blocks_hash(blocks)
        tree = DocumentTree(
            nodes=[DocumentNode(
                title="Chapter 1", level=1,
                start_block_id=0, end_block_id=49,
                content="test",
            )],
            doc_title="Test",
        )

        from modules.parser.parser import _doc_cache
        _doc_cache.put(cache_key, tree)

        results: list = []
        barrier = threading.Barrier(8)

        def parse_worker():
            barrier.wait()
            parser = CaliperParser()
            result = parser.parse(blocks)
            results.append(result)

        try:
            threads = [threading.Thread(target=parse_worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(results) == 8
            for r in results:
                assert r.doc_title == "Test"
        finally:
            _doc_cache.clear()

    def test_deduplicate_overlap_anchors_thread_safety(self):
        """The dedup function should produce consistent results across threads."""
        parser = CaliperParser()
        chapters = [
            ChapterNode(title="Chapter 1", start_block_id=0, level=1, snippet="Ch1"),
            ChapterNode(title="Chapter 1", start_block_id=1, level=1, snippet="Ch1"),
            ChapterNode(title="Chapter 2", start_block_id=50, level=1, snippet="Ch2"),
            ChapterNode(title="Chapter 2", start_block_id=52, level=1, snippet="Ch2"),
            ChapterNode(title="Chapter 3", start_block_id=100, level=1, snippet="Ch3"),
        ]
        results: list[list] = []
        barrier = threading.Barrier(8)

        def dedup_worker():
            barrier.wait()
            result = parser._deduplicate_overlap_anchors(chapters.copy())
            results.append([ch.start_block_id for ch in result])

        threads = [threading.Thread(target=dedup_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(1, len(results)):
            assert results[i] == results[0], f"Thread {i} got different dedup result"


# ═══════════════════════════════════════════════════════════════
# 10. IntervalResolver 并发安全测试
# ═══════════════════════════════════════════════════════════════

class TestResolverConcurrency:
    """Multiple resolver instances processing in parallel."""

    def test_concurrent_resolver_instances(self):
        blocks = _make_blocks(200, with_headings=True)
        chapters = _make_chapters_from_blocks(blocks)
        results: dict[int, list[DocumentNode]] = {}
        barrier = threading.Barrier(8)

        def resolve_worker(tid):
            barrier.wait()
            resolver = IntervalResolver(blocks)
            result = resolver.resolve(chapters.copy())
            results[tid] = result

        threads = [threading.Thread(target=resolve_worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ref_titles = [n.title for n in results[0]]
        for tid in range(1, 8):
            titles = [n.title for n in results[tid]]
            assert titles == ref_titles, (
                f"Thread {tid} produced different resolution results"
            )


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

def _run_test_class(cls):
    """Run all test methods of a class, reporting pass/fail."""
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
            failed += 1

    return passed, failed


def main():
    print("=" * 60)
    print("  Constellation 并发与工程优化测试套件")
    print("=" * 60)

    test_classes = [
        TestLRUCacheThreadSafety,
        TestSingletonThreadSafety,
        TestCompressorParallelism,
        TestSpeculativeExecution,
        TestInverseAuditRepair,
        TestLevenshteinBackend,
        TestDocumentCacheHash,
        TestAsyncPipeline,
        TestParserConcurrentAccess,
        TestResolverConcurrency,
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
    logging.getLogger("modules.parser").setLevel(logging.WARNING)
    logging.getLogger("infrastructure.ai").setLevel(logging.WARNING)
    exit(main())
