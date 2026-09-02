# -*- coding: utf-8 -*-

"""标题解析与 Markdown 装配的回归护栏。

覆盖三类线上真实缺陷：

1. ``full_markdown`` 把同一个标题打两遍（``# 标题`` 紧跟 ``## **标题**``）——起因是
   ``doc_title`` 在没有独立 Title 样式时正是从第一个一级标题兜底来的，而渲染器无条件
   把 H1 留给 doc_title 并把章节整体下移一级。
2. DOCX 加粗标题的 ``**`` 记号渗进结构字段（章节标题、大纲、paper_data）。
3. 文档没有标题时用上传文件名兜底，而上传名常是产物 id / 内容哈希，用户看到一串无意义字符。
"""

from __future__ import annotations

import pytest

from infrastructure.models import Block
from modules.parser.document_tree import DocumentTree
from modules.parser.parser import CaliperParser
from modules.parser.schemas import DocumentNode
from modules.parser.titles import (
    PSEUDO_ROOT_TITLE,
    looks_like_machine_name,
    normalize_title,
    strip_title_emphasis,
)


def _node(title: str, level: int, content: str = "", children=None) -> DocumentNode:
    return DocumentNode(
        title=title,
        level=level,
        start_block_id=0,
        end_block_id=1,
        content=content,
        children=children or [],
    )


def _text_block(block_id: int, text: str, *, style: str = "", heading_level: int = 0) -> Block:
    metadata = {"style": style} if style else {}
    return Block(
        id=block_id,
        type="text",
        text=text,
        is_heading_style=bool(heading_level),
        heading_level=heading_level or None,
        metadata=metadata,
    )


class TestStripTitleEmphasis:
    def test_剥掉成对强调记号(self):
        assert strip_title_emphasis("**支付回调需求**") == "支付回调需求"
        assert strip_title_emphasis("*斜体标题*") == "斜体标题"
        assert strip_title_emphasis("`代码标题`") == "代码标题"
        assert strip_title_emphasis("__粗体标题__") == "粗体标题"

    def test_嵌套强调收敛到不动点(self):
        assert strip_title_emphasis("**_双层强调_**") == "双层强调"

    def test_标题正文里的星号不被删掉(self):
        # 旧实现无条件 replace("*","")，会把标题本身改写成「53 的乘法」。
        assert strip_title_emphasis("5*3 的乘法") == "5*3 的乘法"

    def test_单边残留记号只在首尾清理(self):
        assert strip_title_emphasis("**未闭合标题") == "未闭合标题"

    def test_空值安全(self):
        assert strip_title_emphasis(None) == ""
        assert strip_title_emphasis("") == ""

    def test_比较形态压缩空白(self):
        assert normalize_title("**支付   回调**") == normalize_title("支付 回调")


class TestLooksLikeMachineName:
    @pytest.mark.parametrize("name", [
        "art-userinput-554258c67a503744",
        "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "554258c67a503744",
        "upload_20240101_abcdef123456",
        "tmp-1234",
        "",
        "   ",
    ])
    def test_机器生成的名字被识别(self, name):
        assert looks_like_machine_name(name) is True

    @pytest.mark.parametrize("name", [
        "支付回调需求",
        "PRD-登录改版",
        "2024财年预算",
        "Payment Callback Spec",
    ])
    def test_人起的名字不被误判(self, name):
        assert looks_like_machine_name(name) is False


class TestFullMarkdownDoesNotDuplicateTitle:
    def test_doc_title就是首个章节标题时只出现一次(self):
        """线上真实缺陷：docx 首个 Heading 1 同时成了 doc_title 与 nodes[0]。"""
        tree = DocumentTree(
            nodes=[_node("**支付回调需求**", 1, "商户在收到回调后需在3秒内返回ACK。", [
                _node("**异常流**", 2, "超时未ACK则重投。"),
            ])],
            doc_title="支付回调需求",
        )

        markdown = tree.to_markdown()

        assert tree.title_is_first_heading() is True
        assert markdown.count("支付回调需求") == 1
        # H1 让给章节自己，层级不再整体下移。
        assert markdown == (
            "# 支付回调需求\n"
            "\n"
            "商户在收到回调后需在3秒内返回ACK。\n"
            "\n"
            "## 异常流\n"
            "\n"
            "超时未ACK则重投。\n"
        )

    def test_独立文档大标题仍占H1且章节下移一级(self):
        """有真正独立的大标题（Word Title 样式/封面）时，原来的层级映射必须保留。"""
        tree = DocumentTree(
            nodes=[_node("1 引言", 1, "这是引言正文。", [_node("1.1 范围", 2, "范围说明。")])],
            doc_title="需求规格说明书",
        )

        markdown = tree.to_markdown()

        assert tree.title_is_first_heading() is False
        assert markdown == (
            "# 需求规格说明书\n"
            "\n"
            "## 1 引言\n"
            "\n"
            "这是引言正文。\n"
            "\n"
            "### 1.1 范围\n"
            "\n"
            "范围说明。\n"
        )

    def test_没有doc_title时首个章节就是H1而不是H2(self):
        """旧实现无条件 +1，doc_title 为空的文档会从 ## 开头，整篇没有 H1。"""
        tree = DocumentTree(nodes=[_node("1 引言", 1, "正文。")], doc_title="")

        assert tree.to_markdown().startswith("# 1 引言")

    def test_强调记号不进标题行(self):
        tree = DocumentTree(nodes=[_node("**加粗章节**", 1, "**正文加粗保留**")], doc_title="")

        markdown = tree.to_markdown()

        assert "# 加粗章节" in markdown
        # 正文必须逐字保真，剥离只作用于标题。
        assert "**正文加粗保留**" in markdown


