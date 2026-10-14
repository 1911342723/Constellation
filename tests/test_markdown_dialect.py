"""B10 · Markdown 方言完整性：从真实入口 ``MarkdownProvider.extract_from_bytes``
进，对引用块 / 列表 / 引用式链接定义 / Setext / 波浪与长围栏 / 硬换行做
「extract → Block.to_markdown() → 再 extract，结构等价」的往返验收。

方言目标：CommonMark 0.31.2 块级子集 + GFM 表格扩展（覆盖范围与刻意未覆盖的
部分见 ``markdown_provider.py`` 末尾的「方言覆盖说明」）。
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# NOTE: ``infrastructure.providers`` 包内的 pdf_provider 采用「包相对子模块导入」
# （from infrastructure.providers import pdf_page_roles / pdf_reading_order），
# 冷导入该包时会在 __init__ 尚未绑定这些子模块前就触发 PdfProvider 导入而形成
# 循环。本测试只依赖 markdown_provider，与 pdf 无关；这里显式按文件路径加载它，
# 既拿到真实入口，又不被同批并行改动中的 pdf 包导入顺序牵连。
import importlib.util as _ilu

_MP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "infrastructure", "providers", "markdown_provider.py",
)
_spec = _ilu.spec_from_file_location("mp_dialect_under_test", _MP_PATH)
_mp = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mp)
MarkdownProvider = _mp.MarkdownProvider


def _parse(text: str):
    return MarkdownProvider().extract_from_bytes(text.encode("utf-8"))


def _render(blocks) -> str:
    return "\n\n".join(b.to_markdown() for b in blocks)


def _struct_sig(blocks):
    """结构签名：类型 + 标题层级 + 容器种类。用于「结构等价」断言。"""
    return [
        (b.type, b.is_heading_style, b.heading_level, (b.md_structure or {}).get("kind"))
        for b in blocks
    ]


def _roundtrip(text: str):
    blocks = _parse(text)
    reblocks = _parse(_render(blocks))
    return blocks, reblocks


# ── 缩进代码不成为标题（B10 复现缺陷）────────────────────────────────────
def test_indented_code_not_heading_roundtrip():
    blocks, reblocks = _roundtrip("    # this is indented code\n")
    assert blocks[0].type == "code"
    assert blocks[0].is_heading_style is False
    assert reblocks[0].type == "code"
    assert reblocks[0].text == blocks[0].text


def test_source_span_preserved_unstripped():
    """源切片按原始行保留，未先 strip 缩进（保留 source span 的直接证据）。"""
    b = _parse("    # indented\n")[0]
    assert b.source_text == "    # indented"


# ── 引用块（含嵌套、引用内列表 / 代码）──────────────────────────────────
def test_blockquote_basic_roundtrip():
    blocks, reblocks = _roundtrip("> quoted line\n> second line\n")
    assert (blocks[0].md_structure or {}).get("kind") == "blockquote"
    assert _struct_sig(blocks) == _struct_sig(reblocks)


def test_blockquote_nested_roundtrip():
    blocks, reblocks = _roundtrip("> outer\n> > inner\n")
    md = _render(blocks)
    assert "> > inner" in md  # 嵌套引用双层前缀
    assert _struct_sig(blocks) == _struct_sig(reblocks)


def test_blockquote_with_list_and_code_roundtrip():
    src = "> - a\n> - b\n>\n>     code\n"
    blocks, reblocks = _roundtrip(src)
    assert (blocks[0].md_structure or {}).get("kind") == "blockquote"
    assert _struct_sig(blocks) == _struct_sig(reblocks)


# ── 列表容器 ─────────────────────────────────────────────────────────────
def test_unordered_list_roundtrip():
    blocks, reblocks = _roundtrip("- a\n- b\n- c\n")
    assert (blocks[0].md_structure or {}).get("kind") == "list"
    assert blocks[0].md_structure["ordered"] is False
    assert len(blocks[0].md_structure["items"]) == 3
    assert _struct_sig(blocks) == _struct_sig(reblocks)


def test_ordered_list_start_preserved_roundtrip():
    blocks, reblocks = _roundtrip("3. three\n4. four\n")
    ms = blocks[0].md_structure
    assert ms["ordered"] is True and ms["start"] == 3
    assert reblocks[0].md_structure["start"] == 3


def test_nested_list_roundtrip():
    src = "- top\n  - child1\n  - child2\n- second\n"
    blocks, reblocks = _roundtrip(src)
    ms = blocks[0].md_structure
    nested = [c for c in ms["items"][0]["children"]
              if (c.get("md_structure") or {}).get("kind") == "list"]
    assert nested, "嵌套列表未被识别为子列表容器"
    assert _struct_sig(blocks) == _struct_sig(reblocks)


def test_tight_vs_loose_list():
    tight = _parse("- a\n- b\n")[0].md_structure["tight"]
    loose = _parse("- a\n\n- b\n")[0].md_structure["tight"]
    assert tight is True
    assert loose is False


def test_list_item_code_block_roundtrip():
    src = "- item\n\n      indented code\n"
    blocks, reblocks = _roundtrip(src)
    kinds = [c.get("type") for c in blocks[0].md_structure["items"][0]["children"]]
    assert "code" in kinds
    re_kinds = [c.get("type") for c in reblocks[0].md_structure["items"][0]["children"]]
    assert "code" in re_kinds  # 代码块类型往返保持（缩进→围栏是语义等价的允许变形）


# ── 引用式链接定义（定义与使用都往返保持）──────────────────────────────
def test_link_reference_definition_roundtrip():
    src = 'Use [text][ref] here.\n\n[ref]: https://example.com "T"\n'
    blocks, reblocks = _roundtrip(src)
    defs = [b for b in blocks if (b.md_structure or {}).get("kind") == "link_definitions"]
    assert defs, "链接定义未被捕获"
    d = defs[0].md_structure["defs"][0]
    assert d["label"] == "ref" and d["url"] == "https://example.com" and d["title"] == "T"
    assert any("[text][ref]" in (b.text or "") for b in blocks)
    redefs = [b for b in reblocks if (b.md_structure or {}).get("kind") == "link_definitions"]
    assert redefs and redefs[0].md_structure["defs"][0]["url"] == "https://example.com"


def test_link_definition_without_title_roundtrip():
    blocks, reblocks = _roundtrip("[a]: /path\n")
    d = blocks[0].md_structure["defs"][0]
    assert d["label"] == "a" and d["url"] == "/path" and d["title"] is None
    assert reblocks[0].md_structure["defs"][0]["url"] == "/path"


# ── Setext 标题 ─────────────────────────────────────────────────────────
def test_setext_l1_roundtrip():
    blocks, reblocks = _roundtrip("Title\n=====\n")
    assert blocks[0].is_heading_style and blocks[0].heading_level == 1
    assert reblocks[0].is_heading_style and reblocks[0].heading_level == 1


def test_setext_l2_roundtrip():
    blocks, reblocks = _roundtrip("Subtitle\n----\n")
    assert blocks[0].is_heading_style and blocks[0].heading_level == 2
    assert reblocks[0].heading_level == 2


# ── 波浪围栏 / 长反引号围栏 / info string 优先取源 ──────────────────────
def test_tilde_fence_roundtrip():
    blocks, reblocks = _roundtrip("~~~python\nprint(1)\n~~~\n")
    assert blocks[0].type == "code"
    assert (blocks[0].metadata or {}).get("code_lang") == "python"
    assert reblocks[0].type == "code"
    assert (reblocks[0].metadata or {}).get("code_lang") == "python"


def test_long_backtick_fence_with_inner_backticks_roundtrip():
    src = "````\ninner ``` still code\n````\n"
    blocks, reblocks = _roundtrip(src)
    assert blocks[0].type == "code" and "```" in blocks[0].text
    assert reblocks[0].type == "code" and "```" in reblocks[0].text


def test_info_string_language_prefers_source():
    b = _parse("```rust\nfn main() {}\n```\n")[0]
    assert (b.metadata or {}).get("code_lang") == "rust"
    assert b.to_markdown().startswith("```rust")


# ── 硬换行（行尾两空格 / 反斜杠）──────────────────────────────────────────
def test_hard_break_two_spaces_preserved_roundtrip():
    blocks, reblocks = _roundtrip("line one  \nline two\n")
    assert "line one\\" in (blocks[0].text or "")  # 两空格归一为反斜杠硬换行
    assert "\\" in (reblocks[0].text or "")


def test_hard_break_backslash_preserved_roundtrip():
    blocks, reblocks = _roundtrip("line one\\\nline two\n")
    assert "line one\\" in (blocks[0].text or "")
    assert "\\" in (reblocks[0].text or "")


# ── 组合文档：多类构造混排的整体结构往返 ────────────────────────────────
def test_mixed_document_structure_roundtrip():
    src = (
        "# Heading\n\n"
        "para with [x][r].\n\n"
        "> quote\n> > deep\n\n"
        "- a\n  - a1\n- b\n\n"
        "```js\nconst x = 1;\n```\n\n"
        "[r]: http://e.com\n"
    )
    blocks, reblocks = _roundtrip(src)
    assert _struct_sig(blocks) == _struct_sig(reblocks)
