"""诊断 unit_test_lists.docx 层级准确率问题"""
import sys, os, json, traceback, logging
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

out = open('benchmarks/diagnose_output.txt', 'w', encoding='utf-8')

def p(s=''):
    print(s)
    out.write(s + '\n')

try:
    from infrastructure.providers.docx_provider import DocxProvider
    from modules.parser.parser import CaliperParser

    CaliperParser.clear_cache()
    provider = DocxProvider()
    blocks = provider.extract('benchmarks/docling/unit_test_lists.docx')

    # 先打印 Block 结构
    p('=== Block 结构 ===')
    for b in blocks:
        tags = []
        if getattr(b, 'is_bold', False): tags.append('Bold')
        if getattr(b, 'font_size', None): tags.append(f'Size:{b.font_size}')
        if getattr(b, 'heading_style', None): tags.append(f'H:{b.heading_style}')
        tag_str = f' <{", ".join(tags)}>' if tags else ''
        title_mark = ' [TITLE]' if b.is_potential_title() else ''
        text = (b.text or '')[:60].replace('\n', ' ')
        p(f'  [{b.id:>3}] {b.type:<8}{tag_str:<25}{title_mark}  {text}')

    p()

    parser = CaliperParser()
    tree = parser.parse(blocks)

    p('=== Constellation 实际输出 ===')
    p(f'文档标题: {tree.doc_title}')
    p()

    def walk(nodes, indent=0):
        for n in nodes:
            p(f'{"  "*indent}[block={n.start_block_id}] Level {n.level}: {n.title}')
            if n.children:
                walk(n.children, indent+1)

    walk(tree.nodes)

    p()
    p('=== Ground Truth 期望 ===')
    gt = json.load(open('benchmarks/ground_truth/unit_test_lists.json', encoding='utf-8'))
    for h in gt['headings']:
        p(f'  [block={h["block_id"]}] Level {h["level"]}: {h["title"]}')

    p()
    p('=== 逐项对比 ===')
    pred_map = {}
    def collect(nodes):
        for n in nodes:
            pred_map[n.start_block_id] = n.level
            if n.children:
                collect(n.children)
    collect(tree.nodes)

    for h in gt['headings']:
        bid = h['block_id']
        gt_level = h['level']
        pred_level = pred_map.get(bid, 'MISS')
        match = 'OK' if gt_level == pred_level else 'MISMATCH'
        p(f'  {match} block={bid} GT=Level{gt_level} Pred=Level{pred_level}  "{h["title"]}"')

except Exception as e:
    p(f'ERROR: {e}')
    p(traceback.format_exc())

out.close()