class TestPseudoRootSentinelIsNotUserFacing:
    """零特征文档：伪根标题是内部哨兵，不能变成用户看到的标题。"""

    def _tree(self) -> DocumentTree:
        return DocumentTree(
            nodes=[_node(PSEUDO_ROOT_TITLE, 1, "这是一段没有任何标题结构的说明文字。")],
            doc_title="",
            lossless_fallback=True,
        )

    def test_full_markdown不再凭空多出一个Document标题(self):
        markdown = self._tree().to_markdown()

        assert PSEUDO_ROOT_TITLE not in markdown
        assert markdown.strip() == "这是一段没有任何标题结构的说明文字。"

    def test_章节标题留空而不是Document(self):
        sections = self._tree().to_markdown_sections()

        assert sections[0]["title"] == ""
        assert PSEUDO_ROOT_TITLE not in sections[0]["content"]
        assert "这是一段没有任何标题结构的说明文字。" in sections[0]["content"]

    def test_真实首标题恰好叫Document时不被误伤(self):
        """只比标题文本会误伤真文档；判定必须依赖显式 flag。"""
        tree = DocumentTree(
            nodes=[_node(PSEUDO_ROOT_TITLE, 1, "正文")],
            doc_title=PSEUDO_ROOT_TITLE,
            lossless_fallback=False,
        )

        assert tree.hides_pseudo_root() is False
        assert tree.to_markdown().startswith(f"# {PSEUDO_ROOT_TITLE}")

    def test_evaluation依赖的节点标题保持不变(self):
        """evaluation 按 node.title == "Document" 判定伪根，渲染层的隐藏不能改到它。"""
        tree = self._tree()

        assert tree.nodes[0].title == PSEUDO_ROOT_TITLE
        assert tree.to_paper_data()["sections"][0]["title"] == PSEUDO_ROOT_TITLE


class TestSectionAndPaperTitlesAreStructural:
    def test_章节标题与内容首行都不带强调记号(self):
        tree = DocumentTree(nodes=[_node("**字段说明**", 1, "见下表。")], doc_title="需求")

        sections = tree.to_markdown_sections()

        assert sections[0]["title"] == "字段说明"
        assert sections[0]["content"].startswith("# 字段说明")

    def test_paper_data与json树的标题也已规范化(self):
        tree = DocumentTree(nodes=[_node("**背景**", 1, "正文")], doc_title="**需求**")

        assert tree.to_paper_data()["title"] == "需求"
        assert tree.to_paper_data()["sections"][0]["title"] == "背景"
        assert tree.to_dict()[0]["title"] == "背景"


class TestResolveDocTitle:
    def test_LLM给了标题就用它并剥掉记号(self):
        assert CaliperParser._resolve_doc_title("**登录改版**", [], []) == "登录改版"

    def test_中文模板的标题样式被识别(self):
        """只认英文 "title" 会漏掉中文 Word 模板的「标题」样式。"""
        blocks = [_text_block(0, "支付回调需求", style="标题")]

        assert CaliperParser._resolve_doc_title("", blocks, []) == "支付回调需求"

    def test_没有标题样式时回退第一个一级标题块(self):
        blocks = [_text_block(0, "**支付回调需求**", heading_level=1)]

        assert CaliperParser._resolve_doc_title("", blocks, []) == "支付回调需求"

    def test_没有Word标题样式时回退已解析出的首个章节标题(self):
        """编号标题文档（无 Word 样式）过去拿不到 doc_title，只能退回文件名哈希。"""
        nodes = [_node("**一、背景**", 1, "正文")]

        assert CaliperParser._resolve_doc_title("", [_text_block(0, "正文")], nodes) == "一、背景"

    def test_伪根哨兵不会被当成文档标题(self):
        nodes = [_node(PSEUDO_ROOT_TITLE, 1, "全篇正文")]

        assert CaliperParser._resolve_doc_title("", [], nodes) == ""

    def test_确实没有任何标题时留空(self):
        assert CaliperParser._resolve_doc_title("", [_text_block(0, "正文")], []) == ""
