"""块级磁盘仓库：正文落盘、按 id 偏移随机读（L01 第一遍的产物）。

方案 L01 把处理拆成两遍：第一遍提取并索引（blocks 写磁盘、图片进 AssetStore，内存
只留有界统计），第二遍只用紧凑特征与候选做结构决策，**正文依照最终区间按需读取**。
本模块是第一遍的落地物。

刻意不做的两件事，都是方案明确点过的坑：

1. **不把 ``List[Block]`` 换成 generator 就宣称流式完成。** 页眉页脚统计、全局正文
   字号、目录、跨页表格都必须跨页看，换成一次性生成器只会让这些统计拿不到数据。所以
   本模块提供的是**随机读**（按 block id 定位偏移），而不是单向流。
2. **小文档不强制落盘。** 为了流式让小文档多付一趟磁盘 I/O 是净亏。是否落盘由调用方
   按规模阈值决定，本模块只负责"要落就落得对"。

落盘格式是 JSONL + 内存偏移表：每行一个块的 JSON，偏移表存 ``block_id -> (offset,
length)``。选它而不是 pickle，是因为产物要能被人直接看、能被别的进程读，且不引入
反序列化任意对象的风险；选 JSONL 而不是单个大 JSON，是因为随机读只需 seek 一行，
不必解析整份。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from infrastructure.models import Block


@dataclass(frozen=True)
class BlockFeature:
    """决策阶段真正需要的**紧凑特征**：不含整段正文。

    为什么不是直接拿 ``Block``：全局排版分析、候选生成、分片路由、对齐与解码要的是
    「这块多长、什么字号、是否加粗、编号形状、在第几页」这类结构证据，而不是正文本身。
    把整篇正文大字符串留在内存里贯穿第二遍，是大文档峰值内存的主要来源。

    ``text_head`` / ``text_tail`` 是**有界**前后缀：结构判断（编号前缀、标题相似度、
    骨架压缩的头尾截断、snippet 交叉验证）只需要两端若干字符。``char_count`` 保留真实
    长度，所以"这块有多长"不会因为截断而失真——截断了但声称完整是本仓反复出现的坑。
    """

    block_id: int
    block_type: str
    char_count: int
    text_head: str
    text_tail: str
    font_size: Optional[float] = None
    is_bold: bool = False
    is_heading_style: bool = False
    heading_level: Optional[int] = None
    alignment: Optional[str] = None
    page: Optional[int] = None
    has_heading_numbering: bool = False
    truncated: bool = False

    @property
    def text_preview(self) -> str:
        """用于结构判断的可用文本。截断过就在中间放显式省略标记，不假装完整。"""
        if not self.truncated:
            return self.text_head
        return f"{self.text_head}…{self.text_tail}"


#: 特征里保留的正文头 / 尾字符数。够编号前缀、标题相似度、骨架头尾截断用。
FEATURE_HEAD_CHARS = 160
FEATURE_TAIL_CHARS = 80


#: 特征块允许保留的 metadata 键。atoms / 图片字节 / 源切片不得随预览浅拷贝常驻。
FEATURE_METADATA_KEYS = frozenset({
    "content_relations", "source_type", "source_format", "text_fidelity", "ocr_confidence", "semantic_role",
    "alignment", "page", "bbox", "char_count", "text_is_preview",
    "outline_level", "style_level", "style", "style_id", "numbering_level", "list_level",
    "layout_region_id", "layout_region", "region_id", "layout_column", "layout_band",
    "grid_like", "figure_like",
    "role", "artifact_role", "source_role", "in_table", "canonical",
    "run_in_prefix", "run_in_heading", "line_merge_uncertain", "reading_order_uncertain",
    "toc_destination", "toc_level", "atom_id", "structural_atom_id", "source",
    "heading_syntax", "code_lang", "asset_ref",
})


class OwnedBlocks(list):
    """Service-owned input whose contents may be replaced after durable spilling.

    Ordinary caller-owned lists remain untouched. Every alias of this list sees
    the compact blocks, so suspended outer frames cannot retain the full input.
    """


def compact_block(block: Block, feature: BlockFeature | None = None) -> Block:
    """决策阶段用的紧凑块：有界正文 + 显式字段，不持有 atoms / 完整 metadata。"""
    feature = feature or feature_of(block)
    src = block.metadata or {}
    metadata = {key: src[key] for key in FEATURE_METADATA_KEYS if key in src}
    metadata["char_count"] = feature.char_count
    if feature.truncated or block.image_data or block.table_data or block.md_structure:
        metadata["text_is_preview"] = True
    payload: dict = {
        "id": block.id,
        "type": block.type,
        "text": feature.text_preview,
        "font_size": block.font_size,
        "is_bold": block.is_bold,
        "is_heading_style": block.is_heading_style,
        "heading_level": block.heading_level,
        "alignment": block.alignment,
        "has_heading_numbering": block.has_heading_numbering,
        "caption": block.caption,
        "asset_id": block.asset_id,
        "asset_mime": block.asset_mime,
        "asset_width": block.asset_width,
        "asset_height": block.asset_height,
        "metadata": metadata,
    }
    return Block.model_validate(payload)


def feature_of(block: Block) -> BlockFeature:
    """从一个块抽出紧凑特征（不持有整段正文）。"""
    text = block.text or ""
    total = len(text)
    truncated = total > FEATURE_HEAD_CHARS + FEATURE_TAIL_CHARS
    metadata = block.metadata or {}
    return BlockFeature(
        block_id=block.id,
        block_type=block.type,
        char_count=total,
        text_head=text[:FEATURE_HEAD_CHARS] if truncated else text,
        text_tail=text[-FEATURE_TAIL_CHARS:] if truncated else "",
        font_size=block.font_size,
        is_bold=bool(block.is_bold),
        is_heading_style=bool(block.is_heading_style),
        heading_level=block.heading_level,
        alignment=metadata.get("alignment"),
        page=metadata.get("page"),
        has_heading_numbering=bool(getattr(block, "has_heading_numbering", False)),
        truncated=truncated,
    )


class BlockStore:
    """一次解析的块仓库：写入一次，之后按 block id 随机读。

    生命周期绑在调用方手里（``close()`` 或上下文管理器）；临时文件落在
    ``TMPDIR``（会话作用域，进程结束即回收），不写进用户仓库目录。
    """

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._dir = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="cst-blocks-"))
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "blocks.jsonl"
        self._offsets: dict[int, tuple[int, int]] = {}
        self._order: list[int] = []
        self._handle = None
        self._owns_dir = directory is None

    # ── 第一遍：写入 ────────────────────────────────────────────────

    def write(self, blocks: Iterable[Block]) -> "BlockStore":
        """把块逐条写盘并建偏移表。返回自身以便链式使用。"""
        with open(self._path, "wb") as sink:
            offset = 0
            for block in blocks:
                payload = block.model_dump_json().encode("utf-8") + b"\n"
                sink.write(payload)
                self._offsets[block.id] = (offset, len(payload))
                self._order.append(block.id)
                offset += len(payload)
        return self

    # ── 第二遍：按需随机读 ──────────────────────────────────────────

    def _reader(self):
        if self._handle is None:
            self._handle = open(self._path, "rb")
        return self._handle

    def load(self, block_id: int) -> Optional[Block]:
        """读回单个块（含完整正文）。未登记的 id 返回 ``None``。"""
        entry = self._offsets.get(block_id)
        if entry is None:
            return None
        offset, length = entry
        handle = self._reader()
        handle.seek(offset)
        raw = handle.read(length)
        return Block.model_validate_json(raw.decode("utf-8"))

    def load_many(self, block_ids: Sequence[int]) -> list[Block]:
        """按给定顺序读回多个块。按偏移升序 seek，减少来回跳。"""
        wanted = [bid for bid in block_ids if bid in self._offsets]
        by_offset = sorted(wanted, key=lambda bid: self._offsets[bid][0])
        loaded = {bid: self.load(bid) for bid in by_offset}
        return [loaded[bid] for bid in wanted if loaded.get(bid) is not None]

    def iter_blocks(self) -> Iterator[Block]:
        """按写入顺序顺序读回全部块（导出/装配用，不供决策阶段全篇扫）。"""
        for block_id in self._order:
            block = self.load(block_id)
            if block is not None:
                yield block

    # ── 观测与生命周期 ──────────────────────────────────────────────

    @property
    def block_ids(self) -> tuple[int, ...]:
        return tuple(self._order)

    @property
    def path(self) -> Path:
        return self._path

    def bytes_on_disk(self) -> int:
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    def dispose(self) -> None:
        """关闭并删除落盘产物（本仓库自建目录时才删目录）。"""
        self.close()
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass
        if self._owns_dir:
            try:
                os.rmdir(self._dir)
            except OSError:
                pass

    def __enter__(self) -> "BlockStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.dispose()


@dataclass
class FeatureView:
    """第二遍的输入：全篇紧凑特征 + 按需回读正文的句柄。

    这是「必须全局看的统计」与「可按需读取的内容」的分界线：特征常驻内存且有界，
    正文只在最终区间确定后经 :attr:`store` 回读。
    """

    features: tuple[BlockFeature, ...]
    store: BlockStore
    by_id: dict[int, BlockFeature] = field(default_factory=dict)

    @classmethod
    def build(cls, blocks: Sequence[Block], store: BlockStore) -> "FeatureView":
        features = tuple(feature_of(block) for block in blocks)
        return cls(features=features, store=store, by_id={f.block_id: f for f in features})

    def total_chars(self) -> int:
        """全篇真实字符数（用截断前的 char_count，不是特征前缀长度）。"""
        return sum(f.char_count for f in self.features)

    def resident_chars(self) -> int:
        """特征常驻的字符数——与 :meth:`total_chars` 的差就是"没留在内存里"的量。"""
        return sum(len(f.text_head) + len(f.text_tail) for f in self.features)
