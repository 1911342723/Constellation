"""Task 5 structural-atom tests that do not require PyMuPDF."""
from __future__ import annotations

import json

from infrastructure.models import Block, StructuralAtom
from infrastructure.providers.pdf_provider import PdfProvider


def _atom(text: str, line: int, *, bold: bool = False, size: float = 10.0) -> dict:
    return StructuralAtom.create(
        source="pdf_line",
        source_span={"page": 1, "block": 0, "line": line},
        block_id=None,
        text=text,
        page=1,
        bbox=[72.0, 100.0 + line * 12, 300.0, 110.0 + line * 12],
        font_family="Helvetica",
        font_size=size,
        is_bold=bold,
        alignment="left",
        region="column-1",
        provenance={"provider": "test"},
    ).to_metadata()


def _entry(block_id: int, text: str, y0: float, atoms: list[dict]):
    block = Block(
        id=block_id,
        type="text",
        text=text,
        font_size=10.0,
        alignment="left",
        metadata={
            "source": "pdf",
            "page": 1,
            "line_index": block_id,
            "bbox": [72.0, y0, 300.0, y0 + 10.0],
            "page_width": 612.0,
            "page_height": 792.0,
            "layout_column": 1,
            "layout_region": "column-1",
            "layout_band": 0,
            "atoms": atoms,
        },
    )
    return (0, 1, y0, 72.0, block)


def test_structural_atom_is_stable_and_json_serializable():
    span = {"page": 3, "block": 7, "line": 2}
    first = StructuralAtom.create(source="pdf_line", source_span=span, text="Title")
    second = StructuralAtom.create(source="pdf_line", source_span=dict(reversed(list(span.items()))), text="Changed")

    assert first.atom_id == second.atom_id
    payload = first.to_metadata()
    assert json.loads(json.dumps(payload))["text"] == "Title"
    assert "model_config" not in payload


def test_structural_atom_serializes_inline_formatting_flags():
    atom = StructuralAtom.create(
        source="docx_run",
        source_span={"paragraph": 1, "run": 2},
        text="gone",
        is_bold=True,
        is_italic=True,
        is_strike=True,
        is_superscript=False,
    )

    payload = atom.to_metadata()
    assert payload["is_bold"] is True
    assert payload["is_italic"] is True
    assert payload["is_strike"] is True
    assert payload["is_superscript"] is False


def test_atom_metadata_does_not_change_block_text_or_markdown():
    block = Block(
        id=4,
        type="text",
        text="**Exact** content",
        metadata={"atoms": [_atom("Exact", 0)]},
    )

    assert block.text == "**Exact** content"
    assert block.to_markdown() == "**Exact** content"
    assert "atoms" not in block.model_dump(exclude={"metadata"})


def test_pdf_merge_preserves_atoms_and_rebases_offsets():
    provider = PdfProvider()
    first_text = "This study presents detailed infor-"
    second_text = "mation about the corpus we analysed."
    entries = [
        _entry(0, first_text, 100.0, [_atom(first_text, 0)]),
        _entry(1, second_text, 112.0, [_atom(second_text, 1)]),
    ]

    merged = provider._merge_text_lines(entries)

    assert len(merged) == 1
    block = merged[0][4]
    assert block.text == "This study presents detailed information about the corpus we analysed."
    atoms = block.metadata["atoms"]
    assert [atom["text"] for atom in atoms] == [first_text, second_text]
    assert atoms[0]["provenance"]["join_normalization"] == "soft-hyphen-removed"
    assert atoms[0]["char_start"] == 0
    assert atoms[0]["char_end"] == len(first_text) - 1
    assert atoms[1]["char_start"] == len(first_text) - 1
    assert atoms[1]["char_end"] == len(block.text)
    assert "".join(
        atom.get("provenance", {}).get("canonical_text", atom["text"])
        for atom in atoms
    ) == block.text


def test_pdf_plain_space_merge_preserves_atom_order_and_full_coverage():
    provider = PdfProvider()
    first_text = "A sufficiently long first body line"
    second_text = "continues on the next physical line."
    entries = [
        _entry(0, first_text, 100.0, [_atom(first_text, 0)]),
        _entry(1, second_text, 112.0, [_atom(second_text, 1)]),
    ]

    block = provider._merge_text_lines(entries)[0][4]
    atoms = block.metadata["atoms"]

    assert [atom["text"] for atom in atoms] == [first_text, second_text]
    assert atoms[0]["char_start"] == 0
    assert atoms[-1]["char_end"] == len(block.text)
    assert all(
        left["char_end"] == right["char_start"]
        for left, right in zip(atoms, atoms[1:], strict=False)
    )
    assert "".join(
        atom.get("provenance", {}).get("canonical_text", atom["text"])
        for atom in atoms
    ) == block.text


def test_pdf_format_votes_are_weighted_by_non_whitespace_characters():
    spans = [
        {"text": "x", "size": 20.0, "font": "Display", "flags": 1 << 4},
        {"text": "long ordinary body", "size": 10.0, "font": "Body", "flags": 0},
    ]

    assert PdfProvider._dominant_font_size(spans) == 10.0
    assert PdfProvider._dominant_font_family(spans) == "Body"
    assert PdfProvider._is_majority_bold(spans) is False


