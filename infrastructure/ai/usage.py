"""Per-request LLM token accounting, implemented with contextvars.

Design constraints:

- ``LLMClient`` / ``AsyncLLMClient`` are pooled per configuration
  fingerprint and shared across requests, so usage cannot be attached to
  the client object without concurrent requests polluting each other.
- The parsing pipeline mixes ``asyncio.to_thread`` (synchronous path) and
  ``asyncio.gather`` (speculative execution).  Both inherit the context
  they were created in, so putting a **mutable accumulator object** into a
  ContextVar makes every child task and thread write to the same object:
  isolation and aggregation both fall out for free.
- When tracking is not enabled (the ContextVar is empty),
  ``record_llm_usage`` is a no-op, so library and evaluation callers see
  no behavioural change at all.

Usage::

    usage = begin_usage_tracking()
    ...  # run the pipeline; LLM calls at any depth are recorded
    stats["llm_usage"] = usage.snapshot()
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from typing import Any


class UsageAccumulator:
    """Thread-safe accumulator for a single request's LLM usage."""

    __slots__ = ("_lock", "calls", "prompt_tokens", "completion_tokens", "model")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.model = ""

    def add(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += max(0, int(prompt_tokens or 0))
            self.completion_tokens += max(0, int(completion_tokens or 0))
            if model:
                self.model = str(model)

    def snapshot(self) -> dict[str, Any]:
        """Return a usage snapshot suitable for direct JSON serialisation."""
        with self._lock:
            return {
                "model": self.model,
                "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            }


_current_usage: ContextVar[UsageAccumulator | None] = ContextVar(
    "constellation_llm_usage", default=None
)


def begin_usage_tracking() -> UsageAccumulator:
    """Enable usage tracking in the current context (once per request)."""
    accumulator = UsageAccumulator()
    _current_usage.set(accumulator)
    return accumulator


def record_llm_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Record one LLM call; a no-op when tracking was never started."""
    accumulator = _current_usage.get()
    if accumulator is None:
        return
    accumulator.add(model, prompt_tokens, completion_tokens)
