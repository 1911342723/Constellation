"""PDF Provider — Constellation Stage 1: Physical dimensionality reduction.

Extracts Block objects from PDF documents using PyMuPDF (fitz).

Extraction strategy:

1. **Text blocks** — each PDF text block (paragraph) becomes a
   ``type="text"`` Block with font_size, bold, and alignment features
   inferred from the span-level metadata.
2. **Image blocks** — embedded images are extracted as base64 and
   stored in ``type="image"`` Blocks.
3. **Table blocks** — detected tables are converted to
   ``type="table"`` Blocks with ``table_data={"rows": [...]}``.

Physical feature heuristics:
- Font size: dominant span size in the block.
- Bold: majority of spans have the bold flag set.
- Alignment: inferred from block bounding-box position relative to
  the page text area.
- Heading style: blocks with font size significantly larger than the
  document's body median are flagged as ``is_heading_style=True``.
"""
from __future__ import annotations

import base64
import logging
import os
from collections import Counter
from typing import TYPE_CHECKING, List, Optional

from infrastructure.models import Block, StructuralAtom
from app.core.exceptions import ProviderError

if TYPE_CHECKING:  # PyMuPDF is imported lazily inside methods
    import fitz

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────
_SUPPORTED_EXTENSIONS = {".pdf"}
_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "tiff", "tif", "gif"}
# Sentence-ending / connective punctuation.  A short line ending in one
# of these is body prose (or a wrapped fragment), never a standalone
# heading; used by the no-style heading-merge guard below.
_HEADINGISH_SENTENCE_END = (
    ".", "。", "!", "?", "！", "？", ";", "；", ":", "：", ",", "，", "、",
)
# A standalone heading line is short; cap in characters (CJK-friendly).
_STANDALONE_HEADING_MAX_CHARS = 64
# A line occupying this fraction of the typical body line width is a
# wrapped body line, not a short standalone heading.
_STANDALONE_HEADING_WIDTH_RATIO = 0.7


