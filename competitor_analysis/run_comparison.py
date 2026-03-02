import os
import sys
import time
from pathlib import Path

import mammoth
from markitdown import MarkItDown

def convert_mammoth(filepath):
    st = time.time()
    try:
        with open(filepath, "rb") as f:
            res = mammoth.extract_raw_text(f)
            text = res.value
        cost = time.time() - st
        return len(text.strip()), cost
    except Exception as e:
        print(f"  Mammoth Error: {e}")
        return 0, time.time() - st

def convert_markitdown(filepath):
    st = time.time()
    try:
        md = MarkItDown()
        res = md.convert(str(filepath))
        text = res.text_content
        return len(text.strip()), time.time() - st
    except Exception as e:
        print(f"  MarkItDown Error: {e}")
        return 0, time.time() - st

def main():
    root_dir = Path(__file__).parent.parent
    data_dir = root_dir / "tests" / "data"
    
    if not data_dir.exists():
        print(f"Data dir not found: {data_dir}")
        return

    docx_files = list(data_dir.rglob("*.docx"))
    
    print("Starting Comparison Benchmark...")
    results = []
    
    sys.path.append(str(root_dir))
    from infrastructure.providers.docx_provider import DocxProvider
    provider = DocxProvider()
    
    for f in docx_files:
        print(f"\nProcessing {f.name}...")
        
        # Constellation baseline extraction
        blocks = provider.extract(str(f))
        baseline_chars = sum(len(b.text.strip()) for b in blocks if hasattr(b, 'text') and b.text and b.type == 'text')
        
        # Mammoth
        m_chars, m_cost = convert_mammoth(f)
        
        # MarkItDown
        mk_chars, mk_cost = convert_markitdown(f)
        
        results.append({
            "filename": f.name,
            "baseline": baseline_chars,
            "mammoth_chars": m_chars,
            "mammoth_time": m_cost,
            "markitdown_chars": mk_chars,
            "markitdown_time": mk_cost
        })
        
        print(f"  Baseline:   {baseline_chars}")
        print(f"  Mammoth:    {m_chars} ({m_chars/max(baseline_chars, 1):.2%}) - [{m_cost:.2f}s]")
        print(f"  MarkItDown: {mk_chars} ({mk_chars/max(baseline_chars, 1):.2%}) - [{mk_cost:.2f}s]")
        
    out_dir = Path(__file__).parent.parent / "docs" / "evaluation_reports"
    out_dir.mkdir(exist_ok=True, parents=True)
    out_file = out_dir / "comparison_report.md"
    
    lines = ["# 竞品横向对比测试报告\n", "| 文件名 | Constellation 纯文本绝对覆盖基准 (字) | Mammoth 提取字符数 (占比) | 耗时 | MarkItDown 提取字符数 (占比) | 耗时 |", "|---|---|---|---|---|---|"]
    for r in results:
        m_pct = r['mammoth_chars'] / max(r['baseline'], 1)
        mk_pct = r['markitdown_chars'] / max(r['baseline'], 1)
        lines.append(f"| {r['filename']} | {r['baseline']} | {r['mammoth_chars']} (**{m_pct:.2%}**) | {r['mammoth_time']:.3f}s | {r['markitdown_chars']} (**{mk_pct:.2%}**) | {r['markitdown_time']:.3f}s |")
        
    with open(out_file, "w", encoding="utf-8") as fw:
        fw.write("\n".join(lines))
        
    print(f"\n✅ 横向评测完毕！已保存输出到 {out_file}")

if __name__ == '__main__':
    import logging
    logging.getLogger("infrastructure.providers.docx_provider").setLevel(logging.WARNING)
    main()
