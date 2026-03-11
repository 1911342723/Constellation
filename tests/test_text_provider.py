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
