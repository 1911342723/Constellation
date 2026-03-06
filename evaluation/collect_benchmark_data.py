"""Collect real benchmark data from all test DOCX files.

Runs Stage 1 (extraction) and Stage 2 (compression) on every .docx
in tests/data/ to produce actual performance numbers for the paper.

Also runs character-coverage assembly (Stage 4 with a synthetic root)
to measure character recall without requiring LLM calls.

Usage:
    python evaluation/collect_benchmark_data.py
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


def _count_tree_chars(nodes):
    """Recursively count content chars across entire DocumentNode tree."""
    total = 0
    for n in nodes:
        total += len(n.content or '')
        total += _count_tree_chars(n.children)
    return total


def collect_all():
    data_dirs = ['tests/data', 'tests/data/benchmarks', 'tests/data/stress']
    seen = set()
    all_files = []
    for d in data_dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith('.docx'):
                fp = os.path.join(d, f)
                if fp not in seen:
                    seen.add(fp)
                    all_files.append((fp, f))

    print("=" * 100)
    print("  Constellation Benchmark Data Collection")
    print("=" * 100)
    print()

    # Table 1: Extraction & Compression
    print("## Table: Extraction & Compression Metrics")
    print()
    print("| File | Blocks | Chars | Skeleton Chars | Compression | Extract(s) | Compress(s) | Windows |")
    print("|:-----|-------:|------:|---------------:|------------:|-----------:|------------:|--------:|")

    results = []
    for fpath, fname in all_files:
        try:
            provider = DocxProvider()
            t0 = time.perf_counter()
            blocks = provider.extract(fpath)
            t_extract = time.perf_counter() - t0

            total_chars = sum(len(b.text or '') for b in blocks)

            compressor = SkeletonCompressor()
            t1 = time.perf_counter()
            chunks = compressor.compress(blocks)
            t_compress = time.perf_counter() - t1

            skeleton_chars = sum(len(c) for c in chunks)
            ratio = (1 - skeleton_chars / max(total_chars, 1)) * 100 if total_chars > 0 else 0

            heading_blocks = [b for b in blocks if b.is_heading_style or b.is_potential_title()]
            text_blocks = [b for b in blocks if b.type == 'text']
            table_blocks = [b for b in blocks if b.type == 'table']
            image_blocks = [b for b in blocks if b.type == 'image']
            formula_blocks = [b for b in blocks if b.type == 'formula']

            row = {
                'fname': fname,
                'blocks': len(blocks),
                'chars': total_chars,
                'skeleton': skeleton_chars,
                'ratio': ratio,
                't_extract': t_extract,
                't_compress': t_compress,
                'windows': len(chunks),
                'headings': len(heading_blocks),
                'text': len(text_blocks),
                'tables': len(table_blocks),
                'images': len(image_blocks),
                'formulas': len(formula_blocks),
            }

            print("| %s | %d | %s | %s | %.1f%% | %.3f | %.4f | %d |" % (
                fname, len(blocks),
                format(total_chars, ','), format(skeleton_chars, ','),
                ratio, t_extract, t_compress, len(chunks)))

            # Character coverage via synthetic root assembly
            try:
                root_anchor = ChapterNode(
                    start_block_id=blocks[0].id,
                    title="Root",
                    level=1,
                    snippet="Root",
                )
                resolver = IntervalResolver(blocks)
                nodes = resolver.resolve([root_anchor])
                assembled_chars = _count_tree_chars(nodes)
                baseline_chars = sum(len(b.to_markdown()) for b in blocks)
                coverage = assembled_chars / max(baseline_chars, 1) * 100
                row['coverage'] = coverage
                row['assembled_chars'] = assembled_chars
            except Exception:
                row['coverage'] = 0
                row['assembled_chars'] = 0

            results.append(row)
        except Exception as e:
            print("| %s | ERROR: %s |||||||" % (fname, str(e)[:50]))

    # Table 2: Block Type Distribution
    print()
    print("## Table: Block Type Distribution")
    print()
    print("| File | Text | Table | Image | Formula | Headings | Coverage |")
    print("|:-----|-----:|------:|------:|--------:|---------:|---------:|")
    for r in results:
        print("| %s | %d | %d | %d | %d | %d | %.2f%% |" % (
            r['fname'], r['text'], r['tables'], r['images'],
            r['formulas'], r['headings'], r.get('coverage', 0)))

    # Summary
    print()
    print("## Summary Statistics")
    print()
    total_docs = len(results)
    total_blocks = sum(r['blocks'] for r in results)
    total_chars = sum(r['chars'] for r in results)
    total_skeleton = sum(r['skeleton'] for r in results)
    avg_ratio = sum(r['ratio'] for r in results) / max(total_docs, 1)
    avg_coverage = sum(r.get('coverage', 0) for r in results) / max(total_docs, 1)
    total_extract = sum(r['t_extract'] for r in results)
    total_compress = sum(r['t_compress'] for r in results)

    print("- Documents: %d" % total_docs)
    print("- Total Blocks: %s" % format(total_blocks, ','))
    print("- Total Characters: %s" % format(total_chars, ','))
    print("- Total Skeleton Characters: %s" % format(total_skeleton, ','))
    print("- Average Compression Ratio: %.1f%%" % avg_ratio)
    print("- Average Character Coverage: %.2f%%" % avg_coverage)
    print("- Total Extract Time: %.3fs" % total_extract)
    print("- Total Compress Time: %.4fs" % total_compress)
    print("- Extract Throughput: %s chars/s" % format(int(total_chars / max(total_extract, 0.001)), ','))
    print("- Compress Throughput: %s chars/s" % format(int(total_chars / max(total_compress, 0.001)), ','))

    return results


if __name__ == "__main__":
    collect_all()
