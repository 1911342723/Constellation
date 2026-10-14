"""E04：表格逻辑单元格模型、跨页续表判定、无框表探测门控。

方案要求分别评测表头、单元格内容、合并关系、跨页关系——所以这里的断言也按这四类分开，
不用"渲染结果看起来对"混过去：合并关系算错时渲染结果里重复的文字看起来像正常的合并显示，
只有覆盖不变量能抓出来。
"""
from __future__ import annotations

import io

import openpyxl
import pytest
from docx import Document

from infrastructure.models import LogicalCell, TableModel, model_from_rows
from infrastructure.models.table_continuation import (
    MIN_CONTINUATION_SIGNALS,
    judge_continuation,
    link_continuations,
    suggests_borderless_table,
)
from infrastructure.providers.docx_provider import DocxProvider
from infrastructure.providers.spreadsheet_provider import XlsxProvider


# ── 逻辑模型本身 ─────────────────────────────────────────────────────────


def test_gfm_is_a_derived_view_not_the_truth():
    """合并格在 GFM 矩阵里重复显示值，但真实跨度只在模型里。"""
    model = TableModel(n_rows=2, n_cols=2, header_rows=1, cells=[
        LogicalCell(row=0, col=0, row_span=2, display_value="合并", is_header=True),
        LogicalCell(row=0, col=1, display_value="表头2", is_header=True),
        LogicalCell(row=1, col=1, display_value="数据2"),
    ])
    assert model.to_rows() == [["合并", "表头2"], ["合并", "数据2"]]
    merged = model.cell_at(1, 0)
    assert merged is not None and merged.row_span == 2, "跨度必须能从模型读出"


def test_coverage_invariant_catches_overlap_and_holes():
    """逻辑格必须恰好覆盖网格一次——这是区间覆盖口径在表格维度的同一条不变量。"""
    overlap = TableModel(n_rows=2, n_cols=1, cells=[
        LogicalCell(row=0, col=0, row_span=2, display_value="A"),
        LogicalCell(row=1, col=0, display_value="B"),
    ])
    assert overlap.coverage_is_exact() is False, "重叠必须被判出"

    hole = TableModel(n_rows=2, n_cols=2, cells=[LogicalCell(row=0, col=0, display_value="A")])
    assert hole.coverage_is_exact() is False, "空洞必须被判出"

    exact = model_from_rows([["a", "b"], ["c", "d"]])
    assert exact.coverage_is_exact() is True


def test_sampling_is_recorded_not_disguised():
    """抽样展示不得伪装成完整表。"""
    model = model_from_rows([["a"]] * 5, sampled=True, total_rows=5000)
    assert model.sampled is True
    assert model.total_rows == 5000
    assert model.n_rows == 5, "展示行数与真实总行数分开记录"


def test_header_signature_reflects_header_region():
    model = model_from_rows([["字段", "说明"], ["a", "b"]], header_rows=1)
    assert model.header_signature() == ("字段", "说明")
    headers = [c for c in model.cells if c.is_header]
    assert {c.row for c in headers} == {0}


# ── provider 侧：三种来源同一种表示 ────────────────────────────────────────


def _docx_with_vertical_merge() -> bytes:
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "MERGED"
    table.cell(0, 0).merge(table.cell(1, 0))
    table.cell(0, 1).text = "H2"
    table.cell(1, 1).text = "V2"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_docx_emits_logical_model_with_real_spans():
    blocks = DocxProvider().extract_from_bytes(_docx_with_vertical_merge())
    tables = [b for b in blocks if b.type == "table"]
    assert tables, "表格块应存在"
    model_data = (tables[0].table_data or {}).get("model")
    assert model_data is not None, "DOCX 必须输出逻辑模型"
    model = TableModel.model_validate(model_data)
    assert model.coverage_is_exact(), "合并关系必须恰好覆盖网格一次"
    spans = sorted(c.row_span for c in model.cells)
    assert spans[-1] == 2, "纵向合并的真实 row_span 必须进模型"


