"""DOCX 内嵌电子表格对象的识别、预算与来源装配（共享层）。

**为什么独立成模块**：内嵌对象的三件事——「这坨字节到底是什么格式」「本文档的累计
读取预算还剩多少」「这次出现的位置怎么记」——都不属于 DOCX 解析本身，也不属于 XLSX
解析本身。放进 ``docx_provider`` 会让它继续膨胀且与 XLSX 细节耦合；放进
``spreadsheet_provider`` 会让 XLSX 解析器反过来知道 DOCX 的 relationship / part 概念。
所以判定与账本住在这里，两侧各取所需，**不产生 docx ←→ xlsx 的相互依赖**。

本模块只依赖标准库与 ``spreadsheet_provider`` 的归档层公开函数（单向），不 import
``docx_provider``。
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.providers.spreadsheet_provider import (
    parse_ref_dimension,
    iter_sheet_xml_names,
)

# ── 对象解析状态（进 metadata.embedded_spreadsheet.status） ────────────────
#: 完整解析成功。
STATUS_PARSED = "parsed"
#: 读到了内容但不完整（截断 / 部分工作表失败 / 预算裁剪）。
STATUS_PARTIAL = "partial"
#: 认得出是嵌入对象但本期不支持该容器格式（如 OLE 复合文档 / 旧版 XLS）。
STATUS_UNSUPPORTED = "unsupported"
#: 外部链接对象（Type=Link）——只登记引用，不访问网络。
STATUS_EXTERNAL = "external"
#: 关系缺失、字节损坏或解析抛错。
STATUS_FAILED = "failed"

# ── 告警码（经 SkipReporter 进交付层 warnings） ───────────────────────────
WARN_MISSING = "docx_embedded_missing"
WARN_EXTERNAL = "docx_embedded_external"
WARN_UNSUPPORTED = "docx_embedded_unsupported"
WARN_FAILED = "docx_embedded_failed"
WARN_BUDGET = "docx_embedded_budget_exceeded"

# ── 格式判定结果 ─────────────────────────────────────────────────────────
#: 合法的 OOXML 电子表格包（zip 内含 xl/workbook.xml + 至少一张 worksheet）。
FORMAT_XLSX_PACKAGE = "xlsx_package"
#: OLE 复合文档（旧版 .xls / 通用 OLE 容器）。首期不支持，如实标注。
FORMAT_OLE_COMPOUND = "ole_compound"
#: 是 zip 但不是电子表格包（如内嵌的 .docx / .pptx）。
FORMAT_OTHER_PACKAGE = "other_package"
#: 认不出。
FORMAT_UNKNOWN = "unknown"

#: OLE 复合文档（CFBF）魔数。``.bin`` 内嵌对象通常是这个。
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
#: zip 本地文件头魔数。
_ZIP_MAGIC = b"PK\x03\x04"

#: 电子表格包的判别条目——只有工作簿部件在，才认为它真的是 XLSX。
_WORKBOOK_PART = "xl/workbook.xml"


# ── 文档级共享预算 ───────────────────────────────────────────────────────

#: 单文档内嵌对象数上限。样本是 20 个；留足余量但挡住构造出来的成千上万个。
DEFAULT_MAX_OBJECTS = 200
#: 单文档内嵌工作表总数上限。
DEFAULT_MAX_SHEETS = 400
#: 单文档所有内嵌包**累计**展开字节上限（内层 zip 炸弹的门）。
DEFAULT_MAX_EMBEDDED_EXPANDED_BYTES = 256 * 1024 * 1024
#: 单文档内嵌表格累计读取行数上限。
DEFAULT_MAX_EMBEDDED_ROWS = 50_000
#: 单文档内嵌表格累计读取单元格数上限。
DEFAULT_MAX_EMBEDDED_CELLS = 2_000_000
#: 单个内嵌包的条目数上限（条目爆炸门）。
DEFAULT_MAX_EMBEDDED_ENTRIES = 2048


@dataclass
class EmbeddedBudget:
    """一次 DOCX 解析内**所有**内嵌对象共享的读取预算。

    方案第 5 节的硬要求：预算是文档级的，「不得为每个工作簿重置总预算」。所以它是
    一个被传进去的账本对象，而不是每个 provider 自己算一遍——后者的表现是「每个附件
    都合法、加起来把内存吃光」。

    ``claim_*`` 在**读之前**问，``consume`` 在读之后记。第一次越限时把原因记在
    :attr:`breach` 上，调用方据此登记一条 ``docx_embedded_budget_exceeded`` 告警并
    对后续对象落占位——不是静默跳过。
    """

    max_objects: int = DEFAULT_MAX_OBJECTS
    max_sheets: int = DEFAULT_MAX_SHEETS
    max_expanded_bytes: int = DEFAULT_MAX_EMBEDDED_EXPANDED_BYTES
    max_rows: int = DEFAULT_MAX_EMBEDDED_ROWS
    max_cells: int = DEFAULT_MAX_EMBEDDED_CELLS
    max_entries_per_package: int = DEFAULT_MAX_EMBEDDED_ENTRIES

    objects_seen: int = 0
    sheets_read: int = 0
    expanded_bytes: int = 0
    rows_read: int = 0
    cells_read: int = 0
    breach: Optional[str] = None

    def claim_object(self) -> bool:
        """登记发现了一个对象；返回是否还在预算内。"""
        self.objects_seen += 1
        if self.max_objects > 0 and self.objects_seen > self.max_objects:
            self._breach(f"内嵌对象数超过上限 {self.max_objects}")
            return False
        return True

    def claim_package(self, expanded_bytes: int, entry_count: int) -> bool:
        """读取一个内嵌包之前问预算（展开体积累计 + 本包条目数）。"""
        if self.max_entries_per_package > 0 and entry_count > self.max_entries_per_package:
            self._breach(
                f"单个内嵌包条目数 {entry_count} 超过上限 {self.max_entries_per_package}"
            )
            return False
        if self.max_expanded_bytes > 0 and (
            self.expanded_bytes + expanded_bytes > self.max_expanded_bytes
        ):
            self._breach(
                f"内嵌包累计展开体积超过上限 {self.max_expanded_bytes // (1024 * 1024)} MiB"
            )
            return False
        self.expanded_bytes += expanded_bytes
        return True

    def claim_sheet(self) -> bool:
        """读取一张工作表之前问预算。"""
        if self.max_sheets > 0 and self.sheets_read >= self.max_sheets:
            self._breach(f"内嵌工作表总数超过上限 {self.max_sheets}")
            return False
        self.sheets_read += 1
        return True

    def rows_remaining(self) -> int:
        """还允许读多少行；``-1`` 表示不限。"""
        if self.max_rows <= 0:
            return -1
        return max(self.max_rows - self.rows_read, 0)

    def consume(self, rows: int, cells: int) -> None:
        """记账已读行数/单元格数，并在累计越限时记录 breach。"""
        self.rows_read += max(rows, 0)
        self.cells_read += max(cells, 0)
        if self.max_cells > 0 and self.cells_read > self.max_cells:
            self._breach(f"内嵌表格累计单元格数超过上限 {self.max_cells}")

    def _breach(self, reason: str) -> None:
        if self.breach is None:
            self.breach = reason

    def counters(self) -> Dict[str, int]:
        """观测计数（方案第 5 节：记录发现数/工作表数等，不记单元格正文）。"""
        return {
            "objects_seen": self.objects_seen,
            "sheets_read": self.sheets_read,
            "expanded_bytes": self.expanded_bytes,
            "rows_read": self.rows_read,
            "cells_read": self.cells_read,
        }


# ── 格式判定 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SniffResult:
    """字节层格式判定结果。``expanded_bytes`` / ``entry_count`` 供预算前置检查。"""

    format: str
    reason: str
    expanded_bytes: int = 0
    entry_count: int = 0


def sniff_embedded_format(blob: bytes) -> SniffResult:
    """判定内嵌对象字节的真实格式。

    **只信字节，不信扩展名 / ProgID / Content-Type**——方案 §3.1 明确后三者仅作提示：
    Word 会把 xlsx 存成 ``Microsoft_Excel_Worksheet1.xlsx``，也会把 OLE 容器存成
    ``oleObject1.bin``，而 ProgID 由写入方决定、可以和实际内容不一致。判错的代价是把
    OLE 二进制喂给 openpyxl 拿一个看不懂的异常，而不是一句「本期不支持」。

    ``expanded_bytes`` 从 zip 中央目录加总（不解压），供 :meth:`EmbeddedBudget.claim_package`
    在**真正读取前**过预算门。
    """
    if not blob:
        return SniffResult(FORMAT_UNKNOWN, "对象字节为空")
    if blob[:8] == _OLE_MAGIC:
        return SniffResult(FORMAT_OLE_COMPOUND, "OLE 复合文档（旧版 XLS / 通用 OLE 容器）")
    if blob[:4] != _ZIP_MAGIC:
        return SniffResult(FORMAT_UNKNOWN, "既不是 OOXML 压缩包也不是 OLE 复合文档")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = set(archive.namelist())
            infos = archive.infolist()
            expanded = sum(int(info.file_size) for info in infos)
            if _WORKBOOK_PART not in names:
                return SniffResult(
                    FORMAT_OTHER_PACKAGE,
                    "是 OOXML 压缩包但没有 xl/workbook.xml（不是电子表格）",
                    expanded,
                    len(infos),
                )
            if not iter_sheet_xml_names(archive):
                return SniffResult(
                    FORMAT_OTHER_PACKAGE,
                    "有工作簿部件但没有任何 worksheet XML",
                    expanded,
                    len(infos),
                )
            return SniffResult(
                FORMAT_XLSX_PACKAGE, "OOXML 电子表格包", expanded, len(infos)
            )
    except zipfile.BadZipFile as exc:
        return SniffResult(FORMAT_UNKNOWN, f"压缩包损坏：{exc}")


def estimate_package_cells(blob: bytes) -> int:
    """从内嵌包各 worksheet 的 ``<dimension>`` 估单元格数上界（不解析格子）。

    与 ``spreadsheet_provider.scan_archive_budget`` 同源口径：只用于预算估算，
    绝不作为真实已读范围上报（dimension 会因残留格式虚高）。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            total = 0
            for name in iter_sheet_xml_names(archive):
                with archive.open(name) as fh:
                    head = fh.read(4096)
                m = re.search(rb'<dimension\s+ref="([^"]+)"', head)
                if m:
                    rows, cols = parse_ref_dimension(
                        m.group(1).decode("ascii", "ignore")
                    )
                    total += rows * cols
            return total
    except (zipfile.BadZipFile, KeyError, OSError):
        return 0


