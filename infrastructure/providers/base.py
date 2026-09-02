"""Base provider protocol for Constellation document providers.

All document providers (DocxProvider, PdfProvider, TextProvider, etc.)
must implement this protocol.  The contract ensures the API layer can
dispatch uniformly without duck-typing surprises.
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from infrastructure.models import Block


@runtime_checkable
class BaseProvider(Protocol):
    """Protocol that all document providers must satisfy.

    Two methods are required:

    - ``extract(file_path)`` — reads from a filesystem path.
    - ``extract_from_bytes(file_bytes)`` — reads from raw bytes
      (used by the API upload handler).

    Both return an ordered list of :class:`Block` objects with
    sequential ``id`` values starting from 0.
    """

    def extract(self, file_path: str) -> List[Block]:
        """Extract blocks from a file on disk.

        Args:
            file_path: Path to the source document.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the file is unsupported, not found,
                or cannot be parsed.
        """
        ...

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        """Extract blocks from raw file bytes.

        Args:
            file_bytes: Raw content of the source document.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the bytes cannot be parsed.
        """
        ...
