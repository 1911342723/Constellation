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


def _spanned(title: str, level: int, start: int, end: int, content: str = "", children=None) -> DocumentNode:
    """B08 用：可显式指定来源跨度的节点（section_id 从跨度 + 结构位置派生）。"""
    return DocumentNode(
        title=title,
        level=level,
        start_block_id=start,
        end_block_id=end,
        content=content,
        children=children or [],
    )


class TestB02HeadingLevelMapping:
    """B02：语义层级 → 导出视图标题级别的单一映射，全文 / 分章 / MCP 三出口一致。

    原始缺陷：``_render_child_section`` 在已算好的相对级别上再 +1，L1 根下的 L2 子节被
    打成 ``### Child``（多降一级）；MCP ``render_section_markdown`` 又写死 ``level+1``，把
    独立导出的根打成 ``## Root``（根都不是 H1）。三处偏移各自为政、互相矛盾。
    """

    def test_分章视图_L1_L2_L3_逐级映射(self):
        tree = DocumentTree(
            nodes=[_node("Root", 1, "r", [_node("Child", 2, "c", [_node("Grand", 3, "g")])])],
            doc_title="",
        )
        content = tree.to_markdown_sections()[0]["content"]
        assert "# Root" in content
        assert "## Child" in content and "### Child" not in content
        assert "### Grand" in content and "#### Grand" not in content

    def test_全文视图_有独立大标题时章节整体下移一级(self):
        tree = DocumentTree(
            nodes=[_node("引言", 1, "正文", [_node("范围", 2, "说明")])],
            doc_title="需求规格说明书",
        )
        md = tree.to_markdown()
        assert "# 需求规格说明书" in md
        assert "## 引言" in md
        assert "### 范围" in md  # 下移一级：L2 → H3

    def test_全文视图_无独立大标题时首章占H1(self):
        tree = DocumentTree(
            nodes=[_node("引言", 1, "正文", [_node("范围", 2, "说明")])],
            doc_title="",
        )
        md = tree.to_markdown()
        assert md.startswith("# 引言")
        assert "## 范围" in md  # 不下移：L2 → H2

    def test_伪根不打标题但正文保真(self):
        tree = DocumentTree(
            nodes=[_node(PSEUDO_ROOT_TITLE, 1, "全篇正文")],
            doc_title="",
            lossless_fallback=True,
        )
        assert PSEUDO_ROOT_TITLE not in tree.to_markdown_sections()[0]["content"]
        assert "全篇正文" in tree.to_markdown_sections()[0]["content"]

    def test_从L3章节独立导出根仍是H1(self):
        """分章根节点无论真实语义层级多深，独立导出时都映射为 H1，后代按差平移。"""
        tree = DocumentTree(
            nodes=[_node("深章", 3, "正文", [_node("更深", 5, "子正文")])],
            doc_title="",
        )
        content = tree.to_markdown_sections()[0]["content"]
        assert content.startswith("# 深章")  # L3 根 → H1
        assert "### 更深" in content  # 语义差 5-3=2 → H1+2 = H3

    def test_超过H6的语义层级钳到H6且JSON保留真实层级(self):
        deep = _node("L1", 1, "", [_node("L2", 2, "", [_node("L3", 3, "", [
            _node("L4", 4, "", [_node("L5", 5, "", [_node("L6", 6, "", [
                _node("L7", 7, "叶子"),
            ])])])])])])
        tree = DocumentTree(nodes=[deep], doc_title="")
        content = tree.to_markdown_sections()[0]["content"]
        assert "###### L6" in content
        assert "####### L7" not in content  # H7 不存在，钳到 H6
        assert "###### L7" in content
        # JSON 里仍是真实层级 7。
        assert tree.to_dict()[0]["children"][0]["children"][0]["children"][0][
            "children"][0]["children"][0]["children"][0]["level"] == 7

    def test_全文与分章对同一子树相对层级一致(self):
        """同一棵 L1→L2→L3 树：分章根为 H1，全文（无独立大标题）顶层也为 H1，
        因此两条出口对相同子树给出恒等的相对层级。"""
        tree = DocumentTree(
            nodes=[_node("A", 1, "a", [_node("B", 2, "b", [_node("C", 3, "c")])])],
            doc_title="",
        )
        section = tree.to_markdown_sections()[0]["content"]
        full = tree.to_markdown()
        for marker in ("# A", "## B", "### C"):
            assert marker in section
            assert marker in full


