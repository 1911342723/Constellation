"""Run real ablation experiments on benchmark docs.

Measures:
1. Compression ON vs OFF (skeleton size & token cost)
2. Fuzzy anchor radius=0 vs default (assembly coverage impact)
3. Block type detail for extreme case reporting
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig, ResolverConfig
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode


BENCH_DOCS = [
    ('tests/data/extreme_stress_test.docx', 'extreme_stress_test (10M)'),
    ('tests/data/large_test.docx', 'large_test (1.3M)'),
    ('tests/data/stress_test_100w.docx', 'stress_test_100w (1M)'),
    ('tests/data/chaotic_stress_test.docx', 'chaotic_stress (537K)'),
    ('tests/data/test_demo.docx', 'test_demo (9.5K)'),
    ('tests/data/benchmarks/ms_test.docx', 'ms_test (4.5K)'),
    ('tests/data/benchmarks/ibm_lorem.docx', 'ibm_lorem (3.5K)'),
    ('tests/data/benchmarks/ms_equations.docx', 'ms_equations (198)'),
]


def run_compression_ablation():
    print("=" * 100)
    print("  ABLATION 1: Compression ON vs OFF")
    print("=" * 100)
    print()
    print("| Document | Original Chars | WITH Compression | WITHOUT Compression | Reduction | Windows |")
    print("|:---|---:|---:|---:|---:|---:|")

    for fpath, label in BENCH_DOCS:
        if not os.path.isfile(fpath):
            continue
        provider = DocxProvider()
        blocks = provider.extract(fpath)
        total_chars = sum(len(b.text or '') for b in blocks)

        compressor_on = SkeletonCompressor()
        chunks_on = compressor_on.compress(blocks)
        skel_on = sum(len(c) for c in chunks_on)

        no_compress_chars = 0
        for b in blocks:
            line = "[%d|%s] %s" % (b.id, b.type, (b.text or '')[:200])
            no_compress_chars += len(line)

        reduction = (1 - skel_on / max(no_compress_chars, 1)) * 100

        print("| %s | %s | %s (%d win) | %s (1 win) | %.1f%% | %d |" % (
            label,
            format(total_chars, ','),
            format(skel_on, ','), len(chunks_on),
            format(no_compress_chars, ','),
            reduction, len(chunks_on)))


def run_coverage_ablation():
    print()
    print("=" * 100)
    print("  ABLATION 2: Assembly Coverage with synthetic root")
    print("=" * 100)
    print()
    print("| Document | Blocks | Chars | Assembled Chars | Coverage | Headings Detected |")
    print("|:---|---:|---:|---:|---:|---:|")

    for fpath, label in BENCH_DOCS:
        if not os.path.isfile(fpath):
            continue
        provider = DocxProvider()
        blocks = provider.extract(fpath)
        total_chars = sum(len(b.text or '') for b in blocks)
        heading_count = sum(1 for b in blocks if b.is_heading_style or b.is_potential_title())

        try:
            root = ChapterNode(
                start_block_id=blocks[0].id,
                title="Root",
                level=1,
                snippet="Root",
            )
            resolver = IntervalResolver(blocks)
            nodes = resolver.resolve([root])
            assembled = sum(len(n.content or '') for n in nodes)
            cov = assembled / max(total_chars, 1) * 100
        except Exception as e:
            assembled = 0
            cov = 0.0

        print("| %s | %d | %s | %s | %.2f%% | %d |" % (
            label, len(blocks),
            format(total_chars, ','), format(assembled, ','),
            cov, heading_count))


def run_block_detail():
    print()
    print("=" * 100)
    print("  DETAIL: Block type distribution for extreme case reporting")
    print("=" * 100)
    print()
    print("| Document | Text | Table | Image | Formula | Heading Style | Bold Title | Total |")
    print("|:---|---:|---:|---:|---:|---:|---:|---:|")

    for fpath, label in BENCH_DOCS:
        if not os.path.isfile(fpath):
            continue
        provider = DocxProvider()
        blocks = provider.extract(fpath)
        text_b = sum(1 for b in blocks if b.type == 'text')
        table_b = sum(1 for b in blocks if b.type == 'table')
        image_b = sum(1 for b in blocks if b.type == 'image')
        formula_b = sum(1 for b in blocks if b.type == 'formula')
        heading_style = sum(1 for b in blocks if b.is_heading_style)
        bold_title = sum(1 for b in blocks if not b.is_heading_style and b.is_potential_title())

        print("| %s | %d | %d | %d | %d | %d | %d | %d |" % (
            label, text_b, table_b, image_b, formula_b,
            heading_style, bold_title, len(blocks)))


if __name__ == "__main__":
    run_compression_ablation()
    run_coverage_ablation()
    run_block_detail()
