# IBM Docling 公开基准数据集

## 数据来源

所有 DOCX 测试文件来自 IBM Docling 官方开源仓库:

- **仓库**: [docling-project/docling](https://github.com/docling-project/docling)
- **路径**: `tests/data/docx/`
- **许可**: MIT License

## 文件清单

| 文件名 | 大小 | 测试场景 | 日期 |
| :--- | ---: | :--- | :--- |
| `word_sample.docx` | 102 KB | 通用文档（标题+段落+图片+表格） | 2026-03-08 |
| `unit_test_headers.docx` | 14 KB | 多级标题识别（4 层级） | 2026-03-08 |
| `unit_test_headers_numbered.docx` | 17 KB | 带编号的多级标题 | 2026-03-08 |
| `unit_test_lists.docx` | 15 KB | 多级列表还原（6 组测试） | 2026-03-08 |
| `unit_test_formatting.docx` | 28 KB | 丰富格式排版（粗体/斜体/下划线） | 2026-03-08 |
| `word_tables.docx` | 16 KB | 表格场景（跨行/跨列合并） | 2026-03-08 |
| `equations.docx` | 15 KB | 数学公式 (OMML) | 2026-03-08 |
| `textbox.docx` | 48 KB | 浮动文本框 (txbxContent) | 2026-03-08 |
| `lorem_ipsum.docx` | 15 KB | 纯文本无标题（降级模式测试） | 2026-03-08 |

## Ground Truth 标注

`ground_truth/` 目录下的 JSON 文件基于 DocxProvider 的 Block 提取结果人工标注，
标注格式与 `evaluation/ground_truth/_schema.json` 完全一致。

## 运行测试

```bash
# Stage 1 离线测试（不需要 LLM API）
python benchmarks/run_benchmark.py --stage1-only

# 全流水线测试（需要 LLM API Key）
python benchmarks/run_benchmark.py
```
