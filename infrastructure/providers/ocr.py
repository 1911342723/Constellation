"""Optional local Tesseract adapter. No network, installation, or shell.

The executable and language packs are deployment-owned. An empty command
keeps the existing explicit needs-OCR warning rather than inventing text.
"""
from __future__ import annotations

import csv
import io
import subprocess
from collections import defaultdict


class TesseractOcrEngine:
    def __init__(self, command: str, languages: str = "eng", timeout: float = 60.0):
        if not command.strip() or timeout <= 0:
            raise ValueError("OCR requires an executable and positive timeout")
        self.command, self.languages, self.timeout = command, languages, timeout

    def recognize_page(self, page_image: bytes, *, page_number: int) -> dict:
        result = subprocess.run(
            [self.command, "stdin", "stdout", "-l", self.languages, "tsv"],
            input=page_image, capture_output=True, timeout=self.timeout, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = list(csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"))
        page = next((row for row in rows if row["level"] == "1"), None)
        if page is None:
            raise ValueError("OCR output has no page geometry")
        width, height = max(float(page["width"]), 1), max(float(page["height"]), 1)
        lines = defaultdict(list)
        for row in rows:
            if row["level"] == "5" and row.get("text", "").strip() and float(row["conf"]) >= 0:
                lines[(row["block_num"], row["par_num"], row["line_num"])].append(row)
        blocks = []
        for words in lines.values():
            blocks.append({
                "text": " ".join(word["text"] for word in words),
                "confidence": sum(float(word["conf"]) for word in words) / (100 * len(words)),
                "bbox": [min(float(w["left"]) for w in words) / width,
                         min(float(w["top"]) for w in words) / height,
                         max(float(w["left"]) + float(w["width"]) for w in words) / width,
                         max(float(w["top"]) + float(w["height"]) for w in words) / height],
            })
        return {"blocks": blocks, "bbox_space": "normalized", "source_type": "ocr"}


def configured_ocr_engine():
    from app.core.config import settings
    if not settings.pdf_ocr_command:
        return None
    return TesseractOcrEngine(settings.pdf_ocr_command, settings.pdf_ocr_languages, settings.pdf_ocr_timeout)
