"""CaliperParser — Constellation main parser.

Orchestrates the four-stage pipeline:

Stage 1: Physical dimensionality reduction (DocxProvider)
    .docx -> List[Block] with physical feature annotations

Stage 2: Virtual skeleton compression (SkeletonCompressor)
    List[Block] -> compact skeleton text (I/P-frame + RLE + Meta-Tag)

Stage 3: AI cursor routing (LLMRouter)
    Skeleton text -> section anchor list [{block_id, title, level}]

Stage 4: Cursor closure & assembly (IntervalResolver + DocumentTree)
    Anchors + original Blocks -> DocumentTree -> structured Markdown
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import re
import threading
import time as _time
import sys
from collections import OrderedDict, deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, List, Optional

from infrastructure.models import Block
from modules.parser.analysis_context import analysis_scope, block_index, set_block_store
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig, LLMClientConfig, ParserConfig, ResolverConfig
from modules.parser.anchor_alignment import MonotonicAnchorAligner
from modules.parser.heading_candidates import (
    _body_font_size,
    _semantic_level,
    candidates_in_range,
    generate_heading_candidate_set,
    select_route_candidates,
    infer_numbering_level,
    is_featureless_document,
)
from modules.parser.router import LLMRouter
from modules.parser.resolver import IntervalResolver, _levenshtein_ratio
from modules.parser.review_regions import (
    ReviewPolicy,
    ReviewRegion,
    ReviewReport,
    count_signals,
)
from modules.parser.document_tree import DocumentTree
from modules.parser.titles import (
    PSEUDO_ROOT_TITLE,
    strip_title_emphasis,
    structural_text,
)
from modules.parser.schemas import (
    ChapterNode,
    DocumentNode,
    HeadingCandidate,
    HeadingCandidateSet,
    LLMRouterOutput,
)
from app.core.exceptions import ParserError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # 仅类型标注：运行时按需 import，不把仓库层拉进解析层导入图
    from infrastructure.blocks import BlockStore

#: 触发 L01 两遍处理（正文落盘 + 决策阶段吃有界预览）的规模阈值。
#: 阈值以下走内存路径——方案明确「普通文档保留当前内存模式，避免小文档为了流式付出
#: 过多 I/O」。两个条件取或：块数多（全篇扫描与装配开销）或字符多（正文常驻内存）。
TWO_PASS_MIN_BLOCKS = 4000
TWO_PASS_MIN_CHARS = 2_000_000


# ── Document-level result cache ──────────────────────────────
# Keyed by SHA-256 of the complete Block payload plus pipeline config
# so identical documents skip expensive LLM calls without reusing
# results across different layouts, media, or provider metadata.

def _clone_cache_value(value: Any) -> Any:
    """Return an isolated copy so callers can never mutate cached state."""
    return copy.deepcopy(value)


class _LRUCache:
    """Thread-safe LRU cache with a configurable max size."""

    def __init__(self, max_size: int = 32, max_bytes: int = 64 * 1024 * 1024, ttl: float = 900):
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()
        self._max_bytes = max_bytes
        self._ttl = ttl
        self._weights: dict[str, tuple[int, float]] = {}
        self._bytes = 0

    @staticmethod
    def _weight(value: Any) -> int:
        seen: set[int] = set()
        pending = [value]
        total = 0
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            total += sys.getsizeof(item)
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set, frozenset)):
                pending.extend(item)
            elif hasattr(item, '__dict__'):
                pending.append(vars(item))
        return total

    def _remove(self, key: str) -> None:
        self._store.pop(key, None)
        self._bytes -= self._weights.pop(key, (0, 0))[0]

    def get(self, key: str) -> Optional[Any]:
        """Return a deep copy of the cached value, or ``None``."""
        with self._lock:
            if key not in self._store:
                return None
            if _time.monotonic() >= self._weights[key][1]:
                self._remove(key)
                return None
            self._store.move_to_end(key)
            value = self._store[key]
        return _clone_cache_value(value)

    def put(self, key: str, value: Any) -> None:
        """Insert or refresh a cache entry."""
        weight = self._weight(value)
        if weight > self._max_bytes or self._max_size <= 0 or self._ttl <= 0:
            return
        cloned_value = _clone_cache_value(value)
        with self._lock:
            self._remove(key)
            while self._store and (len(self._store) >= self._max_size or self._bytes + weight > self._max_bytes):
                self._remove(next(iter(self._store)))
            self._store[key] = cloned_value
            self._weights[key] = (weight, _time.monotonic() + self._ttl)
            self._bytes += weight

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._store.clear()
            self._weights.clear()
            self._bytes = 0


_doc_cache = _LRUCache(max_size=32)
_parse_flights: dict[str, Future] = {}
_parse_flights_lock = threading.Lock()


# ── Process-wide request admission, including multiple event loops ──
class _GlobalRequestLimiter:
    """FIFO admission shared by HTTP and background loops without loop-bound futures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = deque()
        self._active: dict[Future, int] = {}

    def _drain(self) -> None:
        while self._pending:
            future, capacity = self._pending[0]
            limit = min([capacity, *self._active.values()])
            if len(self._active) >= limit:
                return
            self._pending.popleft()
            self._active[future] = capacity
            future.set_result(True)

    @contextlib.contextmanager
    def acquire_sync(self, capacity: int, cancel_check=None):
        """Blocking permit for sync callers (selective review / sync route)."""
        if capacity <= 0:
            if cancel_check is not None:
                cancel_check()
            yield
            return
        future = Future()
        with self._lock:
            self._pending.append((future, capacity))
            self._drain()
        try:
            while True:
                if cancel_check is not None:
                    cancel_check()
                try:
                    future.result(timeout=0.1)
                    break
                except TimeoutError:
                    continue
            if cancel_check is not None:
                cancel_check()
            yield
        finally:
            with self._lock:
                if future in self._active:
                    del self._active[future]
                else:
                    self._pending = deque(item for item in self._pending if item[0] is not future)
                    future.cancel()
                self._drain()

    @contextlib.asynccontextmanager
    async def acquire_context(self, capacity: int):
        if capacity <= 0:
            yield
            return
        future = Future()
        with self._lock:
            self._pending.append((future, capacity))
            self._drain()
        try:
            # Shield prevents cancellation of one loop's wrapper from changing
            # the shared concurrent Future before permit accounting completes.
            await asyncio.shield(asyncio.wrap_future(future))
            yield
        finally:
            with self._lock:
                if future in self._active:
                    del self._active[future]
                else:
                    self._pending = deque(item for item in self._pending if item[0] is not future)
                    future.cancel()
                self._drain()

    def reset(self) -> None:
        with self._lock:
            if self._active or self._pending:
                raise RuntimeError("Cannot reset request admission while work is active")


_global_request_limiter = _GlobalRequestLimiter()


def _hash_json_payload(h: Any, value: Any) -> None:
    h.update(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8", errors="replace")
    )


_LARGE_PAYLOAD_THRESHOLD = 256


def _compact_block_payload(payload: dict) -> dict:
    """Replace bulky base64 image data with a digest before hashing.

    Embedded images can reach tens of MB per document; serialising them
    through ``json.dumps`` just to feed a cache key wastes time and
    memory. A SHA-256 digest preserves cache-key sensitivity (any pixel
    change still changes the key) at a fraction of the cost.
    """
    img = payload.get("image_data")
    if isinstance(img, str) and len(img) > _LARGE_PAYLOAD_THRESHOLD:
        digest = hashlib.sha256(img.encode("utf-8", errors="replace")).hexdigest()
        payload["image_data"] = f"sha256:{digest}:len={len(img)}"
    return payload


def _compute_blocks_hash(
    blocks: List[Block],
    *,
    compressor_config: "CompressorConfig | None" = None,
    resolver_config: "ResolverConfig | None" = None,
    parser_config: "ParserConfig | None" = None,
    llm_model: str = "",
) -> str:
    """Derive a deterministic cache key from block contents and pipeline config.

    Includes complete block payloads (text, media, tables, captions,
    provider metadata and physical features), pipeline configuration,
    and LLM model name.
    """
    h = hashlib.sha256()
    for b in blocks:
        _hash_json_payload(h, _compact_block_payload(b.model_dump(mode="json")))
    if compressor_config is not None:
        _hash_json_payload(h, {"compressor_config": compressor_config.model_dump(mode="json")})
    if resolver_config is not None:
        _hash_json_payload(h, {"resolver_config": resolver_config.model_dump(mode="json")})
    if parser_config is not None:
        _hash_json_payload(h, {"parser_config": parser_config.model_dump(mode="json")})
    if llm_model:
        _hash_json_payload(h, {"model": llm_model})
    return h.hexdigest()


