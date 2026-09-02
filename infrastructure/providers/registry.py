"""Single source of truth for format detection and provider dispatch.

Every caller imports this module so that the ``suffix -> format`` and
``format -> provider`` tables live in exactly one place, rather than each
caller keeping its own copy.

This module raises **domain-level exceptions only**
(:class:`~app.core.exceptions.UnsupportedFormatError` /
:class:`~app.core.exceptions.ProviderError`); it knows nothing about any
transport.  Translating those into a transport-level error is the caller's
responsibility.

``get_provider`` returns a **fresh provider instance per call**.  Providers are
not all stateless: :class:`DocxProvider` carries per-extraction state
(``image_store`` / ``_doc_rels`` / style-chain caches), so a shared singleton
extracted concurrently would let one caller reset or repopulate another
caller's document context —
silently mixing images and styles across documents.  Construction is a few
attribute assignments; per-call instantiation is the cheap, correct isolation.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import List, Type

from app.core.exceptions import ProviderError, UnsupportedFormatError
from infrastructure.models import Block
from infrastructure.providers.base import BaseProvider
from infrastructure.providers.docx_provider import DocxProvider
from infrastructure.providers.markdown_provider import MarkdownProvider
from infrastructure.providers.pdf_provider import PdfProvider
from infrastructure.providers.spreadsheet_provider import CsvProvider, XlsxProvider
from infrastructure.providers.text_provider import TextProvider

# Filename suffix -> canonical format name.
_SUFFIX_TO_FORMAT: dict[str, str] = {
    ".docx": "docx",
    ".pdf": "pdf",
    ".txt": "txt",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".md": "md",
    ".markdown": "md",
}

# Canonical format name -> provider class (instantiated per call; see module doc).
_PROVIDER_CLASSES: dict[str, Type] = {
    "docx": DocxProvider,
    "pdf": PdfProvider,
    "txt": TextProvider,
    "csv": CsvProvider,
    "xlsx": XlsxProvider,
    "md": MarkdownProvider,
}

#: Canonical format names with a registered provider (sorted, stable).
SUPPORTED_FORMATS: List[str] = sorted(_PROVIDER_CLASSES)
#: Accepted filename suffixes (sorted, stable).
SUPPORTED_SUFFIXES: List[str] = sorted(_SUFFIX_TO_FORMAT)


def _suffix_format(filename: str | None) -> str | None:
    if not filename:
        return None
    lower_name = filename.lower()
    for suffix, source_format in _SUFFIX_TO_FORMAT.items():
        if lower_name.endswith(suffix):
            return source_format
    return None


def has_known_suffix(filename: str | None) -> bool:
    return _suffix_format(filename) is not None


def sniff_format(content: bytes | None) -> str | None:
    """Guess a canonical format from file bytes when the filename has no suffix."""
    blob = bytes(content or b"")
    if blob.startswith(b"%PDF"):
        return "pdf"
    if blob.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return "docx"
        if any(name.startswith("xl/") for name in names):
            return "xlsx"
        if any(name.startswith("word/") for name in names):
            return "docx"
        return "docx"
    return None


def detect_format(filename: str | None, content: bytes | None = None) -> str:
    """Map a filename / path to a canonical format name by its suffix.

    Args:
        filename: File name or path (case-insensitive suffix match).
        content: Optional file bytes. Used only when the name has no known
            suffix (document exports often return a bare title with no
            ``.docx`` extension).

    Returns:
        The canonical format name (e.g. ``"docx"``).

    Raises:
        UnsupportedFormatError: If ``filename`` is empty or its suffix has no
            registered provider, and the bytes also cannot be sniffed.
    """
    found = _suffix_format(filename)
    if found:
        return found

    name = Path(str(filename or "")).name
    if name and "." in name:
        raise UnsupportedFormatError(
            f"Unsupported file format for '{filename}'. "
            f"Accepted: {', '.join(SUPPORTED_SUFFIXES)}"
        )

    sniffed = sniff_format(content)
    if sniffed:
        return sniffed

    if not filename:
        raise UnsupportedFormatError("No filename provided")
    raise UnsupportedFormatError(
        f"Unsupported file format for '{filename}'. "
        f"Accepted: {', '.join(SUPPORTED_SUFFIXES)}"
    )


def get_provider(source_format: str) -> BaseProvider:
    """Return a **fresh** provider instance for ``source_format``.

    Per-call instantiation isolates per-extraction provider state between
    concurrent requests (see module docstring).

    Raises:
        UnsupportedFormatError: If no provider is registered for the format.
        ProviderError: If the registered provider does not satisfy
            :class:`BaseProvider` (defensive; should never happen at runtime).
    """
    provider_cls = _PROVIDER_CLASSES.get(source_format)
    if provider_cls is None:
        raise UnsupportedFormatError(f"Unsupported source format: {source_format}")
    provider = provider_cls()
    if not isinstance(provider, BaseProvider):
        raise ProviderError(
            f"Provider for '{source_format}' does not satisfy BaseProvider protocol"
        )
    return provider


def extract_blocks(source_format: str, content: bytes) -> List[Block]:
    """Stage 1 dispatch: pick the provider for ``source_format`` and extract.

    Args:
        source_format: Canonical format name (see :data:`SUPPORTED_FORMATS`).
        content: Raw document bytes.

    Returns:
        Ordered list of :class:`Block` objects.

    Raises:
        UnsupportedFormatError: If the format has no registered provider.
        ProviderError: If the provider fails to parse the bytes.
    """
    return get_provider(source_format).extract_from_bytes(content)
