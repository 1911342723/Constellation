import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.providers import TextProvider


def test_text_provider_splits_paragraphs_into_blocks():
    provider = TextProvider()
    content = "Title\n\nFirst paragraph line one.\nFirst paragraph line two.\n\nSecond paragraph."

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    assert len(blocks) == 3
    assert blocks[0].text == "Title"
    assert blocks[1].text == "First paragraph line one. First paragraph line two."
    assert blocks[2].text == "Second paragraph."


def test_text_provider_preserves_structured_short_lines():
    provider = TextProvider()
    content = "1. Intro\n2. Methods\n3. Conclusion"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    assert [block.text for block in blocks] == [
        "1. Intro",
        "2. Methods",
        "3. Conclusion",
    ]


def test_text_provider_decodes_gbk_content():
    provider = TextProvider()

    blocks = provider.extract_from_bytes("???\n\n??".encode("gbk"))

    assert [block.text for block in blocks] == ["???", "??"]


def test_text_provider_preserves_chinese_chapter_lines_without_blank_separators():
    """紧凑排版（标题与正文间无空行）+ 中文章节号：标题不得被拍平进段落。

    2026-08-12 评审实锤：旧结构行判定不认「第一章」，整篇被合并成 1 个块，
    候选层 0 召回、结构恢复全失（仅剩单根无损兜底）。
    """
    provider = TextProvider()
    content = (
        "质量白皮书\n"
        "第一章 现状分析\n"
        "当前缺陷密度集中在集成层。\n"
        "第二章 改进措施\n"
        "推行接口契约测试。\n"
    )

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    texts = [block.text for block in blocks]
    assert "第一章 现状分析" in texts
    assert "第二章 改进措施" in texts


def test_text_provider_preserves_multilevel_numbering_lines():
    """多级编号（1.1 / 2.3.4）紧凑排版同样要逐行保留。"""
    provider = TextProvider()
    content = "1.1 目标\n实现零损耗重建。\n1.2 范围\n只讨论 txt。\n2.1 架构\n四阶段管线。"

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    texts = [block.text for block in blocks]
    assert "1.1 目标" in texts
    assert "1.2 范围" in texts
    assert "2.1 架构" in texts


def test_text_provider_does_not_mistake_leading_numbers_for_structure():
    """以数字开头的普通句子（「2026 年计划…」）不是编号行，仍应合并成段。"""
    provider = TextProvider()
    content = (
        "2026 年计划完成三件事。\n"
        "2027 年再看情况调整。\n"
        "2028 年复盘总结。"
    )

    blocks = provider.extract_from_bytes(content.encode("utf-8"))

    assert len(blocks) == 1
    assert blocks[0].text.startswith("2026 年计划")