# ── 对象出现位置与来源 ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SourcePosition:
    """对象在原文里的位置。

    ``element_index`` 是 body 级元素序号，``event_index`` 是段内事件序号——两者一起
    才能唯一定位「同一段里的第几个对象」。表格单元格内的对象另带 ``parent_table_*``
    与行列坐标，因为它的 body 级序号是外层表格的，靠它无法区分单元格。
    """

    element_index: int
    event_index: int
    parent_table_block_id: Optional[int] = None
    row: Optional[int] = None
    col: Optional[int] = None
    cell_event_index: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "element_index": self.element_index,
            "event_index": self.event_index,
        }
        if self.parent_table_block_id is not None:
            data["parent_table_block_id"] = self.parent_table_block_id
            data["row"] = self.row
            data["col"] = self.col
            data["cell_event_index"] = self.cell_event_index
        return data


@dataclass(frozen=True)
class EmbeddedObjectRef:
    """一次内嵌对象出现的完整身份。

    ``occurrence_id`` 是**出现位置**的稳定标识，``content_hash`` 是**内容**的标识：
    同一个附件在两处出现时 content_hash 相同（读取可去重）而 occurrence_id 不同
    （正文位置绝不去重）。方案 §3.2 明确要求这两件事分开。
    """

    occurrence_id: str
    owner_part: str
    relationship_id: str
    prog_id: str
    ole_type: str
    position: SourcePosition
    target_part: Optional[str] = None
    content_hash: Optional[str] = None


