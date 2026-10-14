"""统一资源仓库（P05）——在 Provider 边界一次性收编二进制资源字节。

**为什么存在**：图片此前在 provider 里就被编成 ``data:<mime>;base64,...`` 字符串塞进
``Block.image_data``，随后原样跟着进段落 Markdown、节点 content、全文、分章、PaperData
五份大字符串；导出环节到管道最末端才用正则把它们逐个
改写回相对路径，并**重复解码 + 重复 sha256 哈希**。原图 ``B`` 字节，base64 文本就是 ``4B/3``，
再乘上述五份表示与 JSON/HTML 包装，内存峰值随资源体量线性放大且反复解码。

AssetStore 把字节收在**一处**：内容按**完整 sha256** 去重，Block / inline atom 只持
``asset_id`` + mime + 尺寸 + caption + 来源。导出层（本包不触碰）拿 ``asset_id``
去 store 取一次字节，决定编成 data URI / 落相对文件 / 发受控 URL。

**资源去重 与 版面出现次数是两件事**（PDF 的核心缺陷即混淆二者，见 ``pdf_provider``）：
同一份字节只 ``put`` 一次（``asset_id`` 相同即命中已存记录，``ref_count`` 累加）；而它在文档里
出现几次，就有几个 :class:`AssetRef`（各自带 bbox/page/occurrence_index）。清理按 ``ref_count``
计数进行——一处引用被丢弃 ``release`` 一次，减到 0 才真正逐出字节，避免仍被别处引用的图被误删。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterator, Optional

# MIME→扩展名的**唯一**一张表。曾经这里和导出层各持一份，注释还写着
# 「两处若漂移会在导出文件名后缀上暴露」——方向（infrastructure 不依赖交付层）是对的，
# 结论错了：正确解法是表放共享层、让交付层来取，而不是复制一份。现在 packaging 从
# 这里重导出，一张表只有一处维护。
_MIME_TO_EXT: Dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/tif": "tiff",
    "image/svg+xml": "svg",
    "image/x-emf": "emf",
    "image/x-wmf": "wmf",
}


def mime_to_ext(mime: str) -> str:
    """MIME → 文件扩展名；未知类型回退 ``bin``，与 packaging 的行为一致。"""
    return _MIME_TO_EXT.get((mime or "").lower(), "bin")


@dataclass
class AssetRecord:
    """去重后的一份资源字节（仓库里唯一，可被多处引用）。

    ``asset_id`` 是内容的完整 sha256 十六进制。选完整哈希而非 packaging 里那种截断
    8 位：8 位十六进制只有 32bit，一篇几百张图的文档发生碰撞的概率虽小但非零，一旦
    碰撞两张不同的图会被去重成一张——这是内容损坏，不可接受。截断只发生在**导出
    文件名**这种人读场景，去重键必须用全长。
    """

    asset_id: str
    content: bytes
    mime: str
    size: int
    ref_count: int = 0

    @property
    def short_id(self) -> str:
        """导出文件名用的短哈希（前 8 位），与 packaging 的命名习惯对齐。"""
        return self.asset_id[:8]

    @property
    def ext(self) -> str:
        return mime_to_ext(self.mime)


@dataclass
class AssetRef:
    """资源在文档中的**一次**出现（occurrence）。

    同一 ``asset_id`` 可对应多个 AssetRef：PDF 里一个 xref 的字节只提取一次，但它在
    版面上出现几次（页眉 logo、章节重复插图）就有几个 AssetRef，各自 page/bbox 不同。
    ``occurrence_index`` 从 0 递增，用于在字节相同、位置不同的多次出现间稳定排序与去重。
    """

    asset_id: str
    mime: str
    occurrence_index: int = 0
    width: Optional[float] = None
    height: Optional[float] = None
    caption: Optional[str] = None
    page: Optional[int] = None
    bbox: Optional[list[float]] = None
    # 无法无损还原的资源（矢量图 / 透明 mask / 裁剪旋转）在此留提示，而不是静默丢弃。
    note: Optional[str] = None

    def to_metadata(self) -> dict:
        """写进 ``Block.metadata['asset_ref']`` 的轻量 JSON 表示（省略空字段）。"""
        data = {
            "asset_id": self.asset_id,
            "mime": self.mime,
            "occurrence_index": self.occurrence_index,
        }
        if self.width is not None:
            data["width"] = self.width
        if self.height is not None:
            data["height"] = self.height
        if self.caption:
            data["caption"] = self.caption
        if self.page is not None:
            data["page"] = self.page
        if self.bbox is not None:
            data["bbox"] = list(self.bbox)
        if self.note:
            data["note"] = self.note
        return data


class AssetStore:
    """按内容哈希去重的资源仓库，按引用计数回收。

    每次提取新建一个实例（与 provider 的 per-call 隔离一致，见 registry 模块说明），
    因此内部状态天然按次隔离，无需担心跨文档串字节。
    """

    def __init__(self) -> None:
        self._records: Dict[str, AssetRecord] = {}
        # 出现次数计数器：为每个 asset_id 分配单调递增的 occurrence_index，
        # 保证同资源多次出现拿到互不相同、可稳定排序的序号。
        self._occurrence_counter: Dict[str, int] = {}

    @staticmethod
    def compute_id(content: bytes) -> str:
        """资源内容的完整 sha256 十六进制——去重键。"""
        return hashlib.sha256(content).hexdigest()

    def put(
        self,
        content: bytes,
        mime: str,
        *,
        width: Optional[float] = None,
        height: Optional[float] = None,
        caption: Optional[str] = None,
        page: Optional[int] = None,
        bbox: Optional[list[float]] = None,
        note: Optional[str] = None,
    ) -> AssetRef:
        """收编一份资源字节，返回指向它的一次出现引用（AssetRef）。

        字节按完整哈希去重：内容相同就复用已有 AssetRecord（不重复存字节），只把
        ``ref_count`` +1 并分配一个新的 ``occurrence_index``——这正是「资源去重」与
        「出现次数」分离的落点。空字节直接拒绝（返回 None 由调用方降级为占位/跳过）。
        """
        if not content:
            raise ValueError("拒绝空资源字节：空内容不应进入资源仓库")

        asset_id = self.compute_id(content)
        record = self._records.get(asset_id)
        if record is None:
            record = AssetRecord(
                asset_id=asset_id,
                content=content,
                mime=(mime or "application/octet-stream").lower(),
                size=len(content),
            )
            self._records[asset_id] = record
        record.ref_count += 1

        occurrence_index = self._occurrence_counter.get(asset_id, 0)
        self._occurrence_counter[asset_id] = occurrence_index + 1

        return AssetRef(
            asset_id=asset_id,
            mime=record.mime,
            occurrence_index=occurrence_index,
            width=width,
            height=height,
            caption=caption,
            page=page,
            bbox=list(bbox) if bbox is not None else None,
            note=note,
        )

    def get(self, asset_id: str) -> Optional[AssetRecord]:
        """按 asset_id 取回资源记录；不存在返回 None。"""
        return self._records.get(asset_id)

    def get_bytes(self, asset_id: str) -> Optional[bytes]:
        record = self._records.get(asset_id)
        return record.content if record is not None else None

    def release(self, asset_id: str) -> bool:
        """撤销一处引用；引用计数减到 0 时逐出字节，返回是否已逐出。

        导出/清理阶段某处引用被丢弃时调用。减到 0 才真正删字节——否则同资源被
        多处引用时，丢掉其中一处会误删仍被别处需要的字节。
        """
        record = self._records.get(asset_id)
        if record is None:
            return False
        record.ref_count -= 1
        if record.ref_count <= 0:
            del self._records[asset_id]
            return True
        return False

    def prune(self, live_asset_ids: set[str]) -> int:
        """按存活引用集合清理：逐出所有不在集合中的资源，返回逐出数量。

        取消 / 过期清理的批量入口——调用方给出「仍被引用的 asset_id 集合」，仓库
        丢弃其余全部字节。用于一次性回收，而非逐处 release。
        """
        dead = [aid for aid in self._records if aid not in live_asset_ids]
        for asset_id in dead:
            del self._records[asset_id]
        return len(dead)

    def unique_count(self) -> int:
        """去重后的资源份数（== 实际占用的字节份数）。"""
        return len(self._records)

    def total_bytes(self) -> int:
        return sum(record.size for record in self._records.values())

    def records(self) -> Iterator[AssetRecord]:
        return iter(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, asset_id: str) -> bool:
        return asset_id in self._records
