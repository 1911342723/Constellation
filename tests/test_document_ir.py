# -*- coding: utf-8 -*-

"""DocumentIR（T01）的契约护栏：源文字 / 结构 / 渲染分三份，三视图共享一棵树。

覆盖 T01 的核心承诺与本轮**有意划定的边界**：

1. 适配器优先接的三类节点（表格 / 图片 / 标题）确实把「源载荷」与「渲染记号」分开，
   IR 里不出现 ``##`` / ``**`` / ``![]()``。
2. 正文的行内语法真的被拆成语义 span，且「逐字回吐」不变量成立——不是靠一个含糊的
   ``provisional`` 标记假装已解析，也不是拆完丢掉记号（注释宣称的性质必须真的做到）。
3. 三视图契约表齐全且规则相互区分（来源显 provenance、导出内联图片走 markdown 表格）。
"""

from __future__ import annotations

from infrastructure.markdown.inline_syntax import inline_plain_text, render_inlines
from infrastructure.models import Block
from modules.parser.document_ir import (
    VIEW_CONTRACTS,
    InlineKind,
    IRNode,
    NodeKind,
    ViewKind,
    ir_from_block,
    ir_from_document_node,
    ir_from_document_nodes,
)
from modules.parser.schemas import DocumentNode


def _node(title, level, content="", children=None, section_type="section", start=0, end=1):
    return DocumentNode(
        title=title,
        level=level,
        start_block_id=start,
        end_block_id=end,
        content=content,
        section_type=section_type,
        children=children or [],
    )


class TestTableAdapter:
    def test_逻辑行列进结构而非拼死的markdown(self):
        block = Block(id=3, type="table", table_data={"rows": [["姓名", "金额"], ["甲", "10"]]}, caption="费用表")
        ir = ir_from_block(block)
        assert ir.kind is NodeKind.TABLE
        assert ir.table.rows == [["姓名", "金额"], ["甲", "10"]]
        assert ir.table.caption == "费用表"
        # 拿到了逻辑行列就不该退回预渲染字符串。
        assert ir.table.markdown_fallback is None
        assert ir.source.block_id == 3

    def test_只有预渲染markdown时如实退回并标明无行列(self):
        block = Block(id=4, type="table", text="| a | b |\n|---|---|\n| 1 | 2 |")
        ir = ir_from_block(block)
        assert ir.table.rows is None
        assert ir.table.markdown_fallback.startswith("| a | b |")


class TestImageAdapter:
    def test_资源引用与caption分开且不拼markdown记号(self):
        block = Block(
            id=7, type="image", image_data="data:image/png;base64,AAAA",
            caption="示意图", asset_id="sha256:deadbeef", asset_mime="image/png",
            asset_width=100.0, asset_height=50.0,
        )
        ir = ir_from_block(block)
        assert ir.kind is NodeKind.IMAGE
        assert ir.image.asset_id == "sha256:deadbeef"
        assert ir.image.image_ref == "data:image/png;base64,AAAA"
        assert ir.image.caption == "示意图"
        assert ir.image.mime == "image/png"
        # IR 里绝不出现 Markdown 图片记号——渲染留给导出视图。
        assert "![" not in (ir.image.caption or "")
        assert ir.source.raw == ""  # 图片源载荷不在 raw 文本里


class TestHeadingAdapter:
    def test_标题剥强调记号并记provenance(self):
        block = Block(id=1, type="text", text="**支付回调需求**", is_heading_style=True, heading_level=2)
        ir = ir_from_block(block)
        assert ir.kind is NodeKind.HEADING
        assert ir.level == 2
        # 源文字是干净标题，不含 ** 记号。
        assert ir.source.raw == "支付回调需求"
        assert ir.source.provenance.get("stripped_title_emphasis") is True

    def test_未剥离时不虚报provenance(self):
        block = Block(id=1, type="text", text="第一章 绪论", is_heading_style=True, heading_level=1)
        ir = ir_from_block(block)
        assert ir.source.raw == "第一章 绪论"
        # 没做变换就不该在 provenance 里声称做过（宣称的性质必须真的做到）。
        assert "stripped_title_emphasis" not in ir.source.provenance

    def test_heading_level钳到1_6(self):
        block = Block(id=1, type="text", text="深标题", is_heading_style=True, heading_level=9)
        assert ir_from_block(block).level == 6


