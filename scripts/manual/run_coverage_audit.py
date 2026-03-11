import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig, ResolverConfig
from modules.parser.schemas import ChapterNode
from modules.parser.resolver import IntervalResolver

def run_coverage_test():
    data_dir = Path(__file__).resolve().parents[2] / "tests" / "data"
    output_dir = Path(__file__).parent / "output" / "coverage" / "markdowns"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    docx_files = list(data_dir.rglob("*.docx"))
    
    provider = DocxProvider()
    compressor = SkeletonCompressor(CompressorConfig(enable_rle=True, max_rle_group=50, sliding_window_threshold=99999))
    
    results = []
    
    for doc_path in docx_files:
        print(f"[{doc_path.name}] 开始验证覆盖率...")
        
        # 1. 物理提取
        blocks = provider.extract(str(doc_path))
        if not blocks:
            print(f"[{doc_path.name}] 空文档，跳过")
            continue
            
        original_text_chars = sum(len(b.text.strip()) for b in blocks if hasattr(b, 'text') and b.text and b.type == 'text')
        
        # 2. 生成一个最高级的伪锚点 (直接囊括全部文件边界)
        # 用以此迫使 Resolver 将所有的底层 Blocks 闭合组装成最终实体
        mock_anchors = [
            ChapterNode(start_block_id=0, level=1, title="Test Root", snippet="Root")
        ]
        
        # 3. 装配还原 (配置模糊容差半径，用于测试容差闭环)
        resolver = IntervalResolver(blocks, ResolverConfig(fuzzy_anchor_radius=3))
        assembled_nodes = resolver.resolve(mock_anchors)
        
        # 4. 生成最终 Markdown (拼装)
        final_md_blocks = []
        assembled_text_chars = 0
        def traverse_and_assemble(node):
            nonlocal assembled_text_chars
            # Header
            if node.title != "Test Root":
                final_md_blocks.append(f"{'#' * node.level} {node.title}")
                assembled_text_chars += len(node.title.strip())
            
            # Sub blocks
            for i in range(node.start_block_id, node.end_block_id + 1):
                b = blocks[i]
                if b.type == 'text':
                    final_md_blocks.append(b.text)
                    assembled_text_chars += len(b.text.strip())
                elif b.type == 'formula':
                    final_md_blocks.append(f"\\n$$\\n{b.text}\\n$$\\n")
                    # formula 算在最终信息载体中，但不算严格净文本字符进行对比
                elif b.type == 'table':
                    final_md_blocks.append(b.text)
                
            for child in node.children:
                traverse_and_assemble(child)
                
        for root_node in assembled_nodes:
            traverse_and_assemble(root_node)
            
        final_md_content = "\n\n".join(final_md_blocks)
        
        # 保存 Markdown
        out_md_path = output_dir / f"{doc_path.stem}.md"
        with open(out_md_path, "w", encoding="utf-8") as f:
            f.write(final_md_content)
            
        # 5. 计算覆盖率
        # 注意: 纯组装过程不应该丢失任何 'text' content.
        diff = original_text_chars - assembled_text_chars
        # 对于 test Root 节点标题是不存在于原块里的 (减去以配平)
        if diff < 0:
            diff = 0
            
        coverage = (assembled_text_chars / max(original_text_chars, 1)) * 100
        coverage = min(100.0, coverage) # 防止 root title 导致超出 100%
        
        print(f"  --> 原文件纯粹文字字数: {original_text_chars}")
        print(f"  --> 落盘 Markdown 文本文字数: {assembled_text_chars}")
        print(f"  --> 绝对文本内容覆盖率: {coverage:.2f}%")
        
        results.append({
            "filename": doc_path.name,
            "original_chars": original_text_chars,
            "md_chars": assembled_text_chars,
            "coverage": coverage,
            "output_path": f"scripts/manual/output/coverage/markdowns/{doc_path.stem}.md"
        })
        
    # 生成报告
    report_path = output_dir.parent / "coverage_report.md"
    lines = ["# 闭合重构无损覆盖率验证报告\n", "| 文件名 | 物理流纯文本基准 | 重组落盘 Markdown 纯文本量 | 绝对文本覆盖率 | 归档路径 |", "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['filename']} | {r['original_chars']} | {r['md_chars']} | **{r['coverage']:.2f}%** | `{r['output_path']}` |")
        
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        
    print(f"\n✅ 验证结束！所有最终组装 Markdown 已保存到: {output_dir}")
    print(f"✅ 覆盖率数据已输出到: {report_path}")

if __name__ == "__main__":
    run_coverage_test()
