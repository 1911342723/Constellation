"""Spreadsheet providers (CSV / XLSX) for the Constellation pipeline.

Both convert tabular files into Stage-1 Block objects, reusing the existing
table block lane (table_data + Markdown text):

- CsvProvider  -> a single table block (encoding + delimiter sniffing,
  zero third-party deps).
- XlsxProvider -> per worksheet a heading text block (the sheet name)
  followed by a table block, with merged-cell fill-down, formula value
  resolution and Excel number-format rendering.

Large-table policy (deliberately positioned for small/medium tables):
the renderer never emits an unusable mega-table. When a table exceeds the
render budget (or hits the read cap), the Markdown becomes a head+tail
*sample* with an explicit in-band warning and a column overview, while
``table_data.rows`` still carries the full rows that were read. This keeps
output consumable by an LLM and stops silent data loss.

Why two classes instead of one sniffing magic bytes: the API layer's
extract_from_bytes never sees the filename, so format selection has to
happen via the provider registry keyed by source_format.

B06 真流式（本次）：
    旧实现先 ``read_only=False`` 整簿载入、再把遍历限制为 5000 行——行列上限约束
    的是**输出**，保不住**载入内存峰值**。现改为：先用 ``read_only=True`` 只读维度
    探针（不物化单元格）为每张 sheet 选路，大表走 ``read_only=True + values_only``
    的流式通道、``iter_rows`` 直接带列上限（不先构造超宽行再切片）。read_only 模式
    拿不到合并单元格信息，故合并信息改为在**打开工作簿之前**用 ``zipfile`` 独立扫描
    ``xl/worksheets/sheetN.xml`` 的 ``<mergeCells>`` 段取得，再回填到流式行——既保住
    有界内存，又不丢合并语义。截断（行 / 列 / 采样）分别如实记录，杜绝「截断却声称
    完整」。

R05 资源预算前移（本次）：
    预算判断从「解码后」提到「解码前」。``extract_from_bytes`` 一进来就用 ``zipfile``
    读中央目录：压缩包展开总大小、条目数、以及从各 sheet XML 声明尺寸估出的单元格
    数——任一越过**硬**上限，在 openpyxl 动手前就抛可操作异常（``UploadTooLargeError``
    / 损坏包 ``ProviderError``），而不是「稍后重试」。**软**上限（单簿累计单元格超预算）
    不阻断，改为记一条跳过 → 交付层标 ``partial``，已读内容仍可下载。
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from app.core.exceptions import ProviderError, UploadTooLargeError
from infrastructure.models import Block, model_from_rows
from infrastructure.providers.base import InstrumentedProvider

try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised only without the optional dep
    openpyxl = None  # type: ignore[assignment]


# Read cap: bounds memory for one table. Rows beyond this are not loaded.
_DEFAULT_MAX_ROWS = 5000
_DEFAULT_MAX_COLS = 256
# Render budget: how many rows the Markdown view may contain before it is
# downgraded to a head+tail sample (keeps LLM token cost bounded).
_DEFAULT_MAX_RENDER_ROWS = 200
_DEFAULT_SAMPLE_TAIL = 20
# Sheets at/below this row count use the read_only=False path (merged-cell
# fill-down + number formats). Larger sheets go streaming to bound load peak.
_DEFAULT_SMALL_SHEET_ROWS = 2000
# Cross-sheet total row budget: one workbook's large sheets share this so
# several big tables cannot each consume max_rows and blow past memory/token.
_DEFAULT_TOTAL_ROW_BUDGET = 20000

# ── R05：解码前资源预算（在 openpyxl 物化任何东西之前强制）────────────────
#
# 这些是**硬**上限：越线即在打开工作簿前拒绝，把「一个恶意/超大 xlsx 撑爆内存」
# 挡在解码之外。xlsx 是 zip：中央目录声明了每个条目的展开大小，无需真正解压就能
# 估算展开体积（zip 炸弹前置门），单元格数从各 sheet XML 头部声明的 dimension /
# sharedStrings 计数估上界。软上限（累计单元格）见 XlsxProvider.__init__ 的
# cell_budget，它不阻断、只降级为 partial。
_DEFAULT_MAX_ARCHIVE_EXPANDED_BYTES = 512 * 1024 * 1024  # 512 MiB 展开上限
_DEFAULT_MAX_ARCHIVE_ENTRIES = 4096                       # 条目数上限（防条目爆炸）
_DEFAULT_MAX_TOTAL_CELLS = 50_000_000                     # 全簿单元格硬上限（≈内存/时间）
# 软上限：单簿累计**已读**单元格超过它 → 记 partial（不阻断，已读仍可用）。
_DEFAULT_CELL_BUDGET = 5_000_000

# openpyxl 的 XML 命名空间（spreadsheetml）。解析 sheet 尺寸 / 合并段用。
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
# workbook.xml 里 sheet -> r:id 的关系命名空间。
_NS_REL = {"r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_COL_RE = re.compile(r"^([A-Z]+)")

# Delimiter sniffing reads a generous head so multi-line records or sparse
# leading rows don't fool the detector.
_CSV_SNIFF_SAMPLE = 65536

_CSV_ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "utf-16",
    "gb18030",
    "gbk",
    "big5",
    "latin-1",
)

_CURRENCY_SYMBOLS = "¥$€£￥₩"
_DECIMALS_RE = re.compile(r"\.([0#]+)")


def _stringify(value: object) -> str:
    """Coerce a spreadsheet cell value into a clean display string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, datetime):
        has_time = bool(value.hour or value.minute or value.second or value.microsecond)
        return value.isoformat(sep=" ") if has_time else value.date().isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)


def _decimals_from_format(number_format: str) -> int:
    """Count the decimal-place digits declared in an Excel number format."""
    match = _DECIMALS_RE.search(number_format)
    return len(match.group(1)) if match else 0


def _currency_prefix(number_format: str) -> str:
    """Return the first bare currency symbol found in a number format, if any."""
    for ch in number_format:
        if ch in _CURRENCY_SYMBOLS:
            return ch
    return ""


def _apply_number_format(value: float, number_format: str) -> Optional[str]:
    """Render a numeric cell per a pragmatic subset of Excel number formats.

    Handles percent, thousands separators, a leading currency symbol and a
    fixed number of decimals. Returns ``None`` when the format carries no such
    signal, so the caller falls back to the plain :func:`_stringify` form.
    """
    if "%" in number_format:
        decimals = _decimals_from_format(number_format)
        return f"{value * 100:.{decimals}f}%"

    prefix = _currency_prefix(number_format)
    has_thousands = "#,#" in number_format or ",0" in number_format
    decimals = _decimals_from_format(number_format)

    if prefix or has_thousands:
        body = f"{value:,.{decimals}f}" if has_thousands else f"{value:.{decimals}f}"
        return f"{prefix}{body}"
    if decimals > 0 and ("0" in number_format or "#" in number_format):
        return f"{value:.{decimals}f}"
    return None


