"""
IBM Docling 公开基准数据集下载脚本
===================================

从 IBM Docling 官方 GitHub 仓库 (docling-project/docling) 下载 DOCX 测试文件，
存放到 benchmarks/docling/ 目录下，用于 Constellation 解析管线的标准化基准测评。

数据来源:
    https://github.com/docling-project/docling/tree/main/tests/data/docx

文件选取策略:
    选取 10 个覆盖不同排版场景的文件，涵盖：
    - 通用结构化文档（标题+段落）
    - 多级标题识别
    - 带编号标题
    - 多级列表
    - 丰富格式排版
    - 表格场景
    - 数学公式
    - 浮动文本框
    - 纯文本无标题（降级模式测试）
    - 编号标题后接列表（边界场景）

使用方式:
    python benchmarks/download_benchmark.py
"""
import os
import sys
import urllib.request
import ssl
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────
# GitHub Raw URL 基础路径（IBM Docling 官方测试数据）
_BASE_URL = (
    "https://raw.githubusercontent.com/docling-project/docling"
    "/main/tests/data/docx"
)

# 要下载的基准文件清单及其测试场景说明
BENCHMARK_FILES = {
    # 文件名 → 测试场景描述
    "word_sample.docx":                "通用文档（标题+段落混排）",
    "unit_test_headers.docx":          "多级标题识别",
    "unit_test_headers_numbered.docx": "带编号的标题",
    "unit_test_lists.docx":            "多级列表还原",
    "unit_test_formatting.docx":       "丰富格式排版",
    "word_tables.docx":                "表格场景",
    "equations.docx":                  "数学公式 (OMML)",
    "textbox.docx":                    "浮动文本框 (txbxContent)",
    "lorem_ipsum.docx":                "纯文本无标题（降级模式）",
    "list_after_num_headers.docx":     "编号标题后接列表",
}


def download_file(url: str, dest: Path, timeout: int = 30) -> bool:
    """
    下载单个文件到指定路径。

    Args:
        url:     远程文件 URL
        dest:    本地保存路径
        timeout: 请求超时秒数

    Returns:
        下载成功返回 True，失败返回 False
    """
    try:
        # 创建不验证 SSL 的上下文（部分企业网络需要）
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, headers={"User-Agent": "Constellation-Benchmark/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read()
            dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"  ✗ 下载失败: {e}")
        return False


def download_all(output_dir: Path | None = None) -> list[Path]:
    """
    批量下载所有基准 DOCX 文件。

    Args:
        output_dir: 输出目录，默认为 benchmarks/docling/

    Returns:
        成功下载的文件路径列表
    """
    if output_dir is None:
        output_dir = Path(__file__).parent / "docling"

    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    print(f"📦 下载 IBM Docling 基准数据集到: {output_dir}")
    print(f"   来源: {_BASE_URL}")
    print(f"   文件数: {len(BENCHMARK_FILES)}")
    print()

    for filename, description in BENCHMARK_FILES.items():
        dest = output_dir / filename
        if dest.exists():
            print(f"  ✓ 已存在，跳过: {filename} ({description})")
            downloaded.append(dest)
            continue

        url = f"{_BASE_URL}/{filename}"
        print(f"  ↓ 下载中: {filename} ({description}) ...")

        if download_file(url, dest):
            size_kb = dest.stat().st_size / 1024
            print(f"    ✓ 完成 ({size_kb:.1f} KB)")
            downloaded.append(dest)
        else:
            print(f"    ✗ 失败: {filename}")

    print()
    print(f"📊 下载结果: {len(downloaded)}/{len(BENCHMARK_FILES)} 个文件")
    return downloaded


if __name__ == "__main__":
    downloaded = download_all()
    if len(downloaded) < len(BENCHMARK_FILES):
        print("⚠️  部分文件下载失败，请检查网络后重试")
        sys.exit(1)
    else:
        print("✅ 全部下载完成！")
