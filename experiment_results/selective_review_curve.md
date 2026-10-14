# A01 选择性复审：结构质量 — 调用数 — Token — 耗时

> **口径声明**：路由器是按 GT 作答的 **oracle 桩**，不是真实模型。因此「结构质量」两列只能读作**上界**（若模型在被选中的区域答对能到多少）；真正可当结论的是**调用数**与**prompt 字符**两列——它们与模型质量无关，只由选区策略决定。没有真实模型语料就不宣称质量提升。

- 语料：`tests/data/mis_styled` 共 24 篇（排版扰动集）
- Token 用 prompt 字符数代替（1 token ≈ 2 个中文字符，估算口径与 evaluation 侧一致）

## 三模式汇总

| 模式 | 文档数 | 模型调用数 | prompt 字符 | 耗时(s) | F1(上界) | 级别准确率(上界) |
|:-----|------:|---------:|----------:|-------:|--------:|---------------:|
| fast | 24 | 0 | 0 | 0.221 | 1.0000 | 1.0000 |
| selective | 24 | 9 | 19147 | 0.509 | 1.0000 | 1.0000 |
| full | 24 | 24 | 53529 | 0.948 | 1.0000 | 1.0000 |

## 三个不确定度分开报（selective 模式）

标题存在概率、级别分差、树结构不确定性是三件不同的事。这里报后两者的文档级汇总；逐站点的存在概率与级别分差在 raw JSON 的 `signals` 里按信号计数。

| 文档 | 扰动 | 站点数 | 不确定站点 | 复审区域 | 延后区域 | 树结构不确定度 |
|:-----|:-----|------:|---------:|--------:|--------:|-------------:|
| cjk_paper__bold_everything | bold_everything | 22 | 0 | 0 | 0 | 0.0 |
| cjk_paper__clean | clean | 10 | 0 | 0 | 0 | 0.0 |
| cjk_paper__flat_style | flat_style | 10 | 4 | 1 | 0 | 0.0 |
| cjk_paper__font_inverted | font_inverted | 10 | 10 | 1 | 0 | 0.0 |
| cjk_paper__numbering_shift | numbering_shift | 10 | 0 | 0 | 0 | 0.0 |
| cjk_paper__over_styled | over_styled | 16 | 0 | 0 | 0 | 0.0 |
| cjk_paper__style_shift | style_shift | 10 | 10 | 1 | 0 | 0.0 |
| cjk_paper__styled_labels | styled_labels | 20 | 0 | 0 | 0 | 0.0 |
| deep__bold_everything | bold_everything | 30 | 0 | 0 | 0 | 0.0 |
| deep__clean | clean | 15 | 0 | 0 | 0 | 0.0 |
| deep__flat_style | flat_style | 15 | 9 | 1 | 0 | 0.0 |
| deep__font_inverted | font_inverted | 15 | 15 | 1 | 0 | 0.0 |
| deep__numbering_shift | numbering_shift | 15 | 0 | 0 | 0 | 0.0 |
| deep__over_styled | over_styled | 22 | 0 | 0 | 0 | 0.0 |
| deep__style_shift | style_shift | 15 | 15 | 1 | 0 | 0.0 |
| deep__styled_labels | styled_labels | 30 | 0 | 0 | 0 | 0.0 |
| report__bold_everything | bold_everything | 21 | 0 | 0 | 0 | 0.0 |
| report__clean | clean | 9 | 0 | 0 | 0 | 0.0 |
| report__flat_style | flat_style | 9 | 3 | 1 | 0 | 0.0 |
| report__font_inverted | font_inverted | 9 | 9 | 1 | 0 | 0.0 |
| report__numbering_shift | numbering_shift | 9 | 0 | 0 | 0 | 0.0 |
| report__over_styled | over_styled | 15 | 0 | 0 | 0 | 0.0 |
| report__style_shift | style_shift | 9 | 9 | 1 | 0 | 0.0 |
| report__styled_labels | styled_labels | 18 | 0 | 0 | 0 | 0.0 |
