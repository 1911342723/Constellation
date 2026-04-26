from io import BytesIO
import base64
import sys
from pathlib import Path
import zipfile

import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.exceptions import ProviderError
from infrastructure.providers.docx_provider import DocxProvider


PNG_BYTES = (
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
)


def _build_docx_bytes(text: str) -> bytes:
    buffer = BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buffer)
    return buffer.getvalue()


def _save_docx_bytes(doc: Document) -> bytes:
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _rewrite_docx_entry(file_bytes: bytes, entry_name: str, old: bytes, new: bytes) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(file_bytes), "r") as input_zip, zipfile.ZipFile(output, "w") as output_zip:
        for zip_info in input_zip.infolist():
            data = input_zip.read(zip_info.filename)
            if zip_info.filename == entry_name:
                data = data.replace(old, new)
            output_zip.writestr(zip_info, data)
    return output.getvalue()


def _insert_inline_omml(file_bytes: bytes) -> bytes:
    math_xml = (
        b'<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        b'<m:r><m:t>O(N)</m:t></m:r>'
        b'</m:oMath>'
    )
    return _rewrite_docx_entry(
        file_bytes,
        "word/document.xml",
        b"<w:t>prefix  suffix</w:t>",
        b"<w:t>prefix </w:t></w:r>" + math_xml + b"<w:r><w:t> suffix</w:t>",
    )


def test_docx_provider_normalizes_strict_ooxml_relationships():
    original = _build_docx_bytes("Hello strict")
    strict_like = _rewrite_docx_entry(
        original,
        "_rels/.rels",
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        b"http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
    )

    blocks = DocxProvider().extract_from_bytes(strict_like)

    assert any(block.text == "Hello strict" for block in blocks)


def test_docx_provider_rejects_invalid_zip_payloads():
    with pytest.raises(ProviderError) as exc_info:
        DocxProvider().extract_from_bytes(b"not-a-docx")

    assert "压缩包" in exc_info.value.message


def test_docx_provider_keeps_inline_omml_in_paragraph_order():
    original = _build_docx_bytes("prefix  suffix")
    with_inline_math = _insert_inline_omml(original)

    blocks = DocxProvider().extract_from_bytes(with_inline_math)

    assert blocks[0].type == "text"
    assert blocks[0].text == "prefix $O(N)$ suffix"
    assert not any(block.type == "formula" for block in blocks)


def test_docx_provider_renders_monospace_run_as_inline_code():
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run("Use ")
    code_run = paragraph.add_run("foo_bar")
    code_run.font.name = "Consolas"
    paragraph.add_run(" here")

    blocks = DocxProvider().extract_from_bytes(_save_docx_bytes(doc))

    assert blocks[0].type == "text"
    assert blocks[0].text == "Use `foo_bar` here"


def test_docx_provider_detects_single_line_code_block():
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run("print('hello')")
    run.font.name = "Consolas"

    blocks = DocxProvider().extract_from_bytes(_save_docx_bytes(doc))

    assert blocks[0].type == "code"
    assert blocks[0].text == "print('hello')"


def test_docx_provider_preserves_center_alignment_on_single_line_code_block():
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("const x = 1")
    run.font.name = "Consolas"

    blocks = DocxProvider().extract_from_bytes(_save_docx_bytes(doc))

    assert blocks[0].type == "code"
    assert blocks[0].text == "const x = 1"
    assert blocks[0].alignment == "center"


def test_docx_provider_keeps_inline_image_in_paragraph_order():
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run("before ")
    image_run = paragraph.add_run()
    image_run.add_picture(BytesIO(PNG_BYTES))
    paragraph.add_run(" after")

    blocks = DocxProvider().extract_from_bytes(_save_docx_bytes(doc))

    assert blocks[0].type == "text"
    assert blocks[0].text.startswith("before ![")
    assert blocks[0].text.endswith(" after")
    assert not any(block.type == "image" for block in blocks)


def test_docx_provider_keeps_image_only_paragraph_as_image_block():
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run().add_picture(BytesIO(PNG_BYTES))

    blocks = DocxProvider().extract_from_bytes(_save_docx_bytes(doc))

    assert any(block.type == "image" for block in blocks)


def test_docx_provider_skips_section_properties_noise():
    blocks = DocxProvider().extract_from_bytes(_build_docx_bytes("plain"))

    assert not any((block.text or "").startswith("[RAW_XML_NODE: sectPr]") for block in blocks)