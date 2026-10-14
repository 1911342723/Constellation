"""Single source of truth for format detection and provider dispatch.

Every caller imports this module so that the ``suffix -> format`` and
``format -> provider`` tables live in exactly one place, rather than each
caller keeping its own copy.

This module raises **domain-level exceptions only**
(:class:`~app.core.exceptions.UnsupportedFormatError` /
:class:`~app.core.exceptions.ProviderError`); it knows nothing about any
transport.  Translating those into a transport-level error is the caller's
responsibility.

``get_provider`` returns a **fresh provider instance per call**.  Providers are
not all stateless: :class:`DocxProvider` carries per-extraction state
(``image_store`` / ``_doc_rels`` / style-chain caches), so a shared singleton
extracted concurrently would let one caller reset or repopulate another
caller's document context —
silently mixing images and styles across documents.  Construction is a few
attribute assignments; per-call instantiation is the cheap, correct isolation.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Type

# 直接 import 子模块，不用 ``from infrastructure.providers import pdf_worker``：
# 后者要先取包属性再回退，而本包的 ``__init__`` 会先导入各 provider——冷导入路径上
# 这种形态曾真实报过循环导入（pdf_provider 引 pdf_page_roles 那次）。
import infrastructure.providers.pdf_worker as pdf_worker
from app.core.exceptions import ProviderError, UnsupportedFormatError
from infrastructure.assets import AssetStore
from infrastructure.models import Block
from infrastructure.providers.base import BaseProvider, ProviderSkip
from infrastructure.providers.docx_provider import DocxProvider
from infrastructure.providers.markdown_provider import MarkdownProvider
from infrastructure.providers.pdf_provider import PdfProvider
from infrastructure.providers.spreadsheet_provider import CsvProvider, XlsxProvider
from infrastructure.providers.text_provider import TextProvider

logger = logging.getLogger(__name__)

# Filename suffix -> canonical format name.
_SUFFIX_TO_FORMAT: dict[str, str] = {
    ".docx": "docx",
    ".pdf": "pdf",
    ".txt": "txt",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".md": "md",
    ".markdown": "md",
}

# Canonical format name -> provider class (instantiated per call; see module doc).
_PROVIDER_CLASSES: dict[str, Type] = {
    "docx": DocxProvider,
    "pdf": PdfProvider,
    "txt": TextProvider,
    "csv": CsvProvider,
    "xlsx": XlsxProvider,
    "md": MarkdownProvider,
}

#: Canonical format names with a registered provider (sorted, stable).
SUPPORTED_FORMATS: List[str] = sorted(_PROVIDER_CLASSES)
#: Accepted filename suffixes (sorted, stable).
SUPPORTED_SUFFIXES: List[str] = sorted(_SUFFIX_TO_FORMAT)

# ── R01：PDF 的执行方式 ────────────────────────────────
#
# 两个交付层都把 Stage 1 放进 ``asyncio.to_thread``（修掉了阻塞事件循环的问题），
# 而并发准入允许多篇文档同时在跑——于是多个**工作线程可能同时**运行 PyMuPDF。
# PyMuPDF 官方明确不支持多线程运行，推荐多进程、每进程各自打开文档：
# https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html
#
# 现在有两条执行方式，由 ``pdf_process_workers`` 选择，且都只在 ``_extract`` 一处进入：
#
# 1. **进程内单执行通道**（默认，``pdf_process_workers=0``）：同一进程内任何时刻最多
#    一个线程在跑 fitz。代价明确且有意选：两篇 PDF 串行、长 PDF 会让后到的排队；
#    换来的是不会串内容、不会在 SWIG 层随机崩溃。
# 2. **有界进程 worker**（``pdf_process_workers>0``，见 :mod:`infrastructure.providers.pdf_worker`）：
#    每个 worker 自行打开文件，多篇 PDF 真并行，一次引擎崩溃不带走服务进程。
#
# 其他格式两种情形都不受影响（python-docx / openpyxl / 纯文本都是普通 Python 对象，
# 无此约束）。
_SERIALIZED_FORMATS: frozenset[str] = frozenset({"pdf"})

_pdf_lane = threading.Lock()


class _NullLane:
    """无需串行化的格式用的空通道（避免为每次调用建对象）。"""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


_null_lane = _NullLane()


def _execution_lane(source_format: str):
    """返回该格式应当占用的执行通道（PDF 串行，其余并行）。"""
    if source_format in _SERIALIZED_FORMATS:
        return _pdf_lane
    return _null_lane


def pdf_lane_is_held() -> bool:
    """PDF 通道当前是否被占用（观测 / 测试用，不参与判定）。"""
    return _pdf_lane.locked()


def _suffix_format(filename: str | None) -> str | None:
    if not filename:
        return None
    lower_name = filename.lower()
    for suffix, source_format in _SUFFIX_TO_FORMAT.items():
        if lower_name.endswith(suffix):
            return source_format
    return None


def has_known_suffix(filename: str | None) -> bool:
    return _suffix_format(filename) is not None


#: Canonical format -> a suffix providers accept on ``extract(path)``.
FORMAT_SUFFIX: dict[str, str] = {
    "docx": ".docx",
    "pdf": ".pdf",
    "txt": ".txt",
    "csv": ".csv",
    "xlsx": ".xlsx",
    "md": ".md",
}


def sniff_format_path(path: str | Path) -> str | None:
    """Guess format from a file on disk without reading the whole payload."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return "docx"
        if any(name.startswith("xl/") for name in names):
            return "xlsx"
        if any(name.startswith("word/") for name in names):
            return "docx"
        return "docx"
    return None