def _format_cell_value(value: object, number_format: Optional[str]) -> str:
    """Stringify a cell, applying Excel number formatting to plain numerics."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fmt = number_format or "General"
        if fmt != "General":
            formatted = _apply_number_format(float(value), fmt)
            if formatted is not None:
                return formatted
    return _stringify(value)


def _trim_empty_columns(rows: List[List[str]]) -> List[List[str]]:
    """Drop trailing all-empty columns; keep interior gaps (may be meaningful)."""
    if not rows:
        return rows
    width = max((len(r) for r in rows), default=0)
    last_nonempty = -1
    for c in range(width):
        if any(c < len(r) and r[c] for r in rows):
            last_nonempty = c
    if last_nonempty < 0:
        return []
    return [r[: last_nonempty + 1] for r in rows]


def _drop_blank_rows(rows: List[List[str]]) -> Tuple[List[List[str]], List[int]]:
    """丢掉整行皆空的行，并返回被保留行的**原始 0 基行号**。

    只丢行不记坐标，会让原文第 5 行在输出里变成第 3 行——之后任何按原始坐标回定位的
    事情（源文对照、合并区、公式引用、嵌入对象的 row_indices）都会静默错位。所以裁空白
    与坐标映射必须一起产出，而不是先裁再想办法猜回来。
    """
    kept: List[List[str]] = []
    indices: List[int] = []
    for index, row in enumerate(rows):
        if any(cell for cell in row):
            kept.append(row)
            indices.append(index)
    return kept, indices


def _rows_to_markdown(rows: List[List[str]]) -> str:
    """Render string rows as a GFM table via the pipeline's single entry point.

    Delegates to :meth:`Block.render_markdown_table` so spreadsheet output shares
    the same robustness as DOCX/PDF tables: max-width alignment, no truncation of
    over-wide rows, in-cell newline -> ``<br>`` (kept losslessly instead of being
    squashed to a space), and pipe escaping.
    """
    return Block.render_markdown_table(rows)


def _looks_numeric(text: str) -> bool:
    """Heuristic: does a cell string represent a number (after format strip)?"""
    t = text.strip().replace(",", "")
    for ch in _CURRENCY_SYMBOLS:
        t = t.replace(ch, "")
    t = t.rstrip("%")
    if not t:
        return False
    try:
        float(t)
        return True
    except ValueError:
        return False


def _column_overview(header: List[str], data: List[List[str]]) -> List[Dict[str, object]]:
    """Build a lightweight per-column profile (name / kind / non-empty count)."""
    width = max([len(header)] + [len(r) for r in data], default=0)
    columns: List[Dict[str, object]] = []
    for c in range(width):
        name = header[c] if c < len(header) and header[c] else f"col{c + 1}"
        nonempty = 0
        numeric = 0
        for row in data:
            if c < len(row) and row[c]:
                nonempty += 1
                if _looks_numeric(row[c]):
                    numeric += 1
        kind = "numeric" if nonempty and numeric >= nonempty * 0.8 else "text"
        columns.append({"name": name, "kind": kind, "nonempty": nonempty})
    return columns


def _overview_line(columns: List[Dict[str, object]]) -> str:
    parts = [f"{c['name']}({c['kind']})" for c in columns[:20]]
    suffix = "" if len(columns) <= 20 else f" …共 {len(columns)} 列"
    return "> 列概览：" + " | ".join(parts) + suffix


def _warning_line(
    total_rows: int,
    ncols: int,
    head_shown: int,
    tail_shown: int,
    read_truncated: bool,
    read_rows: int,
) -> str:
    approx = "约 " if read_truncated else ""
    tail_part = f" + 后 {tail_shown}" if tail_shown else ""
    base = (
        f"> ⚠️ 大表采样：共 {approx}{total_rows} 行 × {ncols} 列，"
        f"本视图仅展示前 {head_shown}{tail_part} 行（采样供 LLM 概览）。"
    )
    if read_truncated:
        base += f" 已超读取上限，仅载入前 {read_rows} 行，其余未读取。"
    else:
        base += " 完整数据见 table_data.rows。"
    return base


def _assemble_table_block(
    block_id: int,
    rows: List[List[str]],
    total_rows: int,
    read_truncated: bool,
    source_meta: Dict[str, object],
    max_render_rows: int,
    sample_tail: int,
    caption: Optional[str] = None,
) -> Block:
    """Build a table Block, downgrading oversized tables to a sampled view."""
    ncols = max((len(r) for r in rows), default=0)
    header = rows[0] if rows else []
    data = rows[1:]
    columns = _column_overview(header, data)
    # 列被截断也算"未完整"：即便行数没超，丢了列同样不能声称完整（B06）。
    read_truncated_cols = bool(source_meta.get("read_truncated_cols", False))
    needs_sampling = read_truncated or read_truncated_cols or len(rows) > max_render_rows

    if not needs_sampling:
        text = _rows_to_markdown(rows)
    else:
        head_n = max(max_render_rows - sample_tail, 1)
        head_data = data[:head_n]
        if sample_tail > 0 and len(data) > head_n + sample_tail:
            tail_data = data[-sample_tail:]
        else:
            tail_data = []
        shown = [header] + head_data + tail_data
        warning = _warning_line(
            total_rows, ncols, len(head_data), len(tail_data), read_truncated, len(rows)
        )
        if read_truncated_cols:
            warning += " 列数超上限，右侧列已截断（table_data.rows 亦不含被截列）。"
        text = f"{warning}\n{_overview_line(columns)}\n\n{_rows_to_markdown(shown)}"

    metadata = dict(source_meta)
    metadata.update(
        {
            "row_count": len(rows),
            "total_rows": total_rows,
            # truncated: 只要行或列被截断即为真，杜绝"截断却声称完整"。
            "truncated": bool(read_truncated or read_truncated_cols),
            "read_truncated_rows": read_truncated,
            "read_truncated_cols": read_truncated_cols,
            "sampled": needs_sampling,
            "render_sampled": needs_sampling,
            "columns": columns,
        }
    )
    # E04：全仓只有一种表格表示。XLSX 本来没有跨度信息（合并已在读取期 fill-down），
    # 所以模型里 span 全为 1——这是如实描述而不是占位；消费方不必区分"有模型的表"
    # 与"只有矩阵的表"。抽样与真实总行数如实进模型，抽样展示不得伪装成完整表。
    model = model_from_rows(
        rows,
        header_rows=1 if rows else 0,
        sampled=bool(len(rows) > max_render_rows),
        total_rows=total_rows,
    )
    return Block(
        id=block_id,
        type="table",
        text=text,
        table_data={"rows": rows, "model": model.model_dump()},
        caption=caption,
        metadata=metadata,
    )


@dataclass(frozen=True)
class EmbeddedSheet:
    """嵌入模式下读到的一张工作表。

    ``row_indices`` 是被保留行的**原始 0 基行号**：裁掉空行后靠它才能把「输出第 3 行」
    映射回「原表第 5 行」。``estimated_*`` 是上界估计（dimension 会因残留格式虚高），
    与 ``read_range`` 分开记录，杜绝「截断却声称完整」。
    """

    name: str
    index: int
    state: str
    rows: List[List[str]]
    total_rows: int
    truncated_rows: bool
    truncated_cols: bool
    estimated_rows: int
    estimated_cols: int
    row_indices: List[int]
    merged_ranges: List[Tuple[int, int, int, int]]


def source_range_report(
    est_rows: int, est_cols: int, read_rows: int, read_cols: int
) -> Dict[str, object]:
    """把「原始范围估计」报成可信的形态，估不出来时如实说未知。

    只读探针的 ``max_row`` 依赖工作簿里的 ``<dimension>`` 声明。真实样本里存在**根本没写
    dimension** 的内嵌工作簿（Word 另存的 Excel 对象就有），此时 openpyxl 只能回 1——把它
    当"原始行数上界"上报，就会出现「上界 1 < 实际读到 33 行」这种自相矛盾的数字，比不报
    更糟：下游据此判断「有没有被截断」会得到反的结论。

    所以估计值低于实际已读时一律标 ``known=False`` 并给出原因，不拿一个假的上界凑数。
    """
    known = est_rows >= read_rows and est_cols >= read_cols
    if known:
        return {"rows": est_rows, "cols": est_cols, "known": True}
    return {
        "rows": None,
        "cols": None,
        "known": False,
        "unknown_reason": "工作簿未声明 dimension，只读探针取不到可信的原始范围上界",
    }


def col_letters_to_index(letters: str) -> int:
    """把 Excel 列字母（A, B, …, AA）转成 0 基列号。仅解析尺寸/合并用。"""
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def parse_ref_dimension(ref: str) -> Tuple[int, int]:
    """从 ``<dimension ref="A1:C40">`` 的 ref 估出 (行数, 列数) 上界。

    这是**估计**：Excel 写的 dimension 可能因空白格 / 残留格式而虚高（B06 明列的坑），
    所以只用它做「大表 / 小表」选路与预算估算，绝不作为真实已读范围上报。
    """
    parts = ref.replace("$", "").split(":")
    max_r = max_c = 0
    for token in parts:
        m = _COL_RE.match(token.upper())
        if m:
            max_c = max(max_c, col_letters_to_index(m.group(1)) + 1)
        digits = re.findall(r"(\d+)", token)
        if digits:
            max_r = max(max_r, int(digits[-1]))
    return max_r, max_c


def iter_sheet_xml_names(archive: "zipfile.ZipFile") -> List[str]:
    """返回压缩包内所有 worksheet XML 路径（``xl/worksheets/sheetN.xml``）。"""
    return sorted(
        name
        for name in archive.namelist()
        if name.startswith("xl/worksheets/") and name.endswith(".xml")
    )


def scan_archive_budget(
    file_bytes: bytes,
    *,
    max_expanded_bytes: int,
    max_entries: int,
    max_total_cells: int,
) -> Tuple[int, int]:
    """R05：解码前扫描 xlsx（zip）中央目录，越硬上限即抛，返回 (展开字节, 估计单元格)。

    在 openpyxl 物化任何东西之前跑。xlsx 是 zip：中央目录声明了每个条目的 ``file_size``
    （展开后大小），无需真正解压就能加总——这正是 zip 炸弹的前置门。单元格数从各 sheet
    XML 的 ``<dimension>`` 声明估上界（行×列之和）。

    抛的都是**可操作**异常，不是「稍后重试」：
      * 展开体积 / 条目数 / 单元格数超硬上限 → ``UploadTooLargeError``（code=upload_too_large，
        文案说清是哪个预算、上限多少）。
      * 不是合法 zip / 缺 worksheet → ``ProviderError``（文件损坏，重试无用）。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            infos = archive.infolist()
            entry_count = len(infos)
            if entry_count > max_entries:
                raise UploadTooLargeError(
                    f"XLSX 压缩包条目数 {entry_count} 超过上限 {max_entries}；"
                    f"该文件可能被构造成条目爆炸，已在解码前拒绝。"
                )
            expanded = sum(int(info.file_size) for info in infos)
            if expanded > max_expanded_bytes:
                raise UploadTooLargeError(
                    f"XLSX 解压后体积约 {expanded // (1024 * 1024)} MiB 超过上限 "
                    f"{max_expanded_bytes // (1024 * 1024)} MiB；已在解码前拒绝（防内存耗尽）。"
                )
            sheet_names = iter_sheet_xml_names(archive)
            if not sheet_names:
                raise ProviderError("XLSX 文件损坏或缺少工作表（无 xl/worksheets）")
            est_cells = 0
            for name in sheet_names:
                # 只读 sheet XML 头部拿 <dimension>，不解析整表格子。
                try:
                    with archive.open(name) as fh:
                        head = fh.read(4096)
                except (KeyError, OSError, zipfile.BadZipFile) as exc:
                    raise ProviderError(f"XLSX 工作表 {name} 无法读取：{exc}") from exc
                m = re.search(rb'<dimension\s+ref="([^"]+)"', head)
                if m:
                    rows, cols = parse_ref_dimension(m.group(1).decode("ascii", "ignore"))
                    est_cells += rows * cols
            if est_cells > max_total_cells:
                raise UploadTooLargeError(
                    f"XLSX 估计单元格数约 {est_cells} 超过上限 {max_total_cells}；"
                    f"已在解码前拒绝。如需处理超大表，请按行区间分次导出。"
                )
            return expanded, est_cells
    except zipfile.BadZipFile as exc:
        raise ProviderError(f"XLSX 文件不是合法的压缩包（可能已损坏）：{exc}") from exc


