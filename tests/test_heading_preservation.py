import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infrastructure.models import Block
from modules.parser.compressor import SkeletonCompressor
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode


def test_long_iframe_titles_are_preserved_in_skeleton():
    long_title = (
        "Chiayi County Shuishang Township Nanjing Elementary School "
        "Affiliated Kindergarten"
    )
    blocks = [
        Block(
            id=0,
            type="text",
            text=long_title,
            is_bold=True,
            font_size=12.0,
            alignment="center",
        )
    ]

    compressor = SkeletonCompressor()
    skeleton = compressor.compress(blocks)[0]

    assert long_title in skeleton
    assert "[\u7701\u7565" not in skeleton


def test_resolver_restores_titles_polluted_by_truncation_markers():
    full_title = (
        "Infectious Disease Reporting Procedure for the 113th Academic Year Kindergarten"
    )
    blocks = [
        Block(
            id=0,
            type="text",
            text=full_title,
            is_bold=True,
            font_size=12.0,
            alignment="center",
        ),
        Block(
            id=1,
            type="text",
            text="Student falls ill",
        ),
    ]

    resolver = IntervalResolver(blocks)
    nodes = resolver.resolve([
        ChapterNode(
            block_id=0,
            title="Infectious Disease Reporting Procedure f...[\u7701\u75659?]...3th Academic Year Kindergarten",
            level=1,
            snippet="Infectious Disease Reporting Procedure f...[\u7701\u75659?]...3th Academic Year Kindergarten",
        )
    ])

    assert nodes[0].title == full_title
