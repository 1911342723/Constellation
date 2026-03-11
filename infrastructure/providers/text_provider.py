"""Plain-text provider for the Constellation pipeline."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

from app.core.exceptions import ProviderError
from infrastructure.models import Block


class TextProvider:
    """Convert a plain-text file into Stage-1 blocks."""

    _ENCODINGS = (
        "utf-8-sig",
        "utf-8",
        "utf-16",
        "gb18030",
        "gbk",
        "big5",
        "latin-1",
    )

    def extract(self, file_path: str) -> List[Block]:
        path = Path(file_path)
        if path.suffix.lower() != ".txt":
            raise ProviderError("Only .txt files are supported")
        return self.extract_from_bytes(path.read_bytes())

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        text = self._decode_bytes(file_bytes)
        return self._text_to_blocks(text)

    def _decode_bytes(self, file_bytes: bytes) -> str:
        for encoding in self._ENCODINGS:
            try:
                return file_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ProviderError("Unable to decode text file; use UTF-8, UTF-16, or GB encodings")

    def _text_to_blocks(self, text: str) -> List[Block]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", normalized) if chunk.strip()]
        if not chunks:
            return []

        raw_blocks: List[str] = []
        for chunk in chunks:
            lines = [line.strip() for line in chunk.split("\n") if line.strip()]
            if self._should_preserve_lines(lines):
                raw_blocks.extend(lines)
            else:
                raw_blocks.append(" ".join(lines))

        return [
            Block(
                id=index,
                type="text",
                text=block_text,
                metadata={"source": "txt"},
            )
            for index, block_text in enumerate(raw_blocks)
        ]

    @staticmethod
    def _should_preserve_lines(lines: List[str]) -> bool:
        if len(lines) <= 1:
            return True

        structured_pattern = re.compile(r"^(?:[#*\-]|\d+[.)]|[A-Za-z][.)])\s")
        structured_lines = sum(1 for line in lines if structured_pattern.match(line))

        return structured_lines >= max(1, len(lines) // 2)
