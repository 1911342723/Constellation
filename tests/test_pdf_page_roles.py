"""E01 页面角色判定测试 —— header/footer/toc/footnote/watermark 的物理判定，
以及"真实页顶标题不被误判成页眉""被排除项可切分恢复"。

Run: python -m pytest tests/test_pdf_page_roles.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.models import Block
from infrastructure.providers import pdf_page_roles as roles


def _blk(bid, text, *, page, y0, y1, font_size=10.0, page_height=800.0):
    return (page, 1, float(y0), 72.0, Block(
        id=bid, type="text", text=text, font_size=font_size,
        metadata={
            "page": page + 1,
            "bbox": [72.0, y0, 300.0, y1],
            "page_width": 600.0,
            "page_height": page_height,
            "atoms": [{"text": text, "provenance": {}}],
        },
    ))


def test_normalise_marginal_text_folds_page_numbers():
    assert roles.normalise_marginal_text("Page 3") == roles.normalise_marginal_text("Page 4")
    assert roles.normalise_marginal_text("Page 3") == "page #"


def test_repeating_header_tagged_across_pages():
    """同一页眉文本在多页页顶重复 → 判 header，带理由与置信度。"""
    entries = []
    for p in range(4):
        entries.append(_blk(p, "Confidential Report", page=p, y0=10, y1=24))
        entries.append(_blk(100 + p, f"Body of page {p}", page=p, y0=400, y1=420))
    roles.classify_marginal_roles(entries, page_count=4, body_font_size=10.0)

    headers = [e[4] for e in entries if (e[4].metadata or {}).get("source_role") == "header"]
    assert len(headers) == 4
    for b in headers:
        assert b.metadata["role_reason"]
        assert 0.0 <= b.metadata["role_confidence"] <= 1.0
        assert b.metadata["canonical"] is False


def test_unique_top_line_not_flagged_header():
    """只在一页页顶出现的行不满足重复阈值 → 不判 header。"""
    entries = [
        _blk(0, "Annual Report 2026", page=0, y0=10, y1=30, font_size=20.0),
    ]
    for p in range(4):
        entries.append(_blk(10 + p, f"Body {p}", page=p, y0=400, y1=420))
    roles.classify_marginal_roles(entries, page_count=4, body_font_size=10.0)
    title = entries[0][4]
    assert title.metadata.get("source_role") not in ("header", "footer")


def test_footer_page_number_tagged():
    """页底重复页码模式 → 判 footer（归一化后 Page N 同模式）。"""
    entries = []
    for p in range(3):
        entries.append(_blk(p, f"Page {p + 1}", page=p, y0=778, y1=792))
        entries.append(_blk(50 + p, f"Body {p}", page=p, y0=400, y1=420))
    roles.classify_marginal_roles(entries, page_count=3, body_font_size=10.0)
    footers = [e[4] for e in entries if (e[4].metadata or {}).get("source_role") == "footer"]
    assert len(footers) == 3


def test_toc_line_tagged():
    """点导引线 + 页码结尾 → 判目录。"""
    entries = [_blk(0, "1.1 Introduction .......... 3", page=0, y0=200, y1=214)]
    for p in range(2):
        entries.append(_blk(10 + p, f"Body {p}", page=p, y0=400, y1=420))
    roles.classify_marginal_roles(entries, page_count=2, body_font_size=10.0)
    assert entries[0][4].metadata.get("source_role") == "toc"


def test_footnote_tagged():
    """页底、字号小于正文、以脚注序号起头 → 判脚注。"""
    entries = [
        _blk(0, "1 See Smith 2020 for details.", page=0, y0=760, y1=770, font_size=7.0),
        _blk(1, "Main body content on page.", page=0, y0=400, y1=420, font_size=10.0),
    ]
    roles.classify_marginal_roles(entries, page_count=2, body_font_size=10.0)
    assert entries[0][4].metadata.get("source_role") == "footnote"


def test_watermark_requires_whole_line_to_be_watermark_words():
    """含水印词但整行是普通标题的不算水印；整行只有水印词（+ 搭配词）或旋转文本才算。

    gpt3 p48 的附录标题 ``F Additional Samples from GPT-3`` 曾因含 ``sample`` 被判成
    水印并从正文里剔除（GT 重对齐时暴露）。
    """
    heading = _blk(0, "F Additional Samples from GPT-3", page=0, y0=330, y1=346, font_size=14.0)
    cjk_heading = _blk(1, "内部控制制度评估", page=0, y0=360, y1=374)
    draft = _blk(2, "DRAFT", page=0, y0=400, y1=420)
    combo = _blk(3, "CONFIDENTIAL — INTERNAL USE ONLY", page=0, y0=430, y1=444)
    cjk_wm = _blk(4, "机密文件 请勿外传", page=0, y0=460, y1=474)
    rotated = _blk(5, "Sample for review", page=0, y0=300, y1=500)
    rotated[4].metadata["writing_direction"] = "rotated"
    body = _blk(6, "Ordinary body sentence that goes on.", page=0, y0=500, y1=514)
    entries = [heading, cjk_heading, draft, combo, cjk_wm, rotated, body]
    roles.classify_marginal_roles(entries, page_count=1, body_font_size=10.0)

    role = lambda e: (e[4].metadata or {}).get("source_role")
    assert role(heading) is None
    assert role(cjk_heading) is None
    assert role(body) is None
    assert role(draft) == "watermark"
    assert role(combo) == "watermark"
    assert role(cjk_wm) == "watermark"
    assert role(rotated) == "watermark"


def test_e2e_samples_heading_stays_canonical():
    """端到端：附录标题含 sample 仍进 canonical，真正的水印短行被排除。"""
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((72, 80), "DRAFT", fontname="hebo", fontsize=28)
    page.insert_text((72, 330), "F Additional Samples from GPT-3", fontname="hebo", fontsize=14)
    page.insert_text((72, 360), "Body paragraph after the appendix heading.", fontname="helv", fontsize=10)
    pdf_bytes = doc.tobytes()
    doc.close()

    from infrastructure.providers.pdf_provider import PdfProvider

    provider = PdfProvider()
    canonical = provider.extract_from_bytes(pdf_bytes)
    texts = [b.text or "" for b in canonical]
    assert any("Additional Samples from GPT-3" in t for t in texts)
    assert not any(t.strip() == "DRAFT" for t in texts)
    full = provider._last_full_sequence or []
    draft = [b for b in full if (b.text or "").strip() == "DRAFT"]
    assert draft and (draft[0].metadata or {}).get("source_role") == "watermark"


def test_split_canonical_and_excluded():
    """被判成非正文角色的块进 excluded，正文进 canonical，均不丢失。"""
    entries = []
    for p in range(3):
        entries.append(_blk(p, "Confidential", page=p, y0=10, y1=24))
        entries.append(_blk(50 + p, f"Body {p}", page=p, y0=400, y1=420))
    roles.classify_marginal_roles(entries, page_count=3, body_font_size=10.0)
    blocks = [e[4] for e in entries]
    canonical, excluded = roles.split_canonical_and_excluded(blocks)
    assert len(canonical) + len(excluded) == len(blocks)
    assert all((b.metadata or {}).get("source_role") == "header" for b in excluded)
    assert all("Body" in (b.text or "") for b in canonical)
