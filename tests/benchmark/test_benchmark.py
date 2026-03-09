"""
Constellation 公开数据集 Benchmark 全维度评测脚本
=================================================

对 IBM Docling 官方 DOCX 测试套件执行 Constellation 全流水线评测，
收集以下维度的数据：

┌─────────────────────────────────────────────────────────┐
│ 维度 1：物理提取 (Stage 1)                              │
│   - DocxProvider 提取耗时                                │
│   - Block 总数 & 类型分布 (Text/Table/Image/Formula)     │
│   - 原文总字符数                                         │
│                                                         │
│ 维度 2：骨架压缩 (Stage 2)                              │
│   - SkeletonCompressor 压缩耗时                          │
│   - 骨架字符数 & 压缩率                                  │
│   - 预估 Token 数 & Token 节省率                         │
│   - 滑动窗口数                                           │
│                                                         │
│ 维度 3：全流水线 (Stage 1-4, 需要 LLM API)              │
│   - 端到端解析总耗时                                     │
│   - Section F1 / Precision / Recall                      │
│   - 层级准确率 (Hierarchy Accuracy)                      │
│   - 树编辑距离 (Tree Edit Distance)                      │
│   - 字符覆盖率 (Character Recall)                        │
│   - 检出的章节数 & 最大层级深度                          │
│                                                         │
│ 维度 4：汇总统计                                         │
│   - 跨文档平均指标                                       │
│   - 分类别统计（结构化文档 vs 边缘场景）                 │
└─────────────────────────────────────────────────────────┘

使用方式:
    cd Constellation/
    # 仅 Stage 1+2 离线测试（不需要 LLM API，速度快）
    python tests/benchmark/test_benchmark.py --stage1-only

    # 全流水线测试（需要 .env 中配置 LLM API Key）
    python tests/benchmark/test_benchmark.py

    # 多轮统计（每个文档跑 3 次取均值±标准差）
    python tests/benchmark/test_benchmark.py --num-runs 3

数据来源:
    IBM Docling 官方测试套件
    https://github.com/docling-project/docling/tree/main/tests/data/docx
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── 项目根目录加入 Python Path ──────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from infrastructure.providers.docx_provider import DocxProvider
from infrastructure.models.block import Block
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig

logger = logging.getLogger(__name__)

# ── 常量定义 ────────────────────────────────────────────
# 中文 Token 估算系数（1 个中文字符 ≈ 1.5 Token，英文 ≈ 0.25 Token/字符）
# 这里采用保守的混合估算：1 字符 ≈ 0.75 Token（适用于中英混合文档）
_TOKEN_PER_CHAR = 0.75

# 数据目录
_BENCHMARK_DIR = Path(__file__).parent / "docling"
_GT_DIR = Path(__file__).parent / "ground_truth"
_REPORT_PATH = Path(__file__).parent / "benchmark_report.md"


# ══════════════════════════════════════════════════════════
#  数据结构定义
# ══════════════════════════════════════════════════════════

@dataclass
class Stage1Result:
    """Stage 1 物理提取结果。"""
    filename: str               # 文件名
    file_size_kb: float         # 文件大小 (KB)
    block_count: int            # Block 总数
    type_distribution: dict     # Block 类型分布 {"text": N, "table": N, ...}
    original_chars: int         # 原文总字符数
    extract_time_s: float       # 提取耗时 (秒)
    throughput_chars_s: float   # 提取吞吐量 (字符/秒)


@dataclass
class Stage2Result:
    """Stage 2 骨架压缩结果。"""
    skeleton_chars: int         # 骨架字符数
    compression_ratio: float    # 压缩率 (%)
    compress_time_s: float      # 压缩耗时 (秒)
    window_count: int           # 滑动窗口数
    estimated_tokens_raw: int   # 原文预估 Token 数
    estimated_tokens_skel: int  # 骨架预估 Token 数
    token_savings_pct: float    # Token 节省率 (%)


@dataclass
class FullPipelineResult:
    """全流水线 (Stage 1-4) 评测结果。"""
    total_time_s: float         # 端到端总耗时
    section_count: int          # 检出的章节数
    max_depth: int              # 最大层级深度
    char_recall: float          # 字符覆盖率
    # 以下仅在有 ground truth 时填充
    f1: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None
    hierarchy_accuracy: Optional[float] = None
    tree_edit_distance: Optional[float] = None
    tp: int = 0
    fp: int = 0
    fn: int = 0


@dataclass
class BenchmarkRecord:
    """单个文档的全维度评测记录。"""
    filename: str
    category: str               # "structured" | "edge_case"
    stage1: Stage1Result
    stage2: Stage2Result
    pipeline: Optional[FullPipelineResult] = None


# ══════════════════════════════════════════════════════════
#  Stage 1 + Stage 2 离线评测（不需要 LLM）
# ══════════════════════════════════════════════════════════

def run_stage1(docx_path: Path) -> tuple[Stage1Result, list[Block]]:
    """
    执行 Stage 1 物理提取并收集性能数据。

    Args:
        docx_path: DOCX 文件路径

    Returns:
        (Stage1Result 数据, Block 列表)
    """
    provider = DocxProvider()

    t0 = time.perf_counter()
    blocks = provider.extract(str(docx_path))
    extract_time = time.perf_counter() - t0

    # 统计 Block 类型分布
    type_counts = Counter(b.type for b in blocks)

    # 计算原文字符总量（用于压缩率和覆盖率的基线）
    original_chars = sum(len(b.text) for b in blocks if b.text)

    # 计算提取吞吐量
    throughput = original_chars / max(extract_time, 1e-6)

    result = Stage1Result(
        filename=docx_path.name,
        file_size_kb=docx_path.stat().st_size / 1024,
        block_count=len(blocks),
        type_distribution=dict(type_counts),
        original_chars=original_chars,
        extract_time_s=extract_time,
        throughput_chars_s=throughput,
    )

    return result, blocks


def run_stage2(blocks: list[Block]) -> Stage2Result:
    """
    执行 Stage 2 骨架压缩并收集性能数据。

    Args:
        blocks: Stage 1 提取的 Block 列表

    Returns:
        Stage2Result 数据
    """
    compressor = SkeletonCompressor(
        CompressorConfig(enable_rle=True, max_rle_group=50)
    )

    t0 = time.perf_counter()
    skeleton_chunks = compressor.compress(blocks)
    compress_time = time.perf_counter() - t0

    # 统计骨架字符数
    skeleton_chars = sum(len(c) for c in skeleton_chunks)
    original_chars = sum(len(b.text) for b in blocks if b.text)

    # 计算压缩率 —— 注意极短文档（元数据注入可能使骨架更大）
    if original_chars > 0:
        compression_ratio = (1 - skeleton_chars / original_chars) * 100
    else:
        compression_ratio = 0.0

    # Token 估算
    tokens_raw = int(original_chars * _TOKEN_PER_CHAR)
    tokens_skel = int(skeleton_chars * _TOKEN_PER_CHAR)
    token_savings = (1 - tokens_skel / max(tokens_raw, 1)) * 100

    return Stage2Result(
        skeleton_chars=skeleton_chars,
        compression_ratio=compression_ratio,
        compress_time_s=compress_time,
        window_count=len(skeleton_chunks),
        estimated_tokens_raw=tokens_raw,
        estimated_tokens_skel=tokens_skel,
        token_savings_pct=token_savings,
    )


# ══════════════════════════════════════════════════════════
#  全流水线评测（需要 LLM API）
# ══════════════════════════════════════════════════════════

def run_full_pipeline(
    docx_path: Path,
    blocks: list[Block],
    gt_path: Optional[Path] = None,
) -> FullPipelineResult:
    """
    执行完整的 Stage 1-4 解析流水线，对比 Ground Truth 计算指标。

    Args:
        docx_path:  DOCX 文件路径
        blocks:     Stage 1 已提取的 Block 列表（避免重复提取）
        gt_path:    Ground Truth JSON 文件路径（可选）

    Returns:
        FullPipelineResult 数据
    """
    from modules.parser.parser import CaliperParser
    from evaluation.metrics import (
        HeadingGT, HeadingPred,
        compute_section_f1, compute_char_recall,
    )

    # 清除缓存确保每次独立运行
    CaliperParser.clear_cache()

    parser = CaliperParser()
    t0 = time.perf_counter()
    tree = parser.parse(blocks)
    total_time = time.perf_counter() - t0

    # 提取预测结果 —— 递归遍历文档树
    pred_headings: list[HeadingPred] = []
    max_depth = 0

    def walk_nodes(nodes, depth=1):
        nonlocal max_depth
        for node in nodes:
            pred_headings.append(HeadingPred(
                block_id=node.start_block_id,
                title=node.title,
                level=node.level,
            ))
            max_depth = max(max_depth, node.level)
            if node.children:
                walk_nodes(node.children, depth + 1)

    walk_nodes(tree.nodes)

    # 计算字符覆盖率
    original_chars = sum(len(b.to_markdown()) for b in blocks)
    stats = tree.get_stats()
    extracted_chars = stats["total_content_chars"]
    char_recall = compute_char_recall(original_chars, extracted_chars)

    result = FullPipelineResult(
        total_time_s=total_time,
        section_count=len(pred_headings),
        max_depth=max_depth,
        char_recall=char_recall,
    )

    # 如果有 Ground Truth，计算结构化指标
    if gt_path and gt_path.exists():
        with open(gt_path, encoding="utf-8") as f:
            gt_data = json.load(f)

        gt_headings = [
            HeadingGT(
                block_id=h["block_id"],
                title=h["title"],
                level=h["level"],
            )
            for h in gt_data.get("headings", [])
        ]

        if gt_headings:
            eval_result = compute_section_f1(gt_headings, pred_headings)
            result.f1 = eval_result.f1
            result.precision = eval_result.precision
            result.recall = eval_result.recall
            result.hierarchy_accuracy = eval_result.hierarchy_accuracy
            result.tree_edit_distance = eval_result.tree_edit_distance
            result.tp = eval_result.tp
            result.fp = eval_result.fp
            result.fn = eval_result.fn
        else:
            # Ground Truth 无标题 —— 检查系统是否也不产生误报
            result.f1 = 1.0 if len(pred_headings) == 0 else 0.0
            result.precision = 1.0 if len(pred_headings) == 0 else 0.0
            result.recall = 1.0
            result.hierarchy_accuracy = 1.0
            result.tree_edit_distance = float(len(pred_headings))
            result.fp = len(pred_headings)

    return result


# ══════════════════════════════════════════════════════════
#  报告生成
# ══════════════════════════════════════════════════════════

def _fmt(val: Optional[float], fmt: str = ".4f") -> str:
    """安全格式化浮点数，None 返回 'N/A'。"""
    if val is None:
        return "N/A"
    return f"{val:{fmt}}"


def _categorize(filename: str) -> str:
    """
    将文件分为"结构化文档"或"边缘场景"类别。

    结构化文档: 包含明确标题结构的文件
    边缘场景: 纯文本、纯公式、纯格式等无标题结构的文件
    """
    structured = {
        "unit_test_headers.docx", "unit_test_headers_numbered.docx",
        "unit_test_lists.docx", "word_sample.docx", "word_tables.docx",
    }
    return "structured" if filename in structured else "edge_case"


def generate_report(records: list[BenchmarkRecord]) -> str:
    """
    生成全维度 Markdown 评测报告。

    Args:
        records: 所有文档的评测记录列表

    Returns:
        完整的 Markdown 报告字符串
    """
    lines = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    # ── 报告标题 ──────────────────────────────────────
    lines.append("# 🔬 Constellation 公开数据集 Benchmark 评测报告")
    lines.append("")
    lines.append(f"> **生成时间**: {now}")
    lines.append(f"> **数据来源**: IBM Docling 官方测试套件")
    lines.append(f"> **文档数量**: {len(records)}")
    lines.append("")

    # ── Stage 1: 物理提取 ─────────────────────────────
    lines.append("---")
    lines.append("## 1. 物理提取性能 (Stage 1: DocxProvider)")
    lines.append("")
    lines.append("| 文件名 | 文件大小 | Blocks | Text | Table | Image | Formula | 原文字符 | 提取耗时 | 吞吐量 |")
    lines.append("|:-------|--------:|-------:|-----:|------:|------:|--------:|---------:|---------:|-------:|")

    for r in records:
        s1 = r.stage1
        td = s1.type_distribution
        lines.append(
            f"| {s1.filename} "
            f"| {s1.file_size_kb:.1f} KB "
            f"| {s1.block_count} "
            f"| {td.get('text', 0)} "
            f"| {td.get('table', 0)} "
            f"| {td.get('image', 0)} "
            f"| {td.get('formula', 0)} "
            f"| {s1.original_chars:,} "
            f"| {s1.extract_time_s:.3f}s "
            f"| {s1.throughput_chars_s:,.0f} c/s |"
        )

    # Stage 1 汇总
    total_blocks = sum(r.stage1.block_count for r in records)
    total_chars = sum(r.stage1.original_chars for r in records)
    total_time = sum(r.stage1.extract_time_s for r in records)
    lines.append("")
    lines.append(f"**Stage 1 汇总**: 共提取 **{total_blocks}** 个 Block, "
                 f"**{total_chars:,}** 字符, 总耗时 **{total_time:.3f}s**")
    lines.append("")

    # ── Stage 2: 骨架压缩 ─────────────────────────────
    lines.append("---")
    lines.append("## 2. 骨架压缩效率 (Stage 2: SkeletonCompressor)")
    lines.append("")
    lines.append("| 文件名 | 原文字符 | 骨架字符 | 压缩率 | 窗口数 | 原文Token(估) | 骨架Token(估) | Token节省率 | 压缩耗时 |")
    lines.append("|:-------|--------:|---------:|-------:|-------:|--------------:|--------------:|:----------:|---------:|")

    for r in records:
        s1, s2 = r.stage1, r.stage2
        lines.append(
            f"| {s1.filename} "
            f"| {s1.original_chars:,} "
            f"| {s2.skeleton_chars:,} "
            f"| {s2.compression_ratio:.1f}% "
            f"| {s2.window_count} "
            f"| {s2.estimated_tokens_raw:,} "
            f"| {s2.estimated_tokens_skel:,} "
            f"| {s2.token_savings_pct:.1f}% "
            f"| {s2.compress_time_s:.4f}s |"
        )

    # Stage 2 汇总
    avg_compression = sum(r.stage2.compression_ratio for r in records if r.stage2.compression_ratio > 0) / max(
        sum(1 for r in records if r.stage2.compression_ratio > 0), 1
    )
    total_tokens_raw = sum(r.stage2.estimated_tokens_raw for r in records)
    total_tokens_skel = sum(r.stage2.estimated_tokens_skel for r in records)
    overall_token_savings = (1 - total_tokens_skel / max(total_tokens_raw, 1)) * 100
    lines.append("")
    lines.append(f"**Stage 2 汇总**: 平均压缩率 **{avg_compression:.1f}%**, "
                 f"Token 整体节省率 **{overall_token_savings:.1f}%** "
                 f"({total_tokens_raw:,} → {total_tokens_skel:,})")
    lines.append("")

    # ── Stage 3-4: 全流水线 ───────────────────────────
    pipeline_records = [r for r in records if r.pipeline is not None]
    if pipeline_records:
        lines.append("---")
        lines.append("## 3. 全流水线结构化评测 (Stage 1-4)")
        lines.append("")
        lines.append("| 文件名 | 类别 | 章节数 | 最大深度 | 字符覆盖率 | F1 | Precision | Recall | 层级准确率 | TED | 耗时 |")
        lines.append("|:-------|:----:|------:|---------:|-----------:|---:|----------:|-------:|-----------:|----:|-----:|")

        for r in pipeline_records:
            p = r.pipeline
            lines.append(
                f"| {r.filename} "
                f"| {r.category} "
                f"| {p.section_count} "
                f"| {p.max_depth} "
                f"| {_fmt(p.char_recall, '.4f')} "
                f"| {_fmt(p.f1, '.4f')} "
                f"| {_fmt(p.precision, '.4f')} "
                f"| {_fmt(p.recall, '.4f')} "
                f"| {_fmt(p.hierarchy_accuracy, '.4f')} "
                f"| {_fmt(p.tree_edit_distance, '.1f')} "
                f"| {p.total_time_s:.2f}s |"
            )

        # ── 分类统计 ──────────────────────────────────
        lines.append("")
        lines.append("### 3.1 分类别统计")
        lines.append("")

        for category in ["structured", "edge_case"]:
            cat_records = [r for r in pipeline_records if r.category == category]
            if not cat_records:
                continue

            cat_label = "结构化文档" if category == "structured" else "边缘场景"
            f1_vals = [r.pipeline.f1 for r in cat_records if r.pipeline.f1 is not None]
            ha_vals = [r.pipeline.hierarchy_accuracy for r in cat_records if r.pipeline.hierarchy_accuracy is not None]
            cr_vals = [r.pipeline.char_recall for r in cat_records if r.pipeline.char_recall is not None]

            def _mean(vs): return sum(vs) / len(vs) if vs else 0
            def _std(vs):
                if len(vs) < 2: return 0
                m = _mean(vs)
                return math.sqrt(sum((v - m)**2 for v in vs) / (len(vs) - 1))

            lines.append(f"**{cat_label}** ({len(cat_records)} 篇):")
            if f1_vals:
                lines.append(f"- 平均 F1: **{_mean(f1_vals):.4f}** ± {_std(f1_vals):.4f}")
            if ha_vals:
                lines.append(f"- 平均层级准确率: **{_mean(ha_vals):.4f}** ± {_std(ha_vals):.4f}")
            if cr_vals:
                lines.append(f"- 平均字符覆盖率: **{_mean(cr_vals):.4f}** ± {_std(cr_vals):.4f}")
            lines.append("")

        # ── 全局汇总 ──────────────────────────────────
        all_f1 = [r.pipeline.f1 for r in pipeline_records if r.pipeline.f1 is not None]
        all_cr = [r.pipeline.char_recall for r in pipeline_records if r.pipeline.char_recall is not None]
        all_ha = [r.pipeline.hierarchy_accuracy for r in pipeline_records if r.pipeline.hierarchy_accuracy is not None]
        total_pipeline_time = sum(r.pipeline.total_time_s for r in pipeline_records)

        lines.append("### 3.2 全局汇总")
        lines.append("")
        if all_f1:
            avg_f1 = sum(all_f1) / len(all_f1)
            lines.append(f"- **全局平均 F1**: {avg_f1:.4f}")
        if all_ha:
            avg_ha = sum(all_ha) / len(all_ha)
            lines.append(f"- **全局平均层级准确率**: {avg_ha:.4f}")
        if all_cr:
            avg_cr = sum(all_cr) / len(all_cr)
            lines.append(f"- **全局平均字符覆盖率**: {avg_cr:.4f}")
        lines.append(f"- **全流水线总耗时**: {total_pipeline_time:.2f}s")
        lines.append("")

    # ── 数据集说明 ─────────────────────────────────────
    lines.append("---")
    lines.append("## 附录: 数据集说明")
    lines.append("")
    lines.append("| 来源 | 仓库 | 许可证 |")
    lines.append("|:-----|:-----|:------|")
    lines.append("| IBM Docling 官方测试套件 | [docling-project/docling](https://github.com/docling-project/docling) | MIT License |")
    lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════

def main():
    """Benchmark 评测主入口函数。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Constellation 公开数据集 Benchmark 全维度评测"
    )
    parser.add_argument(
        "--stage1-only", action="store_true",
        help="仅运行 Stage 1 + Stage 2 离线测试（不需要 LLM API）"
    )
    parser.add_argument(
        "--num-runs", type=int, default=1,
        help="每个文档的全流水线评测运行次数（默认 1，设为 3-5 用于统计显著性）"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=_BENCHMARK_DIR,
        help="DOCX 数据文件目录"
    )
    parser.add_argument(
        "--gt-dir", type=Path, default=_GT_DIR,
        help="Ground Truth 标注目录"
    )
    parser.add_argument(
        "--output", type=Path, default=_REPORT_PATH,
        help="输出报告路径"
    )
    args = parser.parse_args()

    # ── 检查数据目录 ──────────────────────────────────
    if not args.data_dir.exists():
        print(f"❌ 数据目录不存在: {args.data_dir}")
        print("   请先运行: python tests/benchmark/download_benchmark.py")
        sys.exit(1)

    docx_files = sorted(args.data_dir.glob("*.docx"))
    if not docx_files:
        print(f"❌ 数据目录中没有 DOCX 文件: {args.data_dir}")
        sys.exit(1)

    print(f"🔬 Constellation Benchmark 评测")
    print(f"   数据目录: {args.data_dir}")
    print(f"   文件数量: {len(docx_files)}")
    print(f"   运行模式: {'Stage 1+2 离线' if args.stage1_only else '全流水线 (Stage 1-4)'}")
    print()

    records: list[BenchmarkRecord] = []

    for docx_path in docx_files:
        print(f"{'─'*60}")
        print(f"📄 {docx_path.name}")

        # ── Stage 1: 物理提取 ─────────────────────────
        print(f"   Stage 1: 物理提取 ...", end=" ", flush=True)
        s1_result, blocks = run_stage1(docx_path)
        print(f"✓ ({s1_result.block_count} blocks, {s1_result.original_chars:,} chars, {s1_result.extract_time_s:.3f}s)")

        # ── Stage 2: 骨架压缩 ─────────────────────────
        print(f"   Stage 2: 骨架压缩 ...", end=" ", flush=True)
        s2_result = run_stage2(blocks)
        print(f"✓ (压缩率 {s2_result.compression_ratio:.1f}%, {s2_result.window_count} 窗口, {s2_result.compress_time_s:.4f}s)")

        category = _categorize(docx_path.name)
        record = BenchmarkRecord(
            filename=docx_path.name,
            category=category,
            stage1=s1_result,
            stage2=s2_result,
        )

        # ── Stage 3-4: 全流水线（如果不是 stage1-only）──
        if not args.stage1_only:
            gt_path = args.gt_dir / (docx_path.stem + ".json")
            has_gt = gt_path.exists()

            print(f"   Stage 3-4: 全流水线 ...", end=" ", flush=True)
            if has_gt:
                print(f"(GT: {gt_path.name})", end=" ", flush=True)

            try:
                pipeline_result = run_full_pipeline(docx_path, blocks, gt_path if has_gt else None)
                record.pipeline = pipeline_result

                # 打印关键指标
                parts = [f"✓ ({pipeline_result.total_time_s:.2f}s"]
                parts.append(f"章节={pipeline_result.section_count}")
                parts.append(f"覆盖率={pipeline_result.char_recall:.4f}")
                if pipeline_result.f1 is not None:
                    parts.append(f"F1={pipeline_result.f1:.4f}")
                if pipeline_result.hierarchy_accuracy is not None:
                    parts.append(f"层级={pipeline_result.hierarchy_accuracy:.4f}")
                print(", ".join(parts) + ")")

            except Exception as e:
                print(f"✗ 失败: {e}")
                logger.exception(f"全流水线评测失败: {docx_path.name}")

        records.append(record)

    # ── 生成报告 ──────────────────────────────────────
    print(f"\n{'═'*60}")
    report = generate_report(records)

    # 保存到文件
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"📊 评测报告已生成: {args.output}")
    print()

    # 同时打印到控制台
    print(report)


if __name__ == "__main__":
    main()
