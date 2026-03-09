"""大文档 Stage 1+2 性能测试"""
import sys, os, time
sys.path.insert(0, os.path.abspath('.'))
from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.compressor import SkeletonCompressor
from modules.parser.config import CompressorConfig
from collections import Counter

provider = DocxProvider()
compressor = SkeletonCompressor(CompressorConfig(enable_rle=True, max_rle_group=50))

files = [
    'tests/data/test_demo.docx',
    'tests/data/extreme_stress_test.docx',
    'tests/data/large_test.docx',
    'tests/data/chaotic_stress_test.docx',
    'tests/data/stress_test_100w.docx',
]

out = open('tests/benchmark/large_doc_results.txt', 'w', encoding='utf-8')

def p(s=''):
    print(s)
    out.write(s + '\n')

p('=' * 120)
p('Constellation 大文档 Stage 1+2 性能测试')
p('=' * 120)
p()
p(f'{"文件名":30s} | {"大小":>10s} | {"Blocks":>7s} | {"原文字符":>12s} | {"骨架字符":>10s} | {"压缩率":>8s} | {"窗口":>4s} | {"原Token(估)":>12s} | {"骨架Token":>10s} | {"Token节省":>10s} | {"提取耗时":>10s} | {"压缩耗时":>10s}')
p('-' * 120)

for f in files:
    if not os.path.exists(f):
        p(f'{os.path.basename(f):30s} | 文件不存在')
        continue
    
    size_kb = os.path.getsize(f) / 1024
    
    t0 = time.perf_counter()
    blocks = provider.extract(f)
    t_ext = time.perf_counter() - t0
    
    chars = sum(len(b.text) for b in blocks if b.text)
    types = Counter(b.type for b in blocks)
    
    t1 = time.perf_counter()
    chunks = compressor.compress(blocks)
    t_comp = time.perf_counter() - t1
    
    skel_chars = sum(len(c) for c in chunks)
    ratio = (1 - skel_chars / max(chars, 1)) * 100
    tok_raw = int(chars * 0.75)
    tok_skel = int(skel_chars * 0.75)
    tok_save = (1 - tok_skel / max(tok_raw, 1)) * 100
    
    p(f'{os.path.basename(f):30s} | {size_kb:8.1f}KB | {len(blocks):>5d}   | {chars:>10,}   | {skel_chars:>8,}   | {ratio:>6.1f}%  | {len(chunks):>3d}  | {tok_raw:>10,}   | {tok_skel:>8,}   | {tok_save:>7.1f}%   | {t_ext:>8.3f}s  | {t_comp:>8.4f}s')
    p(f'  类型分布: {dict(types)}')
    p()

p('=' * 120)
out.close()
