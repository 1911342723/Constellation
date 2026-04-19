from io import BytesIO
import sys
from pathlib import Path
import zipfile

import pytest
from docx import Document

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.exceptions import ProviderError
from infrastructure.providers.docx_provider import DocxProvider


def _build_docx_bytes(text: str) -> bytes:
    buffer = BytesIO()
    doc = Document()
    doc.add_paragraph(text)
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