class TestParagraphInlineSplit:
    def test_段落行内语法被拆成语义span(self) -> None:
        block = Block(id=2, type="text", text="这是一段含 **粗体** 和 [链接](http://x) 的正文。")
        ir = ir_from_block(block)
        assert ir.kind is NodeKind.PARAGRAPH
        kinds = [span.kind for span in ir.inlines]
        assert InlineKind.STRONG in kinds and InlineKind.LINK in kinds
        # 源文字不含记号；记号由渲染层按视图补。
        strong = next(s for s in ir.inlines if s.kind is InlineKind.STRONG)
        assert inline_plain_text(strong.children) == "粗体"
        # 逐字回吐：接上行内解析不改变导出字节。
        assert render_inlines(ir.inlines) == block.text

    def test_空文本不产生span(self) -> None:
        block = Block(id=2, type="text", text="")
        assert ir_from_block(block).inlines == []

    def test_代码与公式各自映射到对应kind(self) -> None:
        assert ir_from_block(Block(id=1, type="code", text="print(1)")).kind is NodeKind.CODE
        assert ir_from_block(Block(id=1, type="formula", text="x^2")).kind is NodeKind.FORMULA

    def test_代码块与公式块整块是verbatim不做行内解析(self) -> None:
        """``a*b*c`` 在代码里是乘法。如实标 verbatim 及其原因，而不是含糊的「未拆」。"""
        code = ir_from_block(Block(id=1, type="code", text="x = a*b*c"))
        assert [s.kind for s in code.inlines] == [InlineKind.TEXT]
        assert code.inlines[0].attrs["verbatim"] == "code_block"
        formula = ir_from_block(Block(id=1, type="formula", text="a_i*b_j"))
        assert formula.inlines[0].attrs["verbatim"] == "formula"


class TestDocumentNodeAdapter:
    def test_标题与正文拆成两层(self):
        node = _node("**背景**", 1, "背景正文。", children=[_node("范围", 2, "范围正文。")])
        ir = ir_from_document_node(node)
        assert ir.kind is NodeKind.HEADING
        assert ir.level == 1
        assert ir.source.raw == "背景"  # 剥离记号
        # 第一个子节点是本节正文，第二个才是子标题。
        assert ir.children[0].kind is NodeKind.PARAGRAPH
        assert render_inlines(ir.children[0].inlines) == "背景正文。"
        assert ir.children[1].kind is NodeKind.HEADING
        assert ir.children[1].level == 2

    def test_section_type被承接(self):
        node = _node("", 0, "前言正文", section_type="preamble")
        assert ir_from_document_node(node).section_type == "preamble"

    def test_森林入口保序(self):
        nodes = [_node("A", 1), _node("B", 1)]
        forest = ir_from_document_nodes(nodes)
        assert [n.source.raw for n in forest] == ["A", "B"]

    def test_先序遍历覆盖全部后代(self):
        node = _node("A", 1, "a", children=[_node("B", 2, "b")])
        ir = ir_from_document_node(node)
        kinds = [n.kind for n in ir.iter_preorder()]
        # A(heading) → A正文(paragraph) → B(heading) → B正文(paragraph)
        assert kinds.count(NodeKind.HEADING) == 2
        assert kinds.count(NodeKind.PARAGRAPH) == 2


class TestViewContracts:
    def test_三视图齐全(self):
        assert set(VIEW_CONTRACTS) == {ViewKind.SOURCE, ViewKind.READING, ViewKind.EXPORT}

    def test_来源视图取源文字并暴露provenance(self):
        c = VIEW_CONTRACTS[ViewKind.SOURCE]
        assert c.text_representation == "source"
        assert c.show_provenance is True

    def test_阅读与导出视图取渲染形态且不暴露provenance(self):
        for kind in (ViewKind.READING, ViewKind.EXPORT):
            c = VIEW_CONTRACTS[kind]
            assert c.text_representation == "rendered"
            assert c.show_provenance is False

    def test_导出视图内联图片走markdown表格_阅读视图按引用取图(self):
        assert VIEW_CONTRACTS[ViewKind.EXPORT].image_delivery == "inline"
        assert VIEW_CONTRACTS[ViewKind.EXPORT].table_representation == "markdown"
        assert VIEW_CONTRACTS[ViewKind.READING].image_delivery == "ref"
        assert VIEW_CONTRACTS[ViewKind.READING].table_representation == "rows"
