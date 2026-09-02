"""Robust GitHub-flavoured Markdown table rendering shared across providers.

Historically DOCX / PDF / Block each carried their own table renderer, and
each was fragile in a different way:

- The separator row was built from ``len(rows[0])``, so a header narrower
  than a later data row produced a column-count mismatch that broke the
  whole table.
- Data rows were truncated to the header width, silently dropping overflow
  cells (real documents — especially collaborative-editor and Word
  exports — emit ragged rows
  when cells are merged).
- A degenerate table (no rows, or rows of width 0) rendered as ``|  |`` which
  is not a valid GFM table at all.

This module is the single source of truth so all three lanes behave the same
and tolerate the messy shapes real documents produce:

- Column count = the **maximum** width across *all* rows (never drops cells).
- Ragged rows are right-padded with empty cells.
- A cell's newlines become ``<br>`` (a bare newline would split a GFM row).
- Pipe characters are escaped so cell content never spawns phantom columns.
- Empty input (no rows, or every row empty) renders as ``""``.
"""
from __future__ import annotations

from typing import Sequence


def sanitize_table_cell(value: object) -> str:
    """Make any value safe for a single GFM table cell.

    Escapes pipes (otherwise they create phantom columns) and folds every
    flavour of newline into ``<br>`` (a bare newline ends the table row).
    ``None`` represents an empty physical cell rather than the text ``None``.
    """
    if value is None:
        return ""
    text = str(value).replace("\\|", "\x00PIPE\x00")
    return (
        text.replace("|", "\\|").replace("\x00PIPE\x00", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
        .strip()
    )


def rows_to_markdown(rows: Sequence[Sequence[object]]) -> str:
    """Render rows into a robust GFM table.

    The column count is the maximum width across all rows so no cell is ever
    dropped; short rows are right-padded. Returns ``""`` for empty / zero-width
    input rather than emitting a malformed ``|  |`` table.
    """
    if not rows:
        return ""

    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return ""

    def render_row(row: Sequence[object]) -> str:
        cells = [sanitize_table_cell(cell) for cell in row]
        if len(cells) < width:
            cells += [""] * (width - len(cells))
        return "| " + " | ".join(cells) + " |"

    lines = [render_row(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(render_row(row) for row in rows[1:])
    return "\n".join(lines)