def content_hash(blob: bytes) -> str:
    """内嵌资源内容哈希（读取缓存键）。完整 sha256，不截断。"""
    return hashlib.sha256(blob).hexdigest()


def make_occurrence_id(owner_part: str, relationship_id: str, position: SourcePosition) -> str:
    """生成出现位置的稳定标识：同一文档同一版本重复解析必须得到同一个值。

    不用随机 UUID——那会让重复序列化/缓存命中后 id 漂移（B08 踩过的同一类坑）。
    """
    parts = [
        owner_part,
        relationship_id,
        str(position.element_index),
        str(position.event_index),
    ]
    if position.parent_table_block_id is not None:
        parts += [
            f"t{position.parent_table_block_id}",
            f"r{position.row}",
            f"c{position.col}",
            f"e{position.cell_event_index}",
        ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"emb-{digest}"


def build_provenance(
    ref: EmbeddedObjectRef,
    *,
    status: str,
    sheet_name: Optional[str] = None,
    sheet_index: Optional[int] = None,
    sheet_state: Optional[str] = None,
    source_range: Optional[Dict[str, int]] = None,
    read_range: Optional[Dict[str, int]] = None,
    row_indices: Optional[List[int]] = None,
    column_indices: Optional[List[int]] = None,
    read_truncated: bool = False,
    render_sampled: bool = False,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """装配 ``metadata.embedded_spreadsheet``（方案第 4 节的字段表）。

    ``read_truncated``（读取截断）与 ``render_sampled``（显示抽样）**必须分开**：
    内容已完整读入但 Markdown 只渲染前若干行，不是完整性失败；反之才是。方案第 5 节
    专门点了这一条，混在一起会让 partial 语义失真。
    """
    data: Dict[str, Any] = {
        "occurrence_id": ref.occurrence_id,
        "owner_part": ref.owner_part,
        "relationship_id": ref.relationship_id,
        "prog_id": ref.prog_id,
        "ole_type": ref.ole_type,
        "target_part": ref.target_part,
        "content_hash": ref.content_hash,
        "source_position": ref.position.as_dict(),
        "status": status,
        "read_truncated": bool(read_truncated),
        "render_sampled": bool(render_sampled),
    }
    if sheet_name is not None:
        data["sheet_name"] = sheet_name
    if sheet_index is not None:
        data["sheet_index"] = sheet_index
    if sheet_state is not None:
        data["sheet_state"] = sheet_state
    if source_range is not None:
        data["source_range"] = source_range
    if read_range is not None:
        data["read_range"] = read_range
    if row_indices is not None:
        data["row_indices"] = row_indices
    if column_indices is not None:
        data["column_indices"] = column_indices
    if detail:
        data["detail"] = detail
    return data


@dataclass
class EmbeddedReadCache:
    """按内容哈希缓存已解析结果：**只去重读取，不去重正文出现位置**。

    同一附件在文档里引用两次时，读一次字节、解析一次工作簿，但两处各自生成独立的
    Block 与 occurrence_id。方案 §3.2 的原话是「缓存只去重读取，不去重正文出现位置」。
    """

    _entries: Dict[str, Any] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get(self, digest: str) -> Optional[Any]:
        if digest in self._entries:
            self.hits += 1
            return self._entries[digest]
        self.misses += 1
        return None

    def put(self, digest: str, value: Any) -> None:
        self._entries[digest] = value
