# Constellation (游标卡尺)

基于控制-数据流解耦的零损耗文档结构化提取引擎。

[![Paper](https://img.shields.io/badge/Paper-Zenodo-blue)](https://zenodo.org/records/18917045)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)

[English](README_EN.md) | [📄 论文 (Zenodo)](https://zenodo.org/records/18917045)

---

## 概述

Constellation 是一个文档结构化解析工具，通过四阶段流水线将非结构化的 Word 文档转换为精确的章节树。核心思路是将 LLM 的角色限定为"打游标"——只负责在压缩骨架上标注章节边界，而将字符级无损重组交给确定性算法完成。

这一设计源于一个关键观察：LLM 擅长模糊语义提取，但极度不擅长字符级无损重组。Constellation 将两者的职责彻底分离。

## 核心原理

四阶段流水线：

```
Stage 1: 物理降维        .docx --> List[Block]
Stage 2: 虚拟骨架压缩    List[Block] --> 极简骨架文本 (压缩率 90-95%)
Stage 3: AI 游标漫游      骨架文本 --> 章节锚点 [{block_id, title, level, snippet}]
Stage 4: 游标闭合与组装   锚点 + 原始 Blocks --> 文档树 --> Markdown
```

### Stage 1 — 物理降维

混合 XML 引擎（python-docx 主引擎 + lxml 补充引擎）将 .docx 文件转换为标准化的 Block 序列。每个 Block 携带物理特征元数据（加粗、字号、对齐方式、标题样式）。支持段落、表格、图片、OMML 公式、浮动文本框。

### Stage 2 — 虚拟骨架压缩

I帧/P帧分类：结构关键块（标题、多媒体、格式化块）作为 I 帧全量保留并注入 Meta-Tag；正文冗余块作为 P 帧进行头尾截断。连续 P 帧通过游程编码折叠，但每个被折叠的段落保留首行摘要（v2 降级可见机制），确保隐藏标题不被吞噬。

### Stage 3 — AI 游标漫游

LLM 在骨架文本上标注章节边界，输出结构化 JSON。每个锚点包含 `snippet` 字段（标题原文前 30 字），用于 Stage 4 的交叉验证。提示词模板外置于 `modules/parser/prompts/`，支持热加载。

### Stage 4 — 游标闭合与组装

三阶段后处理：
1. 模糊锚定纠偏 — 用 Levenshtein 距离交叉验证 `block_id` 与 `snippet`，在滑轨区间内自动纠正偏差
2. 层级合规性修复 — 检测并修复层级跳跃，支持字号物理特征辅助推断
3. 强制闭合切割 — 计算无重叠、无遗漏的区间，栈式算法构建文档树

## 快速开始

### 环境要求

- Python 3.10+
- 一个兼容 OpenAI 协议的 LLM API Key（DeepSeek / OpenAI / Claude 等）

### 安装

```bash
pip install -r requirements.txt
```

### 配置

复制 `.env` 文件并填入 API 密钥：

```env
LLM_API_KEY=your_api_key_here
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

### 作为 Python 库使用

```python
from infrastructure.providers.docx_provider import DocxProvider
from modules.parser.parser import CaliperParser

# Stage 1: 提取 Block
provider = DocxProvider()
blocks = provider.parse("document.docx")

# Stage 2-4: 解析
parser = CaliperParser()
tree = parser.parse(blocks)

# 输出
print(tree.to_markdown())
print(tree.to_json(indent=2))
```

支持自定义配置（无需依赖全局 settings）：

```python
from modules.parser.config import CompressorConfig, ResolverConfig

parser = CaliperParser(
    compressor_config=CompressorConfig(head_chars=60, rle_threshold=5),
    resolver_config=ResolverConfig(fuzzy_anchor_radius=8),
)
```

### 作为 API 服务使用

```bash
uvicorn app.main:app --host 0.0.0.0 --port 28001
```

API 文档：`http://localhost:28001/docs`

主要端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/parse/docx` | 上传 DOCX，返回 Block 列表（仅 Stage 1） |
| POST | `/api/v1/parse/full` | 上传 DOCX，全流程解析，返回完整结果 |
| POST | `/api/v1/parse` | 传入 Block 列表，执行 Stage 2-4 |
| POST | `/api/v1/parse/paper` | 排版系统专用格式输出 |

## 项目结构

```
Constellation/
├── app/                          # 交付层
│   ├── main.py                   # FastAPI 入口 + 全局异常处理
│   ├── api/
│   │   ├── routes.py             # API 路由
│   │   └── schemas.py            # 请求/响应模型
│   └── core/
│       ├── config/settings.py    # 应用配置 (pydantic-settings)
│       └── exceptions.py         # 异常层级
├── modules/                      # 业务层
│   └── parser/
│       ├── parser.py             # 主解析器 (CaliperParser)
│       ├── compressor.py         # Stage 2: 骨架压缩器
│       ├── router.py             # Stage 3: LLM 路由器
│       ├── resolver.py           # Stage 4: 区间解析器
│       ├── document_tree.py      # 文档树数据结构
│       ├── schemas.py            # 内部数据模型
│       ├── config.py             # 解耦配置 (CompressorConfig 等)
│       └── prompts/              # LLM 提示词模板
│           ├── router_system.txt
│           └── router_user.txt
├── infrastructure/               # 基础设施层
│   ├── ai/
│   │   └── llm_client.py        # LLM 客户端 (单例 + 连接池复用)
│   ├── models/
│   │   └── block.py             # Block 原子数据模型
│   └── providers/
│       └── docx_provider.py     # Stage 1: DOCX 混合 XML 引擎
├── tests/                        # 测试
├── examples/                     # 示例代码
├── docs/                         # 技术文档
└── requirements.txt
```

## 关键设计决策

| 决策 | 理由 |
|------|------|
| I帧/P帧分类 | 结构关键块全量保留，正文冗余块截断压缩，平衡信息完整性与 Token 消耗 |
| P帧降级可见 | 被折叠的段落保留首行摘要，消除"奇葩标题被吞噬"的盲区 |
| snippet 交叉验证 | LLM 返回标题原文片段，用 Levenshtein 距离自动纠正 block_id 偏差 |
| 短 snippet 精确匹配 | 5 字符以下的 snippet 禁用模糊匹配，避免 Levenshtein 在短文本上的噪声 |
| 字号辅助层级修复 | 层级跳跃时对比物理字号与同级祖先，避免盲目夹紧 |
| 配置解耦 | 核心模块通过 Config 对象注入参数，不依赖全局 settings，可作为独立库使用 |
| LLM 客户端单例 | 共享 httpx 连接池，避免高并发下的端口耗尽 |
| 全局异常处理 | 路由层无 try/except 样板，异常由 FastAPI 全局拦截器统一映射为 HTTP 状态码 |
| 提示词外置 | LLM 提示词存储为模板文件，支持热加载，无需改代码即可调优 |

## 配置参考

所有配置项均可通过 `.env` 文件或环境变量设置：

```env
# LLM
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=4096

# 骨架压缩
SKELETON_HEAD_CHARS=40
SKELETON_TAIL_CHARS=30
SKELETON_ENABLE_RLE=true
SKELETON_RLE_THRESHOLD=3
SKELETON_MAX_RLE_GROUP=10

# 滑动窗口 (超长文档)
SLIDING_WINDOW_THRESHOLD=500
WINDOW_SIZE=300
WINDOW_OVERLAP=50

# 模糊锚定
FUZZY_ANCHOR_RADIUS=5
FUZZY_MIN_SIMILARITY=0.4
```

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- Pydantic v2 + pydantic-settings
- OpenAI SDK (兼容 DeepSeek/Claude 等 OpenAI 协议提供商)
- python-docx + lxml (混合 XML 引擎)
- python-Levenshtein (模糊锚定纠偏)

## 论文

如果 Constellation 对您的研究有帮助，请引用我们的论文：

> **Constellation: Lossless Document Structuring via Control-Data Flow Decoupling**
>
> 📄 [Zenodo Preprint](https://zenodo.org/records/18917045)

```bibtex
@article{constellation2025,
  title={Constellation: Lossless Document Structuring via Control-Data Flow Decoupling},
  year={2025},
  doi={10.5281/zenodo.18917045},
  publisher={Zenodo}
}
```

## 版本

当前版本：1.0.0

## 许可证

MIT License