def sniff_format(content: bytes | None) -> str | None:
    """Guess a canonical format from file bytes when the filename has no suffix."""
    blob = bytes(content or b"")
    if blob.startswith(b"%PDF"):
        return "pdf"
    if blob.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return "docx"
        if any(name.startswith("xl/") for name in names):
            return "xlsx"
        if any(name.startswith("word/") for name in names):
            return "docx"
        return "docx"
    return None


def detect_format(
    filename: str | None,
    content: bytes | None = None,
    *,
    path: str | Path | None = None,
) -> str:
    """Map a filename / path to a canonical format name by its suffix.

    Args:
        filename: File name or path (case-insensitive suffix match).
        content: Optional file bytes. Used only when the name has no known
            suffix (document exports often return a bare title with no
            ``.docx`` extension).
        path: Optional on-disk file. Used when there is no suffix and ``content``
            is absent or too small to sniff a zip central directory.

    Returns:
        The canonical format name (e.g. ``"docx"``).

    Raises:
        UnsupportedFormatError: If ``filename`` is empty or its suffix has no
            registered provider, and the bytes also cannot be sniffed.
    """
    found = _suffix_format(filename)
    if found:
        return found

    name = Path(str(filename or "")).name
    if name and "." in name:
        raise UnsupportedFormatError(
            f"Unsupported file format for '{filename}'. "
            f"Accepted: {', '.join(SUPPORTED_SUFFIXES)}"
        )

    sniffed = sniff_format(content)
    if sniffed:
        return sniffed
    if path is not None:
        sniffed = sniff_format_path(path)
        if sniffed:
            return sniffed

    if not filename and path is None:
        raise UnsupportedFormatError("No filename provided")
    raise UnsupportedFormatError(
        f"Unsupported file format for '{filename}'. "
        f"Accepted: {', '.join(SUPPORTED_SUFFIXES)}"
    )


def get_provider(source_format: str) -> BaseProvider:
    """Return a **fresh** provider instance for ``source_format``.

    Per-call instantiation isolates per-extraction provider state between
    concurrent requests (see module docstring).

    Raises:
        UnsupportedFormatError: If no provider is registered for the format.
        ProviderError: If the registered provider does not satisfy
            :class:`BaseProvider` (defensive; should never happen at runtime).
    """
    provider_cls = _PROVIDER_CLASSES.get(source_format)
    if provider_cls is None:
        raise UnsupportedFormatError(f"Unsupported source format: {source_format}")
    if provider_cls is PdfProvider:
        from infrastructure.providers.ocr import configured_ocr_engine
        provider = provider_cls(ocr_engine=configured_ocr_engine())
    else:
        provider = provider_cls()
    if not isinstance(provider, BaseProvider):
        raise ProviderError(
            f"Provider for '{source_format}' does not satisfy BaseProvider protocol"
        )
    return provider


@dataclass(frozen=True)
class ExtractionResult:
    """Stage 1 的一次提取产出：块 + 旁路产物。

    三个公开入口（:func:`extract_blocks` / :func:`extract_blocks_with_skips` /
    :func:`extract_blocks_with_assets`）都只是对本结构的投影。这样再多一种旁路
    产物（per-page 计时、字体清单……）只需在这里加一个字段，不必再抄一遍
    「构造 provider → 进执行通道 → drain」的三步——那三步一旦被抄第二遍，
    PDF 的串行化（R01）迟早会在某个新入口上被漏掉。
    """

    blocks: List[Block]
    skips: List[ProviderSkip]
    assets: "AssetStore | None"


def _extract(
    source_format: str, content: bytes, *, collect_assets: bool = False
) -> ExtractionResult:
    """唯一的 Stage 1 提取实现：所有入口都经由此处。

    ``collect_assets`` 决定是否在提取前挂 AssetStore（挂了图片字节才被收编、
    ``asset_*`` 字段才被填）。不挂时行为与历史逐字节一致。

    PDF 有两条执行方式，都在这唯一一处选择，所以任何入口都不可能绕开（R01）：

    - 配了 ``pdf_process_workers > 0``：交给有界进程 worker，多篇 PDF 真并行，
      一次引擎崩溃不带走服务进程。字节先落一次临时文件，任务只传路径。
    - 否则：进程内单许可通道，多篇 PDF 串行（历史行为）。
    """
    if source_format in _SERIALIZED_FORMATS and pdf_worker.is_enabled():
        return _extract_pdf_in_worker(content, collect_assets=collect_assets)

    provider = get_provider(source_format)
    store: "AssetStore | None" = None
    if collect_assets:
        store = AssetStore()
        attach = getattr(provider, "attach_asset_store", None)
        if callable(attach):
            attach(store)
    # 执行通道在这唯一一处进入：PDF 的单许可串行化（R01，PyMuPDF 不支持多线程
    # 运行）因此不可能被任何入口绕过。
    with _execution_lane(source_format):
        blocks = provider.extract_from_bytes(content)
    drain = getattr(provider, "drain_skips", None)
    return ExtractionResult(
        blocks=blocks,
        skips=(drain() if callable(drain) else []),
        assets=store,
    )


