"""Base provider protocol for Constellation document providers.

All document providers (DocxProvider, PdfProvider, TextProvider, etc.)
must implement this protocol.  The contract ensures the API layer can
dispatch uniformly without duck-typing surprises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from infrastructure.assets import AssetStore
from infrastructure.models import Block


@dataclass(frozen=True)
class ProviderSkip:
    """一处被跳过 / 降级的物理提取（R05 的 ``warnings`` 生产端）。

    容错本身是对的：一页图片解码失败不该让整篇解析失败。错的是**只写日志**——
    调用方拿到的响应和完整成功的响应一模一样，于是「少了一张图的文档」被当成
    「完整的文档」验收、缓存、下发。这条记录把那次跳过物化出来，让交付层能把
    结果标成 ``partial`` 并说清丢在哪里。

    字段与交付层的告警模型一一对应，但**不导入**它：
    infrastructure 层不依赖交付层的模型，转换由 registry 的调用方完成。
    """

    stage: str
    code: str
    message: str
    source_span: Optional[Dict[str, Any]] = None
    retryable: bool = False


class SkipReporter:
    """给 provider 混入的跳过记录收集器。

    provider 各自 ``except ... continue`` 的地方调用 :meth:`_note_skip`，
    ``registry.extract_blocks_with_warnings`` 抽完后用 :meth:`drain_skips` 取走。
    实例是每次调用新建的（见 registry 模块说明），所以这里的状态天然按次隔离。
    """

    _skips: List[ProviderSkip]

    def _note_skip(
        self,
        code: str,
        message: str,
        *,
        stage: str = "extract",
        source_span: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        """记下一次跳过。日志照旧打，这里只负责让调用方也能看见。"""
        if getattr(self, "_skips", None) is None:
            self._skips = []
        self._skips.append(
            ProviderSkip(
                stage=stage,
                code=code,
                message=message,
                source_span=source_span,
                retryable=retryable,
            )
        )

    def drain_skips(self) -> List[ProviderSkip]:
        """取走并清空本次提取累积的跳过记录。"""
        skips = list(getattr(self, "_skips", None) or [])
        self._skips = []
        return skips


class AssetRegistrar:
    """给 provider 混入的图片资源登记器（P05）。

    与 :class:`SkipReporter` 分开是有意的：一个收「跳过了什么」，一个收「二进制
    资源」，两件不相关的事。早先把资源登记塞进 SkipReporter，类名就不再描述它
    做的事——那和一句宣称了却没做到的注释是同一类问题，读代码的人会按名字推断
    职责，然后推错。

    默认不挂 store：老入口（``extract`` / ``extract_from_bytes``，以及 registry 的
    ``extract_blocks`` / ``extract_blocks_with_skips``）行为逐字节不变，Block 只填
    ``image_data``、``asset_id`` 留空。只有需要资源的入口才在提取前挂 store，届时
    图片字节被收编、``asset_*`` 字段才被填。``image_data`` 无论走哪条路径都照填
    ——大量现有用例与 MCP ``extract_blocks`` 依赖它。
    """

    def attach_asset_store(self, store: "AssetStore") -> None:
        self._asset_store = store

    @property
    def asset_store(self) -> Optional["AssetStore"]:
        return getattr(self, "_asset_store", None)

    def _register_image_asset(
        self,
        block: Block,
        *,
        content: bytes,
        mime: str,
        bbox: Optional[list] = None,
        page: Optional[int] = None,
    ) -> None:
        """把一张图片的字节收进 AssetStore，并在 block 上补资源引用字段。

        未挂 store 或字节为空时是无操作（no-op）——这保证老入口行为不变。挂了 store
        时：字节按完整哈希去重（同图多次出现命中同一 asset_id），每次出现记一个 ref，
        ref 的轻量表示写进 ``block.metadata['asset_ref']``，asset_* 字段填到 block 上。
        image_data 由调用方保留，本方法不动它。
        """
        store = self.asset_store
        if store is None or not content:
            return
        ref = store.put(
            content,
            mime,
            caption=block.caption,
            page=page,
            bbox=bbox,
        )
        block.asset_id = ref.asset_id
        block.asset_mime = ref.mime
        metadata = block.metadata or {}
        metadata["asset_ref"] = ref.to_metadata()
        block.metadata = metadata


class InstrumentedProvider(SkipReporter, AssetRegistrar):
    """provider 的旁路产物收集基类：跳过记录 + 资源登记。

    provider 继承这一个类即可拿到两种收集能力；将来再多一种旁路产物（例如
    per-page 计时）就在这里追加一个 mixin，而不必去改每个 provider 的声明。
    """


@runtime_checkable
class BaseProvider(Protocol):
    """Protocol that all document providers must satisfy.

    Two methods are required:

    - ``extract(file_path)`` — reads from a filesystem path.
    - ``extract_from_bytes(file_bytes)`` — reads from raw bytes
      (used by the API upload handler).

    Both return an ordered list of :class:`Block` objects with
    sequential ``id`` values starting from 0.
    """

    def extract(self, file_path: str) -> List[Block]:
        """Extract blocks from a file on disk.

        Args:
            file_path: Path to the source document.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the file is unsupported, not found,
                or cannot be parsed.
        """
        ...

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        """Extract blocks from raw file bytes.

        Args:
            file_bytes: Raw content of the source document.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the bytes cannot be parsed.
        """
        ...
