import os
import sys
import time
from pathlib import Path

import mammoth
from markitdown import MarkItDown

root_dir = Path(__file__).parent.parent
sys.path.append(str(root_dir))
from infrastructure.providers.docx_provider import DocxProvider

def get_constellation_text(filepath):
    try:
        provider = DocxProvider()
        blocks = provider.extract(str(filepath))
        # 组装为可读文本
        res = []
        for b in blocks:
            # 尽力格式化不同 block：如果是段落，输出text；如果是表格，输出其简单形式
            if b.type == 'table':
                cells = b.metadata.get('cells', []) if b.metadata else []
                res.append(f"**[Table Extracted: {len(cells)} cells]**")
                for cell in cells:
                    res.append(f"  - Cell [{cell.get('row')}, {cell.get('col')}], vMerge:{cell.get('vmerge')} ColSpan:{cell.get('col_span')}: {cell.get('text', '')}")
            elif b.metadata and b.metadata.get('list_level') is not None:
                indent = "    " * b.metadata['list_level']
                res.append(f"{indent}* {b.text.strip()}")
            elif getattr(b, 'text', None):
                if b.type == 'formula':
                    res.append(f"$$ {b.text.strip()} $$")
                else:
                    res.append(b.text.strip())
        return "\n".join(res)
    except Exception as e:
        return f"Error: {e}"

def get_mammoth_text(filepath):
    try:
        with open(filepath, "rb") as f:
            res = mammoth.extract_raw_text(f)
            return res.value.strip()
    except Exception as e:
        return f"Error: {e}"

def get_markitdown_text(filepath):
    try:
        md = MarkItDown()
        res = md.convert(str(filepath))
        return res.text_content.strip() if res.text_content else ""
    except Exception as e:
        return f"Error: {e}"

def generate_report():
    out_dir = Path(__file__).parent.parent / "docs" / "evaluation_reports"
    out_dir.mkdir(exist_ok=True, parents=True)
    
    test_dir = Path(__file__).parent.parent / "tests" / "data" / "extreme"
    
    report_file = out_dir / "extreme_scenarios_report.md"
    
    scenarios = [
        ("1_nested_hell.docx", "“嵌套地狱”测试 (Nested Structures)"),
        ("2_math_fidelity.docx", "公式的高保真还原 (MathML/OMML to LaTeX)"),
        ("3_multilevel_list.docx", "复杂多级列表解析 (Multi-level Numbering)"),
        ("4_floating_objects.docx", "浮动元素与锚点 (Floating Objects & Anchors)"),
    ]
    
    lines = [
        "# 极端场景测试评估报告 (Extreme Scenarios Evaluation)\n",
        "本测试专门针对四个学术与日常文档中最恶劣的结构进行解析测试。横向对比了：**Constellation (我们的算法)**，**Mammoth**，以及 **MarkItDown**。\n"
    ]
    
    for filename, title in scenarios:
        filepath = test_dir / filename
        if not filepath.exists():
            print(f"File not found: {filepath}")
            continue
            
        print(f"Evaluating {filename}...")
        
        c_text = get_constellation_text(filepath)
        m_text = get_mammoth_text(filepath)
        mk_text = get_markitdown_text(filepath)
        
        lines.append(f"## {title}")
        lines.append("### 1. Constellation (Our Algorithm)")
        lines.append("```text\n" + c_text + "\n```\n")
        
        lines.append("### 2. Mammoth")
        lines.append("```text\n" + m_text + "\n```\n")
        
        lines.append("### 3. MarkItDown")
        lines.append("```text\n" + mk_text + "\n```\n")
        lines.append("---\n")
        
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        
    print(f"✅ 极端测试报告已生成: {report_file}")

if __name__ == '__main__':
    generate_report()