class PdfProvider:
    """Extract Block objects from PDF documents.

    Uses PyMuPDF (fitz) for PDF parsing.  Each page is processed
    independently; text blocks carry physical formatting metadata
    (font_size, bold, alignment) that downstream stages rely on for
    I-frame / P-frame classification and heading detection.
    """

    def extract(self, file_path: str) -> List[Block]:
        """Extract blocks from a PDF file on disk.

        Args:
            file_path: Path to a ``.pdf`` file.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the file does not exist or has an
                unsupported extension.
        """
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _SUPPORTED_EXTENSIONS:
            raise ProviderError(
                f"Unsupported file extension '{ext}'. "
                f"Expected one of: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
            )
        if not os.path.exists(file_path):
            raise ProviderError(f"File not found: {file_path}")

        with open(file_path, "rb") as f:
            return self.extract_from_bytes(f.read())

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        """Extract blocks from raw PDF bytes.

        Args:
            file_bytes: Raw PDF file content.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the bytes cannot be parsed as PDF.
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ProviderError(
                "PyMuPDF is not installed. "
                "Install it with: pip install PyMuPDF"
            ) from None

        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as e:
            raise ProviderError(f"Cannot parse PDF: {e}") from e

        try:
            # First pass: collect body font size (median of all text
            # spans) for heading detection.
            body_font_size = self._estimate_body_font_size(doc)

            # PDF outline is an independent control-flow source.  It is
            # aligned only after physical blocks have reached canonical order.
            toc_entries = self._extract_toc_entries(doc)

            # Collect blocks with layout coordinates for reading-order sorting.
            # Each entry is (page_index, layout_column, y0, x0, Block).
            unsorted: list[tuple[int, int, float, float, Block]] = []

            for page_index in range(len(doc)):
                page = doc[page_index]

                # Table geometry must be known before ordinary text extraction:
                # PyMuPDF exposes table cell text through both APIs.
                table_entries = self._extract_tables(page, page_index)
                table_bboxes = [
                    tuple((entry[4].metadata or {}).get("bbox", (0, 0, 0, 0)))
                    for entry in table_entries
                ]
                text_entries = self._extract_text(
                    page, page_index, body_font_size, table_bboxes=table_bboxes,
                )
                text_entries = self._deduplicate_table_text(text_entries, table_entries)
                image_entries = self._extract_images(page, page_index)

                page_entries = text_entries + image_entries + table_entries
                page_entries = self._assign_layout_regions(page_entries, page.rect.width)
                self._assign_atom_vertical_gaps(page_entries)
                unsorted.extend(page_entries)

            unsorted = self._filter_repeating_marginal_text(unsorted, len(doc))
            unsorted = self._merge_text_lines(unsorted, body_font_size)
            unsorted.sort(key=self._entry_sort_key)
            self._apply_toc_metadata(unsorted, toc_entries)

            # Assign sequential IDs after sorting and synchronize the lightweight
            # atom dictionaries without adding fields to the public Block model.
            blocks: List[Block] = []
            for block_id, (_, _, _, _, block) in enumerate(unsorted):
                block.id = block_id
                self._set_atom_block_id(block, block_id)
                blocks.append(block)

            logger.info(
                "[PdfProvider] Extracted %d blocks from %d pages",
                len(blocks), len(doc),
            )
            return blocks

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"PDF extraction failed: {e}") from e
        finally:
            doc.close()

    # ── Text extraction ────────────────────────────────────────

    def _extract_text(
        self,
        page: "fitz.Page",
        page_index: int,
        body_font_size: float,
        table_bboxes: Optional[list[tuple[float, float, float, float]]] = None,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Extract text blocks from a single page.

        Uses ``page.get_text("dict")`` to obtain structured block/line/span
        data with font metadata.

        Returns:
            List of ``(page_index, column, y0, x0, Block)`` tuples.
        """
        entries: list[tuple[int, int, float, float, Block]] = []
        page_dict = page.get_text("dict")
        page_width = page.rect.width
        page_height = page.rect.height

        for raw_block_index, raw_block in enumerate(page_dict.get("blocks", [])):
            if raw_block.get("type") != 0:  # 0 = text block
                continue

            raw_lines = raw_block.get("lines", [])
            if not raw_lines:
                continue

            # Keep every physical source line before joining visual-baseline
            # fragments.  _merge_inline_segments carries this list forward.
            lines: list[dict] = []
            for source_line_index, source_line in enumerate(raw_lines):
                annotated = dict(source_line)
                annotated["_source_lines"] = [{
                    "line_index": source_line_index,
                    "bbox": list(source_line.get("bbox", (0, 0, 0, 0))),
                    "spans": list(source_line.get("spans", [])),
                }]
                lines.append(annotated)

            # Merge horizontally adjacent line fragments first: LaTeX
            # templates often emit the section number and the title as
            # two separate "lines" sharing one visual baseline.
            lines = self._merge_inline_segments(lines)

            # Split each PyMuPDF line into a separate Block.
            # This ensures headings on their own line get their own
            # Block (critical for no-style documents where the only
            # signal is the numbering pattern).
            for line_index, line in enumerate(lines):
                line_spans = line.get("spans", [])
                full_text = self._join_line_spans(line_spans)
                if not full_text:
                    continue

                # Physical features from this line's spans
                font_size = self._dominant_font_size(line_spans)
                is_bold = self._is_majority_bold(line_spans)
                has_numbering = self._has_heading_numbering(full_text)

                # Heading detection: font significantly larger than body
                is_heading = False
                if font_size and body_font_size > 0:
                    is_heading = font_size > body_font_size * 1.15

                bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
                x0, y0, x1, y1 = bbox

                # Infer alignment from line bbox
                left_margin = x0
                right_margin = page_width - x1
                if page_width > 0 and abs(left_margin - right_margin) < page_width * 0.10:
                    alignment = "center"
                elif page_width > 0 and left_margin > page_width * 0.40:
                    alignment = "right"
                else:
                    alignment = "left"

                layout_column = self._infer_layout_column(bbox, page_width)
                in_table = any(
                    self._bbox_overlap_ratio(bbox, table_bbox) >= 0.80
                    for table_bbox in (table_bboxes or [])
                )
                atoms = self._build_pdf_line_atoms(
                    page_index=page_index,
                    raw_block_index=raw_block_index,
                    output_line_index=line_index,
                    line=line,
                    block_text=full_text,
                    alignment=alignment,
                )
                if in_table:
                    for atom in atoms:
                        atom["provenance"]["artifact"] = True
                        atom["provenance"]["in_table"] = True

                entries.append((page_index, layout_column, y0, x0, Block(
                    id=0,  # placeholder; assigned after sorting
                    type="text",
                    text=full_text,
                    is_bold=is_bold,
                    font_size=font_size,
                    alignment=alignment,
                    is_heading_style=is_heading,
                    has_heading_numbering=has_numbering,
                    metadata={
                        "source": "pdf",
                        "page": page_index + 1,
                        "line_index": line_index,
                        "bbox": list(bbox),
                        "page_width": page_width,
                        "page_height": page_height,
                        "layout_column": layout_column,
                        "layout_region": "unassigned",
                        "artifact": in_table,
                        "in_table": in_table,
                        "canonical": not in_table,
                        "atoms": atoms,
                    },
                )))

        return entries

    @staticmethod
    def _join_line_spans(line_spans: list[dict]) -> str:
        """Join span texts using geometric gaps instead of blind spaces.

        Small-caps headings (ICLR/ACL templates) render the leading
        capital and the remaining letters as separate spans with a
        near-zero horizontal gap; joining them with a hard space yields
        broken text like ``"I NTRODUCTION"``.  Real word boundaries are
        either an explicit space character inside a span or a gap
        comparable to a space width (~0.25em).  We therefore insert a
        space only when the geometric gap is wide enough.
        """
        parts: list[str] = []
        prev_x1: Optional[float] = None

        for span in line_spans:
            text = span.get("text", "")
            if not text.strip():
                # Whitespace-only span still marks a word boundary.
                if parts and not parts[-1].endswith(" "):
                    parts.append(" ")
                prev_x1 = None
                continue

            bbox = span.get("bbox") or (0, 0, 0, 0)
            x0, x1 = float(bbox[0]), float(bbox[2])
            size = float(span.get("size") or 0) or 12.0

            if parts and prev_x1 is not None:
                gap = x0 - prev_x1
                space_threshold = max(1.0, size * 0.18)
                already_spaced = (
                    parts[-1].endswith(" ") or text.startswith(" ")
                )
                if gap > space_threshold and not already_spaced:
                    parts.append(" ")
            elif parts and not parts[-1].endswith(" ") and not text.startswith(" "):
                # No geometry available; fall back to a space.
                parts.append(" ")

            parts.append(text)
            prev_x1 = x1

        return " ".join("".join(parts).split())

    @staticmethod
    def _merge_inline_segments(lines: list[dict]) -> list[dict]:
        """Merge PyMuPDF "lines" that share one visual baseline.

        LaTeX section headings emit the number (``2``) and the title
        (``RELATED WORK``) as two separate line entries with full
        vertical overlap and a small horizontal gap (the ``\\quad``
        between number and title).  Downstream heading detection needs
        them in one block, so we merge such fragments left-to-right.
        """
        if len(lines) < 2:
            return lines

        def v_overlap(a: tuple, b: tuple) -> float:
            top = max(a[1], b[1])
            bottom = min(a[3], b[3])
            height = min(a[3] - a[1], b[3] - b[1])
            if height <= 0:
                return 0.0
            return max(0.0, bottom - top) / height

        ordered = sorted(
            lines,
            key=lambda ln: (round(float(ln.get("bbox", (0, 0, 0, 0))[1]), 1),
                            float(ln.get("bbox", (0, 0, 0, 0))[0])),
        )

        merged: list[dict] = []
        for line in ordered:
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            spans = line.get("spans", [])
            if merged:
                prev = merged[-1]
                prev_bbox = tuple(float(v) for v in prev.get("bbox", (0, 0, 0, 0)))
                prev_size = max(
                    (float(s.get("size") or 0) for s in prev.get("spans", [])),
                    default=12.0,
                ) or 12.0
                gap = bbox[0] - prev_bbox[2]
                if (
                    v_overlap(prev_bbox, bbox) >= 0.6
                    and -1.0 <= gap <= prev_size * 2.0
                ):
                    prev["spans"] = list(prev.get("spans", [])) + list(spans)
                    prev["_source_lines"] = (
                        list(prev.get("_source_lines", []))
                        + list(line.get("_source_lines", []))
                    )
                    prev["bbox"] = (
                        min(prev_bbox[0], bbox[0]),
                        min(prev_bbox[1], bbox[1]),
                        max(prev_bbox[2], bbox[2]),
                        max(prev_bbox[3], bbox[3]),
                    )
                    continue
            merged.append(dict(line))

        return merged

    def _estimate_body_font_size(self, doc: "fitz.Document") -> float:
        """Estimate the document's body font size.

        Computes the *character-weighted* mode font size, sampling up
        to 50 pages for performance.  Weighting by span character count
        (rather than counting spans) is essential: reference lists,
        table cells, and figure annotations produce thousands of short
        small-font spans, and a plain span-count mode picks their size
        as "body".  On the GPT-4 technical report that mis-estimated
        body as 7pt instead of 10pt, flagging 69% of text blocks as
        heading-style and breaking paragraph merging document-wide.
        Long body paragraphs dominate by character mass, which makes
        the estimate robust to span fragmentation.
        """
        from collections import Counter

        sizes: Counter[float] = Counter()
        sample_pages = min(len(doc), 50)

        for i in range(sample_pages):
            page_dict = doc[i].get_text("dict")
            for raw_block in page_dict.get("blocks", []):
                if raw_block.get("type") != 0:
                    continue
                for line in raw_block.get("lines", []):
                    for span in line.get("spans", []):
                        size = span.get("size", 0)
                        text = span.get("text", "")
                        weight = self._non_whitespace_weight(text)
                        if size > 0 and weight:
                            sizes[round(size, 1)] += weight

        if not sizes:
            return 12.0

        # Character-weighted mode = the size carrying the most text,
        # which is the body text size in the vast majority of documents.
        return sizes.most_common(1)[0][0]

    @staticmethod
    def _non_whitespace_weight(text: str) -> int:
        return sum(1 for char in (text or "") if not char.isspace())

    @classmethod
    def _atom_weight_stats(cls, atoms: list[dict]) -> tuple[Counter, int, int]:
        """Character-mass stats over atoms: (size_weights, bold_weight, total)."""
        size_weights: Counter = Counter()
        bold_weight = 0
        total_weight = 0
        for atom in atoms:
            weight = cls._non_whitespace_weight(str(atom.get("text", "")))
            if not weight:
                continue
            total_weight += weight
            if atom.get("font_size"):
                size_weights[round(float(atom["font_size"]), 1)] += weight
            if atom.get("is_bold") is True:
                bold_weight += weight
        return size_weights, bold_weight, total_weight

    @classmethod
    def _dominant_font_size(cls, spans: list[dict]) -> Optional[float]:
        """Return the font size carrying the most non-whitespace characters."""
        if not spans:
            return None

        from collections import Counter
        sizes: Counter[float] = Counter()
        for span in spans:
            size = span.get("size", 0)
            weight = cls._non_whitespace_weight(span.get("text", ""))
            if size > 0 and weight:
                sizes[round(size, 1)] += weight

        if not sizes:
            return None
        return sizes.most_common(1)[0][0]

    @classmethod
    def _dominant_font_family(cls, spans: list[dict]) -> Optional[str]:
        """Return the font family carrying the most non-whitespace characters."""
        from collections import Counter

        fonts: Counter[str] = Counter()
        for span in spans:
            name = str(span.get("font") or "").strip()
            weight = cls._non_whitespace_weight(span.get("text", ""))
            if name and weight:
                fonts[name] += weight
        return fonts.most_common(1)[0][0] if fonts else None

    @staticmethod
    def _has_heading_numbering(text: str) -> bool:
        """Return ``True`` if *text* starts with a heading numbering pattern.

        Matches patterns like:
        - ``1. Introduction``
        - ``2.1 Background``
        - ``3.2.1 Methods``
        - ``1）简介``
        - ``第一章 总则``
        """
        import re
        if not text:
            return False
        t = text.strip()
        # Arabic numbering: 1. / 2.1 / 3.2.1 / 1）/ 1)
        if re.match(r"^\d+(\.\d+)+\.?\s", t):
            return True
        # Bare top-level numbers are capped at two digits: "100. Smith,
        # J." is a reference-list entry, not a section heading.  Real
        # chapter numbering virtually never exceeds 99 at the top level,
        # while reference lists routinely do.
        if re.match(r"^\d{1,2}[.)）]\s", t):
            return True
        # CJK chapter numbering: 第一章 / 第2节
        if re.match(r"^第[一二三四五六七八九十百千\d]+[章节条款编篇]", t):
            return True
        # Appendix numbering: A. / B.1 / Appendix A
        if re.match(r"^[A-Z](\.\d+)+\.?\s", t):
            return True
        if re.match(r"^Appendix\s+[A-Z]", t, re.IGNORECASE):
            return True
        return False

    @classmethod
    def _is_majority_bold(cls, spans: list[dict]) -> bool:
        """Return True when bold spans carry over half the visible characters."""
        if not spans:
            return False

        bold_weight = 0
        total_weight = 0
        for span in spans:
            weight = cls._non_whitespace_weight(span.get("text", ""))
            if not weight:
                continue
            total_weight += weight
            flags = span.get("flags", 0)
            # PyMuPDF flags: bit 4 = bold, bit 6 = bold (alternate)
            if flags & (1 << 4) or flags & (1 << 6):
                bold_weight += weight

        return total_weight > 0 and bold_weight > total_weight / 2

    @staticmethod
    def _infer_alignment(raw_block: dict, page_width: float) -> Optional[str]:
        """Infer paragraph alignment from the block's bounding box.

        Heuristic:
        - Centred: left margin ≈ right margin (within 10% of page width).
        - Right-aligned: left margin > 40% of page width.
        - Otherwise: left-aligned.
        """
        bbox = raw_block.get("bbox")
        if not bbox or page_width <= 0:
            return "left"

        x0, _, x1, _ = bbox
        left_margin = x0
        right_margin = page_width - x1

        # Centred: margins are roughly equal
        if abs(left_margin - right_margin) < page_width * 0.10:
            return "center"

        # Right-aligned: large left margin
        if left_margin > page_width * 0.40:
            return "right"

        return "left"

    # ── Image extraction ───────────────────────────────────────

    @staticmethod
    def _infer_layout_column(
        bbox: tuple[float, float, float, float],
        page_width: float,
    ) -> int:
        """Infer a coarse reading-order column from a line bbox."""
        if page_width <= 0:
            return 0
        x0, _, x1, _ = bbox
        left_margin = x0
        right_margin = page_width - x1
        if abs(left_margin - right_margin) < page_width * 0.10:
            return 0
        center_x = (x0 + x1) / 2
        return 1 if center_x > page_width * 0.52 else 0

    @staticmethod
    def _bbox_overlap_ratio(
        bbox: tuple[float, float, float, float] | list[float],
        container: tuple[float, float, float, float] | list[float],
    ) -> float:
        """Intersection area divided by *bbox* area."""
        if len(bbox) < 4 or len(container) < 4:
            return 0.0
        x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        cx0, cy0, cx1, cy1 = (float(v) for v in container[:4])
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        if area <= 0:
            return 0.0
        ix0, iy0 = max(x0, cx0), max(y0, cy0)
        ix1, iy1 = min(x1, cx1), min(y1, cy1)
        intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        return intersection / area

    def _build_pdf_line_atoms(
        self,
        *,
        page_index: int,
        raw_block_index: int,
        output_line_index: int,
        line: dict,
        block_text: str,
        alignment: str,
    ) -> list[dict]:
        """Build one auditable atom for every original PyMuPDF line."""
        atoms: list[dict] = []
        source_lines = line.get("_source_lines") or [{
            "line_index": output_line_index,
            "bbox": line.get("bbox", (0, 0, 0, 0)),
            "spans": line.get("spans", []),
        }]
        for source_line in source_lines:
            spans = list(source_line.get("spans", []))
            source_text = self._join_line_spans(spans)
            if not source_text:
                continue
            source_span = {
                "page": page_index + 1,
                "block": raw_block_index,
                "line": int(source_line.get("line_index", output_line_index)),
            }
            bbox = [float(v) for v in source_line.get("bbox", (0, 0, 0, 0))]
            atom = StructuralAtom.create(
                source="pdf_line",
                source_span=source_span,
                block_id=None,
                text=source_text,
                page=page_index + 1,
                bbox=bbox,
                font_family=self._dominant_font_family(spans),
                font_size=self._dominant_font_size(spans),
                is_bold=self._is_majority_bold(spans),
                alignment=alignment,
                region="unassigned",
                provenance={
                    "provider": "pymupdf",
                    "output_line_index": output_line_index,
                },
            ).to_metadata()
            atoms.append(atom)
        self._recalculate_atom_offsets(block_text, atoms)
        return atoms

    @staticmethod
    def _recalculate_atom_offsets(block_text: str, atoms: list[dict]) -> None:
        """Project ordered physical atoms onto one gap-free canonical partition.

        ``atom.text`` remains the physical source text.  Canonical separators,
        removed hyphens, and presentation wrappers are represented only by
        ``provenance.canonical_text``.  The resulting half-open ranges are
        monotonic, contiguous, and together cover the complete block text.
        """
        if not atoms:
            return

        matches: list[tuple[int, int, str, bool]] = []
        cursor = 0
        for atom in atoms:
            provenance = atom.setdefault("provenance", {})
            core = str(provenance.get(
                "canonical_core",
                provenance.get("canonical_text", atom.get("text", "")),
            ))
            start = block_text.find(core, cursor) if core else cursor
            exact = start >= 0
            if not exact and core.strip():
                trimmed = core.strip()
                trimmed_start = block_text.find(trimmed, cursor)
                if trimmed_start >= 0:
                    core = trimmed
                    start = trimmed_start
                    exact = True
                    provenance["canonical_core"] = core
                    provenance["join_normalization"] = (
                        "physical-edge-whitespace-trimmed"
                    )
            if not exact:
                start = min(cursor, len(block_text))
                end = min(len(block_text), start + len(core))
                provenance["offset_alignment"] = "approximate"
            else:
                end = start + len(core)
                provenance.pop("offset_alignment", None)
            matches.append((start, end, core, exact))
            cursor = end

        segment_start = 0
        for index, atom in enumerate(atoms):
            provenance = atom.setdefault("provenance", {})
            _, _, core, exact = matches[index]
            segment_end = (
                matches[index + 1][0] if index + 1 < len(matches)
                else len(block_text)
            )
            segment_end = max(segment_start, min(segment_end, len(block_text)))
            canonical_text = block_text[segment_start:segment_end]
            atom["char_start"] = segment_start
            atom["char_end"] = segment_end

            source_text = str(atom.get("text", ""))
            if canonical_text != source_text:
                provenance.setdefault("canonical_core", core)
                provenance["canonical_text"] = canonical_text
            else:
                provenance.pop("canonical_text", None)
                if provenance.get("canonical_core") == source_text:
                    provenance.pop("canonical_core", None)
            if not exact:
                provenance["offset_alignment"] = "approximate"
            segment_start = segment_end

    def _deduplicate_table_text(
        self,
        text_entries: list[tuple[int, int, float, float, Block]],
        table_entries: list[tuple[int, int, float, float, Block]],
    ) -> list[tuple[int, int, float, float, Block]]:
        """Make table blocks canonical while retaining overlapping line atoms."""
        canonical: list[tuple[int, int, float, float, Block]] = []
        touched_tables: dict[int, Block] = {}
        for entry in text_entries:
            block = entry[4]
            metadata = block.metadata or {}
            if not metadata.get("in_table"):
                canonical.append(entry)
                continue

            bbox = metadata.get("bbox") or [0, 0, 0, 0]
            matches = [
                table_entry for table_entry in table_entries
                if self._bbox_overlap_ratio(
                    bbox, (table_entry[4].metadata or {}).get("bbox", [0, 0, 0, 0])
                ) >= 0.80
            ]
            if not matches:
                # If no table representation can receive this physical text,
                # promote the surviving text block to the canonical container.
                metadata["artifact"] = False
                metadata["canonical"] = True
                for atom in metadata.get("atoms", []):
                    atom.setdefault("provenance", {})["canonical_container"] = "text"
                block.metadata = metadata
                canonical.append(entry)
                continue

            target = max(
                matches,
                key=lambda table_entry: self._bbox_overlap_ratio(
                    bbox, (table_entry[4].metadata or {}).get("bbox", [0, 0, 0, 0])
                ),
            )[4]
            target_meta = target.metadata or {}
            target_meta.update({
                "artifact": False,
                "in_table": True,
                "canonical": True,
            })
            target_atoms = target_meta.setdefault("atoms", [])
            for atom in metadata.get("atoms", []):
                atom.setdefault("provenance", {})["canonical_container"] = "table"
                target_atoms.append(atom)
            target.metadata = target_meta
            touched_tables[id(target)] = target

        # Rebase only after all physical lines have migrated.  Recalculating
        # incrementally would let the first atom consume trailing Markdown
        # syntax and leave later atoms with approximate or overlapping ranges.
        for target in touched_tables.values():
            target_atoms = (target.metadata or {}).get("atoms", [])
            self._recalculate_atom_offsets(target.text or "", target_atoms)
        return canonical

    def _assign_layout_regions(
        self,
        entries: list[tuple[int, int, float, float, Block]],
        page_width: float,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Infer full-width bands plus any number of dynamic column regions."""
        if not entries or page_width <= 0:
            return entries

        raw_descriptors: list[
            tuple[tuple[int, int, float, float, Block], float, float, float, bool]
        ] = []
        column_x0s: list[float] = []
        for entry in entries:
            block = entry[4]
            bbox = (block.metadata or {}).get("bbox") or [entry[3], entry[2], entry[3], entry[2]]
            x0, x1 = float(bbox[0]), float(bbox[2])
            width = max(0.0, x1 - x0)
            is_centered = block.alignment == "center"
            raw_descriptors.append((entry, x0, float(bbox[1]), width, is_centered))
            if block.type == "text" and width > 0 and not is_centered:
                column_x0s.append(x0)

        # Cluster x origins; a wide gap starts another layout column.  This
        # naturally supports one, two, three, or more columns.
        clusters: list[list[float]] = []
        tolerance = max(24.0, page_width * 0.12)
        for x0 in sorted(column_x0s):
            if not clusters or x0 - (sum(clusters[-1]) / len(clusters[-1])) > tolerance:
                clusters.append([x0])
            else:
                clusters[-1].append(x0)
        centers = [sum(cluster) / len(cluster) for cluster in clusters] or [0.0]
        has_multiple_columns = len(centers) > 1
        descriptors = [
            (
                entry,
                is_centered or (has_multiple_columns and width >= page_width * 0.62),
                x0,
                y0,
            )
            for entry, x0, y0, width, is_centered in raw_descriptors
        ]

        full_y_values: list[float] = []
        for _, full_width, _, y0 in descriptors:
            if full_width and not any(abs(y0 - known) <= 2.0 for known in full_y_values):
                full_y_values.append(y0)
        full_y_values.sort()

        result: list[tuple[int, int, float, float, Block]] = []
        for entry, full_width, x0, y0 in descriptors:
            page, _, entry_y, entry_x, block = entry
            metadata = block.metadata or {}
            if full_width:
                region = "full-width"
                column = 0
                separator_index = min(
                    range(len(full_y_values)),
                    key=lambda index: abs(full_y_values[index] - y0),
                ) if full_y_values else 0
                band = separator_index * 2 + 1
            else:
                column_index = min(
                    range(len(centers)), key=lambda index: abs(centers[index] - x0)
                )
                region = f"column-{column_index + 1}"
                column = column_index + 1
                band = 2 * sum(1 for separator_y in full_y_values if separator_y < y0)
            metadata["layout_region"] = region
            metadata["layout_column"] = column
            metadata["layout_band"] = band
            for atom in metadata.get("atoms", []):
                atom["region"] = region
            block.metadata = metadata
            result.append((page, column, entry_y, entry_x, block))
        return result

    @staticmethod
    def _assign_atom_vertical_gaps(
        entries: list[tuple[int, int, float, float, Block]],
    ) -> None:
        """Populate neighbouring physical-line gaps within each layout region."""
        from collections import defaultdict

        groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
        for page, _, _, _, block in entries:
            metadata = block.metadata or {}
            region = str(metadata.get("layout_region", "unassigned"))
            for atom in metadata.get("atoms", []):
                if atom.get("bbox") and len(atom["bbox"]) >= 4:
                    groups[(page, region)].append(atom)
        for atoms in groups.values():
            atoms.sort(key=lambda atom: (float(atom["bbox"][1]), float(atom["bbox"][0])))
            for index, atom in enumerate(atoms):
                atom.setdefault("vertical_gap_before", None)
                atom.setdefault("vertical_gap_after", None)
                if index:
                    atom["vertical_gap_before"] = max(
                        0.0, float(atom["bbox"][1]) - float(atoms[index - 1]["bbox"][3])
                    )
                if index + 1 < len(atoms):
                    atom["vertical_gap_after"] = max(
                        0.0, float(atoms[index + 1]["bbox"][1]) - float(atom["bbox"][3])
                    )

    @staticmethod
    def _entry_sort_key(entry: tuple[int, int, float, float, Block]) -> tuple:
        metadata = entry[4].metadata or {}
        return (
            entry[0],
            int(metadata.get("layout_band", 0)),
            entry[1],
            entry[2],
            entry[3],
        )

    @staticmethod
    def _set_atom_block_id(block: Block, block_id: int) -> None:
        metadata = block.metadata or {}
        for atom in metadata.get("atoms", []):
            atom["block_id"] = block_id
        block.metadata = metadata

    @staticmethod
    def _json_safe(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): PdfProvider._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [PdfProvider._json_safe(item) for item in value]
        return str(value)

    @classmethod
    def _extract_toc_entries(cls, doc) -> list[dict]:
        try:
            raw_toc = doc.get_toc(simple=False) or []
        except TypeError:
            raw_toc = doc.get_toc() or []
        except Exception:
            return []

        entries: list[dict] = []
        for index, raw in enumerate(raw_toc):
            if not isinstance(raw, (list, tuple)) or len(raw) < 3:
                continue
            try:
                level, title, page = int(raw[0]), str(raw[1]).strip(), int(raw[2])
            except (TypeError, ValueError):
                continue
            if not title or page < 1:
                continue
            destination = cls._json_safe(raw[3]) if len(raw) > 3 else {"page": page}
            entries.append({
                "index": index,
                "level": level,
                "title": title,
                "page": page,
                "destination": destination,
            })
        return entries

    @staticmethod
    def _normalise_toc_title(text: str, *, strip_numbering: bool = False) -> str:
        import re
        import unicodedata

        value = unicodedata.normalize("NFKC", text or "").casefold().strip()
        if strip_numbering:
            value = re.sub(
                r"^(?:第[一二三四五六七八九十百千\d]+[章节条款编篇]|"
                r"appendix\s+[a-z0-9]+(?:\.\d+)*|"
                r"\d+(?:\.\d+)*[.)、：:]?|"
                r"[a-z][.)]|[a-z](?:\.\d+)+\.?)\s*",
                "",
                value,
                flags=re.IGNORECASE,
            )
        return "".join(char for char in value if char.isalnum())

    def _apply_toc_metadata(
        self,
        entries: list[tuple[int, int, float, float, Block]],
        toc_entries: list[dict],
    ) -> None:
        """Monotonically align outline entries by page and normalized title."""
        from difflib import SequenceMatcher

        last_match = -1
        for toc in toc_entries:
            toc_norm = self._normalise_toc_title(toc["title"])
            toc_without_number = self._normalise_toc_title(
                toc["title"], strip_numbering=True,
            )
            best: Optional[tuple[float, int]] = None
            for index in range(last_match + 1, len(entries)):
                page_index, _, _, _, block = entries[index]
                if page_index + 1 < toc["page"]:
                    continue
                if page_index + 1 > toc["page"]:
                    break
                if block.type != "text" or not block.text:
                    continue
                text_norm = self._normalise_toc_title(block.text)
                text_without_number = self._normalise_toc_title(
                    block.text, strip_numbering=True,
                )
                if not text_norm:
                    continue
                if toc_norm == text_norm or (
                    toc_without_number and toc_without_number == text_without_number
                ):
                    score = 1.0
                else:
                    score = max(
                        SequenceMatcher(None, toc_norm, text_norm).ratio(),
                        SequenceMatcher(None, toc_without_number, text_without_number).ratio()
                        if toc_without_number and text_without_number else 0.0,
                    )
                if score >= 0.90 and (best is None or score > best[0]):
                    best = (score, index)
            if best is None:
                continue

            score, match_index = best
            block = entries[match_index][4]
            metadata = block.metadata or {}
            metadata["toc_destination"] = toc["destination"]
            metadata["toc_level"] = toc["level"]
            provenance = metadata.setdefault("provenance", {})
            provenance["toc"] = {
                "outline_index": toc["index"],
                "title": toc["title"],
                "page": toc["page"],
                "match_score": round(score, 4),
                "matching": "page+normalized-title+monotonic",
            }
            for atom in metadata.get("atoms", []):
                atom.setdefault("provenance", {})["toc"] = dict(provenance["toc"])
            block.metadata = metadata
            last_match = match_index

    @staticmethod
    def _normalise_marginal_text(text: str | None) -> str:
        """Normalize header/footer text for repeated-line detection."""
        import re

        value = " ".join((text or "").strip().lower().split())
        value = re.sub(r"\b\d+\b", "#", value)
        return value

    def _filter_repeating_marginal_text(
        self,
        entries: list[tuple[int, int, float, float, Block]],
        page_count: int,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Remove repeated short top/bottom lines that look like headers/footers."""
        if page_count < 2:
            return entries

        from collections import defaultdict

        occurrences: dict[tuple[str, str], set[int]] = defaultdict(set)
        for page_index, _, _, _, block in entries:
            if block.type != "text" or not block.text:
                continue
            metadata = block.metadata or {}
            bbox = metadata.get("bbox") or [0, 0, 0, 0]
            page_height = metadata.get("page_height") or 0
            if not page_height:
                continue
            y0, y1 = float(bbox[1]), float(bbox[3])
            zone = ""
            if y0 <= page_height * 0.06:
                zone = "header"
            elif y1 >= page_height * 0.94:
                zone = "footer"
            if not zone or len(block.text.strip()) > 120:
                continue
            normalized = self._normalise_marginal_text(block.text)
            if normalized:
                occurrences[(normalized, zone)].add(page_index)

        threshold = min(page_count, max(2, page_count // 2))
        repeated = {
            key for key, pages in occurrences.items()
            if len(pages) >= threshold
        }
        if not repeated:
            return entries

        filtered: list[tuple[int, int, float, float, Block]] = []
        removed = 0
        for entry in entries:
            block = entry[4]
            if block.type != "text" or not block.text:
                filtered.append(entry)
                continue
            metadata = block.metadata or {}
            bbox = metadata.get("bbox") or [0, 0, 0, 0]
            page_height = metadata.get("page_height") or 0
            zone = ""
            if page_height:
                y0, y1 = float(bbox[1]), float(bbox[3])
                if y0 <= page_height * 0.06:
                    zone = "header"
                elif y1 >= page_height * 0.94:
                    zone = "footer"
            key = (self._normalise_marginal_text(block.text), zone)
            if zone and key in repeated:
                removed += 1
                continue
            filtered.append(entry)

        if removed:
            logger.info("[PdfProvider] Removed %d repeated header/footer lines", removed)
        return filtered

    @staticmethod
    def _typical_line_width(
        entries: list[tuple[int, int, float, float, Block]],
    ) -> float:
        """Median text-line width — the baseline for "occupies a full line".

        Body lines wrap to fill the column, so their width clusters near
        the median.  A standalone heading occupies far less, which lets
        the merge guard tell a short title apart from a wrapped body line
        without any semantic analysis.
        """
        widths: list[float] = []
        for _, _, _, _, block in entries:
            if block.type != "text" or not block.text:
                continue
            bbox = (block.metadata or {}).get("bbox")
            if bbox and len(bbox) >= 4:
                width = float(bbox[2]) - float(bbox[0])
                if width > 0:
                    widths.append(width)
        if not widths:
            return 0.0
        widths.sort()
        return widths[len(widths) // 2]

    @classmethod
    def _looks_like_standalone_heading(
        cls,
        block: Block,
        typical_line_width: float,
    ) -> bool:
        """No-style fallback: is this a short, independent title line?

        In documents with zero formatting cues (uniform font, no bold,
        no numbering, no heading style) a heading is physically identical
        to body text except that it sits on its own short line that does
        not fill the column and is not a complete sentence.  Merging such
        a line into the adjacent paragraph destroys the only evidence the
        downstream stages have, so the merge step must keep it separate.

        Only physical / textual features are used (line width, length,
        terminal punctuation) — the semantic call stays with the LLM.
        Keeping an extra standalone block is cheap (the LLM simply rules
        it out as a non-heading); losing a heading at extraction time is
        irreversible.
        """
        if block.type != "text" or not block.text:
            return False
        text = " ".join(block.text.split())
        if not text:
            return False
        # A sentence-final / connective ending marks prose, not a title.
        if text.endswith(_HEADINGISH_SENTENCE_END):
            return False
        if len(text) > _STANDALONE_HEADING_MAX_CHARS:
            return False
        # Must not fill the column: wrapped body lines (including the
        # narrow columns of two-column papers) reach near the typical
        # width and are therefore excluded, keeping formatted documents
        # untouched.
        if typical_line_width > 0:
            bbox = (block.metadata or {}).get("bbox")
            if bbox and len(bbox) >= 4:
                width = float(bbox[2]) - float(bbox[0])
                if width >= typical_line_width * _STANDALONE_HEADING_WIDTH_RATIO:
                    return False
        return True

    @classmethod
    def _can_merge_text_lines(
        cls,
        prev: Block,
        curr: Block,
        body_font_size: float = 0.0,
        typical_line_width: float = 0.0,
    ) -> bool:
        """Return True when two PDF lines are likely one paragraph."""
        if prev.type != "text" or curr.type != "text":
            return False
        if not prev.text or not curr.text:
            return False
        # Heading evidence is judged against the document's actual body
        # font, not the 12pt default: a 10pt heading over a 9pt body
        # must stay separate, and a uniformly 16pt document must not
        # treat every line as a potential title.
        min_body = body_font_size if body_font_size > 0 else 12.0
        if (
            prev.is_heading_style
            or curr.is_heading_style
            or prev.has_heading_numbering
            or curr.has_heading_numbering
            or prev.is_potential_title(min_body_size=min_body)
            or curr.is_potential_title(min_body_size=min_body)
            # No-style guard: never swallow a short standalone title line
            # into a paragraph (the recovery path for featureless docs).
            or cls._looks_like_standalone_heading(prev, typical_line_width)
            or cls._looks_like_standalone_heading(curr, typical_line_width)
        ):
            return False
        prev_meta = prev.metadata or {}
        curr_meta = curr.metadata or {}
        if prev_meta.get("page") != curr_meta.get("page"):
            return False
        if prev_meta.get("layout_column") != curr_meta.get("layout_column"):
            return False
        if prev_meta.get("layout_band", 0) != curr_meta.get("layout_band", 0):
            return False
        if (prev.alignment or "left") != (curr.alignment or "left"):
            return False
        if prev.alignment == "center" or curr.alignment == "center":
            return False
        if prev.font_size and curr.font_size and abs(prev.font_size - curr.font_size) > 0.5:
            return False

        prev_bbox = prev_meta.get("bbox") or [0, 0, 0, 0]
        curr_bbox = curr_meta.get("bbox") or [0, 0, 0, 0]
        gap = float(curr_bbox[1]) - float(prev_bbox[3])
        font_size = curr.font_size or prev.font_size or 12.0
        if gap < -1 or gap > max(7.0, font_size * 0.75):
            return False
        if abs(float(curr_bbox[0]) - float(prev_bbox[0])) > 36:
            return False
        return True

    def _merge_text_lines(
        self,
        entries: list[tuple[int, int, float, float, Block]],
        body_font_size: float = 0.0,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Merge consecutive PDF body lines into paragraph blocks."""
        if not entries:
            return entries

        typical_line_width = self._typical_line_width(entries)
        sorted_entries = sorted(entries, key=self._entry_sort_key)
        merged: list[tuple[int, int, float, float, Block]] = []
        merge_count = 0
        # 尾部块的运行权重统计（字号分布/加粗量/总量）。旧实现每合并一行就
        # 全量重扫已积累的所有 atoms，段落越长越慢（O(行²) 字符扫描）；权重
        # 可加，增量维护与全量重算逐值相等。首次合并时懒初始化。
        tail_stats: tuple[Counter, int, int] | None = None

        for entry in sorted_entries:
            page, column, y0, x0, block = entry
            if not merged or not self._can_merge_text_lines(
                merged[-1][4], block, body_font_size, typical_line_width,
            ):
                merged.append(entry)
                tail_stats = None
                continue

            prev_page, prev_column, prev_y0, prev_x0, prev_block = merged[-1]
            # Soft-hyphenation repair: drop exactly ONE trailing hyphen
            # when joining ("infor-" + "mation" -> "information").
            # The earlier ``rstrip("- ")`` stripped *all* trailing
            # hyphens/spaces, so an em-dash line ending ("--") or a
            # dash bullet lost characters and words got glued together.
            prev_text = (prev_block.text or "").rstrip()
            soft_hyphen_join = prev_text.endswith("-") and not prev_text.endswith("--")
            if soft_hyphen_join:
                prev_block.text = prev_text[:-1] + (block.text or "").lstrip()
            else:
                prev_block.text = prev_text + " " + (block.text or "").lstrip()

            prev_meta = prev_block.metadata or {}
            curr_meta = block.metadata or {}
            prev_atoms = list(prev_meta.get("atoms", []))
            curr_atoms = list(curr_meta.get("atoms", []))
            if soft_hyphen_join and prev_atoms:
                last_atom = prev_atoms[-1]
                source_text = str(last_atom.get("text", ""))
                if source_text.rstrip().endswith("-"):
                    canonical_core = source_text.rstrip()[:-1]
                    provenance = last_atom.setdefault("provenance", {})
                    provenance["canonical_core"] = canonical_core
                    provenance.pop("canonical_text", None)
                    provenance["join_normalization"] = "soft-hyphen-removed"
            prev_meta["atoms"] = prev_atoms + curr_atoms
            self._recalculate_atom_offsets(prev_block.text or "", prev_meta["atoms"])

            # Recompute paragraph-level formatting from atom character mass
            # (incrementally: weights are additive, so folding only the new
            # atoms into the running counters equals a full rescan).
            if tail_stats is None:
                tail_stats = self._atom_weight_stats(prev_atoms)
            size_weights, bold_weight, total_weight = tail_stats
            curr_sizes, curr_bold, curr_total = self._atom_weight_stats(curr_atoms)
            size_weights.update(curr_sizes)
            bold_weight += curr_bold
            total_weight += curr_total
            tail_stats = (size_weights, bold_weight, total_weight)
            if size_weights:
                prev_block.font_size = size_weights.most_common(1)[0][0]
            prev_block.is_bold = total_weight > 0 and bold_weight > total_weight / 2

            prev_bbox = prev_meta.get("bbox") or [prev_x0, prev_y0, prev_x0, prev_y0]
            curr_bbox = curr_meta.get("bbox") or [x0, y0, x0, y0]
            union_bbox = [
                min(float(prev_bbox[0]), float(curr_bbox[0])),
                min(float(prev_bbox[1]), float(curr_bbox[1])),
                max(float(prev_bbox[2]), float(curr_bbox[2])),
                max(float(prev_bbox[3]), float(curr_bbox[3])),
            ]
            prev_meta["bbox"] = union_bbox
            prev_meta["merged_lines"] = int(prev_meta.get("merged_lines", 1)) + 1
            prev_meta["line_span"] = [
                prev_meta.get("line_span", [prev_meta.get("line_index", 0)])[0],
                curr_meta.get("line_index", prev_meta.get("line_index", 0)),
            ]
            prev_block.metadata = prev_meta
            merged[-1] = (prev_page, prev_column, prev_y0, prev_x0, prev_block)
            merge_count += 1

        if merge_count:
            logger.info("[PdfProvider] Merged %d body text lines", merge_count)
        return merged

    def _extract_images(
        self,
        page: "fitz.Page",
        page_index: int,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Extract embedded images from a single page.

        Returns:
            List of ``(page_index, column, y0, x0, Block)`` tuples.
        """
        entries: list[tuple[int, int, float, float, Block]] = []

        try:
            image_list = page.get_images(full=True)
        except Exception:
            return entries

        for img_info in image_list:
            xref = img_info[0]
            try:
                base_image = page.parent.extract_image(xref)
                if not base_image:
                    continue
                image_bytes = base_image.get("image")
                if not image_bytes:
                    continue

                ext = base_image.get("ext", "png")
                if ext not in _IMAGE_EXTENSIONS:
                    continue

                b64 = base64.b64encode(image_bytes).decode("ascii")
                data_uri = f"data:image/{ext};base64,{b64}"

                # Use image's y-position from the page's image rects
                bbox = (0.0, 0.0, 0.0, 0.0)
                try:
                    rects = page.get_image_rects(xref)
                    if rects:
                        rect = rects[0]
                        bbox = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
                except Exception:
                    pass

                layout_column = self._infer_layout_column(bbox, page.rect.width)

                entries.append((page_index, layout_column, bbox[1], bbox[0], Block(
                    id=0,
                    type="image",
                    image_data=data_uri,
                    metadata={
                        "source": "pdf",
                        "page": page_index + 1,
                        "bbox": list(bbox),
                        "page_width": page.rect.width,
                        "page_height": page.rect.height,
                        "layout_column": layout_column,
                    },
                )))

            except Exception as e:
                logger.debug(
                    "[PdfProvider] Skipping image xref=%d on page %d: %s",
                    xref, page_index + 1, e,
                )

        return entries

    # ── Table extraction ───────────────────────────────────────

    def _extract_tables(
        self,
        page: "fitz.Page",
        page_index: int,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Extract tables from a single page using PyMuPDF's table finder.

        Deduplicates overlapping tables (PyMuPDF may return multiple
        bbox variants for the same table, especially with merged cells).

        Returns:
            List of ``(page_index, column, y0, x0, Block)`` tuples.
        """
        entries: list[tuple[int, int, float, float, Block]] = []
        seen_bboxes: list[tuple[float, float, float, float]] = []

        # find_tables 默认 lines 策略只能依赖矢量路径（边框线/填充矩形）成表，
        # 而它内部的 make_chars 会先把整页字符转成 Python 对象（纯 Python、
        # 极慢）。纯文本页（无任何矢量路径）不可能出表格，先跳过。
        try:
            if not page.get_cdrawings():
                return entries
        except Exception:
            pass

        try:
            table_finder = page.find_tables()
        except Exception:
            return entries

        for table in table_finder.tables:
            try:
                # Dedup: skip if bbox overlaps significantly with an
                # already-seen table (>80% overlap).
                bbox = table.bbox
                if bbox and self._is_duplicate_bbox(bbox, seen_bboxes):
                    continue
                if bbox:
                    seen_bboxes.append(bbox)

                rows: list[list[str]] = []
                for row_data in table.extract():
                    rows.append(
                        [str(cell) if cell is not None else "" for cell in row_data]
                    )

                if not rows:
                    continue

                # GFM 渲染走全管道唯一入口：按最大列宽对齐，超宽行不截断，
                # 分隔行不再只按首行列数（旧实现遇到不规则行会错列 / 丢列）。
                md_text = Block.render_markdown_table(rows)

                bbox_tuple = tuple(float(v) for v in bbox) if bbox else (0.0, 0.0, 0.0, 0.0)
                layout_column = self._infer_layout_column(bbox_tuple, page.rect.width)

                entries.append((page_index, layout_column, bbox_tuple[1], bbox_tuple[0], Block(
                    id=0,
                    type="table",
                    text=md_text,
                    table_data={"rows": rows},
                    metadata={
                        "source": "pdf",
                        "page": page_index + 1,
                        "bbox": list(bbox_tuple),
                        "page_width": page.rect.width,
                        "page_height": page.rect.height,
                        "layout_column": layout_column,
                        "artifact": False,
                        "in_table": True,
                        "canonical": True,
                    },
                )))

            except Exception as e:
                logger.debug(
                    "[PdfProvider] Skipping table on page %d: %s",
                    page_index + 1, e,
                )

        return entries

    @staticmethod
    def _is_duplicate_bbox(
        bbox: tuple[float, float, float, float],
        seen: list[tuple[float, float, float, float]],
        overlap_threshold: float = 0.80,
    ) -> bool:
        """Return ``True`` if *bbox* overlaps significantly with any seen bbox."""
        x0, y0, x1, y1 = bbox
        area = max((x1 - x0) * (y1 - y0), 1.0)

        for sx0, sy0, sx1, sy1 in seen:
            # Compute intersection
            ix0 = max(x0, sx0)
            iy0 = max(y0, sy0)
            ix1 = min(x1, sx1)
            iy1 = min(y1, sy1)
            if ix0 < ix1 and iy0 < iy1:
                intersection = (ix1 - ix0) * (iy1 - iy0)
                if intersection / area >= overlap_threshold:
                    return True

        return False
