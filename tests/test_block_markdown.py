"""Block.to_markdown 结构安全性：单元格换行 / 代码围栏 / 图片转义等不破坏 Markdown。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.models.block import Block


def test_code_block_uses_longer_fence_when_content_has_backticks():
    code = "print('```')  # 内含三连反引号\n```still inside```"
    md = Block(id=1, type="code", text=code).to_markdown()
    # 围栏必须比内容里最长的反引号串更长，否则会提前闭合代码块
    assert md.startswith("````")
    assert md.rstrip().endswith("````")
    assert code in md


def test_code_block_default_triple_fence():
    md = Block(id=1, type="code", text="print('hi')").to_markdown()
    assert md.startswith("```")
    assert "print('hi')" in md


def test_image_caption_and_ref_escaped():
    b = Block(id=1, type="image", caption="图1] 看[这里\n换行", image_data="http://x/a b.png")
    md = b.to_markdown()
    assert "\n" not in md.split("](")[0]   # caption 段不含裸换行
    assert "\\]" in md                       # caption 里的 ] 被转义
    assert "<http://x/a b.png>" in md        # 含空格的 ref 用尖括号包裹


def test_rebuild_table_cell_newline_to_br():
    b = Block(id=1, type="table", table_data={"rows": [["a", "b"], ["x\ny", "p|q"]]})
    md = b.to_markdown()  # 无 text → 走 _rebuild_markdown_table
    lines = md.split("\n")
    assert len(lines) == 3                   # 表头 + 分隔 + 1 数据行（换行没另起行）
    assert "x<br>y" in lines[2]
    assert "p\\|q" in lines[2]
    assert "\n" not in lines[2]


def test_formula_is_stripped():
    assert Block(id=1, type="formula", text="  E=mc^2  ").to_markdown() == "$$ E=mc^2 $$"


def test_render_table_keeps_overwide_rows():
    """超宽数据行（列数 > 表头）绝不截断——旧实现按表头列数截断会静默丢列。"""
    rows = [["a", "b"], ["1", "2", "3", "4"]]
    md = Block(id=1, type="table", table_data={"rows": rows}).to_markdown()
    lines = md.split("\n")
    # 列宽按最大值 4 对齐：表头右侧补空、分隔符 4 列、数据行 4 列全保留
    assert lines[0] == "| a | b |  |  |"
    assert lines[1] == "| --- | --- | --- | --- |"
    assert lines[2] == "| 1 | 2 | 3 | 4 |"


def test_render_table_pads_short_rows():
    """短数据行（列数 < 表头）右侧补空单元格，保持矩形对齐。"""
    md = Block.render_markdown_table([["a", "b", "c"], ["1"]])
    lines = md.split("\n")
    assert lines[2] == "| 1 |  |  |"


def test_render_table_empty_is_safe():
    """空表 / 全空行不生成非法 GFM。"""
    assert Block.render_markdown_table([]) == ""
    assert Block.render_markdown_table(None) == ""
    assert Block.render_markdown_table([[]]) == ""
    # 通过 Block.to_markdown 时，空 rows 退化为空串而非崩溃
    assert Block(id=1, type="table", table_data={"rows": []}).to_markdown() == ""


def test_render_table_none_cell_is_safe():
    md = Block.render_markdown_table([["h1", "h2"], [None, "v"]])
    assert md.split("\n")[2] == "|  | v |"


def test_block_formula_escapes_dollar_and_newline():
    """块级公式：内部裸 $ 转义避免提前闭合定界；折行归一为空格。"""
    md = Block(id=1, type="formula", text="a $ b\n+ c").to_markdown()
    assert md == "$$ a \\$ b + c $$"


def test_block_formula_strips_existing_double_dollar():
    assert Block(id=1, type="formula", text="$$x+y$$").to_markdown() == "$$ x+y $$"


def test_block_formula_empty_placeholder():
    assert Block(id=1, type="formula", text="   ").to_markdown() == "[公式]"


def test_inline_formula_strip_and_escape():
    assert Block.render_inline_formula(" x^2 ") == "$x^2$"
    assert Block.render_inline_formula("$a$") == "$a$"        # 去掉外层重复 $
    assert Block.render_inline_formula("a$b") == "$a\\$b$"     # 内部 $ 转义
    assert Block.render_inline_formula("x\ny") == "$x y$"      # 折行 → 空格
    assert Block.render_inline_formula("   ") == ""
