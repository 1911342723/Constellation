"""
Docx Provider 测试示例
演示如何使用 DocxProvider 解析 .docx 文件
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from infrastructure.providers import DocxProvider
from modules.parser.parser import CaliperParser
from modules.parser.paper_adapter import PaperEditorAdapter


def test_docx_provider():
    """测试 DocxProvider 基本功能"""
    print("=" * 60)
    print("测试 1: DocxProvider 基本功能")
    print("=" * 60)
    
    # 创建测试用的简单 docx 文件路径
    # 注意：需要提供一个真实的 .docx 文件路径
    test_file = "test_document.docx"
    
    if not Path(test_file).exists():
        print(f"⚠️  测试文件不存在: {test_file}")
        print("请创建一个测试 .docx 文件或修改文件路径")
        return
    
    try:
        # 初始化 Provider
        provider = DocxProvider()
        
        # 提取 Block 列表
        print(f"\n正在解析文件: {test_file}")
        blocks = provider.extract(test_file)
        
        print(f"✓ 解析成功，共提取 {len(blocks)} 个 Block\n")
        
        # 显示前 10 个 Block
        print("Block 列表预览:")
        for i, block in enumerate(blocks[:10]):
            content_preview = block.text[:50] + "..." if block.text and len(block.text) > 50 else block.text
            print(f"  [{block.id}] {block.type}: {content_preview}")
        
        if len(blocks) > 10:
            print(f"  ... 还有 {len(blocks) - 10} 个 Block")
        
        return blocks
        
    except Exception as e:
        print(f"✗ 解析失败: {str(e)}")
        return None


def test_full_pipeline(blocks):
    """测试完整的解析流水线"""
    if not blocks:
        print("\n⚠️  跳过完整流水线测试（没有 Block 数据）")
        return
    
    print("\n" + "=" * 60)
    print("测试 2: 完整解析流水线（Docx → Blocks → DocumentTree）")
    print("=" * 60)
    
    try:
        # 使用 Constellation 解析
        parser = CaliperParser()
        print("\n正在使用游标卡尺法解析文档结构...")
        document_tree = parser.parse(blocks)
        
        print("✓ 解析成功\n")
        
        # 显示文档树结构
        print("文档树结构:")
        for node in document_tree.nodes:
            indent = "  " * (node.level - 1) if node.level > 0 else ""
            print(f"{indent}- {node.title} (level {node.level})")
            if node.children:
                for child in node.children:
                    child_indent = "  " * node.level
                    print(f"{child_indent}  - {child.title} (level {child.level})")
        
        # 输出 Markdown
        print("\nMarkdown 输出预览:")
        markdown = document_tree.to_markdown()
        print(markdown[:500] + "..." if len(markdown) > 500 else markdown)
        
        return document_tree
        
    except Exception as e:
        print(f"✗ 解析失败: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def test_paper_adapter(blocks):
    """测试文章排版系统适配器"""
    if not blocks:
        print("\n⚠️  跳过适配器测试（没有 Block 数据）")
        return
    
    print("\n" + "=" * 60)
    print("测试 3: 文章排版系统适配器")
    print("=" * 60)
    
    try:
        # 使用适配器转换
        adapter = PaperEditorAdapter()
        print("\n正在转换为 PaperData 格式...")
        paper_data = adapter.parse_blocks_to_paper_sections(
            blocks=blocks,
            title="测试文档",
            authors="测试作者"
        )
        
        print("✓ 转换成功\n")
        
        # 显示结果
        print(f"文档信息:")
        print(f"  标题: {paper_data['title']}")
        print(f"  作者: {paper_data['authors']}")
        print(f"  模板: {paper_data['schoolTemplate']}")
        print(f"\n章节列表 (共 {len(paper_data['sections'])} 个):")
        
        for section in paper_data['sections'][:10]:
            indent = "  " * section['level'] if section['level'] > 0 else ""
            section_type = f"[{section['type']}]" if section['type'] != 'section' else ""
            print(f"{indent}- {section['title']} {section_type}")
        
        if len(paper_data['sections']) > 10:
            print(f"  ... 还有 {len(paper_data['sections']) - 10} 个章节")
        
    except Exception as e:
        print(f"✗ 转换失败: {str(e)}")
        import traceback
        traceback.print_exc()


def main():
    """运行所有测试"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 15 + "Docx Provider 测试" + " " * 24 + "║")
    print("╚" + "=" * 58 + "╝")
    print()
    
    # 测试 1: DocxProvider 基本功能
    blocks = test_docx_provider()
    
    # 测试 2: 完整解析流水线
    test_full_pipeline(blocks)
    
    # 测试 3: 文章排版系统适配器
    test_paper_adapter(blocks)
    
    print("\n" + "=" * 60)
    print("✓ 所有测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