def scan_merged_cells(file_bytes: bytes) -> Dict[str, List[Tuple[int, int, int, int]]]:
    """独立扫描各 sheet 的合并单元格区域（0 基 (min_r, min_c, max_r, max_c)）。

    read_only 流式模式下 openpyxl 拿不到合并信息，故在**打开工作簿之前**直接解析
    ``xl/worksheets/sheetN.xml`` 的 ``<mergeCells>``，按 sheet 显示名归类，供大表流式路径
    回填合并区的 anchor 值——既保住有界内存，又不丢合并语义（B06 要求）。

    映射 sheetN.xml → 显示名需要串起 workbook.xml（sheet name↔r:id）与
    workbook.xml.rels（r:id↔target 路径）。任一环缺失/损坏时**静默降级**为空（大表因此
    退化为不做 fill-down，并已在 metadata 用 read_truncated_cols/合并缺失如实标注），
    不因元数据扫描失败而让整份解析失败。
    """
    result: Dict[str, List[Tuple[int, int, int, int]]] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            names = set(archive.namelist())
            # 1) workbook.xml: 显示名 + r:id + sheetId 顺序
            try:
                wb_root = ET.fromstring(archive.read("xl/workbook.xml"))
            except (KeyError, ET.ParseError):
                return result
            # 2) rels: r:id -> worksheets/sheetN.xml
            rels: Dict[str, str] = {}
            if "xl/_rels/workbook.xml.rels" in names:
                try:
                    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                    for rel in rels_root:
                        rid = rel.get("Id")
                        target = rel.get("Target") or ""
                        if rid and target:
                            rels[rid] = target.lstrip("/")
                except ET.ParseError:
                    rels = {}
            sheets_el = wb_root.find("m:sheets", _NS)
            if sheets_el is None:
                return result
            order_idx = 0
            for sheet in sheets_el.findall("m:sheet", _NS):
                order_idx += 1
                display = sheet.get("name") or f"Sheet{order_idx}"
                rid = sheet.get(f"{{{_NS_REL['r']}}}id")
                target = rels.get(rid or "")
                # rels target 通常相对 xl/：worksheets/sheetN.xml。回退按顺序猜。
                if target:
                    xml_path = target if target.startswith("xl/") else f"xl/{target}"
                else:
                    xml_path = f"xl/worksheets/sheet{order_idx}.xml"
                if xml_path not in names:
                    continue
                try:
                    merged = _scan_sheet_merges(archive, xml_path)
                except (KeyError, ET.ParseError, OSError):
                    continue
                if merged:
                    result[display] = merged
    except (zipfile.BadZipFile, OSError):
        return {}
    return result


