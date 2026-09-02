import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.providers import BaseProvider, CsvProvider, XlsxProvider
from infrastructure.providers.spreadsheet_provider import _trim_empty_columns


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def test_csv_basic_comma_table():
    provider = CsvProvider()
    content = "name,age\nAlice,30\nBob,25"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    assert len(blocks) == 1
    block = blocks[0]
    assert block.type == "table"
    assert block.table_data["rows"] == [
        ["name", "age"],
        ["Alice", "30"],
        ["Bob", "25"],
    ]
    assert "| name | age |" in block.text
    assert "| --- | --- |" in block.text
    assert block.metadata["source"] == "csv"
    assert block.metadata["truncated"] is False
    assert block.metadata["sampled"] is False


def test_csv_sniffs_semicolon_delimiter():
    provider = CsvProvider()
    content = "a;b;c\n1;2;3"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    assert blocks[0].table_data["rows"] == [["a", "b", "c"], ["1", "2", "3"]]


def test_csv_decodes_gbk():
    provider = CsvProvider()
    content = "姓名,年龄\n张三,30"

    blocks = provider.extract_from_bytes(content.encode("gbk"))

    assert blocks[0].table_data["rows"] == [["姓名", "年龄"], ["张三", "30"]]


def test_csv_truncates_to_max_rows():
    provider = CsvProvider(max_rows=2)
    content = "h\nr1\nr2\nr3\nr4"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    block = blocks[0]
    assert len(block.table_data["rows"]) == 2
    assert block.metadata["truncated"] is True
    assert block.metadata["total_rows"] == 5
    assert block.metadata["row_count"] == 2


def test_csv_empty_returns_no_blocks():
    provider = CsvProvider()
    assert provider.extract_from_bytes(b"") == []


def test_csv_trims_trailing_empty_columns():
    provider = CsvProvider()
    content = "a,b,,\n1,2,,"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    assert blocks[0].table_data["rows"] == [["a", "b"], ["1", "2"]]


def test_csv_large_table_samples_with_warning():
    provider = CsvProvider(max_render_rows=5, sample_tail=2)
    content = "header\n" + "\n".join(f"r{i}" for i in range(20))

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    block = blocks[0]
    assert block.metadata["sampled"] is True
    assert block.metadata["truncated"] is False
    assert block.metadata["total_rows"] == 21
    # Structured data keeps the full read rows even though Markdown is sampled.
    assert len(block.table_data["rows"]) == 21
    assert "采样" in block.text
    assert "列概览" in block.text
    # Rendered body must be far smaller than the full table.
    assert block.text.count("\n| r") <= 6


def test_csv_column_overview_infers_kind():
    provider = CsvProvider()
    content = "name,score\nAlice,90\nBob,80\nCarol,70"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    columns = blocks[0].metadata["columns"]
    assert columns[0]["name"] == "name"
    assert columns[0]["kind"] == "text"
    assert columns[1]["name"] == "score"
    assert columns[1]["kind"] == "numeric"


def test_csv_provider_satisfies_protocol():
    assert isinstance(CsvProvider(), BaseProvider)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_trim_trailing_empty_columns_helper():
    assert _trim_empty_columns([["a", "b", "", ""], ["1", "2", "", ""]]) == [
        ["a", "b"],
        ["1", "2"],
    ]
    # Interior empty columns are preserved (may carry meaning).
    assert _trim_empty_columns([["a", "", "c"]]) == [["a", "", "c"]]


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #
def _make_xlsx(sheets: dict) -> bytes:
    """Build an in-memory .xlsx from {sheet_name: [[row], ...]}."""
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(title=name)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_xlsx_sheet_name_becomes_heading_and_table():
    pytest.importorskip("openpyxl")
    data = _make_xlsx({"Sheet1": [["col1", "col2"], ["v1", "v2"]]})

    blocks = XlsxProvider().extract_from_bytes(data)

    assert len(blocks) == 2
    heading, table = blocks
    assert heading.type == "text"
    assert heading.text == "Sheet1"
    assert heading.is_heading_style is True
    assert heading.heading_level == 1
    assert table.type == "table"
    assert table.caption == "Sheet1"
    assert table.table_data["rows"] == [["col1", "col2"], ["v1", "v2"]]