def test_pdf_layout_supports_full_width_and_three_dynamic_columns():
    provider = PdfProvider()

    def layout_entry(index: int, bbox: list[float], alignment: str = "left"):
        atom = _atom(f"line-{index}", index)
        block = Block(
            id=index,
            type="text",
            text=f"line-{index}",
            alignment=alignment,
            metadata={"bbox": bbox, "page": 1, "atoms": [atom]},
        )
        return (0, 0, bbox[1], bbox[0], block)

    entries = [
        layout_entry(0, [180.0, 20.0, 420.0, 35.0], "center"),
        layout_entry(1, [20.0, 60.0, 150.0, 75.0]),
        layout_entry(2, [220.0, 60.0, 350.0, 75.0]),
        layout_entry(3, [420.0, 60.0, 550.0, 75.0]),
    ]

    assigned = provider._assign_layout_regions(entries, 600.0)
    provider._assign_atom_vertical_gaps(assigned)
    regions = [entry[4].metadata["layout_region"] for entry in assigned]

    assert regions == ["full-width", "column-1", "column-2", "column-3"]
    assert assigned[0][4].metadata["layout_band"] == 1
    assert all(entry[4].metadata["layout_band"] == 2 for entry in assigned[1:])
    assert assigned[3][4].metadata["atoms"][0]["region"] == "column-3"
    assert "vertical_gap_before" in assigned[0][4].metadata["atoms"][0]
    assert "vertical_gap_after" in assigned[0][4].metadata["atoms"][0]


def test_pdf_table_dedup_migrates_physical_atoms_to_canonical_table():
    provider = PdfProvider()
    text_atom = _atom("Cell", 0)
    text = Block(
        id=0,
        type="text",
        text="Cell",
        metadata={
            "bbox": [10.0, 10.0, 80.0, 30.0],
            "in_table": True,
            "artifact": True,
            "canonical": False,
            "atoms": [text_atom],
        },
    )
    table = Block(
        id=0,
        type="table",
        text="| Cell |\n| --- |",
        metadata={"bbox": [0.0, 0.0, 100.0, 50.0]},
    )

    remaining = provider._deduplicate_table_text(
        [(0, 0, 10.0, 10.0, text)],
        [(0, 0, 0.0, 0.0, table)],
    )

    assert remaining == []
    assert table.metadata["artifact"] is False
    assert table.metadata["in_table"] is True
    assert table.metadata["canonical"] is True
    assert table.metadata["atoms"][0]["text"] == "Cell"
    assert table.metadata["atoms"][0]["provenance"]["canonical_container"] == "table"
    assert table.metadata["atoms"][0]["char_start"] == 0
    assert table.metadata["atoms"][0]["char_end"] == len(table.text)
    assert table.metadata["atoms"][0]["provenance"]["canonical_text"] == table.text


def test_pdf_unmatched_table_text_is_promoted_to_canonical_text():
    provider = PdfProvider()
    block = Block(
        id=0,
        type="text",
        text="orphan cell",
        metadata={
            "bbox": [10.0, 10.0, 80.0, 30.0],
            "in_table": True,
            "artifact": True,
            "canonical": False,
            "atoms": [_atom("orphan cell", 0)],
        },
    )
    entry = (0, 0, 10.0, 10.0, block)

    assert provider._deduplicate_table_text([entry], []) == [entry]
    assert block.metadata["artifact"] is False
    assert block.metadata["canonical"] is True
    assert block.metadata["atoms"][0]["provenance"]["canonical_container"] == "text"


def test_pdf_toc_alignment_requires_page_title_match_and_is_monotonic():
    provider = PdfProvider()
    blocks = [
        _entry(0, "1 Introduction", 20.0, [_atom("1 Introduction", 0)]),
        _entry(1, "Body prose", 40.0, [_atom("Body prose", 1)]),
        _entry(2, "2 Methods", 60.0, [_atom("2 Methods", 2)]),
    ]
    toc = [
        {"index": 0, "level": 1, "title": "Introduction", "page": 1, "destination": {"page": 1}},
        {"index": 1, "level": 1, "title": "Missing", "page": 1, "destination": {"page": 1}},
        {"index": 2, "level": 2, "title": "Methods", "page": 1, "destination": {"page": 1}},
    ]

    provider._apply_toc_metadata(blocks, toc)

    assert blocks[0][4].metadata["toc_level"] == 1
    assert "toc_level" not in blocks[1][4].metadata
    assert blocks[2][4].metadata["toc_level"] == 2
    assert blocks[2][4].metadata["provenance"]["toc"]["matching"].endswith("monotonic")



def test_pdf_single_column_wide_body_lines_remain_mergeable():
    provider = PdfProvider()
    first_text = "A long first body line that fills the ordinary single column"
    second_text = "and the second line completes the same paragraph."
    first = _entry(0, first_text, 100.0, [_atom(first_text, 0)])
    second = _entry(1, second_text, 112.0, [_atom(second_text, 1)])
    for entry in (first, second):
        entry[4].metadata["bbox"] = [40.0, entry[2], 560.0, entry[2] + 10.0]
        entry[4].metadata["page_width"] = 600.0

    assigned = provider._assign_layout_regions([first, second], 600.0)

    assert [entry[4].metadata["layout_region"] for entry in assigned] == [
        "column-1", "column-1",
    ]
    assert [entry[4].metadata["layout_band"] for entry in assigned] == [0, 0]
    assert len(provider._merge_text_lines(assigned, body_font_size=10.0)) == 1



def test_pdf_body_font_estimate_ignores_internal_whitespace_mass():
    class _Page:
        @staticmethod
        def get_text(_mode):
            return {"blocks": [{
                "type": 0,
                "lines": [{"spans": [
                    {"text": "x         x", "size": 20.0},
                    {"text": "body", "size": 10.0},
                ]}],
            }]}

    class _Doc:
        def __len__(self):
            return 1

        def __getitem__(self, _index):
            return _Page()

    assert PdfProvider()._estimate_body_font_size(_Doc()) == 10.0
