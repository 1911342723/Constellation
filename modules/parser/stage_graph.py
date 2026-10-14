# -*- coding: utf-8 -*-
"""L03 · 阶段依赖图——**断点续跑正确性的前提**，不是可选的附属品。

方案 10.4 的原话：「若只重试一个网络失败的窗口，可复用已经完成的窗口；若变更 Provider
合行规则或全文排版画像，则依赖该结果的候选/窗口可能全部失效，不能盲目按页复用。
**明确依赖图是断点续跑正确性的前提。**」

上一轮**刻意没做** L03，理由就是这句话：没有依赖图而硬做，会得到一个「看着能续跑、
实际复用失效结果」的版本，比没有续跑更危险。本模块把那张图做出来。

## 为什么不能用「一个全局哈希」糊过去

把所有配置塞进一个哈希当键，安全（任何改动都失配）但**没用**：改一个提示词字也会让
上一次的原生提取、排版画像、候选表全部作废，续跑就退化成重跑。反过来，只按「页」或
按「窗口内容」复用则不安全——窗口内容相同但**合法化规则**变了时，检查点里存的是
**合法化之后**的结果，照旧复用就会拿到按旧规则产生的锚点。

所以要的是一张真正的图：每个阶段的指纹 = 自身版本 + 自身输入 + **它依赖的那些阶段的
指纹**。改动只沿着依赖边传播，不波及无关阶段。本模块的用例专门钉这一条：
改路由提示词**不得**改变 extract / profile / candidates 的指纹。

## 图的形状

```
extract ──┬─> profile ──┬─> candidates ──┬─> compress ──> route_raw
          │             │                │
          │             │                └─> legalize ←── route_raw
          └─────────────┴─────────────────────────────> resolve ──> assemble
```

- ``extract``：物理提取。改 Provider 的合行 / 合表 / 单元格规则动这里，下游全塌。
- ``profile``：全文排版画像（字号基准、样式类、区域）。它吃 extract 的结果。
- ``candidates``：确定性标题候选。吃 extract + profile。
- ``compress``：候选感知的骨架压缩与窗口切分。吃 candidates。
- ``route_raw``：一次模型调用的**原始响应**身份。吃 compress（窗口文本）+ 自身的
  模型 / 提示词 / schema。验证规则变了时可以复用这份原文，不必重调模型。
- ``legalize``：锚点合法化（层级修复、逆向审计、短标题保护）。吃 candidates
  **和** ``route_raw``——候选表是约束，原始响应是被验证的对象。窗口检查点若只存
  验证后的锚点，缓存键必须包含本阶段指纹，不能只用 route 指纹。
- ``resolve``：区间闭合装配。吃 legalize + extract（回读正文）。
- ``assemble``：三视图渲染。吃 resolve。

## 版本号怎么维护

``STAGE_VERSIONS`` 里的字符串是**人工维护**的算法版本。改了某阶段的行为就要 +1——这是
一条纪律，不是自动推导（自动推导只能哈希源码，那样一次无关的注释改动就会作废全部缓存）。
``tests/test_stage_graph.py`` 会钉住图的形状与传播关系，防止有人加了阶段却漏接依赖边。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "STAGES",
    "STAGE_VERSIONS",
    "Stage",
    "dependents_of",
    "stage_fingerprints",
    "checkpoint_namespace",
    "execution_identity",
]


@dataclass(frozen=True)
class Stage:
    """一个阶段：名字、算法版本、它直接依赖哪些阶段。"""

    name: str
    depends_on: tuple[str, ...] = ()


#: 各阶段的**人工维护**算法版本。改了该阶段的行为就要动这里。
STAGE_VERSIONS: dict[str, str] = {
    "extract": "extract.v4",       # v4：OCR 文本入流 + PDF 内容关系
    "profile": "profile.v3",       # v3：紧凑块仍按真实字符数加权
    "candidates": "candidates.v3",  # v3：保留目录、层级、区域等紧凑证据
    "compress": "compress.v3",     # v3：修正 161–240 字符块截断并移出完整载荷
    "route_raw": "route.v2",       # v2：与 legalize 拆开，原始响应可单独缓存
    "legalize": "legalize.v5",     # v5：真实窗口来源、相关投票降权
    "resolve": "resolve.v3",       # v3：祖先关系软约束 + 置信度契约
    "assemble": "assemble.v2",     # v2：T01 IR 成为唯一渲染来源 + B10 行内语法
}

STAGES: dict[str, Stage] = {
    "extract": Stage("extract"),
    "profile": Stage("profile", ("extract",)),
    "candidates": Stage("candidates", ("extract", "profile")),
    "compress": Stage("compress", ("candidates",)),
    "route_raw": Stage("route_raw", ("compress",)),
    "legalize": Stage("legalize", ("candidates", "route_raw")),
    "resolve": Stage("resolve", ("legalize", "extract")),
    "assemble": Stage("assemble", ("resolve",)),
}


def _validate_graph() -> None:
    """启动即校验图是良构的：依赖存在、无环。

    图是手写的，写错一条边的症状是「某阶段改了却没让下游失效」——那正是最危险的
    静默复用。所以宁可导入时就炸。
    """
    for stage in STAGES.values():
        for dependency in stage.depends_on:
            if dependency not in STAGES:
                raise ValueError(f"阶段 {stage.name} 依赖了不存在的阶段 {dependency}")
        if stage.name not in STAGE_VERSIONS:
            raise ValueError(f"阶段 {stage.name} 没有登记版本号")
    # 拓扑排序：走不完说明有环。
    resolved: set[str] = set()
    pending = dict(STAGES)
    while pending:
        ready = [
            name for name, stage in pending.items()
            if all(dep in resolved for dep in stage.depends_on)
        ]
        if not ready:
            raise ValueError(f"阶段依赖图有环，剩余：{sorted(pending)}")
        for name in ready:
            resolved.add(name)
            pending.pop(name)


_validate_graph()


def dependents_of(stage: str) -> set[str]:
    """改了 ``stage`` 会让哪些阶段失效（含它自己，传递闭包）。

    这是依赖图对外**可查询**的形态：「我改了 Provider 的合行规则，哪些缓存要作废」
    有一个可执行的答案，而不是靠人记。
    """
    if stage not in STAGES:
        raise KeyError(stage)
    affected = {stage}
    changed = True
    while changed:
        changed = False
        for name, node in STAGES.items():
            if name in affected:
                continue
            if any(dependency in affected for dependency in node.depends_on):
                affected.add(name)
                changed = True
    return affected


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def stage_fingerprints(inputs: Mapping[str, Any] | None = None) -> dict[str, str]:
    """算出每个阶段的指纹。

    某阶段指纹 = ``sha256(名字 + 版本 + 自身输入 + 各依赖阶段的指纹)``。因此改动只沿依赖边
    传播：改路由提示词只动 ``route_raw`` 及其依赖它的合法化 / 闭合，改 Provider 合行规则则动 extract 及其全部下游。

    Args:
        inputs: ``{阶段名: 该阶段自身的输入}``。缺的阶段按空输入算。典型内容：
            ``extract`` 放源文件哈希 + 格式 + provider 配置；``compress`` 放压缩配置；
            ``route_raw`` 放模型配置 + 提示词版本 + 响应 schema；``legalize`` 放验证
            配置与算法版本；``resolve`` 放 resolver 配置。

    Returns:
        ``{阶段名: 十六进制指纹}``。
    """
    payloads = dict(inputs or {})
    fingerprints: dict[str, str] = {}
    pending = dict(STAGES)
    while pending:
        ready = [
            name for name, stage in pending.items()
            if all(dependency in fingerprints for dependency in stage.depends_on)
        ]
        for name in sorted(ready):
            stage = pending.pop(name)
            fingerprints[name] = _digest({
                "stage": name,
                "version": STAGE_VERSIONS[name],
                "inputs": payloads.get(name, {}),
                # 依赖指纹按名字排序并入：顺序稳定，且「依赖变了我就变」是结构性的。
                "depends": {
                    dependency: fingerprints[dependency] for dependency in sorted(stage.depends_on)
                },
            })
    return fingerprints


def checkpoint_namespace(owner: str, fingerprints: Mapping[str, str], stage: str) -> str:
    """某阶段检查点的存储命名空间：owner 隔离 + 该阶段指纹。

    把指纹放进**命名空间**而不是只放进单条键：版本一升，整个分区自然不再被命中，
    旧分区随 TTL 过期回收，不需要写迁移脚本去逐条失效。
    """
    return json.dumps([owner, stage, fingerprints[stage]], ensure_ascii=False, sort_keys=True)


def execution_identity(fingerprints: Mapping[str, str] | None = None) -> str:
    """算法执行身份：客户端幂等键表示同一次请求，本值表示「用哪一版算法执行」。

    普通重提若只按文件指纹命中旧任务，会绕过新的 extract / legalize / assemble。
    把各阶段指纹收成一个可见身份，提交层把它并进任务键；显式 ``retry`` 仍可强制新跑。
    """
    payload = fingerprints if fingerprints is not None else stage_fingerprints()
    return _digest({"execution": dict(payload), "versions": dict(STAGE_VERSIONS)})
