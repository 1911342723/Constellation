# 🔬 Constellation 横向对比实验报告

> **生成时间**: 2026-03-08 22:19:27
> **数据集**: IBM Docling 官方测试套件 (MIT License)
> **对比方案**: IBM Docling / MarkItDown / Mammoth / Constellation

---
## 1. 标题识别 F1 对比

| 文件名 | IBM Docling | MarkItDown | Mammoth | Constellation |
|:-------| ---:| ---:| ---:| ---:|
| `equations.docx` | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `lorem_ipsum.docx` | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `textbox.docx` | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| `unit_test_formatting.docx` | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `unit_test_headers.docx` | 0.9333 | 1.0000 | 0.2857 | 0.9333 |
| `unit_test_headers_numbered.docx` | 0.6667 | 1.0000 | 0.2857 | 0.9333 |
| `unit_test_lists.docx` | 0.9231 | 0.9231 | 0.9231 | 0.9231 |
| `word_sample.docx` | 0.0000 | 0.0000 | 0.0000 | 0.2857 |
| `word_tables.docx` | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

**平均 F1:**
- IBM Docling: **0.6137**
- MarkItDown: **0.6581**
- Mammoth: **0.4994**
- Constellation: **0.4528**

---
## 2. 层级准确率对比

| 文件名 | IBM Docling | MarkItDown | Mammoth | Constellation |
|:-------| ---:| ---:| ---:| ---:|
| `equations.docx` | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `lorem_ipsum.docx` | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `textbox.docx` | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| `unit_test_formatting.docx` | 1.0000 | 1.0000 | 1.0000 | 0.0000 |
| `unit_test_headers.docx` | 0.0000 | 1.0000 | 1.0000 | 1.0000 |
| `unit_test_headers_numbered.docx` | 0.0000 | 1.0000 | 1.0000 | 1.0000 |
| `unit_test_lists.docx` | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| `word_sample.docx` | 0.0000 | 0.0000 | 0.0000 | 1.0000 |
| `word_tables.docx` | 0.0000 | 0.0000 | 0.0000 | 0.0000 |

---
## 3. 特殊能力对比

| 能力 | IBM Docling | MarkItDown | Mammoth | Constellation |
|:-----| :---:| :---:| :---:| :---:|
| 数学公式 (equations.docx) | ✅ | ❌ | ❌ | ✅ |
| 浮动文本框 (textbox.docx) | ✅ | ✅ | ✅ | ✅ |
| 需要 LLM | ❌ | ❌ | ❌ | ✅ |

---
## 4. 输出字符数与处理速度

| 文件名 | IBM Docling 字符 | IBM Docling 耗时 | MarkItDown 字符 | MarkItDown 耗时 | Mammoth 字符 | Mammoth 耗时 | Constellation 字符 | Constellation 耗时 |
|:-------| ---:| ---:| ---:| ---:| ---:| ---:| ---:| ---:|
| `equations.docx` | 2,266 | 0.104s | 1,475 | 0.423s | 1,552 | 0.021s | 1,922 | 1.117s |
| `lorem_ipsum.docx` | 3,486 | 0.009s | 3,486 | 0.020s | 3,555 | 0.010s | 3,524 | 0.379s |
| `textbox.docx` | 1,376 | 0.036s | 1,977 | 0.078s | 2,004 | 0.033s | 3,615 | 1.168s |
| `unit_test_formatting.docx` | 502 | 0.150s | 500 | 0.037s | 501 | 0.017s | 427 | 0.392s |
| `unit_test_headers.docx` | 372 | 0.029s | 363 | 0.028s | 400 | 0.015s | 415 | 2.371s |
| `unit_test_headers_numbered.docx` | 400 | 0.031s | 363 | 0.027s | 400 | 0.015s | 415 | 2.181s |
| `unit_test_lists.docx` | 559 | 0.032s | 530 | 0.032s | 539 | 0.016s | 517 | 2.004s |
| `word_sample.docx` | 988 | 0.043s | 901 | 0.047s | 107,744 | 0.593s | 107,821 | 1.428s |
| `word_tables.docx` | 2,116 | 0.040s | 1,277 | 0.053s | 980 | 0.027s | 1,363 | 0.625s |

---
## 5. 综合评分

| 指标 | IBM Docling | MarkItDown | Mammoth | Constellation |
|:-----| ---:| ---:| ---:| ---:|
| 平均 F1 | **0.6137** | **0.6581** | **0.4994** | **0.4528** |
| 平均层级准确率 | **0.3333** | **0.5556** | **0.5556** | **0.5556** |
| 总耗时 | 0.47s | 0.74s | 0.75s | 11.67s |
