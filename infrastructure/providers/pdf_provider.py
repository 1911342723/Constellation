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
- Alignment: inferred from the line bounding-box relative to the
  **detected column** it sits in (``pdf_layout``), not the whole page —
  otherwise justified single-column body lines read as "centered".
- Layout: columns / gutters / vertical zones come from
  ``pdf_layout.analyze_page_layout`` *before* any line merging; a block
  is "full-width" only when it straddles a detected gutter.
- Heading style: blocks with font size significantly larger than the
  document's body median are flagged as ``is_heading_style=True``.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from collections import Counter
from typing import TYPE_CHECKING, List, Optional, Protocol, runtime_checkable

from infrastructure.models import Block, StructuralAtom, model_from_rows
from infrastructure.providers.base import InstrumentedProvider
# 直接导入子模块，不用 `from infrastructure.providers import pdf_page_roles`：
# 本模块由 `providers/__init__` 第 4 行导入，那时包对象还没绑好子模块属性，
# `from 包 import 子模块` 要先 getattr 再回退，冷导入路径上曾真实报过循环导入。
# `from 包.子模块 import 名字` 不走 getattr，任何初始化阶段都成立。
from infrastructure.providers.pdf_page_roles import (
    classify_marginal_roles as _classify_marginal_roles,
    split_canonical_and_excluded as _split_canonical_and_excluded,
)
from infrastructure.providers.pdf_reading_order import order_entries as _order_entries
from infrastructure.providers.pdf_content_relations import annotate_content_relations
from infrastructure.providers.pdf_layout import (
    REASON_FIGURE_LIKE,
    REASON_GRID_LIKE,
    REASON_SPANNING_SPLITS_COLUMNS,
    LineBox,
    PageLayout,
    analyze_page_layout,
    group_separators,
)
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


@runtime_checkable
class OcrEngine(Protocol):
    """可注入的 OCR 后端协议（E03）。

    默认路径**不引入任何新第三方依赖**（禁止 pip install / 禁止新增 requirements）：
    ``PdfProvider(ocr_engine=None)`` 时扫描/乱码页只被标 ``needs_ocr=True`` 并进
    warnings，绝不假装识别。需要真正 OCR 时，调用方注入一个满足本协议的对象——
    引擎与其依赖由调用方负责，provider 只按协议调用、按来源类型登记结果。
    """

    def recognize_page(self, page_image: bytes, *, page_number: int) -> dict:
        """识别一页的位图，返回 ``{text, confidence, source_type}`` 等结构。"""
        ...


_TEX_BOLD_FONT_RE = re.compile(
    r"^(?:cmbx|cmssbx|cmbxti|sfbx|sfsx|lmroman\d*-bold|ptmb|phvb)|-medi(?:um)?(?:ital)?$|semibold|demibold|-demi|-black|-heavy|-sb$"
)