def _parse_merge_ref(ref: str) -> Optional[Tuple[int, int, int, int]]:
    """把 ``A1:C3`` 形态的合并区解析成 0 基 ``(min_r, min_c, max_r, max_c)``。"""
    ref = (ref or "").replace("$", "")
    if ":" not in ref:
        return None
    left, right = ref.split(":", 1)
    ma, mb = _COL_RE.match(left.upper()), _COL_RE.match(right.upper())
    ra = re.findall(r"(\d+)", left)
    rb = re.findall(r"(\d+)", right)
    if not (ma and mb and ra and rb):
        return None
    min_c = col_letters_to_index(ma.group(1))
    max_c = col_letters_to_index(mb.group(1))
    min_r = int(ra[-1]) - 1
    max_r = int(rb[-1]) - 1
    return (min(min_r, max_r), min(min_c, max_c), max(min_r, max_r), max(min_c, max_c))


def _scan_sheet_merges(
    archive: "zipfile.ZipFile", xml_path: str
) -> List[Tuple[int, int, int, int]]:
    """流式扫一张 worksheet 的 ``<mergeCells>``——**DOM 不整篇物化**。

    此前这里是 ``ET.fromstring(archive.read(xml_path))``：为了拿几个合并区，把整张
    sheet 的 XML 建成完整 DOM。实测一张 3000x5 的数值表（xlsx 压缩后仅 60 KiB）峰值
    10.2 MiB——放大 170 倍，而且这一步发生在**任何预算门之前**，等于 R05「预算约束的是
    载入峰值而不是输出」在此处被整条绕过：一份内嵌 20 个大表的 DOCX，光扫合并区就能
    把内存吃光，行数预算一行都还没读。

    ``mergeCells`` 在 OOXML 的元素顺序里排在 ``sheetData`` **之后**，所以没法提前退出，
    只能边解析边丢：``row`` / ``sheetData`` 一结束就 ``clear()``，峰值降到一行的量级。
    """
    merged: List[Tuple[int, int, int, int]] = []
    with archive.open(xml_path) as handle:
        for _event, elem in ET.iterparse(handle, events=("end",)):
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "mergeCell":
                parsed = _parse_merge_ref(elem.get("ref") or "")
                if parsed:
                    merged.append(parsed)
                elem.clear()
            elif tag in ("c", "row", "sheetData"):
                # sheetData 是这张 XML 的绝大部分体积，解析完立刻丢。
                elem.clear()
    return merged


