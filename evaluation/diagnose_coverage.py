"""Diagnose anomalous coverage numbers in benchmark docs."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.resolver import IntervalResolver
from modules.parser.schemas import ChapterNode


DOCS = [
    ('tests/data/chaotic_stress_test.docx', 'chaotic_stress'),
    ('tests/data/test_demo.docx', 'test_demo'),
    ('tests/data/benchmarks/ms_test.docx', 'ms_test'),
    ('tests/data/benchmarks/ibm_lorem.docx', 'ibm_lorem'),
    ('tests/data/benchmarks/ms_equations.docx', 'ms_equations'),
    ('tests/data/benchmarks/ibm_grouped_images.docx', 'ibm_grouped_images'),
    ('tests/data/benchmarks/ibm_headers.docx', 'ibm_headers'),
    ('tests/data/large_test.docx', 'large_test'),
    ('tests/data/extreme_stress_test.docx', 'extreme_stress_test'),
    ('tests/data/stress_test_100w.docx', 'stress_100w'),
]


def diagnose():
    print("=" * 90)
    print("  Coverage Diagnosis: Synthetic Root Assembly")
    print("=" * 90)

    for fpath, label in DOCS:
        if not os.path.isfile(fpath):
            continue
        provider = DocxProvider()
        blocks = provider.extract(fpath)
        total_chars = sum(len(b.text or '') for b in blocks)

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

        status = "OK"
        if cov > 110:
            status = "INFLATED (+%.0f%%)" % (cov - 100)
        elif cov < 90:
            status = "LOSS (-%.1f%%)" % (100 - cov)

        print()
        print("--- %s --- [%s]" % (label, status))
        print("  Blocks: %d | Total chars: %s | Nodes: %d | Assembled: %s | Coverage: %.2f%%"
              % (len(blocks), format(total_chars, ','), len(nodes),
                 format(assembled, ','), cov))

        if cov > 200 or cov < 50:
            print("  *** ANOMALY ***")
            for i, n in enumerate(nodes[:8]):
                c_len = len(n.content or '')
                title_safe = (n.title or '')[:40]
                print("    [%d] L%d \"%s\": %s chars" % (i, n.level, title_safe, format(c_len, ',')))
            if len(nodes) > 8:
                print("    ... +%d more nodes" % (len(nodes) - 8))

        # Check for duplicate content
        if len(nodes) > 1:
            contents = [n.content or '' for n in nodes if n.content]
            total_unique = len(set(contents))
            if total_unique < len(contents):
                print("  WARNING: %d duplicate content strings (out of %d)"
                      % (len(contents) - total_unique, len(contents)))

        # Measure block text vs assembled text gap
        block_texts = set()
        for b in blocks:
            if b.text:
                block_texts.add(b.text.strip())
        assembled_text = ' '.join(n.content or '' for n in nodes)
        missing_blocks = 0
        for bt in block_texts:
            if bt and bt not in assembled_text:
                missing_blocks += 1
        if missing_blocks > 0:
            print("  MISSING: %d/%d block texts not found in assembled output"
                  % (missing_blocks, len(block_texts)))


if __name__ == "__main__":
    diagnose()
