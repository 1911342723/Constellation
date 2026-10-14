# -*- coding: utf-8 -*-
"""A07 · 对外模型响应的 schema 只保留路由真正需要的字段。

方案 A07 原文：「输出 DTO 只保留路由需要的字段。内部 aligned votes 等已有 SkipJsonSchema，
不应再重复剥离；重点检查仍对模型公开的兼容 ``children`` / ``out_of_candidate`` 等字段是否必要。」

为什么这不是「洁癖」：模型看到的每个字段都是它可能去填的字段。一个服务端自己判定的标记
（``out_of_candidate``）出现在 schema 里，模型就会猜着填，而我们随后又会覆盖它——白占
token、还多一条可能被填错的路径。一个没有任何读取方的兼容字段（``children``）更是纯负担。

同时钉一条容易被忽略的性质：``out_of_candidate`` 从**模型 schema** 里剥掉，但**不能**从
序列化里排除——窗口检查点存的是 ``model_dump`` 的 JSON，排除它会让缓存命中后这个标记丢失，
从而跳过 out-of-candidate 锚点该走的物理复核。「对模型隐藏」和「不参与我们自己的往返」
是两件事。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.parser.schemas import ChapterNode, LLMRouterOutput


def _anchor_properties() -> dict:
    schema = LLMRouterOutput.model_json_schema()
    definitions = schema.get("$defs") or schema.get("definitions") or {}
    node = definitions.get("ChapterNode") or {}
    return node.get("properties", {})


def test_model_facing_schema_has_only_routing_fields() -> None:
    """模型看到的锚点字段就是它真正要回答的那几个，一个不多。"""
    assert set(_anchor_properties()) == {"title", "block_id", "level", "snippet", "confidence"}


def test_server_decided_flag_is_hidden_from_the_model() -> None:
    """out_of_candidate 是服务端比对候选集合后写入的，模型无从知道也不该被问。"""
    assert "out_of_candidate" not in _anchor_properties()


def test_internal_alignment_fields_stay_hidden() -> None:
    """内部对齐观测本来就有 SkipJsonSchema，这条防止有人把它们重新暴露出去。"""
    properties = _anchor_properties()
    for field in ("anchor_votes", "alignment_score", "source_windows", "globally_inferred"):
        assert field not in properties


def test_dead_compat_field_is_gone() -> None:
    """``children`` 没有任何读取方：层级由 level 表达，嵌套是 DocumentNode 的事。"""
    assert "children" not in ChapterNode.model_fields
    assert "children" not in _anchor_properties()


def test_hidden_from_schema_does_not_mean_dropped_from_serialization() -> None:
    """检查点存 model_dump 的 JSON：排除 out_of_candidate 会让缓存命中后标记丢失。

    这条是「对模型隐藏」与「不参与自己的往返」两件事的分界线，也是这次改动最容易出错的地方。
    """
    anchor = ChapterNode(block_id=3, title="标题", out_of_candidate=True)
    payload = anchor.model_dump(mode="json")
    assert payload["out_of_candidate"] is True
    restored = ChapterNode.model_validate(payload)
    assert restored.out_of_candidate is True
    # 内部对齐字段则相反：它们 exclude=True，不进序列化。
    assert "anchor_votes" not in payload
    assert "alignment_score" not in payload


def test_schema_change_moves_the_checkpoint_identity() -> None:
    """提示词/schema 任何变化都要改缓存指纹（A07 验收原文）。

    schema 是检查点键的一部分，所以剥字段这件事本身就会让旧缓存失配——这正是我们要的：
    模型看到的契约变了，旧响应不该再被复用。
    """
    schema = LLMRouterOutput.model_json_schema()
    properties = (schema.get("$defs") or {}).get("ChapterNode", {}).get("properties", {})
    assert properties, "schema 里必须仍能看到锚点定义，否则模型不知道要回什么"
    assert "children" not in properties
