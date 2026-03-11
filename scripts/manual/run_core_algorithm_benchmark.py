"""
核心算法真实数据基准测试
使用真实的 docx 文件进行性能测试，关注 DocxProvider 的提取速度，和 Compressor 的压缩率骨架生成效率
"""
import sys
import time
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig

def run_benchmark_on_files():
    data_dir = Path(__file__).resolve().parents[2] / "tests" / "data"
    if not data_dir.exists():
        print(f"数据目录不存在: {data_dir}")
        return

    docx_files = list(data_dir.rglob("*.docx"))
    if not docx_files:
        print(f"在 {data_dir} 中未找到任何 .docx 文件")
        return

    provider = DocxProvider()
    compressor = SkeletonCompressor(
        CompressorConfig(
            enable_rle=True, 
            max_rle_group=50, 
            sliding_window_threshold=99999  # 为了统计，暂不主动切分多个 window
        )
    )

    import json
    
    output_dir = Path(__file__).parent / "output" / "core_algorithm"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / "benchmark_results.md"

    results = []
    for doc_path in docx_files:
        print(f"正在测试文件: {doc_path.name} ...")
        file_size_kb = doc_path.stat().st_size / 1024

        st_extract = time.time()
        blocks = provider.extract(str(doc_path))
        extract_cost = time.time() - st_extract
        
        original_chars = sum(len(b.text) for b in blocks if hasattr(b, 'text') and b.text)

        if not blocks:
            print(f"  警告: {doc_path.name} 未能提取任何有效的 Block。")
            continue

        st_compress = time.time()
        skeleton_chunks = compressor.compress(blocks)
        compress_cost = time.time() - st_compress

        compressed_chars = sum(len(c) for c in skeleton_chunks)
        ratio = (1 - compressed_chars / max(original_chars, 1)) * 100
        
        # 预估 token (一般模型 1 个中文字符 大约 1.5 - 2 Tokens)
        estimated_tokens = int(compressed_chars * 1.5)
        total_time = extract_cost + compress_cost

        results.append({
            "filename": doc_path.name,
            "file_size": f"{file_size_kb:.1f} KB",
            "original_chars": original_chars,
            "compressed_chars": compressed_chars,
            "compression_ratio": f"{ratio:.2f}%",
            "extract_time": f"{extract_cost:.3f} s",
            "compress_time": f"{compress_cost:.3f} s",
            "total_time": f"{total_time:.3f} s",
            "estimated_tokens": estimated_tokens
        })

    # 构建 Markdown 表格
    md_lines = []
    md_lines.append("# Core Algorithm Benchmark Results")
    md_lines.append("")
    md_lines.append("| 文件名 | 文件大小 | 提取字符数 | 骨架字符数 | 预估 Token | 压缩率 | 抽取耗时 | 压缩耗时 | 总耗时 |")
    md_lines.append("|---|---|---|---|---|---|---|---|---|")
    
    for r in results:
        md_lines.append(f"| {r['filename']} | {r['file_size']} | {r['original_chars']} | {r['compressed_chars']} | {r['estimated_tokens']} | {r['compression_ratio']} | {r['extract_time']} | {r['compress_time']} | {r['total_time']} |")

    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"\n============================================================")
    print(f"测试完毕！已将性能基准测试报告输出至 {out_file}")
    print(f"============================================================")

if __name__ == "__main__":
    logging.getLogger("infrastructure.providers.docx_provider").setLevel(logging.WARNING)
    run_benchmark_on_files()