class PdfProvider(InstrumentedProvider):
    """Extract Block objects from PDF documents.

    Uses PyMuPDF (fitz) for PDF parsing.  Each page is processed
    independently; text blocks carry physical formatting metadata
    (font_size, bold, alignment) that downstream stages rely on for
    I-frame / P-frame classification and heading detection.
    """

    def __init__(self, ocr_engine: Optional[OcrEngine] = None, *, use_outline: bool = True) -> None:
        """
        Args:
            ocr_engine: 可注入的 OCR 后端（E03）。默认 ``None``——扫描/乱码页只被
                标 ``needs_ocr`` 并进 warnings，不引入任何新依赖、不假装识别。注入
                一个满足 :class:`OcrEngine` 协议的对象即可启用真正的识别（引擎及其
                依赖由调用方负责）。
        """
        super().__init__()
        self._ocr_engine = ocr_engine
        # 评测开关：关闭后内嵌书签不进入任何块元数据（toc_destination/toc_level），
        # 用于「无目录」对照——候选证据、层级先验与对齐都看不到作者书签。
        self.use_outline = use_outline
        # 每页的版面分析结果（分栏 / gutter / 纵向片段），由 _extract_text 在建块
        # 之前算好，供行内片段合并守卫与 _assign_layout_regions 共用同一份几何。
        self._page_layouts: dict[int, PageLayout] = {}

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

        return self._extract_opened(self._open_pdf(path=file_path))

    def extract_from_bytes(self, file_bytes: bytes) -> List[Block]:
        """Extract blocks from raw PDF bytes.

        Args:
            file_bytes: Raw PDF file content.

        Returns:
            Ordered list of :class:`Block` objects.

        Raises:
            ProviderError: If the bytes cannot be parsed as PDF.
        """
        return self._extract_opened(self._open_pdf(file_bytes=file_bytes))

    def _open_pdf(self, *, path: str | None = None, file_bytes: bytes | None = None):
        try:
            import fitz  # PyMuPDF
        except ImportError:
            raise ProviderError(
                "PyMuPDF is not installed. "
                "Install it with: pip install PyMuPDF"
            ) from None
        try:
            if path is not None:
                return fitz.open(path)
            return fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as e:
            raise ProviderError(f"Cannot parse PDF: {e}") from e

    def _extract_opened(self, doc) -> List[Block]:
        """Run extraction on an opened document and always close it."""
        try:
            self._page_layouts = {}
            # First pass: collect body font size (median of all text
            # spans) for heading detection.
            body_font_size = self._estimate_body_font_size(doc)

            # PDF outline is an independent control-flow source.  It is
            # aligned only after physical blocks have reached canonical order.
            toc_entries = self._extract_toc_entries(doc) if self.use_outline else []

            # Collect blocks with layout coordinates for reading-order sorting.
            # Each entry is (page_index, layout_column, y0, x0, Block).
            unsorted: list[tuple[int, int, float, float, Block]] = []

            for page_index in range(len(doc)):
                page = doc[page_index]

                # E03：按页判定页面类型。扫描/乱码页不假装识别——原生文本页照常
                # 提取；扫描/乱码页额外登记 needs_ocr（页码 + 判定依据进 warnings），
                # 并如实报告 OCR 后端是否缺位。
                page_class = self._classify_single_page(page, page_index)
                ocr_entries = []
                if page_class["needs_ocr"]:
                    ocr_entries = self._handle_needs_ocr_page(page, page_class)

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
                # A usable OCR observation replaces only the defective native
                # text stream. Keep original image/table assets for inspection.
                if ocr_entries:
                    text_entries = ocr_entries
                text_entries = self._deduplicate_table_text(text_entries, table_entries)
                image_entries = self._extract_images(page, page_index)

                page_entries = text_entries + image_entries + table_entries
                page_entries = self._assign_layout_regions(
                    page_entries, page.rect.width,
                    layout=self._page_layouts.get(page_index),
                )
                self._assign_atom_vertical_gaps(page_entries)
                unsorted.extend(page_entries)

            unsorted = self._merge_text_lines(unsorted, body_font_size)
            # E01：不再删除重复页眉页脚——改为给页眉/页脚/目录/脚注/水印/边注打
            # source_role/role_confidence/role_reason，真实页顶标题（不重复）不受影响。
            _classify_marginal_roles(unsorted, len(doc), body_font_size)
            # E02：在 x 起点聚类 + 全宽 band + 动态列排序之上，用页面区域图表达
            # 同栏/上方/跨栏分隔/注释附属关系，做稳定拓扑排序；冲突时保留备选顺序
            # 并打 reading_order_uncertain。返回已排好序的 entries。
            unsorted = _order_entries(unsorted)
            self._apply_toc_metadata(unsorted, toc_entries)
            self._mark_front_matter(unsorted)
            # E04：阅读顺序定下来之后再判跨页续表——顺序不定就谈不上"相邻"。
            self._link_table_continuations(unsorted)

            # Assign sequential IDs after sorting and synchronize the lightweight
            # atom dictionaries without adding fields to the public Block model.
            all_blocks: List[Block] = []
            for block_id, (_, _, _, _, block) in enumerate(unsorted):
                block.id = block_id
                self._set_atom_block_id(block, block_id)
                all_blocks.append(block)

            # E01 审计视图：整条含被排除块的序列留在实例上，供 extract_audit_view()
            # 取用；默认交付仍只返回 canonical 正文（调用方行为向后兼容——被判成
            # 页眉页脚的块此前是被直接删掉的，现在同样不在默认返回序列里）。
            canonical, excluded = _split_canonical_and_excluded(all_blocks)
            annotate_content_relations(canonical)
            self._last_full_sequence = all_blocks
            self._last_excluded_blocks = excluded

            logger.info(
                "[PdfProvider] Extracted %d blocks from %d pages "
                "(%d canonical, %d role-excluded)",
                len(all_blocks), len(doc), len(canonical), len(excluded),
            )
            return canonical

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"PDF extraction failed: {e}") from e
        finally:
            doc.close()

    # ── E01 审计视图入口 ───────────────────────────────────────

    def extract_audit_view(self, file_bytes: bytes) -> dict:
        """提取并返回**含被排除块**的完整序列（E01 审计视图入口）。

        默认的 :meth:`extract_from_bytes` 只回 canonical 正文（向后兼容）。校对 / 需要
        看"什么被当成页眉页脚扣掉了"的调用方走这个入口，拿到：

        - ``blocks``：全部块（含被判成非正文角色的），逐块带 ``source_role`` /
          ``role_confidence`` / ``role_reason``。
        - ``canonical`` / ``excluded``：切好的两份，与默认交付口径一致。

        实现上复用一次真实提取（走 :meth:`extract_from_bytes` 的同一条链路），因此
        角色判定、阅读顺序、页码归一化都与默认视图逐字节一致，不存在"审计视图看到
        的和实际交付的不是一回事"。
        """
        canonical = self.extract_from_bytes(file_bytes)
        full = list(getattr(self, "_last_full_sequence", canonical))
        excluded = list(getattr(self, "_last_excluded_blocks", []))
        return {"blocks": full, "canonical": canonical, "excluded": excluded}

    # ── E03 按页选择的 OCR 通道（不引依赖）─────────────────────

    def classify_page_types(self, file_bytes: bytes) -> list[dict]:
        """按页判定 native_text / scanned / garbled（E03），不做任何识别。

        依据四个物理量：文本密度（每页非空白字符数 / 页面面积）、可读字符比例、
        异常字形比例（U+FFFD 及控制字符）、整页图片覆盖率。判定只回一份逐页报告，
        **不假装识别**扫描/乱码页——那些页标 ``needs_ocr=True`` + 页码 + 判定依据。

        返回每页一个 dict：``{page, page_type, needs_ocr, reason, metrics}``。
        """
        try:
            import fitz
        except ImportError:
            raise ProviderError(
                "PyMuPDF is not installed. Install it with: pip install PyMuPDF"
            ) from None
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as e:
            raise ProviderError(f"Cannot parse PDF: {e}") from e
        try:
            return [self._classify_single_page(doc[i], i) for i in range(len(doc))]
        finally:
            doc.close()

    def _classify_single_page(self, page: "fitz.Page", page_index: int) -> dict:
        """单页分类的纯物理判定（无 OCR，无依赖）。"""
        try:
            text = page.get_text("text") or ""
        except Exception:
            text = ""
        non_ws = [ch for ch in text if not ch.isspace()]
        char_count = len(non_ws)
        # 异常字形：替换符 U+FFFD 和不可打印控制字符（乱码 PDF 的典型信号）。
        garbled_chars = sum(
            1 for ch in non_ws
            if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t")
        )
        readable_ratio = (
            (char_count - garbled_chars) / char_count if char_count else 0.0
        )

        try:
            rect = page.rect
            page_area = max(1.0, float(rect.width) * float(rect.height))
        except Exception:
            page_area = 1.0
        text_density = char_count / page_area

        # 整页图片覆盖率：所有图片 rect 面积并集近似（取和后夹到 1.0，够判扫描页）。
        image_area = 0.0
        try:
            for img in page.get_images(full=True):
                for r in page.get_image_rects(img[0]):
                    image_area += max(0.0, float(r.x1 - r.x0)) * max(0.0, float(r.y1 - r.y0))
        except Exception:
            pass
        image_coverage = min(1.0, image_area / page_area)

        metrics = {
            "char_count": char_count,
            "text_density": round(text_density, 6),
            "readable_ratio": round(readable_ratio, 4),
            "image_coverage": round(image_coverage, 4),
            "garbled_chars": garbled_chars,
        }

        # 判定顺序：几乎无文本 + 高图片覆盖 → 扫描页；有文本但可读比例低 → 乱码页；
        # 其余 → 原生文本页。阈值刻意保守（宁可判 native 也不误报 needs_ocr）。
        if char_count < 8 and image_coverage >= 0.5:
            page_type, needs_ocr = "scanned", True
            reason = (
                f"整页几乎无可提取文本（{char_count} 字符）且图片覆盖率 "
                f"{image_coverage:.0%}，判为扫描页，需 OCR。"
            )
        elif char_count >= 8 and readable_ratio < 0.6:
            page_type, needs_ocr = "garbled", True
            reason = (
                f"有文本（{char_count} 字符）但可读字符比例仅 {readable_ratio:.0%}"
                f"（异常字形 {garbled_chars} 个），判为乱码页，需 OCR。"
            )
        else:
            page_type, needs_ocr = "native_text", False
            reason = (
                f"可提取文本 {char_count} 字符、可读比例 {readable_ratio:.0%}，"
                "判为原生文本页，直接提取。"
            )
        return {
            "page": page_index + 1,
            "page_type": page_type,
            "needs_ocr": needs_ocr,
            "reason": reason,
            "metrics": metrics,
        }

    def _handle_needs_ocr_page(self, page: "fitz.Page", page_class: dict) -> list:
        """扫描/乱码页的处理：注入了引擎就调用它，否则如实报"OCR 后端缺位"。

        无论哪条路径都**不假装识别**：无引擎时只登记一条 warning（页码 + 判定依据 +
        后端缺位这一边界），把该页交给人工/后续补 OCR，而不是静默产出空正文或伪造
        文字。``retryable=True``：装上 OCR 后端后重跑这一页是有意义的。
        """
        page_number = page_class["page"]
        if self._ocr_engine is None:
            self._note_skip(
                "pdf_page_needs_ocr",
                (
                    f"第 {page_number} 页判定为 {page_class['page_type']}："
                    f"{page_class['reason']} 当前未注入 OCR 后端（OCR 后端缺位），"
                    "该页文本未识别，仅保留页面引用。"
                ),
                source_span={"page": page_number, "page_type": page_class["page_type"]},
                retryable=True,
            )
            return []
        # 注入了引擎：调用它，失败也如实登记，绝不吞掉。
        try:
            image_bytes = page.get_pixmap().tobytes("png")
            result = self._ocr_engine.recognize_page(image_bytes, page_number=page_number)
            entries = self._ocr_text_entries(result, page, page_number - 1)
            if not entries:
                raise ValueError("OCR returned no usable text")
            self._note_skip(
                "pdf_page_ocr_applied",
                (
                    f"第 {page_number} 页经注入的 OCR 后端识别，"
                    f"置信度 {result.get('confidence', 'n/a')}；识别结果为推断文本，"
                    "非字符级无损原文。"
                ),
                source_span={"page": page_number},
                retryable=False,
            )
            return entries
        except Exception as exc:  # noqa: BLE001 — 引擎是外部注入物，任何异常都要落地
            self._note_skip(
                "pdf_page_ocr_failed",
                f"第 {page_number} 页调用注入的 OCR 后端失败：{exc}",
                source_span={"page": page_number},
                retryable=True,
            )
            return []

    @staticmethod
    def _ocr_text_entries(result: dict, page: "fitz.Page", page_index: int) -> list:
        """Convert inferred text to the same auditable block/atom stream.

        Optional block bboxes are PDF page points (not raster pixels). When an
        engine returns only text, retain paragraph breaks and declare geometry
        unknown; never fabricate font or heading evidence from OCR confidence.
        """
        import math

        if not isinstance(result, dict):
            raise ValueError("OCR result must be an object")
        records = result.get("blocks")
        if records is None:
            text = result.get("text", "")
            if not isinstance(text, str):
                raise ValueError("OCR text must be a string")
            records = [{"text": paragraph} for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
        if not isinstance(records, list):
            raise ValueError("OCR blocks must be a list")
        entries = []
        for index, record in enumerate(records):
            text = record.get("text", "")
            if not isinstance(text, str) or not text.strip():
                continue
            confidence = float(record.get("confidence", result.get("confidence", 0.0)))
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("OCR confidence must be finite and in [0, 1]")
            bbox = record.get("bbox")
            known_geometry = bbox is not None
            if bbox is None:
                bbox = [0.0, 0.0, float(page.rect.width), float(page.rect.height)]
            if len(bbox) != 4 or not all(math.isfinite(float(v)) for v in bbox):
                raise ValueError("invalid OCR bbox")
            bbox = [float(v) for v in bbox]
            if result.get("bbox_space") == "normalized":
                bbox = [bbox[0] * page.rect.width, bbox[1] * page.rect.height,
                        bbox[2] * page.rect.width, bbox[3] * page.rect.height]
            if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
                raise ValueError("inverted OCR bbox")
            metadata = {
                "source": "pdf_ocr", "source_type": "ocr", "page": page_index + 1,
                "bbox": bbox, "ocr_confidence": confidence,
                "text_fidelity": "inferred", "raw_text": text,
                "atoms": [StructuralAtom.create(source="pdf_ocr",
                    source_span={"page": page_index + 1, "ocr_block": index},
                    block_id=-1, text=text, char_start=0, char_end=len(text),
                    bbox=bbox, page=page_index + 1,
                    provenance={"source_type": "ocr", "text_fidelity": "inferred"}).model_dump(mode="json")],
            }
            if not known_geometry:
                metadata["layout_uncertain_reasons"] = ["ocr_geometry_unavailable"]
            block = Block(id=-1, type="text", text=text, metadata=metadata)
            entries.append((page_index, 0, bbox[1], bbox[0], block))
        return entries

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

        # ── 第一遍：只收集原始行几何，先做版面分析，再允许任何合并 ──────
        # 分栏必须在合并之前：窄栏距页面上同一原始块里左右两栏的同一行会被
        # "同基线 + 间隔不大"的行内合并缝成一行，之后再分栏就来不及了。
        raw_text_blocks: list[tuple[int, list[dict]]] = []
        line_boxes: list[LineBox] = []
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
                    "block_index": raw_block_index,
                    "line_index": source_line_index,
                    "bbox": list(source_line.get("bbox", (0, 0, 0, 0))),
                    "spans": list(source_line.get("spans", [])),
                }]
                lines.append(annotated)
                if not self._join_line_spans(source_line.get("spans", [])):
                    continue
                raw_bbox = tuple(float(v) for v in source_line.get("bbox", (0, 0, 0, 0)))
                # 表格单元格里的文字不参与分栏：一张四列表的单元格会排出四条
                # "栏"，把正文栏结构冲掉；它们已由表格块承载。
                in_table_cell = any(
                    self._bbox_overlap_ratio(raw_bbox, table_bbox) >= 0.80
                    for table_bbox in (table_bboxes or [])
                )
                line_boxes.append(LineBox(
                    *raw_bbox,
                    horizontal=self._line_is_horizontal(source_line),
                    is_text=not in_table_cell,
                ))
            raw_text_blocks.append((raw_block_index, lines))

        layout = analyze_page_layout(
            line_boxes, page_width, page_height,
            drawings=self._drawing_boxes(page),
        )
        self._page_layouts[page_index] = layout

        merged_blocks: list[tuple[int, list[dict]]] = []
        for raw_block_index, lines in raw_text_blocks:
            # PyMuPDF 偶尔会把窄栏距两侧的两段文字并成一行的两个 span；先按栏间
            # 空白把这种行拆开，再做行内片段合并。
            lines = self._split_lines_at_gutters(lines, layout)
            # Merge horizontally adjacent line fragments: LaTeX templates often
            # emit the section number and the title as two separate "lines"
            # sharing one visual baseline.  The layout guard refuses to stitch
            # fragments whose gap is a detected column gutter.
            lines = self._merge_inline_segments(lines, layout=layout)
            merged_blocks.append((raw_block_index, lines))
        # 编号片段（"3.2.2"）与其标题正文（"Truncated Diffusion. One can …"）落在
        # 不同原始块时，块内合并够不着；跨块只拼"编号 + 同基线右邻"这一种形态。
        merged_blocks = self._merge_numbering_fragments_across_blocks(
            merged_blocks, layout, table_bboxes,
        )

        for raw_block_index, lines in merged_blocks:
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
                run_in_prefix = self._detect_run_in_prefix(line_spans, full_text)

                # Heading detection: font significantly larger than body
                is_heading = False
                if font_size and body_font_size > 0:
                    is_heading = font_size > body_font_size * 1.15

                bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
                x0, y0, x1, y1 = bbox

                # 对齐相对于所在栏（跨栏块相对于本片段的文字范围）计算，而不是
                # 相对整页：整页口径下两栏的右栏全是"右对齐"、单栏两端对齐正文全是
                # "居中"，后者曾让单栏 PDF 的段落一行都合不起来。
                horizontal = self._line_is_horizontal(line)
                placement, alignment = self._place_line(layout, bbox, horizontal=horizontal)
                layout_column = (
                    0 if placement.spanning else placement.column_index + 1
                )
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

                block_metadata = {
                    "source": "pdf",
                    "page": page_index + 1,
                    "line_index": line_index,
                    "bbox": list(bbox),
                    "page_width": page_width,
                    "page_height": page_height,
                    "layout_column": layout_column,
                    "layout_region": "unassigned",
                    "layout_column_bounds": list(placement.reference),
                    "writing_direction": "horizontal" if horizontal else "rotated",
                    "artifact": in_table,
                    "in_table": in_table,
                    "canonical": not in_table,
                    "atoms": atoms,
                }
                if run_in_prefix:
                    block_metadata["run_in_prefix"] = run_in_prefix
                entries.append((page_index, layout_column, y0, x0, Block(
                    id=0,  # placeholder; assigned after sorting
                    type="text",
                    text=full_text,
                    is_bold=is_bold,
                    font_size=font_size,
                    alignment=alignment,
                    is_heading_style=is_heading,
                    has_heading_numbering=has_numbering,
                    metadata=block_metadata,
                )))

        return entries

    # 只有这种形态的短行才允许跨原始块拼到右邻：章节编号 / 附录编号 / 罗马数字。
    _NUMBERING_FRAGMENT_RE = re.compile(
        r"^(?:\d{1,2}(?:\.\d+){0,4}\.?|[A-Z](?:\.\d+){1,3}\.?|(?:[IVX]{1,4})\.)$"
    )

    @classmethod
    def _merge_numbering_fragments_across_blocks(
        cls,
        blocks: list[tuple[int, list[dict]]],
        layout: PageLayout,
        table_bboxes: Optional[list[tuple[float, float, float, float]]] = None,
    ) -> list[tuple[int, list[dict]]]:
        """把落在别的原始块里的编号片段拼到同基线的右邻行首。

        LaTeX 的 ``\\paragraph{}`` / 小节标题常把 ``3.2.2`` 与 ``Truncated Diffusion.
        One can …`` 放进两个原始块，块内的行内合并够不着，编号就成了孤行、标题正文
        失去编号——下游的 run-in 标题判定（A02）靠的正是行首编号。只拼"编号片段 +
        同基线、间隔不超过两倍字号、中间没有栏间空白的右邻"这一种形态，别的同行
        块（表格单元格、并排图注）一律不动。
        """
        if len(blocks) < 2:
            return blocks

        def _bbox(line: dict) -> tuple[float, float, float, float]:
            return tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))

        def _v_overlap(a, b) -> float:
            top, bottom = max(a[1], b[1]), min(a[3], b[3])
            height = min(a[3] - a[1], b[3] - b[1])
            return max(0.0, bottom - top) / height if height > 0 else 0.0

        consumed: set[tuple[int, int]] = set()
        for bi, (_, lines) in enumerate(blocks):
            for li, line in enumerate(lines):
                if (bi, li) in consumed:
                    continue
                text = cls._join_line_spans(line.get("spans", []))
                if not cls._NUMBERING_FRAGMENT_RE.match(text):
                    continue
                bbox = _bbox(line)
                if any(
                    cls._bbox_overlap_ratio(bbox, tb) >= 0.80 for tb in (table_bboxes or [])
                ):
                    continue
                size = max(
                    (float(s.get("size") or 0) for s in line.get("spans", [])), default=12.0,
                ) or 12.0
                best: tuple[float, int, int] | None = None
                for bj, (_, others) in enumerate(blocks):
                    if bj == bi:
                        continue
                    for lj, other in enumerate(others):
                        if (bj, lj) in consumed:
                            continue
                        obbox = _bbox(other)
                        gap = obbox[0] - bbox[2]
                        if not (-1.0 <= gap <= size * 2.0):
                            continue
                        if _v_overlap(bbox, obbox) < 0.6:
                            continue
                        if cls._NUMBERING_FRAGMENT_RE.match(
                            cls._join_line_spans(other.get("spans", []))
                        ):
                            continue
                        if layout is not None and layout.gap_covers_gutter(
                            bbox[2], obbox[0], min(bbox[1], obbox[1]), max(bbox[3], obbox[3]),
                        ):
                            continue
                        if best is None or gap < best[0]:
                            best = (gap, bj, lj)
                if best is None:
                    continue
                _, bj, lj = best
                target = blocks[bj][1][lj]
                tbbox = _bbox(target)
                target["spans"] = list(line.get("spans", [])) + list(target.get("spans", []))
                target["_source_lines"] = (
                    list(line.get("_source_lines", [])) + list(target.get("_source_lines", []))
                )
                target["bbox"] = (
                    min(bbox[0], tbbox[0]), min(bbox[1], tbbox[1]),
                    max(bbox[2], tbbox[2]), max(bbox[3], tbbox[3]),
                )
                consumed.add((bi, li))

        if not consumed:
            return blocks
        return [
            (raw_index, [line for li, line in enumerate(lines) if (bi, li) not in consumed])
            for bi, (raw_index, lines) in enumerate(blocks)
        ]

    @staticmethod
    def _split_lines_at_gutters(lines: list[dict], layout: PageLayout) -> list[dict]:
        """把横跨栏间空白、且 span 之间的空隙正好压在 gutter 上的行拆成两行。

        只在 span 边界上拆（span 内部没有字符级几何可依据）；拆出的每一行各带一份
        只含自己 span 的 ``_source_lines``，atoms 的物理源文本因此仍逐字对应。
        """
        if not layout or not any(zone.gutters for zone in layout.zones):
            return lines

        def _union_bbox(spans: list[dict]) -> tuple[float, float, float, float]:
            boxes = [s.get("bbox") for s in spans if s.get("bbox")]
            if not boxes:
                return (0.0, 0.0, 0.0, 0.0)
            return (
                min(float(b[0]) for b in boxes), min(float(b[1]) for b in boxes),
                max(float(b[2]) for b in boxes), max(float(b[3]) for b in boxes),
            )

        out: list[dict] = []
        pending = list(lines)
        while pending:
            line = pending.pop(0)
            spans = list(line.get("spans", []))
            bbox = tuple(float(v) for v in line.get("bbox", (0, 0, 0, 0)))
            split_at = None
            if len(spans) >= 2 and layout.crosses_gutter(bbox):
                for index in range(1, len(spans)):
                    prev_bbox = spans[index - 1].get("bbox")
                    curr_bbox = spans[index].get("bbox")
                    if not prev_bbox or not curr_bbox:
                        continue
                    if layout.gap_covers_gutter(
                        float(prev_bbox[2]), float(curr_bbox[0]), bbox[1], bbox[3],
                    ):
                        split_at = index
                        break
            if split_at is None:
                out.append(line)
                continue
            sources = list(line.get("_source_lines", []))
            line_index = sources[0]["line_index"] if sources else 0
            pieces = []
            for part in (spans[:split_at], spans[split_at:]):
                piece = dict(line)
                piece["spans"] = part
                piece["bbox"] = _union_bbox(part)
                piece["_source_lines"] = [{
                    "line_index": line_index,
                    "bbox": list(piece["bbox"]),
                    "spans": list(part),
                }]
                pieces.append(piece)
            out.append(pieces[0])
            pending.insert(0, pieces[1])   # 右半可能还要再拆（三栏及以上）
        return out

    @staticmethod
    def _line_is_horizontal(line: dict) -> bool:
        """PyMuPDF 行的 ``dir`` 是书写方向单位向量；竖排 / 旋转文字不参与分栏。"""
        direction = line.get("dir")
        if not direction or len(direction) < 2:
            return True
        try:
            return abs(float(direction[1])) < 0.5 and float(direction[0]) > 0
        except (TypeError, ValueError):
            return True

    @staticmethod
    def _infer_relative_alignment(
        bbox: tuple[float, float, float, float],
        reference: tuple[float, float],
    ) -> str:
        """相对参考区间（所在栏或片段文字范围）推断行对齐。

        填满参考宽度的行是正文（两端对齐 / 撑满一行），不是"居中"；左右留白相近
        且都非零才是居中；左侧留白超过参考宽度 40% 是右对齐。
        """
        x0, _, x1, _ = bbox
        ref_x0, ref_x1 = reference
        ref_width = ref_x1 - ref_x0
        if ref_width <= 0:
            return "left"
        left_margin = x0 - ref_x0
        right_margin = ref_x1 - x1
        if left_margin + right_margin <= ref_width * 0.10:
            return "left"
        if (
            abs(left_margin - right_margin) < ref_width * 0.10
            and left_margin > ref_width * 0.05
        ):
            return "center"
        if left_margin > ref_width * 0.40:
            return "right"
        return "left"

    @classmethod
    def _place_line(
        cls,
        layout: PageLayout,
        bbox: tuple[float, float, float, float],
        *,
        horizontal: bool = True,
    ):
        """把一行落到版面上并给出栏相对对齐。

        对齐依赖落位（参考区间），"居中且压住 gutter 的块算跨栏"又依赖对齐，
        所以先按纯几何落位算一次对齐，若判成居中再带着对齐复判一次。
        """
        def _reference(p) -> tuple[float, float]:
            # 只有一两行撑起来的"栏"，边界就是这一两行自己，拿它当参考每行都
            # "填满"；此时退回整页口径。
            if p.support >= cls._ALIGNMENT_REFERENCE_MIN_SUPPORT:
                return p.reference
            return (0.0, float(layout.page_width))

        placement = layout.classify(bbox, horizontal=horizontal)
        alignment = cls._infer_relative_alignment(bbox, _reference(placement))
        if alignment == "center" and not placement.spanning:
            reclassified = layout.classify(bbox, alignment=alignment, horizontal=horizontal)
            if reclassified.spanning:
                placement = reclassified
                alignment = cls._infer_relative_alignment(bbox, _reference(placement))
        return placement, alignment

    # 栏至少要有这么多行支撑，其边界才能作为对齐参考；不足则用整页。
    _ALIGNMENT_REFERENCE_MIN_SUPPORT = 3

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
    def _merge_inline_segments(
        lines: list[dict],
        layout: PageLayout | None = None,
    ) -> list[dict]:
        """Merge PyMuPDF "lines" that share one visual baseline.

        LaTeX section headings emit the number (``2``) and the title
        (``RELATED WORK``) as two separate line entries with full
        vertical overlap and a small horizontal gap (the ``\\quad``
        between number and title).  Downstream heading detection needs
        them in one block, so we merge such fragments left-to-right.

        ``layout``（可选）是本页的版面分析结果：两个片段之间的空隙若压住了一条
        已检测到的栏间空白，它们是左右两栏各自的一行，**不合并**——窄栏距页面上
        "同基线 + 间隔不超过两倍字号"这条旧规则会把两栏缝成一行。
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

        def _bbox_of(ln: dict) -> tuple[float, float, float, float]:
            return tuple(float(v) for v in ln.get("bbox", (0, 0, 0, 0)))

        # 先按纵向重叠归"视觉行"，行内再按 x 从左到右合并。直接按 (y0, x0) 排序会
        # 让小字号的编号（字框更矮、y0 更低）排到同基线大字号标题的**后面**，
        # 于是"3.2.2"永远接不到它右边的"Truncated Diffusion."。
        rows: list[list[dict]] = []
        row_boxes: list[tuple[float, float, float, float]] = []
        for line in sorted(lines, key=lambda ln: (_bbox_of(ln)[1], _bbox_of(ln)[0])):
            bbox = _bbox_of(line)
            if rows and v_overlap(row_boxes[-1], bbox) >= 0.6:
                rows[-1].append(line)
                rb = row_boxes[-1]
                row_boxes[-1] = (
                    min(rb[0], bbox[0]), min(rb[1], bbox[1]),
                    max(rb[2], bbox[2]), max(rb[3], bbox[3]),
                )
            else:
                rows.append([line])
                row_boxes.append(bbox)
        ordered = [
            line for row in rows for line in sorted(row, key=lambda ln: _bbox_of(ln)[0])
        ]

        merged: list[dict] = []
        for line in ordered:
            bbox = _bbox_of(line)
            spans = line.get("spans", [])
            if merged:
                prev = merged[-1]
                prev_bbox = tuple(float(v) for v in prev.get("bbox", (0, 0, 0, 0)))
                prev_size = max(
                    (float(s.get("size") or 0) for s in prev.get("spans", [])),
                    default=12.0,
                ) or 12.0
                gap = bbox[0] - prev_bbox[2]
                gap_is_gutter = layout is not None and layout.gap_covers_gutter(
                    prev_bbox[2], bbox[0], min(prev_bbox[1], bbox[1]), max(prev_bbox[3], bbox[3]),
                )
                if (
                    v_overlap(prev_bbox, bbox) >= 0.6
                    and -1.0 <= gap <= prev_size * 2.0
                    and not gap_is_gutter
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

    @staticmethod
    def _span_is_bold(span: dict) -> bool:
        flags = int(span.get("flags", 0) or 0)
        # PyMuPDF flags: bit 4 = bold, bit 6 = bold (alternate)
        if flags & (1 << 4) or flags & (1 << 6):
            return True
        font = str(span.get("font", "") or "").lower()
        if "bold" in font or font.endswith("-b") or "-bd" in font:
            return True
        # TeX and URW font names carry weight without the word "bold":
        # CMBX12 / CMSSBX10 / SFBX1200 (Computer Modern bold extended),
        # NimbusRomNo9L-Medi (Times bold in pdfTeX), *-Semibold / *-Black.
        name = font.split("+", 1)[-1]
        return bool(_TEX_BOLD_FONT_RE.search(name))

    @staticmethod
    def _span_is_italic(span: dict) -> bool:
        flags = int(span.get("flags", 0) or 0)
        if flags & (1 << 1):
            return True
        font = str(span.get("font", "") or "").lower()
        return "italic" in font or "oblique" in font or font.endswith(("-it", "ti", "-i"))

    @classmethod
    def _span_style_signature(cls, span: dict) -> tuple[str, bool, bool]:
        return (
            str(span.get("font", "") or ""),
            cls._span_is_bold(span),
            cls._span_is_italic(span),
        )

    # run-in 标题引导片段：最多这么多字符。
    _RUN_IN_PREFIX_MAX_CHARS = 80
    _RUN_IN_PREFIX_END = (".", "。", ":", "：", "．")

    @classmethod
    def _detect_run_in_prefix(cls, spans: list[dict], full_text: str) -> Optional[str]:
        """行首一段**样式与其后正文不同**（加粗 / 斜体 / 换了字体）的引导片段，
        其后同一行还有 ≥ 20 个字符的正文 → 返回引导片段文本，否则 ``None``。

        片段要么以句读收尾（``Truncated Diffusion. One can …``，LaTeX ``\\paragraph``
        的典型形态，常是斜体），要么是加粗且与正文之间留了 ≥ 半个字宽的空档
        （``Model Architecture   BERT's model …``，ACL 模板）。行首一个加粗强调词
        后面跟普通空格的（``Transformers are …``）不算。

        PDF 块文本不带 ``**`` 记号，下游 A02 的 run-in 判定看不见"这几个字样式不同"；
        这里把物理证据落到 ``metadata["run_in_prefix"]``。只报几何 / 字体事实，
        "它是不是标题"仍由候选层与模型判。
        """
        visible = [s for s in spans if str(s.get("text", "")).strip()]
        if len(visible) < 2 or not full_text:
            return None
        lead_signature = cls._span_style_signature(visible[0])
        lead_spans: list[dict] = []
        rest_spans: list[dict] = []
        for span in visible:
            if not rest_spans and cls._span_style_signature(span) == lead_signature:
                lead_spans.append(span)
            else:
                rest_spans.append(span)
        if not lead_spans or not rest_spans:
            return None
        rest_weight = sum(cls._non_whitespace_weight(s.get("text", "")) for s in rest_spans)
        if rest_weight < 20:
            return None
        lead = cls._join_line_spans(lead_spans)
        if not lead or len(lead) > cls._RUN_IN_PREFIX_MAX_CHARS:
            return None
        # 以小写字母起头的是换行处恰好落在强调词上的正文（``framework: pre-training
        # and …``），不是标题。
        if lead[0].islower():
            return None

        if lead.endswith(cls._RUN_IN_PREFIX_END):
            return lead
        if lead_signature[1]:  # 加粗且与正文之间有明显空档
            last_bbox = lead_spans[-1].get("bbox")
            next_bbox = rest_spans[0].get("bbox")
            size = float(lead_spans[-1].get("size") or 0) or 12.0
            if last_bbox and next_bbox and float(next_bbox[0]) - float(last_bbox[2]) >= 0.5 * size:
                return lead
        return None

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
            if cls._span_is_bold(span):
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

    @staticmethod
    def _drawing_boxes(page: "fitz.Page") -> list[tuple[float, float, float, float]]:
        """矢量路径 bbox，供版面层认图内标签簇。本方法只收几何，不做表识别。"""
        boxes: list[tuple[float, float, float, float]] = []
        try:
            drawings = page.get_cdrawings() or []
        except Exception:
            return boxes
        for item in drawings:
            rect = item.get("rect") if isinstance(item, dict) else None
            if rect is None:
                continue
            try:
                x0, y0, x1, y1 = (
                    float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1),
                )
            except AttributeError:
                try:
                    x0, y0, x1, y1 = (float(v) for v in rect[:4])
                except (TypeError, ValueError):
                    continue
            if x1 > x0 and y1 > y0:
                boxes.append((x0, y0, x1, y1))
        return boxes

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
                # 跨原始块拼接的编号片段带着自己的块号，物理来源不被改写。
                "block": int(source_line.get("block_index", raw_block_index)),
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
        layout: PageLayout | None = None,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Assign full-width bands plus any number of column regions.

        跨栏与否由"是否横跨已检测到的栏间空白"决定（:mod:`pdf_layout`），不再看
        "居中"或"宽度占页宽 62%"——后者把宽左栏 + 窄右栏页面的每一行左栏都当成
        跨栏分隔，读成 R1 → L1 → R2 → L2。

        band 约定不变：跨栏块 band 为奇数、栏内块 band 为偶数、同 band 内按
        (栏, y, x) 排。纵向相邻的跨栏块归为同一 band（一段跨栏摘要是一个 band，
        而不是每行一个），这样它们之间才允许合段。

        ``layout`` 缺省时从 entries 的文字块 bbox 现场分析（直接调用本方法的测试
        与旧调用方无需改动）。
        """
        if not entries or page_width <= 0:
            return entries

        def _bbox_of(entry) -> tuple[float, float, float, float]:
            block = entry[4]
            bbox = (block.metadata or {}).get("bbox")
            if bbox and len(bbox) >= 4:
                return tuple(float(v) for v in bbox[:4])
            return (float(entry[3]), float(entry[2]), float(entry[3]), float(entry[2]))

        def _is_horizontal(block: Block) -> bool:
            return (block.metadata or {}).get("writing_direction", "horizontal") != "rotated"

        if layout is None:
            page_height = max(
                (float((e[4].metadata or {}).get("page_height") or 0.0) for e in entries),
                default=0.0,
            )
            boxes = [
                LineBox(*_bbox_of(entry), horizontal=_is_horizontal(entry[4]),
                        is_text=entry[4].type == "text" and bool(entry[4].text))
                for entry in entries
            ]
            layout = analyze_page_layout(boxes, page_width, page_height)

        placed: list[tuple[tuple, tuple[float, float, float, float], object]] = []
        for entry in entries:
            block = entry[4]
            bbox = _bbox_of(entry)
            placement = layout.classify(
                bbox, alignment=block.alignment, horizontal=_is_horizontal(block),
            )
            placed.append((entry, bbox, placement))

        # 跨栏块群 → band。栏内块作为"夹在中间就不算同群"的分隔依据。
        spanning_boxes = [
            LineBox(*bbox) for _, bbox, placement in placed if placement.spanning
        ]
        column_boxes = [
            LineBox(*bbox) for _, bbox, placement in placed if not placement.spanning
        ]
        separators = group_separators(spanning_boxes, column_boxes, layout.line_height)
        separator_centers = [(y0 + y1) / 2.0 for y0, y1 in separators]

        def _separator_index(bbox) -> int:
            yc = (bbox[1] + bbox[3]) / 2.0
            for index, (y0, y1) in enumerate(separators):
                if y0 - 0.5 <= yc <= y1 + 0.5:
                    return index
            return min(
                range(len(separators)), key=lambda i: abs(separator_centers[i] - yc),
            ) if separators else 0

        result: list[tuple[int, int, float, float, Block]] = []
        for entry, bbox, placement in placed:
            page, _, _, _, block = entry
            metadata = block.metadata or {}
            yc = (bbox[1] + bbox[3]) / 2.0
            reasons = list(placement.reasons)
            if placement.figure:
                # 图区域是二维的：标签留在所在栏的阅读流（column 仍参与排序），
                # 只有整幅图横跨 gutter 时才升成跨栏 band。区域名单独叫 figure，
                # 下游按图区算污染，不占阅读顺序不确定通道。
                region = "figure"
                if placement.spanning:
                    column = 0
                    band = _separator_index(bbox) * 2 + 1
                else:
                    column = (placement.column_index or 0) + 1
                    band = 2 * sum(1 for center in separator_centers if center < yc)
            elif placement.spanning:
                # 网格区与真正的跨栏块共用"奇数 band、同 band 内按 (y, x) 排"。
                # 这正是表格的行主序；区域名区分开，下游按区域评估风险时
                # 不会把它当成正文里的跨栏标题。
                region = "grid" if placement.grid else "full-width"
                column = 0
                band = _separator_index(bbox) * 2 + 1
                if block.type != "text" and placement.column_count > 1 and not placement.grid:
                    # 跨栏图表夹在栏内正文中间：栏是绕着它继续，还是被它切成上下
                    # 两段？几何上无法唯一判定，如实标记。
                    zone = layout.zones[placement.zone_index]
                    above = below = False
                    for _, other_bbox, other_placement in placed:
                        if other_placement.spanning or other_placement.zone_index != placement.zone_index:
                            continue
                        if other_bbox[3] <= bbox[1] + 0.5:
                            above = True
                        elif other_bbox[1] >= bbox[3] - 0.5:
                            below = True
                    if above and below and not zone.spanning:
                        reasons.append(REASON_SPANNING_SPLITS_COLUMNS)
            else:
                region = f"column-{placement.column_index + 1}"
                column = placement.column_index + 1
                band = 2 * sum(1 for center in separator_centers if center < yc)
            metadata["layout_region"] = region
            metadata["layout_column"] = column
            metadata["layout_band"] = band
            metadata["layout_zone"] = placement.zone_index
            metadata["layout_column_bounds"] = list(placement.reference)
            metadata["layout_column_support"] = placement.support
            metadata["layout_columns_in_zone"] = placement.column_count
            # 网格区表达的是**区域类型**存疑（这片本该是一张表，却没被 find_tables
            # 抓成表），不是阅读顺序存疑——表格按行主序读本来就是对的。所以它单独
            # 落一个 grid_like 标记，不进 layout_uncertain_reasons：把整页表格都塞进
            # 不确定通道，会让"只复审不确定区域"的门控退化成复审全文。
            if placement.grid:
                metadata["grid_like"] = True
                reasons = [r for r in reasons if r != REASON_GRID_LIKE]
            else:
                metadata.pop("grid_like", None)
            if placement.figure:
                metadata["figure_like"] = True
                reasons = [r for r in reasons if r != REASON_FIGURE_LIKE]
            else:
                metadata.pop("figure_like", None)
            existing = list(metadata.get("layout_uncertain_reasons") or [])
            merged_reasons = existing + [r for r in reasons if r not in existing]
            if merged_reasons:
                metadata["layout_uncertain_reasons"] = merged_reasons
            else:
                metadata.pop("layout_uncertain_reasons", None)
            for atom in metadata.get("atoms", []):
                atom["region"] = region
            block.metadata = metadata
            result.append((page, column, bbox[1], bbox[0], block))
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
            best = None
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
                # A same-name figure label must not steal a TOC destination
                # from an actual numbered heading on the same page.
                rank = (score,
                        int(bool(re.match(r"^\d+(?:\.\d+)*[.)]?\s+", block.text))),
                        int(block.is_heading_style or block.is_bold),
                        int(toc_norm == text_norm), -index)
                if score >= 0.90 and (best is None or rank > best[0]):
                    best = (rank, index)
            if best is None:
                continue

            rank, match_index = best
            score = rank[0]
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

    @classmethod
    def _mark_front_matter(cls, entries: list) -> None:
        """Keep cover metadata as source/preamble, not section headings.

        Only an early independently identified section boundary activates the
        rule. Visible numbered headings before it are not treated as metadata.
        """
        first = None
        for index, entry in enumerate(entries[:200]):
            block = entry[4]
            meta = block.metadata or {}
            if entry[0] > 2:
                break
            text = cls._normalise_toc_title(block.text or "")
            if block.type == "text" and ((text in {"abstract", "摘要", "introduction", "引言", "前言"}
                    and (block.is_heading_style or block.is_bold))
                    or (meta.get("toc_destination") and meta.get("toc_level") == 1)):
                first = index
                break
        if first is None:
            return
        for entry in entries[:first]:
            block = entry[4]
            if block.type == "text" and not re.match(
                r"^\s*(?:\d+(?:\.\d+)*[.)、．]?\s+|第[一二三四五六七八九十百\d]+[章节])", block.text or ""
            ):
                block.metadata = {**(block.metadata or {}), "semantic_role": "front_matter"}

    # E01 之后这里原有 _normalise_marginal_text / _filter_repeating_marginal_text
    # 两个方法：它们把重复页眉页脚**直接从块流删掉**。现在改由
    # infrastructure/providers/pdf_page_roles.py 打 source_role 标注、由
    # split_canonical_and_excluded 分流，两个旧方法已无调用方，故删除——
    # 留一个名字写着「删除页眉」却永不执行的函数，会让后来人误判当前行为。
    # 归一化页码的等价实现在 pdf_page_roles.normalise_marginal_text。

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

    # 所在栏至少要有这么多行支撑，栏宽才能作为"占满一行"的参考；否则退回全文口径。
    _COLUMN_WIDTH_REFERENCE_MIN_SUPPORT = 5

    @classmethod
    def _line_width_reference(cls, block: Block, doc_typical: float) -> float:
        """"占满一行"的参考宽度：所在栏的栏宽，栏支撑不足时退回全文行宽中位数。

        双栏论文的单栏摘要行比正文栏宽一倍：用全文中位数（正文栏宽）衡量摘要区的
        行，摘要里的短行会被当成"占满一行"；反过来窄边栏里的行永远"填不满"全文
        口径。栏宽来自版面分析（栏内多行共同的左右边界），比按页 / 按区域取行宽
        中位数稳：一页里编号、公式、段末短行一多，中位数就被拖矮。
        """
        metadata = block.metadata or {}
        bounds = metadata.get("layout_column_bounds")
        support = int(metadata.get("layout_column_support") or 0)
        if bounds and len(bounds) >= 2 and support >= cls._COLUMN_WIDTH_REFERENCE_MIN_SUPPORT:
            width = float(bounds[1]) - float(bounds[0])
            if width > 0:
                return width
        return doc_typical

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
        # 字号"明显大于正文"要留 5% 容差：同一段里 PyMuPDF 报出的字号会在
        # 10.0 / 10.1 之间抖动，严格 ``>`` 会把抖到 10.1 的那一行当成潜在标题，
        # 整段从此合不起来（单栏两端对齐正文曾因此逐行成块）。
        min_body = (body_font_size if body_font_size > 0 else 12.0) * 1.05
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
        # 行首带样式不同的引导片段、且上一行已经以句读收尾的，是新段落的开头
        # （run-in 标题形态），不能作为上一段的续行并进去——并进去之后它就藏在段落
        # 中间，run-in 判定只看块首。上一行没收尾（句子还没说完）的照常合并：那只是
        # 换行处恰好落在一个强调词上。
        if curr_meta.get("run_in_prefix") and (
            not prev.text or prev.text.rstrip().endswith(_HEADINGISH_SENTENCE_END)
        ):
            return False
        if prev_meta.get("page") != curr_meta.get("page"):
            return False
        if prev_meta.get("layout_column") != curr_meta.get("layout_column"):
            return False
        if prev_meta.get("layout_band", 0) != curr_meta.get("layout_band", 0):
            return False
        # 书写方向不同（横排 vs 旋转 / 竖排）不属于同一阅读流。
        if (
            prev_meta.get("writing_direction", "horizontal")
            != curr_meta.get("writing_direction", "horizontal")
        ):
            return False
        if (prev.alignment or "left") != (curr.alignment or "left"):
            return False
        if prev.alignment == "center" or curr.alignment == "center":
            return False
        if prev.font_size and curr.font_size and abs(prev.font_size - curr.font_size) > 0.5:
            return False
        # 粗细不同的两行不是同一段：与正文同字号、只靠加粗区分的小节标题（小字号
        # 论文模板常见）否则会被吞进下一段。段首几个加粗引导词撑不起"多数加粗"，
        # 普通段落不受影响。
        if bool(prev.is_bold) != bool(curr.is_bold):
            return False

        prev_bbox = prev_meta.get("bbox") or [0, 0, 0, 0]
        curr_bbox = curr_meta.get("bbox") or [0, 0, 0, 0]
        gap = float(curr_bbox[1]) - float(prev_bbox[3])
        font_size = curr.font_size or prev.font_size or 12.0
        # 相邻两行的 bbox 会因行距小于字框高度而重叠 1–3pt（10pt 字、11pt 行距
        # 的正文重叠约 1.1pt），重叠容差按字号给；超过 0.4 倍字号的重叠才是同一
        # 行的两个片段或压字，不属于上下行关系。
        if gap < -0.4 * font_size or gap > max(7.0, font_size * 0.75):
            return False
        if abs(float(curr_bbox[0]) - float(prev_bbox[0])) > 36:
            return False
        return True

    # 词内断字判定用的字符类。CJK 之间不插空格（中文正文机械插空格会毁排版），
    # 拉丁小写字母之间的行末连字符才是候选软连字符。
    _CJK_RE = re.compile(
        r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
    )
    _URLISH_RE = re.compile(r"(https?://|www\.|@|/|\\)")

    @classmethod
    def _is_cjk(cls, ch: str) -> bool:
        return bool(ch) and bool(cls._CJK_RE.match(ch))

    @classmethod
    def _join_wrapped_lines(cls, prev_text: str, curr_text: str) -> tuple[str, bool]:
        """决定两条同段物理行如何拼接，返回 (拼接结果, 是否删掉了行末连字符)。

        与"是否同段"分离：本函数只回答"字符怎么接"。核心证据——显式软连字符、
        行末字符类、下一行首字符类、语言（CJK vs 拉丁）、是否 URL/编号/减号：

        - 显式软连字符 U+00AD：一律删除并无缝拼接（排版层就是这个语义）。
        - 行末普通 ``-``：仅当"拉丁小写 + ``-`` + 拉丁小写"这种确凿的断词才删；
          ``--``（破折号）、``COVID-19``（数字侧）、``foo-``+``/bar``（URL/代码）、
          无法确定时一律**保留**连字符（``state-of-the-`` + ``art`` →
          ``state-of-the-art``），宁可留字符也不吞字。
        - 跨行两侧任一为 CJK：无缝拼接，不插西文空格。
        - 其余：插单个空格。
        """
        left = prev_text.rstrip()
        right = curr_text.lstrip()
        if not left:
            return right, False
        if not right:
            return left, False

        # 显式软连字符：排版层已声明"这里断的是一个词"，删除后无缝拼接。
        if left.endswith("\u00ad"):
            return left[:-1] + right, True

        last = left[-1]
        first = right[0]

        # 行末普通连字符：默认保留（含普通连字符复合词、编号、减号、破折号），
        # 只有确凿的拉丁小写断词才删除。
        if last == "-":
            core = left[:-1]
            is_double_dash = core.endswith("-")  # ``--`` 破折号，两杠都保留
            before = core[-1] if core else ""
            # 行末的空白分隔 token：若它本身已含连字符，说明是连字符复合词
            # (``state-of-the-`` + ``art`` → ``state-of-the-art``)，末尾 ``-``
            # 是词的一部分而非断词软连字符——保留。真正的软连字符断的是单个词
            # (``infor-`` + ``mation``)，其 token 内不含其它连字符。
            trailing_token = core.rsplit(None, 1)[-1] if core.strip() else ""
            compound = "-" in trailing_token
            latin_wrap = (
                before.isalpha() and before.isascii() and before.islower()
                and first.isalpha() and first.isascii() and first.islower()
            )
            is_urlish = bool(cls._URLISH_RE.search(core[-12:])) or cls._URLISH_RE.match(right)
            if latin_wrap and not is_double_dash and not is_urlish and not compound:
                return core + right, True  # infor- + mation -> information
            if is_double_dash:
                # ``--`` 是破折号句内分隔，两杠保留且按普通西文换行插空格。
                return left + " " + right, False
            # 编号(COVID-19)/减号/URL/连字符复合词：保留连字符、无缝拼接。
            return left + right, False

        # CJK 连续正文跨行：不插空格。
        if cls._is_cjk(last) or cls._is_cjk(first):
            return left + right, False

        # 默认西文换行：单空格。
        return left + " " + right, False

    @classmethod
    def _line_join_boundary(cls, prev_text: str, curr_text: str) -> tuple[str, bool, bool]:
        """决定两条同段物理行**边界处**如何连接，返回三元组：

        ``(separator, drop_prev_trailing_hyphen, keep_curr_leading_ws)``

        - ``separator``：插在两个片段之间的字符（``""`` 无缝 / ``" "`` 单空格）。
        - ``drop_prev_trailing_hyphen``：是否要删掉前一片段末尾的（软）连字符。
        - 该函数只看边界两侧字符，**不拼接**——P02 要求段落只在结束时 join 一次，
          因此把"边界规则"从"整段拼接"里剥出来，逐边界产出增量决定，最后统一落位。

        判定与 :meth:`_join_wrapped_lines` 完全同源（软连字符 / 行末普通连字符的
        拉丁小写断词 / ``--`` 破折号 / 编号 / 减号 / URL / CJK / 默认西文空格），
        只是不再在这里把 O(段长) 的累计串重新 rstrip/拼接。
        """
        left = prev_text.rstrip()
        right = curr_text.lstrip()
        if not left:
            return "", False, False
        if not right:
            return "", False, False

        # 显式软连字符：排版层已声明"这里断的是一个词"，删除后无缝拼接。
        if left.endswith("\u00ad"):
            return "", True, False

        last = left[-1]
        first = right[0]

        if last == "-":
            core = left[:-1]
            is_double_dash = core.endswith("-")
            before = core[-1] if core else ""
            trailing_token = core.rsplit(None, 1)[-1] if core.strip() else ""
            compound = "-" in trailing_token
            latin_wrap = (
                before.isalpha() and before.isascii() and before.islower()
                and first.isalpha() and first.isascii() and first.islower()
            )
            is_urlish = bool(cls._URLISH_RE.search(core[-12:])) or cls._URLISH_RE.match(right)
            if latin_wrap and not is_double_dash and not is_urlish and not compound:
                return "", True, False   # infor- + mation -> information
            if is_double_dash:
                return " ", False, False  # ``--`` 破折号：保留两杠 + 西文空格
            return "", False, False       # COVID-19 / 减号 / URL / 复合词：留杠无缝

        # CJK 连续正文跨行：不插空格。
        if cls._is_cjk(last) or cls._is_cjk(first):
            return "", False, False

        # 默认西文换行：单空格。
        return " ", False, False

    def _merge_text_lines(
        self,
        entries: list[tuple[int, int, float, float, Block]],
        body_font_size: float = 0.0,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Merge consecutive PDF body lines into paragraph blocks.

        P02：改为"先分组、结束时一次落位"。旧实现每并入一行就 ``rstrip`` 整段
        累计串并重新拼接（O(段长²) 字符工作），再由 ``finish_paragraph`` 重扫
        atoms 落偏移——128/256/512 行呈 ~4 倍/档的超线性增长。

        现在过程中每行只往运行状态里累积**轻量增量**：字符片段的 rstrip 结果、
        本边界的 separator、是否删前一片段末尾连字符、atom 引用、可加的字号/加粗
        权重、bbox 与行号跨度。段落结束时才：① 一次 ``"".join`` 得到最终正文；
        ② 一次 :meth:`_recalculate_atom_offsets` 落 atom 偏移。连字符删除只记在
        对应片段的局部 delta 上，最后统一体现在拼接结果与 atom.provenance 里。
        原子的物理源文字始终保留、不就地篡改。
        """
        if not entries:
            return entries

        doc_typical_line_width = self._typical_line_width(entries)
        sorted_entries = sorted(entries, key=self._entry_sort_key)
        merged: list[tuple[int, int, float, float, Block]] = []
        merge_count = 0

        # 当前段落的运行状态（收集期只累积增量，段末一次落位）。
        # frags: 每片段的 rstrip 后文本；seps: 片段之间的 separator（len == len(frags)-1）；
        # drop_hyphen: 每片段是否要删自身末尾的（软）连字符。
        run_block: Block | None = None
        run_page = run_column = 0
        run_y0 = run_x0 = 0.0
        frags: list[str] = []
        seps: list[str] = []
        drop_hyphen: list[bool] = []
        run_atoms: list[dict] = []
        run_size_weights: Counter = Counter()
        run_bold_weight = 0
        run_total_weight = 0
        run_bbox: list[float] | None = None
        run_line_start = 0
        run_line_end = 0
        # 段内"是否同段"判定必须对着**上一物理行**做（gap/对齐/列都是行间关系）。
        # run_block 是段首块、其 bbox 在收集期不更新，拿它判 gap 会随段变长而失真，
        # 导致长段落被误切。因此单独持有 run_last_block（上一并入行的原块）。
        run_last_block: Block | None = None

        def _fragment_text(frag: str, drop: bool) -> str:
            """按本片段的局部 delta 落位：删末尾（软）连字符只影响这一片段。"""
            if drop and frag and frag[-1] in ("-", "\u00ad"):
                return frag[:-1]
            return frag

        def finish_paragraph():
            nonlocal run_block
            if run_block is None:
                return
            # ① 一次拼接：片段按各自 delta 落位后用记录的 separator 缝合。
            parts: list[str] = []
            for index, frag in enumerate(frags):
                parts.append(_fragment_text(frag, drop_hyphen[index]))
                if index < len(seps):
                    parts.append(seps[index])
            text = "".join(parts)
            run_block.text = text

            meta = run_block.metadata or {}
            meta["atoms"] = run_atoms
            if run_bbox is not None:
                meta["bbox"] = list(run_bbox)
            if len(frags) > 1:
                meta["merged_lines"] = len(frags)
                meta["line_span"] = [run_line_start, run_line_end]
            # 段落级格式从可加的字符权重一次算出（等价于全量重扫）。
            if run_size_weights:
                run_block.font_size = run_size_weights.most_common(1)[0][0]
            run_block.is_bold = (
                run_total_weight > 0 and run_bold_weight > run_total_weight / 2
            )
            # 软连字符删杠：把决定写进对应 atom.provenance（物理源文本不动）。
            for atom, drop in zip(run_atoms, _atom_drop_flags()):
                if not drop:
                    continue
                source_text = str(atom.get("text", ""))
                if source_text.rstrip().endswith(("-", "\u00ad")):
                    provenance = atom.setdefault("provenance", {})
                    provenance["canonical_core"] = source_text.rstrip()[:-1]
                    provenance.pop("canonical_text", None)
                    provenance["join_normalization"] = "soft-hyphen-removed"
            # ② 一次偏移落位。
            self._recalculate_atom_offsets(text, run_atoms)
            run_block.metadata = meta
            merged[-1] = (run_page, run_column, run_y0, run_x0, run_block)
            run_block = None

        # 记录每个片段末尾对应的 atom 是否删杠：删杠总是发生在片段的**最后一个**
        # atom 上（该物理行以连字符结尾）。用一个与 run_atoms 等长的布尔列表标注。
        atom_drop_marks: list[bool] = []

        def _atom_drop_flags() -> list[bool]:
            return atom_drop_marks

        def _reset_run(entry):
            nonlocal run_block, run_page, run_column, run_y0, run_x0
            nonlocal frags, seps, drop_hyphen, run_atoms, atom_drop_marks
            nonlocal run_size_weights, run_bold_weight, run_total_weight
            nonlocal run_bbox, run_line_start, run_line_end, run_last_block
            page, column, y0, x0, block = entry
            run_block = block
            run_last_block = block
            run_page, run_column, run_y0, run_x0 = page, column, y0, x0
            meta = block.metadata or {}
            atoms = list(meta.get("atoms", []))
            frags = [(block.text or "").rstrip()]
            seps = []
            drop_hyphen = [False]
            run_atoms = atoms
            atom_drop_marks = [False] * len(atoms)
            sizes, bold, total = self._atom_weight_stats(atoms)
            run_size_weights = sizes
            run_bold_weight = bold
            run_total_weight = total
            bbox = meta.get("bbox") or [x0, y0, x0, y0]
            run_bbox = [float(v) for v in bbox]
            run_line_start = meta.get("line_index", 0)
            run_line_end = meta.get("line_index", 0)
            merged.append((page, column, y0, x0, block))

        for entry in sorted_entries:
            page, column, y0, x0, block = entry
            typical_line_width = self._line_width_reference(block, doc_typical_line_width)
            if run_last_block is None or not self._can_merge_text_lines(
                run_last_block, block, body_font_size, typical_line_width,
            ):
                finish_paragraph()
                _reset_run(entry)
                continue

            # 只决定"字符如何拼接"（是否同段已由 _can_merge_text_lines 判过）。
            separator, drop_prev, _ = self._line_join_boundary(
                frags[-1], block.text or ""
            )
            if drop_prev:
                drop_hyphen[-1] = True
                if atom_drop_marks:
                    atom_drop_marks[-1] = True

            curr_meta = block.metadata or {}
            curr_atoms = list(curr_meta.get("atoms", []))
            frags.append((block.text or "").rstrip())
            seps.append(separator)
            drop_hyphen.append(False)
            run_atoms.extend(curr_atoms)
            atom_drop_marks.extend([False] * len(curr_atoms))

            curr_sizes, curr_bold, curr_total = self._atom_weight_stats(curr_atoms)
            run_size_weights.update(curr_sizes)
            run_bold_weight += curr_bold
            run_total_weight += curr_total

            curr_bbox = curr_meta.get("bbox") or [x0, y0, x0, y0]
            if run_bbox is not None:
                run_bbox = [
                    min(run_bbox[0], float(curr_bbox[0])),
                    min(run_bbox[1], float(curr_bbox[1])),
                    max(run_bbox[2], float(curr_bbox[2])),
                    max(run_bbox[3], float(curr_bbox[3])),
                ]
            run_line_end = curr_meta.get("line_index", run_line_end)
            run_last_block = block
            merge_count += 1

        finish_paragraph()
        if merge_count:
            logger.info("[PdfProvider] Merged %d body text lines", merge_count)
        return merged

    def _extract_images(
        self,
        page: "fitz.Page",
        page_index: int,
    ) -> list[tuple[int, int, float, float, Block]]:
        """Extract embedded images from a single page.

        修复（P05）：旧实现对每个 xref 只取 ``get_image_rects(xref)`` 的**第一个**
        rect，同一张图在一页里出现多次（重复插图、水印、拼版）时，第 2 次起的位置
        被静默丢弃——版面出现次数被资源去重吃掉了。现在把两件事分开：

        - **资源去重**：同一 xref 的字节只 ``extract_image`` 解码一次（旧实现即便
          只用第一个 rect 也已只解码一次，这点保留），进 AssetStore 后按内容哈希
          还会跨 xref/跨页再去重一层。
        - **版面出现次数**：遍历该 xref 的**每一个** rect，各生成一个 image Block
          （一次 occurrence），bbox 各自不同。无 rect 时退化为一个 (0,0,0,0) 占位
          出现，与旧行为一致，不丢图。

        Returns:
            List of ``(page_index, column, y0, x0, Block)`` tuples.
        """
        entries: list[tuple[int, int, float, float, Block]] = []

        try:
            image_list = page.get_images(full=True)
        except Exception:
            self._note_skip(
                "pdf_page_images_unavailable",
                f"第 {page_index + 1} 页的图片清单读取失败，该页全部图片缺失。",
                source_span={"page": page_index + 1},
            )
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
                    # 矢量图 / 无法直接还原为位图的资源：不静默丢弃，落一条带页面
                    # 引用的跳过提示，让调用方知道这一页有一张图未能位图化。
                    self._note_skip(
                        "pdf_image_unsupported_format",
                        f"第 {page_index + 1} 页有一张图片为 {ext} 格式，无法还原为位图，仅保留位置引用。",
                        source_span={"page": page_index + 1, "xref": xref},
                    )
                    continue

                mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"

                # 该 xref 的字节此处只解码一次；出现次数由下面的 rect 循环决定。
                b64 = base64.b64encode(image_bytes).decode("ascii")
                data_uri = f"data:{mime};base64,{b64}"

                try:
                    rects = page.get_image_rects(xref)
                except Exception:
                    rects = []

                # 无 rect：保留一次 (0,0,0,0) 占位出现，不丢图（与旧行为一致）。
                bboxes: list[tuple[float, float, float, float]]
                if rects:
                    bboxes = [
                        (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
                        for r in rects
                    ]
                else:
                    bboxes = [(0.0, 0.0, 0.0, 0.0)]

                for bbox in bboxes:
                    layout_column = self._infer_layout_column(bbox, page.rect.width)
                    block = Block(
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
                            "xref": xref,
                        },
                    )
                    # 走 AssetStore 入口时才收编字节：字节按哈希去重（同 xref 的多次
                    # 出现、乃至跨页同图都命中同一 asset_id），每次出现各记一个 ref。
                    self._register_image_asset(
                        block,
                        content=image_bytes,
                        mime=mime,
                        bbox=bbox,
                        page=page_index + 1,
                    )
                    # 元组口径与文字 / 表格一致：(page, column, y0, x0, block)。
                    entries.append((page_index, layout_column, bbox[1], bbox[0], block))

            except Exception as e:
                logger.debug(
                    "[PdfProvider] Skipping image xref=%d on page %d: %s",
                    xref, page_index + 1, e,
                )
                # R05：图片解码失败此前只落在 debug 日志里，调用方完全无感知。
                # retryable=False：同一份字节再抽一次还是这个结果，重试没有意义。
                self._note_skip(
                    "pdf_image_failed",
                    f"第 {page_index + 1} 页有一张图片无法解码，该图缺失。",
                    source_span={"page": page_index + 1, "xref": xref},
                )

        # 上面为可读性用 (page, y0, x0, column, block) 顺序 append，此处纠正为调用方
        # 约定的 (page, column, y0, x0, block) 五元组。
        return [(page, column, y0, x0, block) for (page, y0, x0, column, block) in entries]

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
        # 极慢）。纯文本页（无任何矢量路径）不可能靠 lines 策略成表，先跳过。
        #
        # E04：这条跳过**不能无条件移除**（方案原话），它是对既有 lines 策略的性能优化。
        # 但无框表确实存在，所以加一道门控：仅当页面上检测到「对齐列 + 一致空白结构」时
        # 才多花一次识别的钱，走 text 策略再试一次。门控只回答"要不要花这笔钱"，
        # 不回答"表在哪"——识别仍由 PyMuPDF 负责。
        borderless_worth_trying = False
        try:
            if not page.get_cdrawings():
                borderless_worth_trying = self._suggests_borderless_table(page)
                if not borderless_worth_trying:
                    return entries
        except Exception:
            pass

        try:
            if borderless_worth_trying:
                # 无矢量页：lines 策略必然无果，直接用 text 策略。
                table_finder = page.find_tables(strategy="text")
            else:
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
                # E04：PDF 的 lines 策略给不出合并跨度，模型里 span 全为 1（如实描述）；
                # 但页码与整表 bbox 必须带上——跨页续表判定要靠列几何与相邻页边界。
                table_model = model_from_rows(
                    rows, header_rows=1 if rows else 0, page=page_index + 1,
                )
                table_model.bbox = list(bbox_tuple)

                entries.append((page_index, layout_column, bbox_tuple[1], bbox_tuple[0], Block(
                    id=0,
                    type="table",
                    text=md_text,
                    table_data={"rows": rows, "model": table_model.model_dump()},
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
                self._note_skip(
                    "pdf_table_failed",
                    f"第 {page_index + 1} 页有一张表格无法解析，整张表未纳入结果。",
                    source_span={"page": page_index + 1},
                )

        return entries

    def _link_table_continuations(self, entries) -> None:
        """给按阅读顺序排好的表格块打跨页续表关系（E04）。

        判定委托给 :func:`table_continuation.link_continuations`——多信号一致才接续，
        且把判定依据一并写回，使"为什么接/为什么不接"可审计。只在**相邻**两张表之间
        判断：跨过中间一张无关表去接续没有依据支持。
        """
        from infrastructure.models.table_continuation import link_continuations
        from infrastructure.models.table_model import TableModel

        tables: list[tuple[Block, TableModel]] = []
        for entry in entries:
            block = entry[4]
            if block.type != "table":
                continue
            data = block.table_data or {}
            payload = data.get("model")
            if not payload:
                continue
            try:
                tables.append((block, TableModel.model_validate(payload)))
            except Exception:  # noqa: BLE001 - 模型不合法就不参与续表判定
                continue
        if len(tables) < 2:
            return
        linked = link_continuations([model for _block, model in tables])
        for (block, _old), model in zip(tables, linked):
            data = dict(block.table_data or {})
            data["model"] = model.model_dump()
            block.table_data = data
            metadata = dict(block.metadata or {})
            metadata["table_id"] = model.table_id
            if model.continuation_of:
                metadata["continuation_of"] = model.continuation_of
            metadata["continuation_signals"] = model.continuation_signals
            block.metadata = metadata

    def _suggests_borderless_table(self, page) -> bool:
        """本页是否出现「对齐列 + 一致空白结构」，值得为无框表多跑一次识别。

        判定委托给 :func:`table_continuation.suggests_borderless_table`（阈值与口径集中
        在那里），本方法只负责把 PyMuPDF 的 span 几何整理成「每行各片段的 x 起点」。
        取失败一律返回 False——门控本身绝不能成为新的失败源。
        """
        from infrastructure.models.table_continuation import suggests_borderless_table

        try:
            data = page.get_text("dict")
        except Exception:  # noqa: BLE001 - 门控失败就不启用额外识别
            return False
        line_starts: list[list[float]] = []
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                starts = [
                    float(span.get("bbox", (0,))[0])
                    for span in line.get("spans", [])
                    if (span.get("text") or "").strip()
                ]
                if len(starts) >= 2:
                    line_starts.append(sorted(starts))
        return suggests_borderless_table(line_starts)

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
