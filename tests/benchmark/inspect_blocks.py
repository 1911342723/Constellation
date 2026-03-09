"""
Block 结构检查辅助脚本
====================

对 tests/benchmark/docling/ 目录下的所有 DOCX 文件运行 DocxProvider，
输出每个文件的 Block 列表摘要，用于辅助编写 Ground Truth 标注。

输出信息包括:
  - Block ID、类型、物理特征标签（加粗/字号/标题样式）
  - 前 80 个字符的文本预览
  - 是否被 is_potential_title() 判定为潜在标题

使用方式:
    cd Constellation/
    python tests/benchmark/inspect_blocks.py
"""
import sys
import os
from pathlib import Path

# 确保项目根目录在 Python Path 中
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from infrastructure.providers.docx_provider import DocxProvider


def inspect_file(docx_path: Path) -> str:
    """
    提取单个 DOCX 文件的 Block 列表，返回格式化的摘要文本。

    Args:
        docx_path: DOCX 文件路径

    Returns:
        包含 Block 摘要的多行字符串
    """
    provider = DocxProvider()
    blocks = provider.extract(str(docx_path))

    lines = []
    lines.append(f"{'='*80}")
    lines.append(f"📄 {docx_path.name}  ({docx_path.stat().st_size / 1024:.1f} KB)")
    lines.append(f"   Blocks: {len(blocks)}")
    lines.append(f"{'='*80}")

    for b in blocks:
        # 构建物理特征标签
        tags = []
        if getattr(b, 'is_bold', False):
            tags.append("Bold")
        if getattr(b, 'font_size', None) and b.font_size:
            tags.append(f"Size:{b.font_size}")
        if getattr(b, 'heading_style', None) and b.heading_style:
            tags.append(f"H:{b.heading_style}")
        if getattr(b, 'alignment', None) and b.alignment:
            tags.append(f"Align:{b.alignment}")

        tag_str = f" <{', '.join(tags)}>" if tags else ""

        # 文本预览（截断至 80 字符）
        text = getattr(b, 'text', '') or ''
        preview = text[:80].replace('\n', '↵') + ('...' if len(text) > 80 else '')

        # 是否为潜在标题
        is_title = "★" if b.is_potential_title() else " "

        lines.append(
            f"  [{b.id:>4}] {b.type:<8}{tag_str:<30} {is_title} {preview}"
        )

    lines.append("")
    return "\n".join(lines)


def main():
    """扫描 tests/benchmark/docling/ 目录下所有 DOCX 文件并输出 Block 摘要。"""
    data_dir = Path(__file__).parent / "docling"
    if not data_dir.exists():
        print(f"❌ 数据目录不存在: {data_dir}")
        print("   请先运行 download_benchmark.py 下载数据集")
        sys.exit(1)

    docx_files = sorted(data_dir.glob("*.docx"))
    if not docx_files:
        print(f"❌ 目录中没有 DOCX 文件: {data_dir}")
        sys.exit(1)

    # 输出到文件，方便查看
    output_path = Path(__file__).parent / "block_inspection.txt"
    all_output = []

    for docx_path in docx_files:
        try:
            result = inspect_file(docx_path)
            all_output.append(result)
            print(result)
        except Exception as e:
            msg = f"❌ 解析失败: {docx_path.name}: {e}"
            all_output.append(msg)
            print(msg)

    # 保存到文件
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_output))

    print(f"\n📝 Block 检查结果已保存到: {output_path}")


if __name__ == "__main__":
    main()
