"""Relative Size: meta-tag behaviour (P2: absolute 12pt threshold removal).

The Size: tag must fire relative to the document's body font size:
- 9pt-body LaTeX paper with 10pt headings → tag fires (legacy missed it);
- 16pt-body CJK document with 16pt body lines → tag silent (legacy
  spammed every line);
- no baseline → legacy absolute >12pt behaviour preserved.
"""

from infrastructure.models import Block
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig


def _text_block(bid: int, text: str, font_size: float, bold: bool = False) -> Block:
    return Block(
        id=bid, type="text", text=text,
        font_size=font_size, is_bold=bold,
    )


# ── Block._build_meta_tags ───────────────────────────────────

def test_size_tag_fires_for_small_font_heading_with_small_body():
    """10pt heading over 9pt body must emit Size: (legacy threshold missed it)."""
    block = _text_block(0, "2.1 Experimental Setup", font_size=10.0)
    assert "Size:10" in block._build_meta_tags(body_font_size=9.0)


def test_size_tag_silent_for_body_sized_text_with_large_body():
    """16pt body line in a 16pt-body CJK document must NOT emit Size:."""
    block = _text_block(0, "这是一段正文内容，与正文字号相同。", font_size=16.0)
    assert "Size" not in block._build_meta_tags(body_font_size=16.0)


def test_size_tag_fires_for_large_font_heading_with_large_body():
    """22pt heading over 16pt body must emit Size:."""
    block = _text_block(0, "第一章 总则", font_size=22.0)
    assert "Size:22" in block._build_meta_tags(body_font_size=16.0)


def test_size_tag_legacy_absolute_threshold_without_baseline():
    """No baseline → legacy behaviour: >12pt fires, <=12pt silent."""
    big = _text_block(0, "Heading", font_size=16.0)
    small = _text_block(1, "Heading", font_size=10.0)
    assert "Size:16" in big._build_meta_tags()
    assert "Size" not in small._build_meta_tags()


# ── Compressor integration ───────────────────────────────────

def _small_font_doc() -> list[Block]:
    """9pt-body document with a 10.5pt heading (LaTeX-like)."""
    blocks = [
        _text_block(0, "Small Font Paper Title", font_size=10.5, bold=True),
    ]
    body = (
        "This is a long body paragraph that keeps going for quite a while "
        "so that it is classified as a P-frame rather than an I-frame. " * 3
    )
    for i in range(1, 5):
        blocks.append(_text_block(i, body, font_size=9.0))
    blocks.append(_text_block(5, "2 Method", font_size=10.5, bold=True))
    for i in range(6, 10):
        blocks.append(_text_block(i, body, font_size=9.0))
    return blocks


def test_compressor_emits_relative_size_tags_for_small_font_doc():
    """End-to-end: 10.5pt headings in a 9pt-body doc get Size: tags in the skeleton."""
    compressor = SkeletonCompressor(config=CompressorConfig())
    chunks = compressor.compress(_small_font_doc())
    skeleton = "\n".join(chunks)

    assert "2 Method" in skeleton
    method_line = next(
        line for line in skeleton.splitlines() if "2 Method" in line
    )
    assert "Size:" in method_line, (
        "10.5pt heading over 9pt body must carry a Size: tag "
        f"(line was: {method_line!r})"
    )


def test_compressor_keeps_small_font_heading_as_iframe():
    """The heading must be I-frame (full text), not truncated as P-frame."""
    compressor = SkeletonCompressor(config=CompressorConfig())
    chunks = compressor.compress(_small_font_doc())
    skeleton = "\n".join(chunks)

    method_line = next(
        line for line in skeleton.splitlines() if "2 Method" in line
    )
    assert "省略" not in method_line and "omitted" not in method_line


def test_compressor_no_size_spam_for_uniform_large_font_doc():
    """16pt-body CJK doc: plain body lines must not all carry Size: tags."""
    body = (
        "这是一段足够长的正文内容，反复出现以确保被判定为正文段落而不是标题。" * 4
    )
    blocks = [_text_block(i, body, font_size=16.0) for i in range(8)]

    compressor = SkeletonCompressor(config=CompressorConfig())
    chunks = compressor.compress(blocks)
    skeleton = "\n".join(chunks)

    size_lines = [
        line for line in skeleton.splitlines()
        if "Size:" in line and "[" in line
    ]
    assert size_lines == [], (
        f"Uniform 16pt body must not emit Size: tags, got {len(size_lines)} lines"
    )
