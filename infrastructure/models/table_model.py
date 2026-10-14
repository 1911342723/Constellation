"""表格的**逻辑单元格模型**（方案 E04）。

为什么需要它：``table_data['rows']`` 是一个扁平字符串矩阵，它足以渲染 GFM，但表达不了
合并单元格、多行表头、单元格内嵌内容与来源 bbox。方案 E04 的原话是「Markdown 不是合并
单元格、复杂表头和嵌套内容的完整表示」——所以 GFM 只能是**一个兼容视图**，不能是唯一
表示；精细排版、跨页续表判断、单元格级定位都要落在逻辑模型上。

三条刻意的边界：

1. **GFM 是派生视图，不是真相。** :meth:`TableModel.to_rows` 把逻辑格展开成矩阵供 GFM
   使用；合并格在展开时按覆盖位置重复 display 值（GFM 无合并语法），但 ``row_span`` /
   ``col_span`` 仍在模型里，任何需要真实跨度的消费方读模型而不是读矩阵。**不能反过来**
   从矩阵猜跨度——那是把降级视图当权威。
2. **抽样展示不得伪装成完整表。** 超大表以行分页提供完整数据；``sampled`` /
   ``total_rows`` 如实记录，渲染层据此标注。这是本仓反复出现的坑（XLSX 曾截断却报
   ``truncated=false``）。
3. **跨页续表要多信号一致才接续。** 只凭"上下相邻"就把两张同构独立表合成一张，是把
   两份数据搅在一起；:class:`TableModel` 只承载 ``continuation_of`` 这个**结论**与判定
   依据，判定本身由探测方给出。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class LogicalCell(BaseModel):
    """一个逻辑单元格（起始格）。被合并覆盖的位置不单独成格。"""

    row: int = Field(ge=0, description="起始行（0 基，逻辑行）")
    col: int = Field(ge=0, description="起始列（0 基，逻辑列）")
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    #: 原始值：源文件里那一格写的东西（未经渲染/格式化）。
    raw_value: str = ""
    #: 展示值：应用了数字格式 / 合并 fill-down 之后用于呈现的形态。
    display_value: str = ""
    #: 是否属于表头区域（多行表头时可有多行为 True）。
    is_header: bool = False
    #: 来源 bbox（PDF 有；DOCX 无分页信息时为 None，不编造）。
    bbox: Optional[List[float]] = None
    #: 单元格内的块 id 列表（图片 / 公式 / 嵌套表都在这里，不塞进 display_value）。
    block_ids: List[int] = Field(default_factory=list)

    def covers(self) -> List[tuple[int, int]]:
        """该格覆盖的全部网格位置（含起始位置）。"""
        return [
            (self.row + dr, self.col + dc)
            for dr in range(self.row_span)
            for dc in range(self.col_span)
        ]


class TableModel(BaseModel):
    """一张表的逻辑表示。GFM 矩阵由 :meth:`to_rows` 派生。"""

    n_rows: int = Field(ge=0)
    n_cols: int = Field(ge=0)
    cells: List[LogicalCell] = Field(default_factory=list)
    #: 表头占前几行（0 表示没有识别出表头区域）。
    header_rows: int = Field(default=0, ge=0)
    #: 页码（PDF 有；其它来源为 None）。
    page: Optional[int] = None
    #: 整表 bbox（PDF 有）。
    bbox: Optional[List[float]] = None
    #: 展示是否被抽样，以及真实总行数——抽样展示不得伪装成完整表。
    sampled: bool = False
    total_rows: Optional[int] = None
    #: 跨页续表：本表接续的上一张表标识（None 表示自身是首表）。
    continuation_of: Optional[str] = None
    #: 判定续表所依据的信号（列位置 / 列数 / 表头重复 / 相邻页），供审计与回归。
    continuation_signals: Dict[str, Any] = Field(default_factory=dict)
    #: 稳定标识：同一文档同一版本重复序列化必须一致（不用随机 UUID，B08 踩过）。
    table_id: Optional[str] = None

    def cell_at(self, row: int, col: int) -> Optional[LogicalCell]:
        """取覆盖 ``(row, col)`` 的逻辑格（含被合并覆盖的位置）。"""
        for cell in self.cells:
            if (
                cell.row <= row < cell.row + cell.row_span
                and cell.col <= col < cell.col + cell.col_span
            ):
                return cell
        return None

    def to_rows(self) -> List[List[str]]:
        """展开成字符串矩阵——**GFM 兼容视图**，不是真相。

        合并格在覆盖位置重复 display 值（GFM 没有合并语法）。真实跨度仍在 ``cells``
        里，需要跨度的消费方读模型；从矩阵反推跨度是把降级视图当权威。
        """
        grid = [["" for _ in range(self.n_cols)] for _ in range(self.n_rows)]
        for cell in self.cells:
            value = cell.display_value or cell.raw_value
            for row, col in cell.covers():
                if 0 <= row < self.n_rows and 0 <= col < self.n_cols:
                    grid[row][col] = value
        return grid

    def header_signature(self) -> tuple[str, ...]:
        """表头文字签名，供跨页续表比对「表头是否重复出现」。"""
        if self.header_rows <= 0 or self.n_cols <= 0:
            return ()
        rows = self.to_rows()
        if not rows:
            return ()
        return tuple(cell.strip() for cell in rows[0])

    def column_signature(self) -> tuple[int, ...]:
        """列几何签名（PDF 有 bbox 时按列左边界取整），供跨页续表比对列位置。"""
        lefts: Dict[int, float] = {}
        for cell in self.cells:
            if cell.bbox and cell.col not in lefts:
                lefts[cell.col] = float(cell.bbox[0])
        if not lefts:
            return ()
        return tuple(int(round(lefts[col])) for col in sorted(lefts))

    def coverage_is_exact(self) -> bool:
        """校验逻辑格恰好覆盖网格一次：既无重叠也无空洞。

        这是「区间覆盖既无遗漏也无重复」在表格维度的同一口径。合并关系算错时它会红，
        而只看渲染结果是看不出来的（重复的文字看起来像正常的合并显示）。
        """
        seen: set[tuple[int, int]] = set()
        for cell in self.cells:
            for position in cell.covers():
                if position in seen:
                    return False
                seen.add(position)
        expected = {
            (row, col) for row in range(self.n_rows) for col in range(self.n_cols)
        }
        return seen == expected


def model_from_rows(
    rows: List[List[str]],
    *,
    header_rows: int = 1,
    page: Optional[int] = None,
    sampled: bool = False,
    total_rows: Optional[int] = None,
) -> TableModel:
    """从扁平矩阵建一个**无合并**的逻辑模型（每格 span 均为 1）。

    给本来就没有跨度信息的来源（XLSX、PDF 的 lines 策略结果）用：这样全仓只有一种表格
    表示，消费方不必区分"有模型的表"和"只有矩阵的表"。span 全为 1 是如实描述，不是占位。
    """
    n_rows = len(rows)
    n_cols = max((len(row) for row in rows), default=0)
    cells: List[LogicalCell] = []
    for row_index, row in enumerate(rows):
        for col_index in range(n_cols):
            value = row[col_index] if col_index < len(row) else ""
            cells.append(LogicalCell(
                row=row_index,
                col=col_index,
                raw_value=value,
                display_value=value,
                is_header=row_index < header_rows,
            ))
    return TableModel(
        n_rows=n_rows,
        n_cols=n_cols,
        cells=cells,
        header_rows=min(header_rows, n_rows),
        page=page,
        sampled=sampled,
        total_rows=total_rows if total_rows is not None else n_rows,
    )