@dataclass
class ParseTimings:
    """Per-stage timing data for a single parse run."""
    stage2_compress: float = 0.0
    stage3_route: float = 0.0
    stage4_resolve: float = 0.0
    total: float = 0.0
    skeleton_chars: int = 0
    window_count: int = 0
    heading_count: int = 0
    candidate_count: int = 0
    #: 12.2 生产者：Stage 3 路由层的原始锚点 ``(block_id, title, level)``。
    #: 此前实验产物只有**最终层** P/R/F1，于是「漏检发生在候选层还是路由层」永远看不出
    #: 来——而这两种漏检的修法完全不同（补候选信号 vs 改提示词/窗口）。锚点在这里出一份
    #: 侧通道，评测侧就能用同一个 compute_section_f1 口径把三层放在一起比。
    #: 空列表表示本次没有走路由（缓存命中 / 零特征兜底），评测侧据此标未测量而不是报 0。
    router_anchors: list[tuple[int, str, int]] = field(default_factory=list)


class CaliperParser:
    """Constellation main parser — orchestrates the four-stage pipeline.

    Accepts optional :class:`CompressorConfig` and :class:`ResolverConfig`
    so that the pipeline can be driven without a global ``settings``
    singleton (useful for testing and library-mode usage).
    """

    def __init__(
        self,
        compressor_config: CompressorConfig | None = None,
        resolver_config: ResolverConfig | None = None,
        parser_config: ParserConfig | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
        llm_config: LLMClientConfig | None = None,
        review_policy: "ReviewPolicy | None" = None,
    ):
        self._compressor_config = compressor_config or CompressorConfig()
        self._resolver_config = resolver_config or ResolverConfig()
        self._parser_config = parser_config or ParserConfig()
        # A01 选择性复审的选区与预算参数。默认值保守——宁可多复审一个区域，
        # 也不静默放过一片不确定的正文。
        self._review_policy = review_policy or ReviewPolicy()
        # 可选进度回调：以 (event_type, payload) 上报四阶段进度（cache_hit / stage_started / stage_completed）。
        # 默认 None 时零行为改变；回调内异常被吞掉，绝不影响解析主流程。
        self._progress_callback = progress_callback
        # 可选 BYOK：用户自带 key/model/base_url（None 时用 settings 默认池化客户端）。
        self._llm_config = llm_config
        self.compressor = SkeletonCompressor(config=self._compressor_config)
        self.router = LLMRouter(
            downgrade_out_of_candidate=self._parser_config.enable_anchor_downgrade,
            llm_config=llm_config,
        )

    def _emit_progress(self, event_type: str, payload: dict[str, Any]) -> None:
        """上报一次进度事件；回调失败绝不能影响解析主流程。"""
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(event_type, payload)
        except Exception:
            return

    def _candidate_views(
        self,
        blocks: List[Block],
    ) -> tuple[HeadingCandidateSet, List[HeadingCandidate]]:
        """Return uncapped inference proposals and the existing route view.

        The second view deliberately continues through
        ``generate_heading_candidates`` so its candidate-table/input-budget
        cap remains untouched.  Only the first view reaches global inference.
        """
        if not self._parser_config.enable_heading_candidates:
            return HeadingCandidateSet(), []
        candidate_set = generate_heading_candidate_set(blocks)
        return candidate_set, select_route_candidates(candidate_set, blocks)

    def _compress_candidate_aware(
        self,
        blocks: List[Block],
        candidate_set: HeadingCandidateSet,
        route_candidates: List[HeadingCandidate],
    ) -> List[str]:
        """Invoke sparse compression while tolerating minimal test doubles."""
        if not self._parser_config.enable_heading_candidates:
            return self.compressor.compress(blocks)
        try:
            return self.compressor.compress(
                blocks,
                candidates=route_candidates,
                region_risks=candidate_set.region_risks,
            )
        except TypeError as exc:
            # Existing embedders/tests may replace ``compress`` with a unary
            # callable.  Production SkeletonCompressor always accepts the
            # candidate-aware contract; only that narrow adapter falls back.
            if "unexpected keyword" not in str(exc) and "positional" not in str(exc):
                raise
            return self.compressor.compress(blocks)

    def _prepare_skeleton(self, blocks):
        candidate_set, candidates = self._candidate_views(blocks)
        chunks = self._compress_candidate_aware(blocks, candidate_set, candidates)
        chunks = self.router.fit_skeleton_chunks(
            chunks, candidates=candidates if self._parser_config.enable_heading_candidates else None,
        )
        return candidate_set, candidates, chunks

    # ── Public API ─────────────────────────────────────────────

    @analysis_scope
    def parse_fast(self, blocks: List[Block]) -> DocumentTree:
        """Explicit no-model mode: deterministic proposals, global decode, full closure."""
        if not blocks:
            raise ParserError("Block list is empty, cannot parse.")
        key = self.cache_key_for(blocks) + ":deterministic-v1"
        cached = _doc_cache.get(key)
        if cached is not None:
            return cached
        # L01：先按规模决定是否落盘。落盘后决策阶段看到的是有界预览，正文在 Stage 4
        # 按最终区间回读；缓存键仍用**原始** blocks 算，落盘与否不改变结果身份。
        blocks, store = self._spill_for_two_pass(blocks)
        if store is not None:
            set_block_store(store)
        try:
            candidates, _route_view = self._candidate_views(blocks)
            tree = self._resolve_and_build_tree(
                blocks, LLMRouterOutput(doc_title="", doc_authors="", chapters=[]), candidates, key,
            )
        finally:
            if store is not None:
                store.dispose()
        _doc_cache.put(key, tree)
        return tree

    @staticmethod
    def clear_cache() -> None:
        """Clear the document-level result cache."""
        _doc_cache.clear()

    # ── A01 · 选择性复审 ───────────────────────────────────────────

    def parse_selective(self, blocks: List[Block]) -> DocumentTree:
        """A01：确定性初判 + 只对不确定区域调模型 + 只闭合一次。"""
        return self.parse_selective_with_report(blocks)[0]

    @analysis_scope
    def parse_selective_with_report(
        self, blocks: List[Block], cancel_check=None,
    ) -> tuple[DocumentTree, ReviewReport]:
        """同 :meth:`parse_selective`，另回一份可观测记录。

        五步与方案 A01 逐条对应：

        1. 无投票初判（走**同一个** :class:`GlobalHeadingInference`，不是第二份实现）。
        2. 按六类信号选出复审区域（见 :mod:`modules.parser.review_regions`）。
        3. 按不确定度排序、受 ``max_regions`` 与文档级 token 预算准入。
        4. 每个区域一次请求，新增投票作为证据喂回同一个解码器；**只闭合一次**。
        5. 模型不可用 / 预算不足时输出确定性结果，未解决的高风险区域标 needs_confirmation。

        关键约束：复审的产出是**投票**，绝不直接改正文或直接插标题。旧式「闭合完了再到处
        插标题」的多点修改是方案点名禁止的——最终决策只有 Stage 4 这一个出口。

        ``ReviewReport`` 只报可数事实（调用数 / 估算 prompt 字符 / 三个不确定度），
        **不报质量提升**：没有真实模型语料就不该宣称质量变好（方案验收原文）。
        """
        if not blocks:
            raise ParserError("Block list is empty, cannot parse.")
        key = (
            self.cache_key_for(blocks)
            + ":selective-v2:"
            + json.dumps(self._review_policy.__dict__, sort_keys=True, default=str)
        )
        cached = _doc_cache.get(key)
        if cached is not None:
            tree, historic = cached
            historic.this_run_model_calls = 0
            historic.cached_model_calls = historic.model_calls
            return tree, historic

        blocks, store = self._spill_for_two_pass(blocks)
        if store is not None:
            set_block_store(store)
        try:
            candidate_set, _route_view = self._candidate_views(blocks)
            resolver = IntervalResolver(
                blocks,
                config=self._resolver_config,
                parser_config=self._parser_config,
                heading_candidates=candidate_set,
            )
            _initial, plan = resolver.initial_review_plan(self._review_policy)
            report = ReviewReport(
                total_sites=plan.total_sites,
                uncertain_sites=plan.uncertain_sites,
                regions_planned=len(plan.regions),
                tree_uncertainty=plan.tree_uncertainty,
                signals=count_signals(plan.sites),
            )
            max_block_id = max((block.id for block in blocks), default=-1)
            votes: List[ChapterNode] = []
            doc_title = ""
            reviewed: set[int] = set()
            for region in plan.admitted_regions:
                if cancel_check is not None:
                    cancel_check()
                chunk, region_candidates = self._region_request(blocks, candidate_set, region)
                if not chunk:
                    continue
                try:
                    calls_before = getattr(self.router, "_actual_model_calls", 0)
                    output = self._route_single_chunk(
                        chunk, candidates=region_candidates, max_block_id=max_block_id,
                        cancel_check=cancel_check,
                    )
                except Exception as exc:  # noqa: BLE001 - 模型不可用是 A01 明确要求覆盖的一路
                    if cancel_check is not None:
                        cancel_check()
                    logger.warning(
                        "[Parser] A01 区域 %d 复审失败，改用确定性结果：%s",
                        region.region_index, exc,
                    )
                    report.model_available = False
                    break
                finally:
                    report.this_run_model_calls += getattr(self.router, "_actual_model_calls", 0) - calls_before
                report.model_calls += 1
                report.prompt_chars += len(chunk)
                reviewed.add(region.region_index)
                votes.extend(output.chapters)
                resolver.record_routed_blocks(output._observed_block_ids)
                doc_title = doc_title or (output.doc_title or "")
            report.regions_reviewed = len(reviewed)
            unresolved = [
                region for region in plan.regions if region.region_index not in reviewed
            ]
            report.regions_deferred = len(unresolved)
            report.needs_confirmation_block_ids = sorted({
                block_id for region in unresolved for block_id in region.site_block_ids
            })
            logger.info(
                "[Parser] A01 selective: %d/%d 站点不确定 → %d 区域，复审 %d 次，"
                "延后 %d 区域，树结构不确定度 %.3f",
                plan.uncertain_sites, plan.total_sites, len(plan.regions),
                report.model_calls, report.regions_deferred, plan.tree_uncertainty,
            )
            tree = self._resolve_and_build_tree(
                blocks,
                LLMRouterOutput(doc_title=doc_title, doc_authors="", chapters=votes),
                candidate_set,
                key,
                resolver=resolver,
            )
        finally:
            if store is not None:
                store.dispose()
        # 中途失败的结果不能进文档缓存，否则显式重试会直接返回半成品，
        # 已成功区域应靠窗口检查点跳过，只重调未完成的那些。
        if report.model_available:
            _doc_cache.put(key, (tree, report))
        return tree, report

    def _region_request(
        self,
        blocks: List[Block],
        candidate_set: HeadingCandidateSet,
        region: "ReviewRegion",
    ) -> tuple[str, List[HeadingCandidate]]:
        """把一个复审区域组装成「局部骨架 + 局部候选」。

        区域块 = 不确定站点 + 两侧局部内容 + **相邻可靠锚点**。带上锚点是必须的：
        只给一段孤立正文，模型无从判断它在文档结构里的深度，级别只能靠猜。

        骨架仍走既有 :class:`SkeletonCompressor`——区域请求不另写一套压缩，否则区域视图
        与全量视图对同一段正文会给出不同表示。
        """
        wanted = set(region.block_ids) | set(region.anchor_block_ids)
        subset = [block for block in blocks if block.id in wanted]
        if not subset:
            return "", []
        region_candidates = [
            candidate for candidate in candidate_set.candidates
            if candidate.block_id in wanted
        ]
        chunks = self._compress_candidate_aware(subset, candidate_set, region_candidates)
        return "\n".join(chunks), region_candidates

    # ── L01 两遍处理 ────────────────────────────────────────────────

    def _spill_for_two_pass(
        self, blocks: List[Block]
    ) -> tuple[List[Block], Optional["BlockStore"]]:
        """规模够大时把正文落盘，返回 ``(有界预览块, 仓库)``；否则原样返回。

        方案 L01 的两个约束在这里同时成立：

        * **小文档不落盘。** 为了"流式"让小文档多付一趟磁盘 I/O 是净亏，所以阈值以下
          直接走内存路径（``store`` 为 ``None``），行为与从前逐字一致。
        * **不是把 List[Block] 换成 generator。** 页眉页脚统计、全局正文字号、目录、
          跨页表格都必须跨页看，单向流会让这些统计拿不到数据。落盘给的是**随机读**：
          决策阶段照旧看到全篇（只是正文被替换成有界预览），正文在最终区间定下来之后
          按 id 回读。

        预览块把真实长度记进 ``metadata['char_count']`` 并打 ``text_is_preview``：
        长度不因截断而失真，且任何试图用预览拼装正文的路径都会在 resolver 里被
        显式拒绝，不可能静默交付残缺正文。
        """
        if not blocks:
            return blocks, None
        total_chars = sum(len(b.text or "") for b in blocks)
        if len(blocks) < TWO_PASS_MIN_BLOCKS and total_chars < TWO_PASS_MIN_CHARS:
            return blocks, None

        from infrastructure.blocks import BlockStore, OwnedBlocks, compact_block, feature_of

        store = BlockStore()
        try:
            store.write(blocks)
            lean = [compact_block(block, feature_of(block)) for block in blocks]
        except BaseException:
            store.dispose()
            raise
        if isinstance(blocks, OwnedBlocks):
            blocks[:] = lean
            lean = blocks
        logger.info(
            "[Parser] L01 two-pass: %d blocks, %d chars spilled to %s; "
            "resident preview chars %d",
            len(blocks), total_chars, store.path, sum(len(b.text or "") for b in lean),
        )
        return lean, store

    def _llm_cache_identity(self) -> str:
        """Include request-shaping LLM settings in the document cache key."""
        client = self.router._client
        from modules.parser.stage_graph import execution_identity
        identity = {
            "pipeline": "structure-v2",
            "execution": execution_identity(),
            "config": getattr(client, "_cfg", {}),
            "settings": {name: getattr(client, name, None) for name in (
                "model", "temperature", "max_tokens", "max_input_tokens",
                "input_token_safety_margin", "base_url",
            )},
            "system_prompt": getattr(self.router, "_system_prompt", ""),
            "user_prompt": getattr(self.router, "_user_template", ""),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()

    def cache_key_for(self, blocks: List[Block]) -> str:
        """Compute the cache key for *blocks* using this parser's config."""
        llm_model = self._llm_cache_identity()
        if self._parser_config.confidence_calibration_path:
            from pathlib import Path
            artifact_hash = hashlib.sha256(Path(self._parser_config.confidence_calibration_path).read_bytes()).hexdigest()
            llm_model += ":calibration:" + artifact_hash
        return _compute_blocks_hash(
            blocks,
            compressor_config=self._compressor_config,
            resolver_config=self._resolver_config,
            parser_config=self._parser_config,
            llm_model=llm_model,
        )

    @analysis_scope
    def parse(self, blocks: List[Block]) -> DocumentTree:
        """Parse a Block list into a DocumentTree (sync entry point).

        Four-stage pipeline:
            1. Skeleton compression
            2. LLM routing -> section anchors
            3. Forced closure -> interval slicing
            4. Lossless assembly -> DocumentTree

        R03：本入口与 :meth:`async_parse` 共用**同一张**在途表 ``_parse_flights``。
        此前只有异步路径做合并，同步路径两个线程拿同一份 blocks 会各自 miss 缓存、
        各自真调一次模型——两倍费用、两倍延迟，而且两份结果可能不同（非零温度）。
        共用一张表还让跨模型的等待成立：同步 follower 可以等一个异步 leader，反之亦然，
        因为表里放的是 ``concurrent.futures.Future``（既能 ``result()`` 阻塞等，
        也能被 ``asyncio.wrap_future`` await）。

        Args:
            blocks: Block list provided by a DocxProvider or similar.

        Returns:
            A :class:`DocumentTree` representing the parsed document.

        Raises:
            ParserError: If the block list is empty or parsing fails.
        """
        if not blocks:
            raise ParserError("Block list is empty, cannot parse.")

        cache_key = self.cache_key_for(blocks)
        cached = _doc_cache.get(cache_key)
        if cached is not None:
            logger.info("[Parser] Cache hit, skipping LLM call (hash=%s)", cache_key[:12])
            return cached

        with _parse_flights_lock:
            flight = _parse_flights.get(cache_key)
            leader = flight is None
            if leader:
                flight = Future()
                _parse_flights[cache_key] = flight
        if not leader:
            # 已有同键解析在跑：等它，不再发第二次模型请求。异常也照原样传播给
            # 每个等待者（leader 的 except 分支会 set_exception）。
            tree = flight.result()
            logger.info(
                "[Parser] Joined in-flight parse, no extra LLM call (hash=%s)", cache_key[:12]
            )
            self._emit_progress("cache_hit", {"message": "复用正在执行的同文档解析结果。"})
            return _clone_cache_value(tree)

        try:
            tree = self._parse_impl(blocks, cache_key)
            flight.set_result(_clone_cache_value(tree))
            return tree
        except BaseException as exc:
            flight.set_exception(exc)
            # leader 自己也会抛，这里把共享 future 的异常标记为已观测，避免无 follower
            # 时在 GC 阶段冒出误导性的 "Future exception was never retrieved"。
            flight.exception()
            raise
        finally:
            with _parse_flights_lock:
                _parse_flights.pop(cache_key, None)

    def _parse_impl(self, blocks: List[Block], cache_key: str) -> DocumentTree:
        """同步四阶段流水线本体（缓存与在途合并由 :meth:`parse` 负责）。"""
        # L01：按规模决定是否落盘。缓存键已用**原始** blocks 算过，落盘与否不改变
        # 结果身份；决策阶段之后看到的是有界预览，正文在 Stage 4 按最终区间回读。
        blocks, _block_store = self._spill_for_two_pass(blocks)
        if _block_store is not None:
            set_block_store(_block_store)

        try:
            logger.info("[Parser] Starting parse (%d blocks)", len(blocks))

            # Stage 2: evidence-first candidate generation drives sparse compression.
            logger.info("[Parser] Stage 2/4: candidate-aware sparse skeleton")
            candidate_set, heading_candidates = self._candidate_views(blocks)
            skeleton_chunks = self._compress_candidate_aware(
                blocks, candidate_set, heading_candidates,
            )
            skeleton_chunks = self.router.fit_skeleton_chunks(
                skeleton_chunks,
                candidates=(
                    heading_candidates
                    if self._parser_config.enable_heading_candidates else None
                ),
            )
            total_chars = sum(len(c) for c in skeleton_chunks)
            logger.info(
                "[Parser] Skeleton ready (%d chars, %d chunks, %d candidates)",
                total_chars, len(skeleton_chunks), len(heading_candidates),
            )

            # Stage 3: AI cursor routing (Map-Reduce)
            logger.info("[Parser] Stage 3/4: AI cursor routing")
            llm_output = self._map_reduce_route(
                skeleton_chunks,
                blocks=blocks,
                candidates=(
                    heading_candidates
                    if self._parser_config.enable_heading_candidates else None
                ),
            )
            logger.info(
                "[Parser] LLM routing done: title='%s', chapters=%d",
                llm_output.doc_title, len(llm_output.chapters),
            )

            # Stage 4: cursor closure & assembly
            logger.info("[Parser] Stage 4/4: cursor closure & assembly")
            resolver = IntervalResolver(
                blocks,
                config=self._resolver_config,
                parser_config=self._parser_config,
                heading_candidates=candidate_set,
            )

            resolver.record_routed_blocks(llm_output._observed_block_ids)
            document_nodes = resolver.resolve(llm_output.chapters)

            # Preamble must be sliced at the *resolved* first anchor:
            # resolve() sorts, fuzzy-corrects and dedups anchors, so
            # slicing at the raw LLM claim could lose blocks (gap) or
            # duplicate them (overlap) at the preamble boundary.
            preamble_content = self._extract_preamble(
                resolver, document_nodes, had_anchors=bool(llm_output.chapters),
            )

            tree = DocumentTree(
                nodes=document_nodes,
                doc_title=self._resolve_doc_title(llm_output.doc_title, blocks, document_nodes),
                doc_authors=llm_output.doc_authors,
                preamble_content=preamble_content,
                lossless_fallback=resolver.lossless_fallback,
                source_identity=cache_key,
            )

            _doc_cache.put(cache_key, tree)

            stats = tree.get_stats()
            logger.info(
                "[Parser] Parse complete\n"
                "  title: %s\n  authors: %s\n"
                "  top-level sections: %d\n  total sections: %d\n"
                "  content: %d chars\n  max depth: %d",
                stats["doc_title"], stats["doc_authors"],
                stats["top_level_sections"], stats["total_sections"],
                stats["total_content_chars"], stats["max_depth"],
            )

            return tree

        except ParserError:
            raise
        except Exception as e:
            raise ParserError(f"Document parsing failed: {e}") from e
        finally:
            # 落盘产物的生命周期跟着这次解析：树里的正文已经装配完成，仓库不再需要。
            if _block_store is not None:
                _block_store.dispose()

    @analysis_scope
    def parse_with_timing(self, blocks: List[Block]) -> tuple[DocumentTree, ParseTimings]:
        """Parse with per-stage timing instrumentation.

        Returns:
            ``(tree, timings)`` where ``timings`` is a
            :class:`ParseTimings` dataclass with per-stage latency
            in seconds.
        """
        timings = ParseTimings()
        total_start = _time.perf_counter()

        if not blocks:
            raise ParserError("Block list is empty, cannot parse.")

        llm_model = self._llm_cache_identity()
        cache_key = _compute_blocks_hash(
            blocks,
            compressor_config=self._compressor_config,
            resolver_config=self._resolver_config,
            parser_config=self._parser_config,
            llm_model=llm_model,
        )
        cached = _doc_cache.get(cache_key)
        if cached is not None:
            timings.total = _time.perf_counter() - total_start
            return cached, timings

        # Stage 2: candidates are evidence, so they must precede compression.
        t0 = _time.perf_counter()
        candidate_set, heading_candidates = self._candidate_views(blocks)
        skeleton_chunks = self._compress_candidate_aware(
            blocks, candidate_set, heading_candidates,
        )
        skeleton_chunks = self.router.fit_skeleton_chunks(
            skeleton_chunks,
            candidates=(
                heading_candidates
                if self._parser_config.enable_heading_candidates else None
            ),
        )
        timings.stage2_compress = _time.perf_counter() - t0
        timings.skeleton_chars = sum(len(c) for c in skeleton_chunks)
        timings.window_count = len(skeleton_chunks)
        timings.candidate_count = len(heading_candidates)

        # Stage 3
        t0 = _time.perf_counter()
        llm_output = self._map_reduce_route(
            skeleton_chunks,
            blocks=blocks,
            candidates=(
                heading_candidates
                if self._parser_config.enable_heading_candidates else None
            ),
        )
        timings.stage3_route = _time.perf_counter() - t0
        timings.heading_count = len(llm_output.chapters)
        timings.router_anchors = [
            (
                int(getattr(chapter, "block_id", -1) or -1),
                str(getattr(chapter, "title", "") or ""),
                int(getattr(chapter, "level", 1) or 1),
            )
            for chapter in llm_output.chapters
        ]

        # Stage 4
        t0 = _time.perf_counter()
        resolver = IntervalResolver(
            blocks,
            config=self._resolver_config,
            parser_config=self._parser_config,
            heading_candidates=candidate_set,
        )
        resolver.record_routed_blocks(llm_output._observed_block_ids)
        document_nodes = resolver.resolve(llm_output.chapters)
        preamble_content = self._extract_preamble(
            resolver, document_nodes, had_anchors=bool(llm_output.chapters),
        )
        tree = DocumentTree(
            nodes=document_nodes,
            doc_title=self._resolve_doc_title(llm_output.doc_title, blocks, document_nodes),
            doc_authors=llm_output.doc_authors,
            preamble_content=preamble_content,
            lossless_fallback=resolver.lossless_fallback,
            source_identity=cache_key,
        )
        timings.stage4_resolve = _time.perf_counter() - t0

        _doc_cache.put(cache_key, tree)
        timings.total = _time.perf_counter() - total_start
        return tree, timings

    # Word 的「文档大标题」样式名：英文 Title，以及中文模板里无级别数字的「标题」
    # （区别于「标题 1」这种章节标题样式）。只认 "title" 会漏掉中文模板。
    _TITLE_STYLE_NAMES = frozenset({"title", "标题", "標題", "doc-title", "documenttitle"})

    @staticmethod
    def _strip_title_emphasis(text: str) -> str:
        """去掉标题文本里的行内 Markdown 强调记号（实现见 ``modules.parser.titles``）。"""
        return strip_title_emphasis(text)

    @classmethod
    def _resolve_doc_title(
        cls,
        declared: str,
        blocks: List[Block] | None,
        nodes: List[DocumentNode] | None = None,
    ) -> str:
        """确定文档标题：LLM 给了就用它的；没给时按确定性信号逐级兜底。

        优先级：LLM > Word「Title」样式段 > 第一个一级标题块 > 已解析出的首个章节标题。

        最后那一级是必要的：只有前三级时，一份「没用 Word 标题样式、但有可识别编号标题」
        的文档拿不到 doc_title，调用方只能退回文件名——而上传名常是产物 id / 内容哈希，
        用户看到的就成了一串无意义字符。文档里明明有标题，就不该去用文件名。

        伪根节点的标题是解析器处理零特征文档的内部哨兵，不是文档的名字，不进这条链。
        """
        title = (declared or "").strip()
        if title:
            return strip_title_emphasis(title)
        for block in blocks or []:
            style = str((block.metadata or {}).get("style", "")).strip().lower()
            if style in cls._TITLE_STYLE_NAMES and block.text and block.text.strip():
                return strip_title_emphasis(block.text)
        for block in blocks or []:
            if block.is_heading_style and block.heading_level == 1 and block.text and block.text.strip():
                return strip_title_emphasis(block.text)
        first = next(iter(nodes or []), None)
        if first is not None:
            candidate = strip_title_emphasis(first.title)
            if candidate and candidate != PSEUDO_ROOT_TITLE:
                return candidate
        return ""

    # ── Map-Reduce LLM routing ─────────────────────────────────

    def _map_reduce_route(
        self,
        skeleton_chunks: List[str],
        blocks: List[Block] | None = None,
        candidates: List[HeadingCandidate] | None = None,
    ) -> LLMRouterOutput:
        """Route skeleton chunks through the LLM using Map-Reduce.

        For single-chunk documents, falls back to the original
        ``router.route()`` path.  For multi-chunk documents, each
        chunk is sent as an independent LLM request via
        ``router.route_chunk()``, and the results are merged into a
        single :class:`LLMRouterOutput`.
        """
        total = len(skeleton_chunks)

        # A single chunk is still one observation window.  Route its raw
        # votes through the same physical aligner used by Map-Reduce so no
        # parser-side validation can run before alignment.
        if total == 1:
            logger.info("[Map-Reduce] Single-chunk mode, direct route")
            max_block_id = max((b.id for b in blocks), default=-1) if blocks else -1
            output = self._route_single_chunk(
                skeleton_chunks[0],
                candidates=candidates,
                max_block_id=max_block_id,
            )
            return self._merge_route_results(
                {0: output}, 1, blocks=blocks, candidates=candidates,
            )

        logger.info(
            "[Map-Reduce] Serial mode with state projection: %d windows", total,
        )

        results: dict[int, LLMRouterOutput] = {}
        previous_tail_context = ""
        max_block_id = max((b.id for b in blocks), default=-1) if blocks else -1

        for idx, chunk in enumerate(skeleton_chunks):
            start_id, end_id = self._chunk_id_range(chunk, max_block_id)
            window_candidates = (
                candidates_in_range(candidates or [], start_id, end_id)
                if candidates is not None else None
            )
            output = self._route_window_chunk(
                chunk,
                idx,
                total,
                previous_tail_context,
                candidates=window_candidates,
                max_block_id=max_block_id,
            )
            results[idx] = output

            logger.info(
                "[Map-Reduce] Window %d/%d done: %d anchors",
                idx + 1, total, len(output.chapters),
            )

            # Build tail context from last 3 anchors for next window
            if output.chapters:
                tail = output.chapters[-3:]
                previous_tail_context = "\n".join(
                    f"- Level {ch.level}: {ch.title}" for ch in tail
                )

        # Reduce is physical alignment, not local winner selection.  Preserve
        # every window as an independent vote sequence so overlap evidence and
        # its source window survive into the global decoder.
        return self._merge_route_results(
            results, total, blocks=blocks, candidates=candidates,
        )

    def _deduplicate_overlap_anchors(self, chapters: list) -> list:
        """Remove duplicate anchors produced by overlapping windows.

        For each candidate anchor, scans backwards through recent
        results within ``id_diff_threshold`` blocks.  If a title
        match exceeds ``sim_threshold``, the candidate is a duplicate.

        Uses a sliding window pointer to avoid rescanning the full
        result list for each candidate.
        """
        if not chapters:
            return chapters

        cfg = self._resolver_config if self._resolver_config else ResolverConfig()
        id_diff_threshold = cfg.dedup_id_diff
        sim_threshold = cfg.dedup_sim_threshold

        result = [chapters[0]]
        # window_start tracks the oldest result that could still be
        # within id_diff_threshold of the current candidate.
        window_start = 0

        for ch in chapters[1:]:
            ch_id = ch.start_block_id
            ch_title_lower = ch.title.strip().lower()
            is_dup = False

            # Advance window_start past results that are too far away
            while window_start < len(result):
                if ch_id - result[window_start].start_block_id <= id_diff_threshold:
                    break
                window_start += 1

            # Scan the relevant window (only results within threshold)
            for i in range(len(result) - 1, window_start - 1, -1):
                prev = result[i]
                id_diff = abs(ch_id - prev.start_block_id)
                if id_diff > id_diff_threshold:
                    break

                sim = _levenshtein_ratio(
                    ch_title_lower, prev.title.strip().lower(),
                )
                if sim >= sim_threshold:
                    is_dup = True
                    break

            if not is_dup:
                result.append(ch)

        return result

    @staticmethod
    def _extract_preamble(
        resolver: IntervalResolver,
        document_nodes: list,
        *,
        had_anchors: bool,
    ) -> str:
        """Render the preamble using the *resolved* first anchor position.

        Anchors are sorted, fuzzy-corrected, deduplicated and possibly
        dropped inside ``resolver.resolve``; the raw LLM anchor list is
        therefore not a safe boundary source.  Slicing the preamble at
        ``llm_output.chapters[0]`` (as earlier versions did) loses the
        blocks between the claimed and the corrected first anchor when
        fuzzy correction moves it forward, and duplicates them when it
        moves backward — violating the partition guarantee.

        ``had_anchors`` is retained for call compatibility, but global
        inference can select an inverse proposal even when the LLM emitted no
        anchors.  The resolver's explicit pseudo-root state is therefore the
        only safe way to distinguish a real inferred first heading from the
        lossless fallback root.
        """
        del had_anchors
        if not document_nodes or resolver._pseudo_root:
            return ""
        first_start_id = min(node.start_block_id for node in document_nodes)
        preamble_blocks = resolver.get_preamble_blocks(first_start_id)
        if not preamble_blocks:
            return ""
        logger.info("[Parser] Preamble: %d blocks", len(preamble_blocks))
        return "\n\n".join(
            b.to_markdown() for b in preamble_blocks if b.to_markdown()
        )

    # ── Bounded, reclaiming window fan-out (R02) ───────────────

    @contextlib.asynccontextmanager
    async def _window_request_slot(self) -> AsyncIterator[None]:
        """Hold one per-document AND one global slot for a single window request.

        Composition is the whole point: a window may only start once it owns a
        slot in *both* ceilings.  Per-document caps a single big document's
        fan-out; the global cap sums across every document on the loop.  A giant
        document can therefore occupy at most its per-document quota of global
        slots, leaving the rest for other documents' windows — that is the
        anti-starvation guarantee.  Order (per-document first, then global) is
        fixed across all callers so the two semaphores can never deadlock.
        """
        cfg = self._parser_config
        per_doc = self._per_document_limiter(cfg.max_inflight_requests_per_document)
        async with per_doc:
            async with _global_request_limiter.acquire_context(
                cfg.max_inflight_requests_global,
            ):
                yield

    def _per_document_limiter(self, capacity: int):
        """Lazily build this parse's per-document semaphore on the live loop.

        Bound lazily (not in ``__init__``) because a ``CaliperParser`` is
        constructed outside any running loop and may be reused across parses;
        binding here keeps the semaphore attached to the loop that actually runs
        the fan-out.  ``capacity <= 0`` disables this layer (global still holds).
        """
        if capacity <= 0:
            return contextlib.nullcontext()
        loop = asyncio.get_running_loop()
        existing = getattr(self, "_per_doc_sem", None)
        if (
            existing is None
            or existing[0] is not loop
            or existing[1] != capacity
        ):
            existing = (loop, capacity, asyncio.Semaphore(capacity))
            self._per_doc_sem = existing
        return existing[2]

    async def _gather_windows_bounded(
        self,
        factories: "List[Callable[[], Any]]",
    ) -> List[Any]:
        """Run window-request coroutine factories under both concurrency ceilings.

        Failure policy — **CANCEL THE WHOLE GROUP** (chosen over "keep
        successful windows"): R02's concrete harm is that ``asyncio.gather``
        surfaces the first exception to the caller while the *other* windows
        keep burning tokens in the background with no owner.  The routing result
        is one merged structure over all windows anyway, so a partial set is not
        a shippable document — there is no successful-window value worth
        preserving here.  Cancelling immediately is also what makes reclamation
        *observable*: a cancelled request stops issuing/consuming tokens at once,
        which the tests assert via a live-task count and a call counter.

        Reclamation contract: when any window raises or times out, every sibling
        task is cancelled AND awaited before this coroutine returns/re-raises, so
        no unmanaged background request survives this call.  ``factories`` (not
        ready-made coroutines) let us cancel not-yet-started work without leaving
        un-awaited coroutine objects.  Python 3.10-safe: explicit ``cancel()`` +
        ``await``, no :class:`asyncio.TaskGroup`.
        """
        timeout = self._parser_config.window_request_timeout or None

        async def _run(factory: "Callable[[], Any]") -> Any:
            async with self._window_request_slot():
                coro = factory()
                if timeout is not None:
                    return await asyncio.wait_for(coro, timeout=timeout)
                return await coro

        tasks = [asyncio.ensure_future(_run(factory)) for factory in factories]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            # First failure (exception, timeout, or outer cancellation): tear the
            # whole group down.  Cancel then await EVERY task — including the one
            # that already failed — so there is provably no request still running
            # in the background once we re-raise.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    # ── Async pipeline ─────────────────────────────────────────

    @staticmethod
    def _chunk_id_range(chunk: str, max_block_id: int) -> tuple[int, int]:
        """Infer the inclusive block-id range represented by a skeleton chunk."""
        import re

        budget_range = re.search(
            r"<!--\s*Constellation budget range:\s*(\d+)\.\.(\d+)\s*-->",
            chunk,
        )
        if budget_range:
            start = int(budget_range.group(1))
            end = int(budget_range.group(2))
            return min(start, end), max(start, end)

        ids: list[int] = []
        for match in re.finditer(
            r"(?m)^[ \t]*\[(\d+)(?:\s+to\s+(\d+))?\]",
            chunk,
        ):
            ids.append(int(match.group(1)))
            if match.group(2):
                ids.append(int(match.group(2)))
        if not ids:
            # Budget splitting can produce a continuation shard without a
            # block marker.  An empty range correctly attaches no candidates;
            # the previous 0..max fallback attached the entire document table
            # and could recreate the very overflow being prevented.
            return -1, -1
        return min(ids), max(ids)

    def _route_single_chunk(
        self,
        chunk: str,
        *,
        candidates: List[HeadingCandidate] | None,
        max_block_id: int,
        cancel_check=None,
    ) -> LLMRouterOutput:
        """Route one complete skeleton, tolerating old test doubles.

        Sync callers share the same global in-flight ceiling as the async path.
        """
        capacity = self._parser_config.max_inflight_requests_global
        with _global_request_limiter.acquire_sync(capacity, cancel_check=cancel_check):
            try:
                return self.router.route(
                    chunk,
                    candidates=candidates,
                    max_block_id=max_block_id,
                )
            except TypeError:
                # Backward compatibility for tests that monkeypatch a minimal router.
                return self.router.route(chunk)

    def _route_window_chunk(
        self, *args, **kwargs,
    ) -> LLMRouterOutput:
        with _global_request_limiter.acquire_sync(self._parser_config.max_inflight_requests_global):
            return self._route_window_chunk_unlimited(*args, **kwargs)

    def _route_window_chunk_unlimited(
        self,
        chunk: str,
        idx: int,
        total: int,
        previous_tail_context: str,
        *,
        candidates: List[HeadingCandidate] | None,
        max_block_id: int,
    ) -> LLMRouterOutput:
        """Route one map-reduce window, tolerating old test doubles."""
        try:
            return self.router.route_chunk(
                chunk,
                idx,
                total,
                previous_tail_context,
                candidates=candidates,
                max_block_id=max_block_id,
            )
        except TypeError:
            return self.router.route_chunk(
                chunk,
                idx,
                total,
                previous_tail_context,
            )

    @analysis_scope
    async def async_parse(self, blocks: List[Block]) -> DocumentTree:
        if not blocks:
            raise ParserError("Block list is empty, cannot parse.")
        key = await asyncio.to_thread(self.cache_key_for, blocks)
        with _parse_flights_lock:
            future = _parse_flights.get(key)
            leader = future is None
            if future is None:
                future = Future()
                _parse_flights[key] = future
        if not leader:
            tree = await asyncio.shield(asyncio.wrap_future(future))
            self._emit_progress("cache_hit", {"message": "复用正在执行的同文档解析结果。"})
            return await asyncio.to_thread(_clone_cache_value, tree)
        try:
            tree = await self._async_parse_impl(blocks, key)
            future.set_result(await asyncio.to_thread(_clone_cache_value, tree))
            return tree
        except BaseException as exc:
            future.set_exception(exc)
            # The leader also raises directly. Mark the shared future's
            # exception as observed so a failure with no followers does not
            # emit a misleading "Future exception was never retrieved" at GC;
            # followers can still retrieve the same exception normally.
            future.exception()
            raise
        finally:
            with _parse_flights_lock:
                _parse_flights.pop(key, None)

    async def _async_parse_impl(self, blocks: List[Block], cache_key: str) -> DocumentTree:
        """Async version of :meth:`parse`.

        Uses :class:`AsyncLLMClient` for LLM calls so the event loop
        is never blocked.  CPU-bound Stage 4 (resolution) is offloaded
        to a thread via ``asyncio.to_thread`` for pipeline overlap.
        """
        import asyncio

        if not blocks:
            raise ParserError("Block list is empty, cannot parse.")

        cached = await asyncio.to_thread(_doc_cache.get, cache_key)
        if cached is not None:
            logger.info("[Parser] Cache hit, skipping LLM call (hash=%s)", cache_key[:12])
            self._emit_progress("cache_hit", {
                "message": "命中结构化解析缓存，直接复用上次章节树结果。",
            })
            return cached

        spill = asyncio.create_task(asyncio.to_thread(self._spill_for_two_pass, blocks))
        try:
            blocks, block_store = await asyncio.shield(spill)
        except asyncio.CancelledError:
            _, abandoned = await spill
            if abandoned is not None:
                abandoned.dispose()
            raise
        if block_store is not None:
            set_block_store(block_store)

        try:
            logger.info("[Parser] Async parse start (%d blocks)", len(blocks))

            self._emit_progress("stage_started", {
                "stage": "compress",
                "task": "Constellation 阶段2/4：虚拟骨架压缩",
                "description": "压缩正文为结构骨架，保留标题、字号与层级信号。",
            })
            candidate_set, heading_candidates, skeleton_chunks = await asyncio.to_thread(
                self._prepare_skeleton, blocks,
            )
            total_chars = sum(len(c) for c in skeleton_chunks)
            logger.info(
                "[Parser] Skeleton ready (%d chars, %d chunks, %d candidates)",
                total_chars, len(skeleton_chunks), len(heading_candidates),
            )
            self._emit_progress("stage_completed", {
                "stage": "compress",
                "task": "Constellation 阶段2/4：虚拟骨架压缩",
                "success": True,
                "output": f"生成 {len(skeleton_chunks)} 个骨架分片、{len(heading_candidates)} 个标题候选。",
            })

            self._emit_progress("stage_started", {
                "stage": "route",
                "task": "Constellation 阶段3/4：AI 游标识别",
                "description": "基于骨架识别章节边界、标题与层级。",
            })
            llm_output = await self._async_map_reduce_route(
                skeleton_chunks,
                blocks=blocks,
                candidates=(
                    heading_candidates
                    if self._parser_config.enable_heading_candidates else None
                ),
            )
            logger.info(
                "[Parser] LLM routing done: title='%s', chapters=%d",
                llm_output.doc_title, len(llm_output.chapters),
            )
            self._emit_progress("stage_completed", {
                "stage": "route",
                "task": "Constellation 阶段3/4：AI 游标识别",
                "success": True,
                "output": f"识别 {len(llm_output.chapters)} 个章节锚点，标题：{llm_output.doc_title or '未识别'}。",
            })

            self._emit_progress("stage_started", {
                "stage": "resolve",
                "task": "Constellation 阶段4/4：闭合组装文档",
                "description": "回填全文并组装为结构化章节树。",
            })
            resolving = asyncio.create_task(asyncio.to_thread(
                self._resolve_and_build_tree,
                blocks,
                llm_output,
                candidate_set,
                cache_key,
            ))
            try:
                tree = await asyncio.shield(resolving)
            except asyncio.CancelledError:
                await resolving  # Native/thread work must relinquish the block store before disposal.
                raise

            await asyncio.to_thread(_doc_cache.put, cache_key, tree)

            stats = tree.get_stats()
            logger.info(
                "[Parser] Async parse complete\n"
                "  title: %s\n  total sections: %d\n  max depth: %d",
                stats["doc_title"], stats["total_sections"], stats["max_depth"],
            )
            self._emit_progress("stage_completed", {
                "stage": "resolve",
                "task": "Constellation 阶段4/4：闭合组装文档",
                "success": True,
                "output": f"组装 {stats['total_sections']} 个章节，最大层级 {stats['max_depth']}。",
            })
            return tree

        except ParserError:
            raise
        except Exception as e:
            raise ParserError(f"Async document parsing failed: {e}") from e
        finally:
            if block_store is not None:
                block_store.dispose()

    def _resolve_and_build_tree(
        self,
        blocks: List[Block],
        llm_output: LLMRouterOutput,
        heading_candidates: HeadingCandidateSet | List[HeadingCandidate] | None = None,
        source_identity: str | None = None,
        resolver: "IntervalResolver | None" = None,
    ) -> DocumentTree:
        """Stage 4: globally infer anchors and build the document tree.

        Extracted as a standalone method so it can be submitted to
        ``asyncio.to_thread`` or a ``ThreadPoolExecutor``.

        ``source_identity`` 是本次解析的块内容指纹（缓存键），透传给
        :class:`DocumentTree` 作为 section_id 的文档身份，见 B08。

        ``resolver`` 可由调用方预先构造并传入（A01 的选择性复审用它，让初判与终判落在
        **同一个** resolver / 解码器实例上）。不传则按参数新建，行为与从前一致。
        """
        if resolver is None:
            resolver = IntervalResolver(
                blocks,
                config=self._resolver_config,
                parser_config=self._parser_config,
                heading_candidates=heading_candidates,
            )

        resolver.record_routed_blocks(llm_output._observed_block_ids)
        document_nodes = resolver.resolve(llm_output.chapters)
        preamble_content = self._extract_preamble(
            resolver, document_nodes, had_anchors=bool(llm_output.chapters),
        )

        return DocumentTree(
            nodes=document_nodes,
            doc_title=self._resolve_doc_title(llm_output.doc_title, blocks, document_nodes),
            doc_authors=llm_output.doc_authors,
            preamble_content=preamble_content,
            lossless_fallback=resolver.lossless_fallback,
            source_identity=source_identity,
        )

    async def _async_map_reduce_route(
        self,
        skeleton_chunks: List[str],
        blocks: List[Block] | None = None,
        candidates: List[HeadingCandidate] | None = None,
    ) -> LLMRouterOutput:
        """Async Map-Reduce — dispatches to serial or speculative strategy."""
        total = len(skeleton_chunks)

        if total == 1:
            logger.info("[Map-Reduce] Async single-chunk mode")
            max_block_id = max((b.id for b in blocks), default=-1) if blocks else -1
            output = await self._async_route_single_chunk(
                skeleton_chunks[0],
                candidates=candidates,
                max_block_id=max_block_id,
            )
            return self._merge_route_results(
                {0: output}, 1, blocks=blocks, candidates=candidates,
            )

        if self._parser_config.enable_speculative_execution and total > 2:
            return await self._async_speculative_route(
                skeleton_chunks,
                blocks=blocks,
                candidates=candidates,
            )

        return await self._async_serial_route(
            skeleton_chunks,
            blocks=blocks,
            candidates=candidates,
        )

    async def _async_serial_route(
        self,
        skeleton_chunks: List[str],
        blocks: List[Block] | None = None,
        candidates: List[HeadingCandidate] | None = None,
    ) -> LLMRouterOutput:
        """Serial routing with state phantom projection."""
        total = len(skeleton_chunks)
        logger.info("[Map-Reduce] Async serial mode: %d windows", total)

        results: dict[int, LLMRouterOutput] = {}
        previous_tail_context = ""
        max_block_id = max((b.id for b in blocks), default=-1) if blocks else -1

        for idx, chunk in enumerate(skeleton_chunks):
            start_id, end_id = self._chunk_id_range(chunk, max_block_id)
            window_candidates = (
                candidates_in_range(candidates or [], start_id, end_id)
                if candidates is not None else None
            )
            # Serial mode is naturally 1-at-a-time per document, but a document
            # parsing serially still shares the process with speculative ones, so
            # its single in-flight window must also count against the ceilings.
            async with self._window_request_slot():
                output = await self._async_route_window_chunk(
                    chunk,
                    idx,
                    total,
                    previous_tail_context,
                    candidates=window_candidates,
                    max_block_id=max_block_id,
                )
            results[idx] = output

            logger.info(
                "[Map-Reduce] Window %d/%d done: %d anchors",
                idx + 1, total, len(output.chapters),
            )

            if output.chapters:
                tail = output.chapters[-3:]
                previous_tail_context = "\n".join(
                    f"- Level {ch.level}: {ch.title}" for ch in tail
                )

        return self._merge_route_results(
            results, total, blocks=blocks, candidates=candidates,
        )

    async def _async_speculative_route(
        self,
        skeleton_chunks: List[str],
        blocks: List[Block] | None = None,
        candidates: List[HeadingCandidate] | None = None,
    ) -> LLMRouterOutput:
        """Speculative parallel execution with boundary verification.

        Strategy:
            1. Fire all window LLM requests in parallel (no state
               projection).
            2. Verify that each window boundary is level-consistent
               with the previous window's tail context.
            3. Re-request only the inconsistent windows serially with
               proper state projection.

        This yields parallel speed in the common case (most boundaries
        are consistent) and falls back to serial only for mismatched
        windows.
        """
        import asyncio

        total = len(skeleton_chunks)
        tolerance = self._parser_config.speculative_boundary_tolerance

        logger.info(
            "[Speculative] Parallel mode: %d windows (tolerance=%d)",
            total, tolerance,
        )

        # Phase 1: parallel fire under both concurrency ceilings.
        # Hundreds of windows no longer become hundreds of simultaneous model
        # requests: the bounded fan-out admits at most per-document (and, across
        # documents, global) requests at a time, and reclaims the whole group if
        # any window raises or times out.
        max_block_id = max((b.id for b in blocks), default=-1) if blocks else -1
        factories: List[Callable[[], Any]] = []
        for idx, chunk in enumerate(skeleton_chunks):
            start_id, end_id = self._chunk_id_range(chunk, max_block_id)
            window_candidates = (
                candidates_in_range(candidates or [], start_id, end_id)
                if candidates is not None else None
            )

            def _make(idx=idx, chunk=chunk, window_candidates=window_candidates):
                return self._async_route_window_chunk(
                    chunk,
                    idx,
                    total,
                    "",
                    candidates=window_candidates,
                    max_block_id=max_block_id,
                )

            factories.append(_make)
        speculative_results: List[LLMRouterOutput] = await self._gather_windows_bounded(
            factories,
        )
        results: dict[int, LLMRouterOutput] = dict(enumerate(speculative_results))

        logger.info("[Speculative] Parallel phase done, verifying boundaries")

        # Phase 2: boundary verification + selective re-request
        rerun_count = 0
        boundary_aligner = MonotonicAnchorAligner(blocks, self._resolver_config) if blocks else None
        for idx in range(1, total):
            prev_output = results[idx - 1]
            curr_output = results[idx]

            if not prev_output.chapters or not curr_output.chapters:
                continue

            prev_tail = prev_output.chapters[-1]
            curr_head = curr_output.chapters[0]
            if boundary_aligner is not None:
                previous = await asyncio.to_thread(boundary_aligner.align, prev_output.chapters, candidates)
                current = await asyncio.to_thread(boundary_aligner.align, curr_output.chapters, candidates)
                _, previous_end = self._chunk_id_range(skeleton_chunks[idx - 1], max_block_id)
                current = [anchor for anchor in current if anchor.start_block_id > previous_end]
                if not previous or not current:
                    continue
                prev_tail, curr_head = previous[-1], current[0]

            # Check 1: level jump
            level_inconsistent = (
                curr_head.level > prev_tail.level + tolerance
            )

            # Check 2: block_id regression
            id_regression = (
                curr_head.start_block_id < prev_tail.start_block_id
            )

            if level_inconsistent or id_regression:
                tail = prev_output.chapters[-3:]
                tail_ctx = "\n".join(
                    f"- Level {ch.level}: {ch.title}" for ch in tail
                )

                reason = (
                    "level jump" if level_inconsistent
                    else "block_id regression"
                )
                logger.info(
                    "[Speculative] Window %d boundary inconsistent "
                    "(%s: prev=L%d/id=%d, curr=L%d/id=%d), re-requesting",
                    idx + 1, reason,
                    prev_tail.level, prev_tail.start_block_id,
                    curr_head.level, curr_head.start_block_id,
                )
                chunk = skeleton_chunks[idx]
                start_id, end_id = self._chunk_id_range(chunk, max_block_id)
                async with self._window_request_slot():
                    results[idx] = await self._async_route_window_chunk(
                        chunk,
                        idx,
                        total,
                        tail_ctx,
                        candidates=(
                            candidates_in_range(candidates or [], start_id, end_id)
                            if candidates is not None else None
                        ),
                        max_block_id=max_block_id,
                    )
                rerun_count += 1

        logger.info(
            "[Speculative] Boundary check done: %d/%d windows re-requested",
            rerun_count, total - 1,
        )

        return self._merge_route_results(
            results, total, blocks=blocks, candidates=candidates,
        )

    # ── Merge & hierarchy normalization ────────────────────────

    async def _async_route_single_chunk(
        self,
        chunk: str,
        *,
        candidates: List[HeadingCandidate] | None,
        max_block_id: int,
    ) -> LLMRouterOutput:
        """Async route one complete skeleton, tolerating old test doubles."""
        try:
            return await self.router.async_route(
                chunk,
                candidates=candidates,
                max_block_id=max_block_id,
            )
        except TypeError:
            return await self.router.async_route(chunk)

    async def _async_route_window_chunk(
        self,
        chunk: str,
        idx: int,
        total: int,
        previous_tail_context: str,
        *,
        candidates: List[HeadingCandidate] | None,
        max_block_id: int,
    ) -> LLMRouterOutput:
        """Async route one map-reduce window, tolerating old test doubles."""
        try:
            return await self.router.async_route_chunk(
                chunk,
                idx,
                total,
                previous_tail_context,
                candidates=candidates,
                max_block_id=max_block_id,
            )
        except TypeError:
            return await self.router.async_route_chunk(
                chunk,
                idx,
                total,
                previous_tail_context,
            )

    def _merge_route_results(
        self,
        results: dict[int, LLMRouterOutput],
        total: int,
        blocks: List[Block] | None = None,
        candidates: List[HeadingCandidate] | None = None,
    ) -> LLMRouterOutput:
        """Physically align raw per-window votes without local selection.

        Window boundaries are semantic evidence: overlapping windows may cast
        independent votes for the same physical heading.  They must reach the
        monotonic aligner before any deduplication or physical validation, so
        the merged anchor retains every :class:`LLMAnchorVote` and its actual
        ``window_index`` for global inference.

        ``blocks is None`` is a compatibility path for direct test/external
        callers that invoke routing helpers without a document.  It retains
        their historical deterministic dedup contract; production parse paths
        always provide blocks and therefore always use physical alignment.
        """
        doc_title = results[0].doc_title
        doc_authors = results[0].doc_authors
        windows = [results[index].chapters for index in range(total)]
        raw_count = sum(len(window) for window in windows)

        if blocks is not None:
            chapters = MonotonicAnchorAligner(
                blocks, self._resolver_config,
            ).align_windows(windows, candidates=candidates)
        else:
            # Legacy adapter only: direct callers without Blocks cannot perform
            # physical alignment.  Preserve their historical deterministic
            # sort/dedup contract; every production parse supplies ``blocks``
            # and never enters this branch.
            raw_chapters = [
                copy.deepcopy(chapter)
                for window in windows
                for chapter in window
            ]
            raw_chapters.sort(key=lambda chapter: chapter.start_block_id)
            chapters = self._deduplicate_overlap_anchors(raw_chapters)

        logger.info(
            "[Map-Reduce] Global alignment: %d raw votes -> %d physical anchors",
            raw_count,
            len(chapters),
        )
        merged = LLMRouterOutput(
            doc_title=doc_title,
            doc_authors=doc_authors,
            chapters=chapters,
        )
        observed = [output._observed_block_ids for output in results.values()
                    if output._observed_block_ids is not None]
        merged._observed_block_ids = set().union(*observed) if observed else None
        return merged

    # ── Out-of-candidate anchor re-validation (low-confidence channel) ──

    def _verify_downgraded_anchors(
        self,
        output: LLMRouterOutput,
        blocks: List[Block] | None,
        candidates: List[HeadingCandidate] | None,
    ) -> LLMRouterOutput:
        """Physically re-validate anchors the router downgraded.

        The router marks LLM anchors outside the Stage 2.5 candidate set
        as ``out_of_candidate`` instead of dropping them.  Here each such
        anchor must pass a physical-feature check on its actual block:

        - block exists, is text, and is not paragraph-length prose;
        - the LLM title is consistent with the block text (anti-
          hallucination guard);
        - at least one heading signal fires: physical title heuristic,
          numbering pattern, semantic title keyword, or proximity to a
          known candidate block.

        Anchors that fail are dropped; survivors keep their lowered
        confidence so the resolver widens its fuzzy-anchor search.
        """
        flagged = [ch for ch in output.chapters if ch.out_of_candidate]
        if not flagged:
            return output
        if not blocks:
            # No physical evidence available — be conservative, drop.
            output.chapters = [
                ch for ch in output.chapters if not ch.out_of_candidate
            ]
            return output

        cfg = self._parser_config
        # 索引由本次解析共享的 BlockIndex 提供（L01），不再自建第七份。
        block_map = block_index(blocks).by_id
        body_size = _body_font_size(blocks)
        cand_ids = sorted(c.block_id for c in (candidates or []))
        featureless = is_featureless_document(blocks)

        kept: List[ChapterNode] = []
        dropped = 0
        for ch in output.chapters:
            if not ch.out_of_candidate:
                kept.append(ch)
                continue
            if self._passes_physical_check(
                ch, block_map, body_size, cand_ids, cfg, featureless,
            ):
                logger.info(
                    "[Parser] Out-of-candidate anchor verified: "
                    "block_id=%d, title='%s'",
                    ch.start_block_id, ch.title[:30],
                )
                kept.append(ch)
            else:
                dropped += 1
                logger.warning(
                    "[Parser] Out-of-candidate anchor failed physical "
                    "re-validation, dropped: block_id=%d, title='%s'",
                    ch.start_block_id, ch.title[:30],
                )

        if dropped:
            logger.info(
                "[Parser] Downgrade channel: %d/%d out-of-candidate "
                "anchors dropped after physical re-validation",
                dropped, len(flagged),
            )
        output.chapters = kept
        return output

    @staticmethod
    def _passes_physical_check(
        ch: ChapterNode,
        block_map: dict[int, Block],
        body_size: float,
        sorted_candidate_ids: List[int],
        cfg: ParserConfig,
        featureless: bool = False,
    ) -> bool:
        """Check one downgraded anchor against physical block evidence.

        Tightened 2026-06-11 after the long-doc rerun exposed a precision
        regression (chain_of_thought: 198 predictions vs 35 GT).  The
        original single-signal pass conditions ("short bold line",
        bare ``^[A-Z]\\s`` prefix, unconditional candidate proximity)
        admitted figure labels, emphasised prose, and any sentence
        starting with "A ..." / "I ...".
        """
        block = block_map.get(ch.start_block_id)
        if block is None or block.type != "text" or not block.text:
            return False

        # Shape gates run on the structural form; ``**bold**`` transport markers
        # would otherwise defeat every punctuation check below.
        text = structural_text(block.text)
        if not text or len(text) > cfg.downgrade_max_text_len:
            return False

        # Anti-hallucination: the claimed title must actually appear in
        # (or strongly resemble the head of) the block text.
        title = structural_text(ch.title).lower()
        text_lower = text.lower()
        title_consistent = bool(title) and (
            title in text_lower
            or _levenshtein_ratio(title, text_lower[: max(len(title) * 2, 20)])
            >= cfg.downgrade_title_similarity
        )
        if not title_consistent:
            return False

        # ── Titleness negative gates (prose rejection) ──
        words = text.split()
        if len(words) > 1:
            # Space-delimited text: headings rarely exceed 12 words.
            if len(words) > 12:
                return False
        elif len(text) > 50:
            # CJK / single-token text: headings rarely exceed 50 chars.
            return False
        # Hanging punctuation = truncated prose, never a heading.
        if text.endswith((",", ";", ":", "，", "；", "：")):
            return False
        is_sentence_final = text.endswith((".", "。", "!", "?", "！", "？"))
        # A long sentence-final line is prose even when short-ish.
        if is_sentence_final and len(words) > 6:
            return False

        # ── Positive signals (any one fires) ──
        if block.is_potential_title(min_body_size=body_size):
            return True
        if infer_numbering_level(text) is not None:
            return True
        if _semantic_level(text) is not None:
            return True
        # Short bold line covers run-in / same-font-size headings
        # (LaTeX papers), but sentence-final bold lines are emphasised
        # prose or figure labels, not headings.
        if block.is_bold and len(text) <= 80 and not is_sentence_final:
            return True
        # Appendix-style numbering ("A. Setup", "B.1 Details"): the
        # letter MUST be followed by a dot — a bare ``^[A-Z]\s`` would
        # match any sentence starting with "A ..." or "I ...".
        if len(text) <= 80 and re.match(
            r"^[A-Z](?:\.\d+)+\.?\s+\S+|^[A-Z]\.\s+\S+", text,
        ):
            return True
        # Candidate proximity alone is no longer sufficient (Stage 4
        # fuzzy anchoring already handles off-by-one); it must be backed
        # by a physical formatting cue.
        if sorted_candidate_ids and (
            block.is_bold
            or (block.font_size and body_size > 0
                and block.font_size >= body_size * 1.05)
        ):
            import bisect
            pos = bisect.bisect_left(sorted_candidate_ids, ch.start_block_id)
            for idx in (pos - 1, pos):
                if 0 <= idx < len(sorted_candidate_ids) and (
                    abs(sorted_candidate_ids[idx] - ch.start_block_id)
                    <= cfg.downgrade_candidate_proximity
                ):
                    return True
        # No-style fallback: with zero physical formatting signals the
        # positive cues above can never fire, yet the LLM still reads a
        # heading off the skeleton.  This block already cleared the
        # titleness negative gates (<=12 words / <=50 CJK chars, no
        # hanging punctuation, no long sentence-final line) and the
        # anti-hallucination check (title ~= block text), so a short,
        # non-sentence line is the canonical shape of a featureless
        # heading.  Gated on ``featureless`` so formatted documents keep
        # the strict physical bar (precision unaffected).
        if featureless and not is_sentence_final:
            return True
        return False

    def _normalize_hierarchy(
        self,
        chapters: List[ChapterNode],
        blocks: List[Block],
    ) -> List[ChapterNode]:
        """Hierarchy normalization via the unified :class:`HierarchyRepairer`.

        Retained as a public-ish entry point (tests and external callers
        use it), but the main pipeline no longer calls it: hierarchy
        repair runs exactly once, inside ``IntervalResolver.resolve``,
        after fuzzy anchor correction when block ids are final.  Running
        it twice (post-merge AND post-correction) made level fixes
        impossible to attribute.
        """
        if len(chapters) <= 1:
            return chapters

        from modules.parser.hierarchy import HierarchyRepairer

        repairer = HierarchyRepairer(
            blocks,
            candidates=None,
            config=self._resolver_config,
        )
        return repairer.repair(chapters)
