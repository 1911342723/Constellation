"""跨页续表探测与无框表探测门控（方案 E04）。

两件事都遵循同一条纪律：**多信号一致才下结论，宁可不接也不错接**。

跨页续表：只凭"上下相邻"就把两张同构独立表合成一张，等于把两份数据搅在一起。方案给的
信号是列位置、列数、表头是否重复、相邻页边界与标题编号；本模块要求其中若干条同时成立
才判续表，并把**判定依据一并记录**（``continuation_signals``），使结论可审计、可回归。

无框表：``find_tables`` 的默认 lines 策略依赖矢量路径（边框线/填充矩形），无矢量的页
直接跳过是既有的性能优化，方案明确「不应无条件移除」。所以这里只提供一个**门控**：
仅当页面上检测到对齐列 + 一致空白结构时，才建议启用额外识别通道。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from infrastructure.models.table_model import TableModel

#: 列左边界差异容忍（PDF 点）。同一张表跨页时列位置通常几乎不变。
COLUMN_TOLERANCE_PT = 6.0
#: 判定续表所需的最少一致信号数。列数一致是必要项，其余信号里还要再中若干条。
MIN_CONTINUATION_SIGNALS = 2


@dataclass(frozen=True)
class ContinuationVerdict:
    """续表判定结论 + 依据。``is_continuation`` 为假时 ``signals`` 说明差在哪。"""

    is_continuation: bool
    signals: Dict[str, Any]


def _columns_align(previous: TableModel, current: TableModel) -> Optional[bool]:
    """列几何是否对齐。两侧都没有 bbox 时返回 ``None``（无从判断，不算证据）。"""
    left = previous.column_signature()
    right = current.column_signature()
    if not left or not right:
        return None
    if len(left) != len(right):
        return False
    return all(abs(a - b) <= COLUMN_TOLERANCE_PT for a, b in zip(left, right))


def judge_continuation(
    previous: TableModel,
    current: TableModel,
    *,
    previous_page: Optional[int] = None,
    current_page: Optional[int] = None,
) -> ContinuationVerdict:
    """判断 ``current`` 是否是 ``previous`` 的跨页续表。

    必要条件（不满足直接否）：
      * 列数相同——列数都不一样的两张表不可能是同一张；
      * 页码相邻（有页码时）——隔页的两张表不接续。

    在此之上还需至少 :data:`MIN_CONTINUATION_SIGNALS` 条正向信号：
      * ``columns_align``：列左边界几乎不变；
      * ``header_repeated``：续页重复了表头（Word/排版常见做法）；
      * ``header_absent``：续页**没有**表头行，直接接着数据行——这也是续表的强信号，
        与 ``header_repeated`` 互斥但同样有效；
      * ``adjacent_pages``：页码严格相邻。

    刻意不把"标题编号连续"算进来：那需要表外上下文，属调用方职责；此处只用表自身的
    几何与结构证据，避免把一个需要语义判断的信号混进确定性判定里。
    """
    signals: Dict[str, Any] = {}
    page_a = previous_page if previous_page is not None else previous.page
    page_b = current_page if current_page is not None else current.page

    same_cols = previous.n_cols == current.n_cols and previous.n_cols > 0
    signals["same_column_count"] = same_cols
    if not same_cols:
        return ContinuationVerdict(False, signals)

    if page_a is not None and page_b is not None:
        adjacent = page_b == page_a + 1
        signals["adjacent_pages"] = adjacent
        if not adjacent:
            return ContinuationVerdict(False, signals)
    else:
        signals["adjacent_pages"] = None

    aligned = _columns_align(previous, current)
    signals["columns_align"] = aligned

    previous_header = previous.header_signature()
    current_first_row = current.to_rows()[0] if current.n_rows else []
    current_first = tuple(cell.strip() for cell in current_first_row)
    header_repeated = bool(previous_header) and previous_header == current_first
    signals["header_repeated"] = header_repeated
    header_absent = bool(previous_header) and current.header_rows == 0
    signals["header_absent"] = header_absent

    positive = sum(
        1 for key in ("columns_align", "header_repeated", "header_absent", "adjacent_pages")
        if signals.get(key) is True
    )
    signals["positive_signal_count"] = positive
    return ContinuationVerdict(positive >= MIN_CONTINUATION_SIGNALS, signals)


def link_continuations(models: Sequence[TableModel]) -> List[TableModel]:
    """按顺序给一串表打续表关系（就地写 ``continuation_of`` 与判定依据）。

    只在**相邻**两张表之间判断：跨过中间一张无关表去接续，方案没有依据支持。
    ``table_id`` 缺失时用「页码:序号」兜底，保证同文档同版本重复解析得到同一标识
    （不用随机 UUID——B08 踩过重复序列化 id 漂移的坑）。
    """
    out = list(models)
    for index, model in enumerate(out):
        if model.table_id is None:
            model.table_id = f"table:{model.page if model.page is not None else 0}:{index}"
    for index in range(1, len(out)):
        verdict = judge_continuation(out[index - 1], out[index])
        out[index].continuation_signals = verdict.signals
        out[index].continuation_of = out[index - 1].table_id if verdict.is_continuation else None
    return out


# ── 无框表探测门控 ────────────────────────────────────────────────────────


#: 判定"存在对齐列"所需的最少行数与最少列数。太少的样本谈不上"对齐"。
MIN_ALIGNED_ROWS = 3
MIN_ALIGNED_COLS = 2
#: 同一列的 x 起点允许的抖动（PDF 点）。
ALIGN_TOLERANCE_PT = 3.0


def suggests_borderless_table(
    line_x_starts: Sequence[Sequence[float]],
    *,
    min_rows: int = MIN_ALIGNED_ROWS,
    min_cols: int = MIN_ALIGNED_COLS,
    tolerance: float = ALIGN_TOLERANCE_PT,
) -> bool:
    """页面上是否出现了"对齐列 + 一致空白结构"，值得启用额外的无框表识别。

    入参是若干行、每行内各文本片段的 x 起点。判定条件刻意保守：至少 ``min_rows`` 行、
    每行至少 ``min_cols`` 个片段，且各行的第 k 个片段 x 起点在 ``tolerance`` 内一致。

    这是**门控**而不是识别器：方案明确「无矢量路径跳过 find_tables 是对已有 lines 策略
    的性能优化，不应无条件移除；只在检测到对齐列/一致空白结构时启用额外识别」。所以这里
    只回答"要不要多花一次识别的钱"，不回答"表在哪"。
    """
    rows = [list(starts) for starts in line_x_starts if len(starts) >= min_cols]
    if len(rows) < min_rows:
        return False
    width = min(len(row) for row in rows)
    if width < min_cols:
        return False
    for column in range(width):
        values = [row[column] for row in rows]
        if max(values) - min(values) > tolerance:
            return False
    return True