class CsvProvider:
    """Convert a CSV file into a single Stage-1 table block."""

    def __init__(
        self,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_cols: int = _DEFAULT_MAX_COLS,
        max_render_rows: int = _DEFAULT_MAX_RENDER_ROWS,
        sample_tail: int = _DEFAULT_SAMPLE_TAIL,
    ) -> None:
        self.max_rows = max_rows
        self.max_cols = max_cols
        self.max_render_rows = max_render_rows
        self.sample_tail = sample_tail

    def extract(self, file_path: str) -> List[Block]:
        path = Path(file_path)
        if path.suffix.lower() != ".csv":
            raise ProviderError("Only .csv files are supported")
        return self.extract_from_bytes(path.read_bytes())

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        text = self._decode(file_bytes)
        all_rows = self._parse_rows(text)
        if not all_rows:
            return []

        total_rows = len(all_rows)
        read_truncated = total_rows > self.max_rows
        # 列截断如实上报，和 XLSX 路径一致：切掉列却声称完整同样是静默丢数据。
        # 用去掉尾部空列后的有效宽度判断，避免 "a,b,," 这种尾随空列误报截断。
        trimmed_all = _trim_empty_columns([[_stringify(c) for c in row] for row in all_rows])
        max_width = max((len(row) for row in trimmed_all), default=0)
        read_truncated_cols = max_width > self.max_cols
        kept = all_rows[: self.max_rows]
        kept = [[_stringify(c) for c in row[: self.max_cols]] for row in kept]
        kept = _trim_empty_columns(kept)
        if not kept:
            return []

        return [
            _assemble_table_block(
                0,
                kept,
                total_rows,
                read_truncated,
                {"source": "csv", "read_truncated_cols": read_truncated_cols},
                self.max_render_rows,
                self.sample_tail,
            )
        ]

    def _decode(self, file_bytes: bytes) -> str:
        for encoding in _CSV_ENCODINGS:
            try:
                return file_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ProviderError("Unable to decode CSV; use UTF-8, UTF-16, or GB encodings")

    def _parse_rows(self, text: str) -> List[List[str]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(
                normalized[:_CSV_SNIFF_SAMPLE], delimiters=",;\t|"
            )
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
        reader = csv.reader(io.StringIO(normalized), delimiter=delimiter)
        return [row for row in reader if any(cell.strip() for cell in row)]


class XlsxProvider(InstrumentedProvider):
    """Convert an XLSX workbook into per-sheet heading + table blocks."""

    def __init__(
        self,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_cols: int = _DEFAULT_MAX_COLS,
        max_render_rows: int = _DEFAULT_MAX_RENDER_ROWS,
        sample_tail: int = _DEFAULT_SAMPLE_TAIL,
        small_sheet_rows: int = _DEFAULT_SMALL_SHEET_ROWS,
        total_row_budget: int = _DEFAULT_TOTAL_ROW_BUDGET,
        max_archive_expanded_bytes: int = _DEFAULT_MAX_ARCHIVE_EXPANDED_BYTES,
        max_archive_entries: int = _DEFAULT_MAX_ARCHIVE_ENTRIES,
        max_total_cells: int = _DEFAULT_MAX_TOTAL_CELLS,
        cell_budget: int = _DEFAULT_CELL_BUDGET,
    ) -> None:
        self.max_rows = max_rows
        self.max_cols = max_cols
        self.max_render_rows = max_render_rows
        self.sample_tail = sample_tail
        # 探针行数 ≤ small_sheet_rows 且列数 ≤ max_cols 才走 read_only=False 的
        # 合并单元格路径；否则走流式，把载入峰值约束住。
        self.small_sheet_rows = small_sheet_rows
        # 跨 sheet 的总行预算：一簿多张大表共享，防止叠加吃爆内存/token。
        self.total_row_budget = total_row_budget
        # R05 硬上限：解码前用 zipfile 扫中央目录，越线即拒（可操作异常，非"稍后重试"）。
        self.max_archive_expanded_bytes = max_archive_expanded_bytes
        self.max_archive_entries = max_archive_entries
        self.max_total_cells = max_total_cells
        # R05 软上限：单簿累计**已读**单元格超它 → 记 partial（不阻断，已读仍可下载）。
        self.cell_budget = cell_budget

    def extract(self, file_path: str) -> List[Block]:
        path = Path(file_path)
        if path.suffix.lower() not in (".xlsx", ".xlsm"):
            raise ProviderError("Only .xlsx files are supported")
        return self.extract_from_bytes(path.read_bytes())

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        if openpyxl is None:
            raise ProviderError("请安装 openpyxl: pip install openpyxl")

        # R05：**解码前**资源预算。在 openpyxl 物化任何单元格之前，用 zipfile 扫中央
        # 目录：展开体积 / 条目数 / 估计单元格数任一越硬上限即抛可操作异常。旧实现是
        # 「解码后」才发现太大——那时内存峰值已经产生。损坏包也在这里被判成 ProviderError。
        self._scan_budget(file_bytes)

        # read_only 流式模式拿不到合并单元格，所以在打开工作簿前独立扫一遍合并区，
        # 供大表流式路径回填 anchor 值（B06：合并信息通过独立 XML 元数据扫描取得）。
        # 扫描失败静默降为空——大表退化为不做 fill-down 且如实标注，不让元数据扫描
        # 失败拖垮整份解析。
        merged_by_sheet = scan_merged_cells(file_bytes)

        # 先只读维度探针（read_only=True 不物化整簿），据此为每个 sheet 选路：
        # 小表走 read_only=False 拿合并单元格；大表走流式 read_only=True +
        # 有界 iter_rows，把行列上限约束在"载入峰值"而不是"输出"。旧实现先
        # read_only=False 整簿载入再截断，限制的是输出、保不住内存。
        try:
            dims = self._probe_dimensions(file_bytes)
        except Exception as exc:  # noqa: BLE001 - normalize into ProviderError
            raise ProviderError(f"无法解析 XLSX 文件: {exc}") from exc

        blocks: List[Block] = []
        block_id = 0
        # 跨 sheet 总预算：一簿多张大表不能各自吃满 max_rows 把内存/token 叠爆。
        # total_row_budget<=0 表示不设总预算（各表各自受 max_rows 约束）。
        row_budget = self.total_row_budget
        # R05 软上限：跨 sheet 累计**已读**单元格；超 cell_budget 记一次 partial（只记一次）。
        cells_read = 0
        cell_budget_reported = False

        for sheet_name, est_rows, est_cols, sheet_state in dims:
            blocks.append(
                Block(
                    id=block_id,
                    type="text",
                    text=sheet_name,
                    is_heading_style=True,
                    heading_level=1,
                    metadata={
                        "source": "xlsx",
                        "sheet": sheet_name,
                        "sheet_state": sheet_state,
                    },
                )
            )
            block_id += 1

            if self.total_row_budget > 0:
                sheet_row_cap = min(self.max_rows, max(row_budget, 0))
                budget_exhausted = row_budget <= 0
            else:
                # 未设总预算：每张表各自受 max_rows 约束。
                sheet_row_cap = self.max_rows
                budget_exhausted = False
            if budget_exhausted:
                sheet_row_cap = 0
            is_large = (
                est_rows > self.small_sheet_rows
                or est_cols > self.max_cols
                or sheet_row_cap < self.max_rows
            )
            # 单张 sheet 解析失败**只影响该表**：记一条跳过 → partial，其余 sheet 照常抽。
            # 旧实现在这里抛 ProviderError 会让「一张坏表」毁掉整份多 sheet 文档（R05：
            # 「某一页失败也得到准确状态、已保留内容仍可下载」）。
            try:
                if is_large:
                    (
                        rows,
                        tr_rows,
                        tr_cols,
                        total_rows,
                        est_orig_cols,
                        _row_indices,
                    ) = self._read_sheet_streaming(
                        file_bytes,
                        sheet_name,
                        sheet_row_cap,
                        merged_by_sheet.get(sheet_name, []),
                    )
                else:
                    (
                        rows,
                        tr_rows,
                        tr_cols,
                        total_rows,
                        est_orig_cols,
                        _row_indices,
                    ) = self._read_sheet_full(file_bytes, sheet_name, sheet_row_cap)
            except Exception as exc:  # noqa: BLE001 - 隔离到本 sheet，不毁整簿
                self._note_skip(
                    "xlsx_sheet_failed",
                    f"工作表「{sheet_name}」解析失败，已跳过该表；其余工作表不受影响。",
                    source_span={"sheet": sheet_name, "reason": str(exc)[:200]},
                    retryable=True,
                )
                continue

            if row_budget > 0:
                row_budget -= len(rows)
            cells_read += len(rows) * max((len(r) for r in rows), default=0)

            if not rows:
                # 空工作表：不装表块（一个没有内容的表格块比没有更糟），但要如实说一声。
                # 此前这里是静默 continue——读者只看到一个工作表名标题下什么都没有，
                # 分不清「表本来是空的」还是「我们读失败了」，而这两件事的处理完全不同。
                self._note_empty_sheet(sheet_name, embedded=False)
                continue

            blocks.append(
                _assemble_table_block(
                    block_id,
                    rows,
                    total_rows,
                    tr_rows,
                    {
                        "source": "xlsx",
                        "sheet": sheet_name,
                        "read_truncated_rows": tr_rows,
                        "read_truncated_cols": tr_cols,
                        # 实际已读范围 vs 原始范围估计，分开记录（原始为上界估计，
                        # 因为空白格/格式会虚高 max_row/max_column）。
                        "read_range": {"rows": len(rows), "cols": max((len(r) for r in rows), default=0)},
                        "estimated_source_range": source_range_report(
                            est_rows, est_orig_cols, len(rows),
                            max((len(r) for r in rows), default=0),
                        ),
                    },
                    self.max_render_rows,
                    self.sample_tail,
                    caption=sheet_name,
                )
            )
            # R05：B06 已经把「截断」如实记进 block metadata，但那只有翻 metadata
            # 的消费者看得见。截断意味着这份结果**不完整**，所以同时上报成一条
            # 跳过记录，让交付层把整个响应标成 partial 而不是 complete。
            if tr_rows or tr_cols:
                lost = "行" if tr_rows and not tr_cols else ("列" if tr_cols and not tr_rows else "行和列")
                self._note_skip(
                    "xlsx_sheet_truncated",
                    f"工作表「{sheet_name}」超出读取上限，{lost}已截断；完整数据需按行区间分次读取。",
                    source_span={
                        "sheet": sheet_name,
                        "read_range": {"rows": len(rows), "cols": max((len(r) for r in rows), default=0)},
                        "estimated_source_range": source_range_report(
                            est_rows, est_orig_cols, len(rows),
                            max((len(r) for r in rows), default=0),
                        ),
                    },
                )
            block_id += 1

        # R05 软上限：跨 sheet 累计已读单元格超 cell_budget → 记一次 partial（不阻断，
        # 已读内容照常交付）。与硬上限区别：硬上限在解码前拒绝整份，软上限只标注这份
        # 结果规模偏大、可能需要分次导出。
        if self.cell_budget > 0 and cells_read > self.cell_budget and not cell_budget_reported:
            self._note_skip(
                "xlsx_cell_budget_exceeded",
                f"本工作簿已读单元格约 {cells_read} 超过软预算 {self.cell_budget}；"
                f"结果已交付但规模偏大，建议按工作表 / 行区间分次导出。",
                source_span={"cells_read": cells_read, "cell_budget": self.cell_budget},
                retryable=False,
            )

        return blocks

    def read_embedded(self, file_bytes: bytes, *, budget) -> List["EmbeddedSheet"]:
        """嵌入模式读取：只回工作表数据，**不生成工作表名标题 Block**。

        与 :meth:`extract_from_bytes` 共用同一套读取核心（探针选路 → 流式/小表读取 →
        坐标映射 → 逐表容错），差别只在三处，且这三处正是方案 §3.3 要求的：

        1. **不产出 heading Block**。内嵌对象的工作表名往往是 ``Sheet1`` 这类随机名，
           提成一级标题会直接篡改文档章节结构；它只进 metadata / 局部 caption。
        2. **预算是文档级共享的**（``budget`` 参数）。一份 DOCX 里 20 个附件不能各自
           重置总预算——那正是「每个附件都合法、加起来把内存吃光」的来源。
        3. **章节归属由调用方的插入位置决定**，本方法不关心，也不碰标题层级。

        逐表隔离沿用标准路径：某张表读失败只记一条 skip 并跳过该表，已成功的表照常返回。
        """
        if openpyxl is None:
            raise ProviderError("请安装 openpyxl: pip install openpyxl")

        dims = self._probe_dimensions(file_bytes)
        merged_by_sheet = scan_merged_cells(file_bytes)
        out: List[EmbeddedSheet] = []

        for sheet_index, (sheet_name, est_rows, est_cols, sheet_state) in enumerate(dims):
            if not budget.claim_sheet():
                break
            remaining = budget.rows_remaining()
            sheet_row_cap = self.max_rows if remaining < 0 else min(self.max_rows, remaining)
            if sheet_row_cap <= 0:
                break
            is_large = (
                est_rows > self.small_sheet_rows
                or est_cols > self.max_cols
                or sheet_row_cap < self.max_rows
            )
            try:
                if is_large:
                    (
                        rows,
                        tr_rows,
                        tr_cols,
                        total_rows,
                        est_orig_cols,
                        row_indices,
                    ) = self._read_sheet_streaming(
                        file_bytes,
                        sheet_name,
                        sheet_row_cap,
                        merged_by_sheet.get(sheet_name, []),
                    )
                else:
                    (
                        rows,
                        tr_rows,
                        tr_cols,
                        total_rows,
                        est_orig_cols,
                        row_indices,
                    ) = self._read_sheet_full(file_bytes, sheet_name, sheet_row_cap)
            except Exception as exc:  # noqa: BLE001 - 隔离到本表，不毁整个对象
                self._note_skip(
                    "xlsx_sheet_failed",
                    f"内嵌工作簿的工作表「{sheet_name}」解析失败，已跳过该表；"
                    f"同一对象内其余工作表不受影响。",
                    source_span={"sheet": sheet_name, "reason": str(exc)[:200]},
                    retryable=True,
                )
                continue

            width = max((len(r) for r in rows), default=0)
            if not rows:
                # 空工作表不产表块。嵌入模式没有工作表名标题（见本方法第 1 条差别），
                # 一张空表若照常装配，就会在正文里留下一个**没有任何内容的表格块**——
                # 独立 XLSX 路径早就有 `if not rows: continue` 这道守卫，嵌入路径漏了。
                # 全部工作表都空时，调用方的 `if not sheets:` 分支会落「工作簿无可读内容」
                # 占位（partial），emptiness 不会被静默吞掉。
                self._note_empty_sheet(sheet_name, embedded=True)
                continue
            budget.consume(len(rows), len(rows) * width)
            out.append(
                EmbeddedSheet(
                    name=sheet_name,
                    index=sheet_index,
                    state=sheet_state,
                    rows=rows,
                    total_rows=total_rows,
                    truncated_rows=tr_rows,
                    truncated_cols=tr_cols,
                    estimated_rows=est_rows,
                    estimated_cols=est_orig_cols,
                    row_indices=row_indices,
                    merged_ranges=merged_by_sheet.get(sheet_name, []),
                )
            )
        return out

    def assemble_embedded_table_block(
        self,
        block_id: int,
        sheet: "EmbeddedSheet",
        *,
        extra_metadata: Dict[str, object],
    ) -> Block:
        """把一张内嵌工作表装成 table Block（复用标准表格装配与渲染）。

        ``caption`` 只在多工作表时给出工作表名——单表时工作表名没有信息量，反而会在
        正文里插一句噪声。``extra_metadata`` 由调用方带来源（``embedded_spreadsheet``），
        本方法不构造来源信息，避免 XLSX 解析器知道 DOCX 的 part / relationship 概念。
        """
        width = max((len(r) for r in sheet.rows), default=0)
        metadata: Dict[str, object] = {
            "source": "xlsx_embedded",
            "sheet": sheet.name,
            "sheet_state": sheet.state,
            "read_truncated_rows": sheet.truncated_rows,
            "read_truncated_cols": sheet.truncated_cols,
            "read_range": {"rows": len(sheet.rows), "cols": width},
            "estimated_source_range": source_range_report(
                sheet.estimated_rows, sheet.estimated_cols, len(sheet.rows), width
            ),
        }
        metadata.update(extra_metadata)
        return _assemble_table_block(
            block_id,
            sheet.rows,
            sheet.total_rows,
            sheet.truncated_rows,
            metadata,
            self.max_render_rows,
            self.sample_tail,
            caption=None,
        )

    def _note_empty_sheet(self, sheet_name: str, *, embedded: bool) -> None:
        """「这张工作表是空的」只有一处措辞与一处告警码。

        独立与嵌入两条读取路径都会遇到空表，两边各写一遍就会漂移（一边报、一边不报，
        或者码不一样让交付层要认两个）。``embedded`` 只影响文案里的定位说明。
        """
        where = "内嵌工作簿的" if embedded else ""
        self._note_skip(
            "xlsx_sheet_empty",
            f"{where}工作表「{sheet_name}」没有任何非空单元格，未产出表格块。",
            source_span={"sheet": sheet_name, "embedded": embedded},
            retryable=False,
        )

    def _scan_budget(self, file_bytes: bytes) -> None:
        """R05 解码前硬上限门。委托给模块级 :func:`scan_archive_budget`。"""
        scan_archive_budget(
            file_bytes,
            max_expanded_bytes=self.max_archive_expanded_bytes,
            max_entries=self.max_archive_entries,
            max_total_cells=self.max_total_cells,
        )

    def _probe_dimensions(self, file_bytes: bytes) -> List[Tuple[str, int, int, str]]:
        """只读探针：返回每 sheet 的 (名, 估计行数, 估计列数) 上界。

        用 read_only=True 打开，openpyxl 只解析 worksheet 尺寸而不物化单元格，
        这样"多大表"的判定不需要先把整簿载入内存。
        """
        wb = openpyxl.load_workbook(
            io.BytesIO(file_bytes), data_only=True, read_only=True
        )
        try:
            out: List[Tuple[str, int, int, str]] = []
            for ws in wb.worksheets:
                # sheet_state 一并带出：隐藏工作表默认仍然读取（静默丢内容比多读更糟），
                # 但必须如实标注隐藏状态，让消费方自己决定展示与否。
                state = str(getattr(ws, "sheet_state", "visible") or "visible")
                out.append(
                    (str(ws.title), int(ws.max_row or 0), int(ws.max_column or 0), state)
                )
            return out
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _read_sheet_streaming(
        self,
        file_bytes: bytes,
        sheet_name: str,
        row_cap: int,
        merged_ranges: List[Tuple[int, int, int, int]],
    ) -> Tuple[List[List[str]], bool, bool, int, int]:
        """大表流式读取：read_only=True + 有界 iter_rows，峰值受行列上限约束。

        合并单元格信息在 read_only 模式不可靠，故通过 :func:`scan_merged_cells` 在打开
        工作簿前独立扫 XML 得到 ``merged_ranges``（0 基 (min_r,min_c,max_r,max_c)），在此
        对**读取窗口内**的合并区做 anchor 值 fill-down——保住有界内存的同时不丢合并语义。
        超出行/列上限的合并区自然被裁掉（如实体现在 read_truncated_*）。
        """
        wb = openpyxl.load_workbook(
            io.BytesIO(file_bytes), data_only=True, read_only=True
        )
        try:
            ws = wb[sheet_name]
            est_cols = int(ws.max_column or 0)
            tr_cols = est_cols > self.max_cols
            values: List[List[object]] = []
            hit_cap = False
            # iter_rows 直接带列上限，避免先构造超宽行再切片。
            row_iter = ws.iter_rows(
                min_row=1, max_col=self.max_cols, values_only=True
            )
            for row_index, row in enumerate(row_iter):
                if row_cap >= 0 and row_index >= row_cap:
                    hit_cap = True
                    break
                values.append(list(row))
            # 合并区 fill-down：把 anchor（左上角）值铺满读取窗口内的合并范围。
            # 只在窗口内生效（越界的合并格已被行/列上限裁掉，属实截断，不补造）。
            if merged_ranges and values:
                for min_r, min_c, max_r, max_c in merged_ranges:
                    if min_r >= len(values) or min_c >= len(values[min_r]):
                        continue
                    anchor = values[min_r][min_c]
                    for rr in range(min_r, min(max_r + 1, len(values))):
                        for cc in range(min_c, min(max_c + 1, len(values[rr]))):
                            values[rr][cc] = anchor
            str_rows = [[_stringify(c) for c in row] for row in values]
            str_rows, row_indices = _drop_blank_rows(str_rows)
            str_rows = _trim_empty_columns(str_rows)
            total_rows = (
                max(int(ws.max_row or 0), len(str_rows)) if hit_cap else len(str_rows)
            )
            return str_rows, hit_cap, tr_cols, total_rows, est_cols, row_indices
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _read_sheet_full(
        self, file_bytes: bytes, sheet_name: str, row_cap: int
    ) -> Tuple[List[List[str]], bool, bool, int, int]:
        """小表读取：read_only=False 拿合并单元格 fill-down 与 number_format。

        仅用于探针判定为小表的情形，规模受 small_sheet_rows / max_cols 双重把关。
        """
        wb = openpyxl.load_workbook(
            io.BytesIO(file_bytes), data_only=True, read_only=False
        )
        # 公式无缓存值处理：data_only=True 时，「有公式但工作簿未缓存计算结果」的格子
        # 返回 None，与真正的空格无从区分——直接输出会得到一个空格却声称完整（静默丢
        # 数据）。所以对小表再开一份 data_only=False 拿公式源文本作 overlay：某格 data-only
        # 为 None 而其公式形态是以 '=' 开头的字符串时，用公式源顶上（如 ``=SUM(A1:A3)``），
        # 并让调用方记一条 partial。小表才做这第二遍（规模已被 small_sheet_rows 把关）。
        formula_overlay: Dict[Tuple[int, int], str] = {}
        try:
            wb_f = openpyxl.load_workbook(
                io.BytesIO(file_bytes), data_only=False, read_only=False
            )
            try:
                ws_f = wb_f[sheet_name]
                for r_idx, row in enumerate(
                    ws_f.iter_rows(min_row=1, max_col=self.max_cols)
                ):
                    if row_cap >= 0 and r_idx >= row_cap:
                        break
                    for c_idx, cell in enumerate(row):
                        v = cell.value
                        if isinstance(v, str) and v.startswith("="):
                            formula_overlay[(r_idx, c_idx)] = v
            finally:
                try:
                    wb_f.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - 第二遍失败不影响主读取
            formula_overlay = {}
        try:
            ws = wb[sheet_name]
            (
                rows,
                tr_rows,
                tr_cols,
                total_rows,
                est_cols,
                uncached,
                row_indices,
            ) = self._sheet_to_rows(ws, row_cap, formula_overlay)
            if uncached:
                self._note_skip(
                    "xlsx_formula_no_cached_value",
                    f"工作表「{sheet_name}」有 {uncached} 个公式单元格没有缓存计算结果，"
                    f"已用公式源文本占位（非计算值）；如需数值请在 Excel 中重算后另存。",
                    source_span={"sheet": sheet_name, "formula_cells": uncached},
                    retryable=False,
                )
            return rows, tr_rows, tr_cols, total_rows, est_cols, row_indices
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _sheet_to_rows(
        self,
        worksheet,
        row_cap: int,
        formula_overlay: Optional[Dict[Tuple[int, int], str]] = None,
    ) -> Tuple[List[List[str]], bool, bool, int, int, int]:
        formula_overlay = formula_overlay or {}
        try:
            merged_ranges = list(worksheet.merged_cells.ranges)
        except Exception:  # noqa: BLE001 - some workbooks expose no merge info
            merged_ranges = []

        est_cols = int(worksheet.max_column or 0)
        # 列截断如实上报：max_cols 小于实际列数时第三列会消失，旧实现却让
        # metadata 仍报 truncated=false（B06 复现的静默丢列）。
        tr_cols = est_cols > self.max_cols

        values: List[List[object]] = []
        formats: List[List[Optional[str]]] = []
        hit_cap = False
        uncached_formulas = 0
        # iter_rows 带列上限，避免先构造超宽行再切片。
        for row_index, row in enumerate(
            worksheet.iter_rows(min_row=1, max_col=self.max_cols)
        ):
            if row_cap >= 0 and row_index >= row_cap:
                hit_cap = True
                break
            row_values: List[object] = []
            row_formats: List[Optional[str]] = []
            for col_index, cell in enumerate(row):
                value = cell.value
                # 公式无缓存值：data-only 为 None 但该格是公式 → 用公式源占位，计数上报。
                if value is None and (row_index, col_index) in formula_overlay:
                    value = formula_overlay[(row_index, col_index)]
                    uncached_formulas += 1
                row_values.append(value)
                row_formats.append(cell.number_format)
            values.append(row_values)
            formats.append(row_formats)
        if not values:
            return [], False, tr_cols, 0, est_cols, uncached_formulas, []

        self._apply_merged_cells(merged_ranges, values, formats)
        str_rows = [
            [_format_cell_value(values[r][c], formats[r][c]) for c in range(len(values[r]))]
            for r in range(len(values))
        ]
        str_rows, row_indices = _drop_blank_rows(str_rows)
        str_rows = _trim_empty_columns(str_rows)

        if hit_cap:
            # max_row is an upper-bound estimate (may include trailing blanks).
            total_rows = max(worksheet.max_row or len(str_rows), len(str_rows) + 1)
        else:
            total_rows = len(str_rows)
        return (
            str_rows,
            hit_cap,
            tr_cols,
            total_rows,
            est_cols,
            uncached_formulas,
            row_indices,
        )

    def _apply_merged_cells(
        self,
        merged_ranges,
        values: List[List[object]],
        formats: List[List[Optional[str]]],
    ) -> None:
        """Propagate each merged region's top-left value AND format.

        openpyxl only stores a merged value in the anchor cell; the rest read
        as None. Without fill-down the Markdown table would be misaligned. The
        anchor's number format is copied too, so a merged numeric/percent cell
        renders consistently across the region.
        """
        for rng in merged_ranges:
            min_r, min_c = rng.min_row - 1, rng.min_col - 1
            max_r, max_c = rng.max_row - 1, rng.max_col - 1
            if min_r < 0 or min_c < 0 or min_r >= len(values):
                continue
            if min_c >= len(values[min_r]):
                continue
            anchor_value = values[min_r][min_c]
            anchor_format = formats[min_r][min_c]
            for rr in range(min_r, min(max_r + 1, len(values))):
                for cc in range(min_c, min(max_c + 1, len(values[rr]))):
                    values[rr][cc] = anchor_value
                    formats[rr][cc] = anchor_format