def test_xlsx_multiple_sheets_keep_order_and_ids():
    pytest.importorskip("openpyxl")
    data = _make_xlsx(
        {
            "Alpha": [["a"]],
            "Beta": [["b"]],
        }
    )

    blocks = XlsxProvider().extract_from_bytes(data)

    assert [b.id for b in blocks] == [0, 1, 2, 3]
    assert blocks[0].text == "Alpha"
    assert blocks[2].text == "Beta"


def test_xlsx_merged_cells_fill_down():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Merged"
    worksheet["A1"] = "Region"
    worksheet["C1"] = "Q1"
    worksheet.merge_cells("A1:B1")
    worksheet["A2"] = "East"
    worksheet["B2"] = "Beijing"
    worksheet["C2"] = "100"
    buffer = io.BytesIO()
    workbook.save(buffer)

    blocks = XlsxProvider().extract_from_bytes(buffer.getvalue())

    table = blocks[1]
    # The merged A1:B1 anchor value "Region" propagates to B1.
    assert table.table_data["rows"][0] == ["Region", "Region", "Q1"]


def test_xlsx_coerces_value_types():
    pytest.importorskip("openpyxl")
    data = _make_xlsx(
        {
            "Types": [
                ["int", "wholefloat", "frac", "date", "flag"],
                [42, 5.0, 3.14, datetime(2024, 1, 15), True],
            ]
        }
    )

    blocks = XlsxProvider().extract_from_bytes(data)

    assert blocks[1].table_data["rows"][1] == [
        "42",
        "5",
        "3.14",
        "2024-01-15",
        "TRUE",
    ]


def test_xlsx_percentage_number_format():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Pct"
    worksheet["A1"] = "rate"
    worksheet["A2"] = 0.5
    worksheet["A2"].number_format = "0%"
    worksheet["A3"] = 0.1234
    worksheet["A3"].number_format = "0.0%"
    buffer = io.BytesIO()
    workbook.save(buffer)

    blocks = XlsxProvider().extract_from_bytes(buffer.getvalue())

    rows = blocks[1].table_data["rows"]
    assert rows[1][0] == "50%"
    assert rows[2][0] == "12.3%"


def test_xlsx_thousands_and_currency_number_format():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Money"
    worksheet["A1"] = "amount"
    worksheet["B1"] = "price"
    worksheet["A2"] = 1234567
    worksheet["A2"].number_format = "#,##0"
    worksheet["B2"] = 1200
    worksheet["B2"].number_format = '"¥"#,##0.00'
    buffer = io.BytesIO()
    workbook.save(buffer)

    blocks = XlsxProvider().extract_from_bytes(buffer.getvalue())

    row = blocks[1].table_data["rows"][1]
    assert row[0] == "1,234,567"
    assert row[1] == "¥1,200.00"


def test_xlsx_large_table_samples_with_warning():
    pytest.importorskip("openpyxl")
    data = _make_xlsx({"Big": [["h"]] + [[f"r{i}"] for i in range(20)]})

    blocks = XlsxProvider(max_render_rows=5, sample_tail=2).extract_from_bytes(data)

    table = blocks[1]
    assert table.metadata["sampled"] is True
    assert len(table.table_data["rows"]) == 21
    assert "采样" in table.text


def test_xlsx_escapes_pipe_in_markdown():
    pytest.importorskip("openpyxl")
    data = _make_xlsx({"S": [["a|b", "c"]]})

    blocks = XlsxProvider().extract_from_bytes(data)

    assert "a\\|b" in blocks[1].text
    # Raw structured data keeps the literal pipe (escaping is Markdown-only).
    assert blocks[1].table_data["rows"][0][0] == "a|b"


def test_xlsx_empty_sheet_yields_only_heading():
    pytest.importorskip("openpyxl")
    data = _make_xlsx({"Empty": []})

    blocks = XlsxProvider().extract_from_bytes(data)

    assert len(blocks) == 1
    assert blocks[0].type == "text"
    assert blocks[0].text == "Empty"


def test_xlsx_provider_satisfies_protocol():
    assert isinstance(XlsxProvider(), BaseProvider)