class TestB08StableSectionId:
    """B08：section_id 由文档身份 + 来源跨度 + 结构位置确定性派生，跨序列化稳定。

    原始缺陷：``_node_to_paper_section`` 每次 ``uuid.uuid4()``，同一棵树两次
    ``to_paper_data()`` 的同一章节 ID 都不同 —— 缓存命中 / 再次下载 / 切换预览后
    编辑器的评论、锚点、折叠状态全部对不上。
    """

    def _tree(self) -> DocumentTree:
        return DocumentTree(
            nodes=[_spanned("背景", 1, 0, 5, "b", [_spanned("范围", 2, 1, 3, "s")]),
                   _spanned("背景", 1, 6, 9, "b2")],  # 同名标题、不同跨度
            doc_title="需求",
        )

    def test_重复序列化ID恒等(self):
        tree = self._tree()
        first = tree.to_paper_data()["sections"]
        second = tree.to_paper_data()["sections"]
        assert first[0]["id"] == second[0]["id"]
        assert first[0]["subsections"][0]["id"] == second[0]["subsections"][0]["id"]
        assert first[1]["id"] == second[1]["id"]

    def test_重建等价树ID稳定_模拟缓存命中再下载(self):
        """内容相同的两棵独立构造的树（缓存命中 / 再次下载会发生）给出相同 ID。"""
        assert (
            self._tree().to_paper_data()["sections"][0]["id"]
            == self._tree().to_paper_data()["sections"][0]["id"]
        )

    def test_同名不同跨度的章节ID不相同(self):
        """两个都叫「背景」的章节靠跨度 + 结构位置区分，绝不并成同一 ID（标题哈希会）。"""
        sections = self._tree().to_paper_data()["sections"]
        assert sections[0]["id"] != sections[1]["id"]

    def test_ID不随标题改变而改变(self):
        """标题会被编辑；只要跨度 + 结构位置不变，ID 就应保持，避免局部纠错换掉整段身份。"""
        a = DocumentTree(nodes=[_spanned("旧标题", 1, 0, 5, "正文")], doc_title="需求")
        b = DocumentTree(nodes=[_spanned("新标题", 1, 0, 5, "正文")], doc_title="需求")
        # doc_identity 只看源指纹（缺省时退回 doc_title + 作者），不含节点跨度，
        # 因此仅改章节标题不改 section_id。
        assert a.to_paper_data()["sections"][0]["id"] == b.to_paper_data()["sections"][0]["id"]

    def test_局部纠错只改必要节点的ID(self):
        """把第二章的起始块挪一格，第一章及其子节的 ID 必须一个都不变。

        这是 B08 验收里「局部纠错只改变必要节点」的直接检查，也是身份串**不能**
        掺进全部节点跨度的原因：一旦掺进去，改任何一处都会换掉全篇每个
        section_id，评论、锚点、折叠状态一起丢。
        """
        def _build(second_start: int) -> DocumentTree:
            return DocumentTree(
                nodes=[
                    _spanned("背景", 1, 0, 5, "b", [_spanned("范围", 2, 1, 3, "s")]),
                    _spanned("方案", 1, second_start, 9, "p"),
                ],
                doc_title="需求",
                source_identity="fingerprint-unchanged",
            )

        before = _build(6).to_paper_data()["sections"]
        after = _build(7).to_paper_data()["sections"]
        assert before[0]["id"] == after[0]["id"]
        assert before[0]["subsections"][0]["id"] == after[0]["subsections"][0]["id"]
        # 唯一该变的就是被改动的那一节。
        assert before[1]["id"] != after[1]["id"]

    def test_不同源文档指纹的ID不相同(self):
        """同标题、同结构的两份不同源文档靠源指纹区分，不会共用一套 section_id。"""
        def _build(identity: str) -> DocumentTree:
            return DocumentTree(
                nodes=[_spanned("背景", 1, 0, 5, "b")],
                doc_title="需求",
                source_identity=identity,
            )

        assert (
            _build("doc-a").to_paper_data()["sections"][0]["id"]
            != _build("doc-b").to_paper_data()["sections"][0]["id"]
        )

    def test_解析器把源指纹作为文档身份传入(self):
        """契约检查：``_resolve_and_build_tree`` 必须把缓存键透传成 source_identity。

        身份不落到树上，B08 就退回「同名文档撞车」的兜底口径；这条钉住接线本身。
        """
        import inspect

        from modules.parser.parser import CaliperParser

        sig = inspect.signature(CaliperParser._resolve_and_build_tree)
        assert "source_identity" in sig.parameters

    def test_ID形如UUID(self):
        sec_id = self._tree().to_paper_data()["sections"][0]["id"]
        parts = sec_id.split("-")
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]