def _extract_pdf_in_worker(content: bytes, *, collect_assets: bool) -> ExtractionResult:
    """把 PDF 交给有界进程 worker（R01 中期落地）。

    字节落一次临时文件、任务只传路径：这既满足「每个进程自行打开文件」，也避免
    「把整篇 bytes pickle 一遍穿过管道」——大文件下那笔复制比解析本身还贵。
    临时文件在本函数退出时删除，worker 拿到的是它自己 ``open`` 的句柄。
    """
    handle, path = tempfile.mkstemp(suffix=".pdf", prefix="constellation-pdf-")
    try:
        with os.fdopen(handle, "wb") as sink:
            sink.write(content)
        extraction = pdf_worker.run_in_worker(path, collect_assets=collect_assets)
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - best-effort cleanup
            logger.debug("[Registry] could not remove temp pdf %s", path)
    return ExtractionResult(
        blocks=extraction.blocks,
        skips=extraction.skips,
        assets=extraction.assets,
    )


def _extract_from_path(
    source_format: str, path: str, *, collect_assets: bool = False
) -> ExtractionResult:
    """Stage 1 从已落盘路径提取：不再把文件读回父进程再 pickle / stream。

    PDF worker 直接拿这份路径，省掉「bytes → 临时文件」的第二次拷贝。
    """
    if source_format in _SERIALIZED_FORMATS and pdf_worker.is_enabled():
        extraction = pdf_worker.run_in_worker(path, collect_assets=collect_assets)
        return ExtractionResult(
            blocks=extraction.blocks,
            skips=extraction.skips,
            assets=extraction.assets,
        )

    provider = get_provider(source_format)
    store: "AssetStore | None" = None
    if collect_assets:
        store = AssetStore()
        attach = getattr(provider, "attach_asset_store", None)
        if callable(attach):
            attach(store)
    with _execution_lane(source_format):
        blocks = provider.extract(path)
    drain = getattr(provider, "drain_skips", None)
    return ExtractionResult(
        blocks=blocks,
        skips=(drain() if callable(drain) else []),
        assets=store,
    )


def extract_blocks(source_format: str, content: bytes) -> List[Block]:
    """Stage 1 dispatch: pick the provider for ``source_format`` and extract.

    Args:
        source_format: Canonical format name (see :data:`SUPPORTED_FORMATS`).
        content: Raw document bytes.

    Returns:
        Ordered list of :class:`Block` objects.

    Raises:
        UnsupportedFormatError: If the format has no registered provider.
        ProviderError: If the provider fails to parse the bytes.
    """
    return _extract(source_format, content).blocks


def extract_blocks_with_skips(
    source_format: str, content: bytes
) -> tuple[List[Block], List[ProviderSkip]]:
    """同 :func:`extract_blocks`，但同时交回本次提取跳过了什么（R05）。

    providers 对坏元素 / 坏图片 / 坏表格是 ``except ... continue``——容错本身是对的，
    但此前跳过只落在日志里，调用方拿到的响应与完整成功的响应一模一样，于是残缺被
    当完整验收、缓存、下发。这个变体把 provider 累积的跳过记录一并交回，交付层据此
    把结果标成 ``partial``。
    """
    result = _extract(source_format, content)
    return result.blocks, result.skips


def extract_blocks_with_assets(
    source_format: str, content: bytes
) -> tuple[List[Block], List[ProviderSkip], AssetStore]:
    """同 :func:`extract_blocks_with_skips`，但另外交回收编了图片字节的 AssetStore（P05）。

    图片字节按完整哈希收进 store 并去重，Block 只留 ``asset_id`` + mime +
    ``metadata['asset_ref']``（同时保留 ``image_data`` 兼容路径）。交付层据此可按
    引用取字节，避免同一张图的 base64 文本在多份大字符串里反复常驻、反复解码与
    重复哈希。

    另两个入口的签名刻意保持不变：REST、MCP、大量测试都在用它们。
    """
    result = _extract(source_format, content, collect_assets=True)
    assert result.assets is not None  # collect_assets=True 时必然有
    return result.blocks, result.skips, result.assets


def extract_blocks_with_assets_from_path(
    source_format: str, path: str
) -> tuple[List[Block], List[ProviderSkip], AssetStore]:
    """同 :func:`extract_blocks_with_assets`，从已落盘路径进入，不把整文件读回内存。"""
    result = _extract_from_path(source_format, str(path), collect_assets=True)
    assert result.assets is not None
    return result.blocks, result.skips, result.assets
