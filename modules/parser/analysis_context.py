"""Reuse deterministic full-document measurements within one immutable parse."""
from __future__ import annotations

import bisect
import copy
import inspect
import threading
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Optional

_context = ContextVar("document_analysis", default=None)


@dataclass(frozen=True)
class BlockIndex:
    """一次解析内**唯一**的块索引：顺序视图 + id 映射 + 有序 id + 正文块子集。

    为什么必须只有一份：决策各阶段此前各建一份 ``{b.id: b for b in blocks}``——
    resolver、anchor_alignment、global_inference、hierarchy（两处）、
    heading_candidates、typography（还全篇扫三遍）。同一份不变的 blocks 被反复重新
    物化成索引，一篇上万块的文档就要重建七八次；更糟的是「同一件事多实现」——
    resolver 校验了重复 id、别处没校验，谁先谁后取决于调用顺序。

    本类是只读的（``frozen`` + 元组/只读映射），所以可以安全共享，不需要像
    :func:`measured_once` 那样每次取用都 deepcopy 一份。

    Attributes:
        blocks: 按入参顺序的块序列。
        by_id: ``block_id -> Block``。重复 id 由 :meth:`build` 一次性拒绝。
        ordered_ids: 升序 block id，供区间二分定位（B09：稀疏 id 不得线性扫数值区间）。
        text_block_ids: 只含 ``type == "text"`` 且有文字的块 id，排版统计的复用点。
    """

    blocks: tuple
    by_id: Mapping[int, object]
    ordered_ids: tuple[int, ...]
    text_block_ids: tuple[int, ...]

    @classmethod
    def build(cls, blocks: Sequence) -> "BlockIndex":
        ordered = tuple(blocks)
        by_id: dict[int, object] = {}
        for block in ordered:
            block_id = getattr(block, "id", None)
            if block_id is None:
                continue
            if block_id in by_id:
                # 重复 id 会让 dict 后写者覆盖前者、静默丢内容（B09）。索引是唯一入口，
                # 所以这道校验只在这里做一次，不再由各消费方各写一遍；调用方按自己的
                # 领域异常类型转译即可（消息前缀保持稳定，便于对外契约不变）。
                raise ValueError(
                    f"Duplicate block id(s) [{block_id}] would silently "
                    f"overwrite entries in the block index and drop their content"
                )
            by_id[block_id] = block
        text_ids = tuple(
            bid for bid in sorted(by_id)
            if getattr(by_id[bid], "type", None) == "text" and (getattr(by_id[bid], "text", "") or "")
        )
        return cls(
            blocks=ordered,
            by_id=by_id,
            ordered_ids=tuple(sorted(by_id)),
            text_block_ids=text_ids,
        )

    def block(self, block_id: int):
        return self.by_id.get(block_id)

    def ids_in_range(self, start_id: int, end_id: Optional[int]) -> tuple[int, ...]:
        """半开区间 ``[start_id, end_id)`` 内的真实块 id（``end_id=None`` 表到末尾）。

        二分定位后只走真实块，耗时随**真实块数**而不是 id 数值跨度增长——稀疏 id
        （0 与 1_000_000）不会退化成遍历一百万个整数。
        """
        left = bisect.bisect_left(self.ordered_ids, start_id)
        right = (
            len(self.ordered_ids)
            if end_id is None
            else bisect.bisect_left(self.ordered_ids, end_id)
        )
        return self.ordered_ids[left:right]

    def text_blocks(self) -> tuple:
        """正文 text 块（按 id 升序）。排版统计的三处全篇扫描共用这一份。"""
        return tuple(self.by_id[bid] for bid in self.text_block_ids)


_STORE_KEY = "__block_store__"


def set_block_store(store) -> None:
    """把本次解析的块仓库登记进当前作用域。

    走作用域（contextvar）而不是挨个函数加参数：仓库句柄要被 Stage 4 的区间装配用到，
    而它与 Stage 1/2/3 之间隔着好几层调用；逐层塞参数会污染一串与"正文从哪读"无关的
    签名。contextvar 天然按线程 / 按 task 隔离，与既有的 :func:`analysis_scope` 同一
    机制，不引入第二套上下文。作用域之外调用是 no-op——库被直接使用时本来就没有仓库。
    """
    context = _context.get()
    if context is None:
        return
    cache, lock = context
    with lock:
        cache[_STORE_KEY] = store


def current_block_store():
    """取当前作用域登记的块仓库；没有则 ``None``（走内存路径）。"""
    context = _context.get()
    if context is None:
        return None
    cache, lock = context
    with lock:
        return cache.get(_STORE_KEY)


def block_index(blocks: Sequence) -> BlockIndex:
    """取本次解析的唯一 :class:`BlockIndex`；同一批 blocks 只构建一次。

    在 :func:`analysis_scope` 之外（库被直接调用、单测）退化为即时构建——行为一致，
    只是少了复用。缓存键用块**身份**而非内容哈希：同一次解析里 blocks 不可变，
    身份足以判同，而内容哈希要把整篇文字再读一遍，正是本条要消除的开销。
    """
    context = _context.get()
    if context is None or not isinstance(blocks, Sequence):
        return BlockIndex.build(blocks)
    cache, lock = context
    key = ("__block_index__", tuple(id(block) for block in blocks))
    with lock:
        entry = cache.get(key)
        if entry is None:
            # 连同 blocks 元组一起留住，避免 id() 被回收复用导致索引错配。
            entry = (tuple(blocks), BlockIndex.build(blocks))
            cache[key] = entry
        return entry[1]


def analysis_scope(function):
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            token = _context.set(({}, threading.RLock()))
            try:
                return await function(*args, **kwargs)
            finally:
                _context.reset(token)
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        token = _context.set(({}, threading.RLock()))
        try:
            return function(*args, **kwargs)
        finally:
            _context.reset(token)
    return synchronous


def measured_once(function):
    @wraps(function)
    def measure(blocks):
        context = _context.get()
        if context is None or not isinstance(blocks, Sequence):
            return function(blocks)
        cache, lock = context
        # Different list adapters over the same immutable blocks share stats.
        key = (function.__module__, function.__name__, tuple(id(block) for block in blocks))
        with lock:
            if key not in cache:
                # Retain block references so their identities cannot be reused
                # by a later temporary sequence within the same parse.
                cache[key] = (tuple(blocks), function(blocks))
            return copy.deepcopy(cache[key][1])
    return measure
