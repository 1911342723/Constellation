# -*- coding: utf-8 -*-
"""R01 · PDF 的有界进程 worker。

## 为什么需要进程而不是线程

两个交付层都把 Stage 1 放进 ``asyncio.to_thread``，并发准入又允许多篇文档同时在跑——
于是多个**工作线程可能同时**运行 PyMuPDF。PyMuPDF 官方明确说明不支持多线程运行，
推荐多进程且**每个进程自行打开文档**：
https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html

短期落地是 ``registry`` 里那条只有一个许可的进程内通道：同一时刻最多一个线程跑 fitz。
它消除了并发风险，代价是两篇 PDF 必须串行，且一次 SWIG 层崩溃仍会带走整个服务进程。
本模块是方案要的中期落地：**有界的常驻 worker 进程池**，PDF 抽取提交进去。

## 四条硬约束，逐条对应实现

1. **不跨进程传 ``fitz.Document``**。任务参数是一个**文件路径**，子进程自己打开。
   Document 持有 C 层句柄，pickle 不了；就算能，跨进程共享它正是官方点名禁止的用法。
2. **传文件引用而不是整篇 bytes**。调用方把上传字节落一次临时文件，之后每个任务只传
   路径。否则每提交一个任务就要把整篇 PDF pickle 一遍穿过管道——大文件下这笔复制
   比解析本身还贵。
3. **Windows 必须用可导入的顶层任务函数**。Windows 只有 spawn 启动方式，子进程靠
   ``import 模块 + getattr`` 拿到任务函数；闭包、lambda、实例方法都过不去。
   :func:`extract_pdf_from_path` 因此是模块级函数，且不捕获任何外部状态。
4. **worker 在服务启动阶段管理，不按任务/按页重建**。spawn 一个子进程再 import fitz 是
   秒级开销，按任务重建会比串行还慢。池是进程级单例，:func:`configure` 在启动时设定
   规模，:func:`shutdown` 在关闭时回收。

## 崩溃隔离能做到什么、不能做到什么（如实说明）

一个 worker 硬崩（段错误）时，``ProcessPoolExecutor`` 会把整个池标记为
``BrokenProcessPool``——这是标准库的语义，不是本模块的选择。所以这里的做法是：捕获它、
**重建池**、把该任务**重试一次**。效果上「单个任务失败不影响其他任务」成立（兄弟任务
经一次透明重试后照常完成，后续任务落在新池上），但**不是**「崩溃只影响那一个子进程、
其余子进程原地不动」——想要后者得自建进程管理与 IPC，收益不抵复杂度，故不宣称。

重试只做一次：如果某份 PDF 就是会让 PyMuPDF 崩，重试无限次只会反复重建池。第二次仍
崩则如实抛 :class:`~app.core.exceptions.ProviderError`。
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import List, Optional

from app.core.exceptions import ProviderError
from infrastructure.assets import AssetStore
from infrastructure.models import Block
from infrastructure.providers.base import ProviderSkip

logger = logging.getLogger(__name__)

__all__ = [
    "PdfExtraction",
    "extract_pdf_from_path",
    "configure",
    "shutdown",
    "worker_count",
    "is_enabled",
    "run_in_worker",
]


@dataclass(frozen=True)
class PdfExtraction:
    """一次子进程提取的完整产出。

    三个字段都必须可 pickle：``Block`` 是 pydantic 模型、``ProviderSkip`` 是冻结
    dataclass、``AssetStore`` 只装 dataclass 与 bytes。刻意**不**回 provider 实例——
    它持有 fitz 句柄，跨进程传不过来也不该传。
    """

    blocks: List[Block]
    skips: List[ProviderSkip]
    assets: Optional[AssetStore]


def extract_pdf_from_path(path: str, collect_assets: bool) -> PdfExtraction:
    """**顶层可导入**的任务函数：在子进程里自行打开文件并抽块。

    Windows 的 spawn 子进程靠 ``import infrastructure.providers.pdf_worker`` 再取这个
    名字来执行任务，所以它必须是模块级函数、不捕获任何外部状态（约束 3）。

    provider 在**子进程内**构造并自行 ``extract(path)``——没有任何 fitz 对象跨越进程
    边界（约束 1）。
    """
    from infrastructure.providers.registry import get_provider

    provider = get_provider("pdf")
    store: Optional[AssetStore] = None
    if collect_assets:
        store = AssetStore()
        provider.attach_asset_store(store)
    blocks = provider.extract(path)
    return PdfExtraction(blocks=blocks, skips=provider.drain_skips(), assets=store)


# ── 进程级池管理 ──────────────────────────────────────────────────────────

_lock = threading.Lock()
_pool: Optional[ProcessPoolExecutor] = None
_pool_generation = 0
_rebuilds = 0
_workers = 0
_shutting_down = False


def _spawn_pool(size: int) -> ProcessPoolExecutor:
    # 显式用 spawn：Windows 只有它，而 Linux 默认的 fork 会把父进程里已经初始化过的
    # fitz 状态、线程与锁一并复制到子进程，正是「不要在 fork 后用同一个引擎」的坑。
    # 两平台统一走 spawn，行为一致且可预测。
    return ProcessPoolExecutor(max_workers=size, mp_context=multiprocessing.get_context("spawn"))


def configure(max_workers: int) -> None:
    """设定 worker 规模（服务启动阶段调用一次）。

    ``max_workers <= 0`` 表示关闭进程池，PDF 退回 ``registry`` 的进程内单执行通道。
    规模变化时先回收旧池：留着两个池就变成「上界是两个池之和」，有界就不成立了。
    """
    global _workers, _shutting_down
    with _lock:
        _shutting_down = False
        if max_workers == _workers:
            return
        _shutdown_locked()
        _workers = max(int(max_workers), 0)
    logger.info("[PdfWorker] configured max_workers=%d", _workers)


def worker_count() -> int:
    """当前配置的 worker 上限（0 = 关闭）。"""
    return _workers


def is_enabled() -> bool:
    return _workers > 0


def _shutdown_locked(target: Optional[ProcessPoolExecutor] = None) -> None:
    """关闭指定池。迟到的旧异常只能关自己那一代，不能借全局指针关新池。"""
    global _pool
    pool = target if target is not None else _pool
    if pool is None:
        return
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        logger.warning("[PdfWorker] shutdown of a broken pool raised", exc_info=True)
    if _pool is pool:
        _pool = None


def shutdown() -> None:
    """回收 worker 进程（服务关闭阶段调用）。退出期间禁止再重建池。"""
    global _shutting_down
    with _lock:
        _shutting_down = True
        _shutdown_locked()


def _ensure_pool() -> ProcessPoolExecutor:
    """懒建池：进程在**第一次真的要解析 PDF** 时才起。

    不在 :func:`configure` 里就起，是为了让「配了但这次没上传 PDF」的进程不白付
    spawn + import fitz 的秒级代价；起来之后常驻复用（约束 4）。
    """
    global _pool, _pool_generation, _rebuilds
    with _lock:
        if _shutting_down:
            raise ProviderError("PDF 进程 worker 正在关闭，拒绝重建池")
        if _workers <= 0:
            raise ProviderError("PDF 进程 worker 未启用")
        if _pool is None:
            _pool = _spawn_pool(_workers)
            _pool_generation += 1
            if _pool_generation > 1:
                _rebuilds += 1
            logger.info(
                "[PdfWorker] pool started generation=%d workers=%d rebuilds=%d",
                _pool_generation, _workers, _rebuilds,
            )
        return _pool


def run_in_worker(path: str, *, collect_assets: bool) -> PdfExtraction:
    """把一份 PDF 的抽取提交给 worker 并等结果（调用方线程阻塞）。

    调用方仍在自己的线程/协程里等待，所以并发上界由**上层准入**决定；本池只保证
    「同时真正在跑 fitz 的进程数」不超过配置值。

    ``BrokenProcessPool`` 时只关闭**这一代**失败的池并重试一次（见模块说明）。
    迟到的旧异常不得关闭已经接替它的健康新池。
    """
    if not os.path.exists(path):
        raise ProviderError(f"File not found: {path}")
    last_error: Optional[BrokenProcessPool] = None
    for attempt in (1, 2):
        pool = _ensure_pool()
        try:
            return pool.submit(extract_pdf_from_path, path, collect_assets).result()
        except BrokenProcessPool as exc:
            last_error = exc
            with _lock:
                current_gen = _pool_generation
                if _shutting_down:
                    raise ProviderError("PDF 进程 worker 正在关闭，停止恢复") from exc
                if _pool is pool:
                    logger.warning(
                        "[PdfWorker] generation %d broke (attempt %d/2): %s",
                        current_gen, attempt, exc,
                    )
                    _shutdown_locked(pool)
                else:
                    logger.warning(
                        "[PdfWorker] stale BrokenProcessPool ignored "
                        "(failed pool is not current generation %d)",
                        current_gen,
                    )
                    try:
                        pool.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
            if attempt == 2:
                raise ProviderError(
                    "PDF 解析在独立 worker 进程中崩溃两次，已停止重试；"
                    "该文件很可能触发了 PyMuPDF 的崩溃路径。"
                ) from exc
    raise ProviderError("PDF worker retry exhausted") from last_error