def test_xlsx_emits_logical_model_with_unit_spans():
    """XLSX 没有跨度信息，模型里 span 全为 1——如实描述，不是占位。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["字段", "说明"])
    ws.append(["appId", "应用标识"])
    buf = io.BytesIO()
    wb.save(buf)
    blocks = XlsxProvider().extract_from_bytes(buf.getvalue())
    tables = [b for b in blocks if b.type == "table"]
    assert tables
    model = TableModel.model_validate((tables[0].table_data or {})["model"])
    assert model.coverage_is_exact()
    assert all(c.row_span == 1 and c.col_span == 1 for c in model.cells)
    assert model.header_signature() == ("字段", "说明")


# ── 跨页续表：多信号一致才接续 ────────────────────────────────────────────


def _paged(page: int, rows, *, header_rows: int = 1, lefts=(10.0, 100.0)) -> TableModel:
    model = model_from_rows(rows, header_rows=header_rows, page=page)
    for cell in model.cells:
        cell.bbox = [lefts[cell.col], 0.0, lefts[cell.col] + 80.0, 10.0]
    return model


def test_continuation_accepted_when_signals_agree():
    first = _paged(1, [["字段", "说明"], ["a", "1"]])
    second = _paged(2, [["字段", "说明"], ["b", "2"]])
    verdict = judge_continuation(first, second)
    assert verdict.is_continuation is True
    assert verdict.signals["adjacent_pages"] is True
    assert verdict.signals["columns_align"] is True
    assert verdict.signals["header_repeated"] is True


def test_two_independent_tables_with_same_shape_are_not_merged():
    """同构但不相邻的两张表绝不接续——否则等于把两份数据搅在一起。"""
    first = _paged(1, [["字段", "说明"], ["a", "1"]])
    far = _paged(5, [["字段", "说明"], ["b", "2"]])
    verdict = judge_continuation(first, far)
    assert verdict.is_continuation is False
    assert verdict.signals["adjacent_pages"] is False


def test_different_column_count_is_never_a_continuation():
    first = _paged(1, [["a", "b"], ["1", "2"]])
    second = model_from_rows([["a", "b", "c"], ["1", "2", "3"]], page=2)
    verdict = judge_continuation(first, second)
    assert verdict.is_continuation is False
    assert verdict.signals["same_column_count"] is False


def test_misaligned_columns_alone_do_not_carry_the_verdict():
    """列位置对不上时，仅靠"页码相邻"这一条信号不足以判续表。"""
    first = _paged(1, [["字段", "说明"], ["a", "1"]], lefts=(10.0, 100.0))
    second = _paged(2, [["其他", "内容"], ["b", "2"]], lefts=(200.0, 400.0))
    verdict = judge_continuation(first, second)
    assert verdict.signals["columns_align"] is False
    assert verdict.signals["header_repeated"] is False
    assert verdict.signals["positive_signal_count"] < MIN_CONTINUATION_SIGNALS
    assert verdict.is_continuation is False


def test_continuation_without_repeated_header_still_detected():
    """续页直接接数据行（没重复表头）也是续表的强信号。"""
    first = _paged(1, [["字段", "说明"], ["a", "1"]])
    second = _paged(2, [["b", "2"], ["c", "3"]], header_rows=0)
    verdict = judge_continuation(first, second)
    assert verdict.signals["header_absent"] is True
    assert verdict.is_continuation is True


def test_link_continuations_records_ids_and_evidence():
    models = [
        _paged(1, [["字段", "说明"], ["a", "1"]]),
        _paged(2, [["字段", "说明"], ["b", "2"]]),
        _paged(7, [["字段", "说明"], ["c", "3"]]),
    ]
    linked = link_continuations(models)
    assert linked[0].continuation_of is None
    assert linked[1].continuation_of == linked[0].table_id
    assert linked[2].continuation_of is None, "隔页的表不接续"
    assert linked[1].continuation_signals, "判定依据必须留痕，供审计与回归"


def test_table_ids_are_stable_across_calls():
    """重复调用不得让 table_id 漂移（B08 踩过随机 UUID 的坑）。"""
    def build():
        return link_continuations([
            _paged(1, [["a", "b"], ["1", "2"]]),
            _paged(2, [["a", "b"], ["3", "4"]]),
        ])

    assert [m.table_id for m in build()] == [m.table_id for m in build()]


# ── 无框表探测门控 ────────────────────────────────────────────────────────


def test_aligned_columns_suggest_borderless_table():
    aligned = [[10.0, 100.0, 200.0]] * 4
    assert suggests_borderless_table(aligned) is True


def test_ragged_lines_do_not_trigger_extra_detection():
    """普通段落的 x 起点不对齐——不该为它多付一次识别的钱。"""
    ragged = [[10.0, 55.0], [12.0, 130.0], [40.0, 90.0], [11.0, 70.0]]
    assert suggests_borderless_table(ragged) is False


def test_too_few_lines_never_trigger_detection():
    assert suggests_borderless_table([[10.0, 100.0], [10.0, 100.0]]) is False
    assert suggests_borderless_table([[10.0]] * 6) is False
