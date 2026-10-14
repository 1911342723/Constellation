"""LLM token 用量按请求累计的确定性单测（零网络）。"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from infrastructure.ai.llm_client import _record_completion_usage
from infrastructure.ai.usage import (
    UsageAccumulator,
    begin_usage_tracking,
    record_llm_usage,
)


def test_record_without_tracking_is_noop():
    """未开启跟踪时 record 是 no-op（库模式 / 评测脚本零行为变化）。"""
    record_llm_usage("m", 100, 50)  # 不应抛异常


def test_accumulator_sums_multiple_calls():
    usage = begin_usage_tracking()
    record_llm_usage("deepseek-chat", 100, 20)
    record_llm_usage("deepseek-chat", 200, 30)
    snap = usage.snapshot()
    assert snap == {
        "model": "deepseek-chat",
        "calls": 2,
        "prompt_tokens": 300,
        "completion_tokens": 50,
        "total_tokens": 350,
    }


def test_completion_usage_prefers_gateway_numbers():
    usage = begin_usage_tracking()
    completion = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=123, completion_tokens=45))
    _record_completion_usage("m1", completion, estimated_input=999, raw_content="ignored")
    snap = usage.snapshot()
    assert snap["prompt_tokens"] == 123
    assert snap["completion_tokens"] == 45
    assert snap["model"] == "m1"


def test_completion_usage_falls_back_to_estimates():
    """网关不返回 usage 时退化为本地估算（输入用请求前估算、输出按文本估算）。"""
    usage = begin_usage_tracking()
    completion = SimpleNamespace(usage=None)
    _record_completion_usage("m2", completion, estimated_input=777, raw_content="hello world output")
    snap = usage.snapshot()
    assert snap["prompt_tokens"] == 777
    assert snap["completion_tokens"] > 0


def test_usage_propagates_through_to_thread_and_tasks():
    """to_thread（同步管线）与并发任务（投机执行）都记进同一请求累加器。"""

    async def main() -> dict:
        usage = begin_usage_tracking()

        def sync_work():
            record_llm_usage("m", 10, 1)

        async def task_work():
            record_llm_usage("m", 20, 2)

        await asyncio.to_thread(sync_work)
        await asyncio.gather(task_work(), task_work())
        return usage.snapshot()

    snap = asyncio.run(main())
    assert snap["calls"] == 3
    assert snap["prompt_tokens"] == 50
    assert snap["completion_tokens"] == 5


def test_concurrent_requests_are_isolated():
    """两个并发请求 context 各自累计，互不污染。"""

    async def one_request(tokens: int) -> dict:
        usage = begin_usage_tracking()
        record_llm_usage("m", tokens, tokens)
        await asyncio.sleep(0)
        return usage.snapshot()

    async def main():
        return await asyncio.gather(one_request(10), one_request(99))

    first, second = asyncio.run(main())
    assert first["prompt_tokens"] == 10
    assert second["prompt_tokens"] == 99


def test_snapshot_of_fresh_accumulator_is_zero():
    usage = UsageAccumulator()
    assert usage.snapshot() == {
        "model": "",
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
