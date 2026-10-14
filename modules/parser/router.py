"""LLM Router — Constellation Stage 3: Cursor Pointer Routing.

Routes the compressed virtual skeleton through an LLM to obtain
section boundary anchors.  The LLM acts exclusively as a *pointer
annotator*: it outputs ``(block_id, level, title, snippet)`` tuples
and is strictly forbidden from generating any body content.

Prompt templates are loaded from ``modules/parser/prompts/`` at
initialisation time so they can be edited without touching code.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from infrastructure.ai.llm_client import (
    LLMClient,
    estimate_messages_tokens,
    estimate_text_tokens,
    get_async_llm_client,
    get_llm_client,
)
from modules.parser.prompts import load_prompt
from infrastructure.ai.checkpoints import current_checkpoints, current_raw_checkpoints
from modules.parser.heading_candidates import format_candidate_table
from modules.parser.config import LLMClientConfig
from modules.parser.schemas import ChapterNode, HeadingCandidate, LLMRouterOutput
from app.core.exceptions import LLMRouterError

# ── Validation & retry constants ─────────────────────────────
_VALID_LEVEL_MIN = 1
_VALID_LEVEL_MAX = 6
_MAX_ANCHOR_RETRIES = 2
_RETRY_BACKOFF_BASE = 1.0  # seconds
# Confidence assigned to out-of-candidate anchors entering the
# low-confidence channel (widens the resolver's fuzzy search radius).
_DOWNGRADE_CONFIDENCE = 0.4

# 提示词协议版本。任何提示词文本/输出契约的改动都必须递增它。
# 它随缓存/检查点指纹一起参与哈希（见 ``_async_call_with_validation_retry``），
# 且提示词文件本身已整段进入文档级缓存指纹（parser._llm_cache_identity 读取
# ``_system_prompt`` / ``_user_template`` 全文），因此改提示词必然使旧缓存失效。
# 这里额外把版本号显式并入检查点键，是为了让「指纹已含提示词」这一事实在代码里
# 可追溯，而不是隐式依赖模板全文比较。
ROUTER_PROMPT_VERSION = "router.v2"

# ── 已知模型的 tokenizer / 上下文能力档案（P04 第 7 点）─────────
# 键为模型名的**小写子串**；命中即认为该模型与对应 tokenizer 家族一致，可以
# 用更贴近真实的估算。未命中的网关一律走保守估算——不假设「OpenAI 兼容协议」
# 就等于同一 tokenizer（DeepSeek/Qwen/Claude 各有自己的分词）。档案只用于**记录
# 与离线调优**，不放宽任何硬校验：真实请求仍逐条精确/保守校验。
_MODEL_TOKENIZER_PROFILES: Dict[str, Dict[str, object]] = {
    # OpenAI cl100k / o200k 家族：本地 cl100k 估算与其最接近。
    "gpt-4": {"encoding": "cl100k_base", "context_tokens": 128_000},
    "gpt-3.5": {"encoding": "cl100k_base", "context_tokens": 16_385},
    "gpt-4o": {"encoding": "o200k_base", "context_tokens": 128_000},
    # DeepSeek：非 cl100k，本地估算按保守上界处理（继续取 heuristic/cl100k 大值）。
    "deepseek": {"encoding": None, "context_tokens": 65_536},
    # Qwen / GLM(Zhipu)：同样各自分词，保守估算。
    "qwen": {"encoding": None, "context_tokens": 32_768},
    "glm": {"encoding": None, "context_tokens": 131_072},
}


def resolve_model_profile(model: str) -> Optional[Dict[str, object]]:
    """按模型名子串匹配一个已知 tokenizer/上下文档案；未知网关返回 None。

    返回 None 表示「该走保守估算」——调用方不得据此放宽任何硬校验。
    """
    name = (model or "").lower()
    for needle, profile in _MODEL_TOKENIZER_PROFILES.items():
        if needle in name:
            return profile
    return None


# Matches both ordinary ``[42]`` lines and folded ``[42 to 51]`` ranges.
_SKELETON_ID_RE = re.compile(
    r"(?m)^[ \t]*\[(\d+)(?:\s+to\s+(\d+))?\]"
)
_BUDGET_RANGE_RE = re.compile(
    r"<!--\s*Constellation budget range:\s*(\d+)\.\.(\d+)\s*-->"
)

logger = logging.getLogger(__name__)


# ── 窗口的显式控制范围（P04 第 5 点）─────────────────────────
@dataclass(frozen=True)
class RouterWindow:
    """一个待路由分片的显式控制范围。

    不再依赖分片文本里的 ``[id]`` 数字标签反推控制范围：切点规划阶段就把每个
    分片的**可决策范围** ``core_range`` 与**上下文范围** ``context_range`` 显式
    记下来。二者都是**闭区间的块 ID** ``(start_id, end_id)``；无任何块标记的
    续片用 ``(-1, -1)`` 表示空范围（与 ``CaliperParser._chunk_id_range`` 对齐）。

    - ``core_range``：本分片对模型「负责下判断」的块 ID 区间。窗口重叠时，只有
      落在 core_range 内的锚点算这一窗口的主判，用于去重与归属。
    - ``context_range``：本分片实际携带的全部块 ID 区间（含为保持上下文而带进来
      的重叠块）。恒有 ``context_range`` ⊇ ``core_range``。

    当前主路径按分片文本切分、窗口之间不做块级重叠时，两者相等；字段分开保存是为
    了让「控制范围」成为一等数据，未来引入重叠窗口时无需再回到文本里反推。
    """

    text: str
    core_range: tuple[int, int]
    context_range: tuple[int, int]


@dataclass
class RouterBudgetStats:
    """预算规划与路由的可观测统计（验收口径，非「字符压缩率」）。

    这些计数按 ``LLMRouter`` 实例累加，供离线调优与验收断言读取：
    - ``full_request_estimates``：**完整请求** Token 估算的调用次数。前缀和找切点
      的目标就是把它压到远低于「逐行线性重估」。开销计数用例断言它随行数亚线性增长。
    - ``prefix_estimates``：按原子单元做的**轻量**成本估算次数（前缀和构建时）。
    - ``shards_planned`` / ``fixed_prompt_tokens`` / ``utilization_ratios``：分片数、
      固定提示词 Token 占比、每片利用率（估算 Token / 安全预算）。
    - ``finish_reason_*`` / ``output_truncations`` / ``format_errors`` /
      ``empty_titles`` / ``refusals`` / ``retries``：输出侧分支计数（P04 第 6 点）。
    - ``usage_deviations``：模型返回 usage 后，(估算-实际) 的偏差样本，离线调优依据。
    """

    full_request_estimates: int = 0
    prefix_estimates: int = 0
    shards_planned: int = 0
    fixed_prompt_tokens: int = 0
    utilization_ratios: List[float] = field(default_factory=list)
    output_truncations: int = 0
    format_errors: int = 0
    empty_titles: int = 0
    refusals: int = 0
    retries: int = 0
    usage_deviations: List[int] = field(default_factory=list)

    def snapshot(self) -> Dict[str, object]:
        """返回一个只读快照，便于日志与验收读取。"""
        return {
            "full_request_estimates": self.full_request_estimates,
            "prefix_estimates": self.prefix_estimates,
            "shards_planned": self.shards_planned,
            "fixed_prompt_tokens": self.fixed_prompt_tokens,
            "utilization_ratios": list(self.utilization_ratios),
            "fixed_prompt_ratio": (
                self.fixed_prompt_tokens
                / max(1, int(sum(self.utilization_ratios) or 0) or 1)
            ),
            "output_truncations": self.output_truncations,
            "format_errors": self.format_errors,
            "empty_titles": self.empty_titles,
            "refusals": self.refusals,
            "retries": self.retries,
            "usage_deviations": list(self.usage_deviations),
        }


# ── Prompt fragment builders ─────────────────────────────────

def _build_window_hint(
    chunk_index: int,
    total_chunks: int,
    previous_tail_context: str,
) -> str:
    """Build the window-aware prompt fragment for multi-chunk routing.

    Returns a string that is appended to the user prompt to inform
    the LLM about window position, metadata expectations, and
    inherited hierarchy state from the previous window.
    """
    is_first = chunk_index == 0
    hint = f"\n\n[Window] This is chunk {chunk_index + 1}/{total_chunks}."

    if is_first:
        hint += "\nExtract doc_title and doc_authors normally."
    else:
        hint += (
            "\nThis is NOT the beginning of the document. "
            "Set doc_title and doc_authors to empty strings. "
            "Focus solely on heading detection."
        )

    if previous_tail_context:
        # Hierarchy state is advisory.  Cap it so an anomalously long LLM
        # title from the previous window cannot make the next request exceed
        # the gateway input limit after skeleton budgeting.
        inherited = previous_tail_context.strip()
        if len(inherited) > 240:
            inherited = inherited[:237] + "..."
        hint += (
            f"\n[Inherited State] Immediately preceding this window, "
            f"the heading hierarchy was:\n{inherited}\n"
            f"Continue the same level structure (Level 1-6) to "
            f"prevent level discontinuities."
        )

    return hint


def _build_candidate_prompt(
    base_prompt: str,
    candidates: Optional[List[HeadingCandidate]],
) -> tuple[str, Optional[Set[int]]]:
    """Append candidate-routing instructions and return allowed block IDs."""
    if candidates is None:
        return base_prompt, None

    allowed_ids = {candidate.block_id for candidate in candidates}
    candidate_table = format_candidate_table(candidates)
    candidate_prompt = (
        f"{base_prompt}\n\n"
        "[Heading Candidates]\n"
        "The deterministic Stage 2.5 candidate generator produced the "
        "following candidate headings. Prefer choosing headings from "
        "these block IDs. If a candidate is a caption, list item, or "
        "body text, skip it. If you find a REAL section heading in the "
        "skeleton (including inside folded regions) whose block ID is "
        "NOT in this list, you may still output it — such anchors are "
        "strictly re-validated against physical features downstream, "
        "so only do so when the line is clearly a heading. "
        "Use style=L and number=L hints before font-size hints when "
        "assigning level.\n"
        f"{candidate_table}\n"
    )
    return candidate_prompt, allowed_ids


class LLMRouter:
    """Annotate section boundaries on a virtual skeleton via LLM.

    The router sends the skeleton text together with a structured
    system prompt to the configured LLM and parses the response into
    a flat list of :class:`ChapterNode` anchors.
    """

    def __init__(
        self,
        downgrade_out_of_candidate: bool = True,
        llm_config: LLMClientConfig | None = None,
    ) -> None:
        # ``llm_config`` carries BYOK overrides (user-supplied key/model/base_url);
        # None falls back to the default pooled client from settings.
        self._llm_config = llm_config
        self._client = get_llm_client(llm_config)
        self._system_prompt = load_prompt("router_system")
        self._user_template = load_prompt("router_user")
        self._downgrade_out_of_candidate = downgrade_out_of_candidate
        # P04 第 1 点：不可变的 system/user/schema 固定段成本按 (router, 预算) 缓存，
        # 一次解析里对同一 router 只算一次，不随每个分片重复求。schema 序列化本身
        # 已由 llm_client._schema_instruction_for 的 lru_cache 承担。
        self._fixed_cost_cache: Dict[tuple, int] = {}
        # 可观测统计（验收口径）。跨线程共享，用锁保护自增。
        self.budget_stats = RouterBudgetStats()
        self._stats_lock = threading.Lock()
        # 已知模型档案（None = 未知网关，走保守估算）。仅用于记录/离线调优。
        self._model_profile = resolve_model_profile(
            getattr(self._client, "model", ""),
        )

    # ── Input-token budgeting ─────────────────────────────────

    @property
    def input_token_budget(self) -> int:
        """Safe estimated-token budget used before reaching the hard limit."""
        limit = int(getattr(self._client, "max_input_tokens", 8192))
        margin = int(getattr(self._client, "input_token_safety_margin", 512))
        return max(1, limit - margin)

    @staticmethod
    def _candidates_for_skeleton(
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]],
    ) -> Optional[List[HeadingCandidate]]:
        """Return only candidates whose IDs are represented by a shard."""
        if candidates is None:
            return None
        budget_ranges = [
            (int(match.group(1)), int(match.group(2)))
            for match in _BUDGET_RANGE_RE.finditer(skeleton_text)
        ]
        ranges = budget_ranges
        if not ranges:
            for match in _SKELETON_ID_RE.finditer(skeleton_text):
                start = int(match.group(1))
                end = int(match.group(2) or start)
                ranges.append((min(start, end), max(start, end)))
        if not ranges:
            return []
        range_start = min(min(start, end) for start, end in ranges)
        range_end = max(max(start, end) for start, end in ranges)
        return [
            candidate
            for candidate in candidates
            if range_start <= candidate.block_id <= range_end
        ]

    def _build_user_prompt(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]],
        window_hint: str = "",
    ) -> tuple[str, Optional[Set[int]]]:
        prompt = self._user_template.format(skeleton_text=skeleton_text) + window_hint
        return _build_candidate_prompt(prompt, candidates)

    def _ensure_budget_state(self) -> None:
        """惰性建立预算统计/缓存/锁。

        既有回归用例用 ``object.__new__(LLMRouter)`` 绕过 ``__init__`` 直接调
        ``fit_skeleton_chunks``（只塞了一个假 ``_client``）。为兼容这种构造方式，
        所有触及 stats/缓存/锁 的入口先调本方法惰性补齐这些属性，而不假设 __init__
        必然跑过。正常构造时它们已存在，这里是无操作。
        """
        if not hasattr(self, "_stats_lock"):
            self._stats_lock = threading.Lock()
        if not hasattr(self, "budget_stats"):
            self.budget_stats = RouterBudgetStats()
        if not hasattr(self, "_fixed_cost_cache"):
            self._fixed_cost_cache = {}
        if not hasattr(self, "_model_profile"):
            self._model_profile = resolve_model_profile(
                getattr(self._client, "model", ""),
            )

    def _estimate_request_tokens(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]],
        window_hint: str = "",
    ) -> int:
        """精确构建**完整请求**并计数——这是最贵的一档估算。

        每次调用都会把 system/schema/user 模板/候选表/窗口提示全部拼出来重新计数，
        因此计入 ``budget_stats.full_request_estimates``。前缀和找切点的目的就是让
        这个计数随行数亚线性增长（开销计数用例断言此点）。真正要发送的完整请求仍
        必须经过它做最终硬校验，前缀和绝不替代它。
        """
        self._ensure_budget_state()
        with self._stats_lock:
            self.budget_stats.full_request_estimates += 1
        user_prompt, _ = self._build_user_prompt(
            skeleton_text, candidates, window_hint,
        )
        messages = LLMClient._build_messages(
            user_prompt, LLMRouterOutput, self._system_prompt,
        )
        return estimate_messages_tokens(messages)

    def _fixed_prompt_tokens(self, window_hint: str) -> int:
        """缓存不可变固定段（空骨架 + 空候选 + 给定窗口提示）的完整请求成本。

        固定段 = system prompt + schema 指令 + user 模板外壳 + 消息信封 + 该窗口提示。
        按 ``window_hint`` 分键缓存（保留提示随窗口位置变化的最坏投影）。命中缓存时
        **不**计入 full_request_estimates——它是「每次解析算一次」的常量项，不是逐分片
        重估的一部分。
        """
        self._ensure_budget_state()
        cache_key = ("fixed", window_hint)
        cached = self._fixed_cost_cache.get(cache_key)
        if cached is not None:
            return cached
        value = self._estimate_request_tokens("", [], window_hint)
        self._fixed_cost_cache[cache_key] = value
        with self._stats_lock:
            self.budget_stats.fixed_prompt_tokens = value
        return value

    def _estimate_unit_tokens(self, unit_text: str) -> int:
        """按原子单元（一行/一段骨架文本）估算**内容**成本——轻量，不含固定段。

        用 ``estimate_text_tokens`` 直接对该单元文本计数（与完整请求估算同源的
        cl100k/UTF-8 取大值口径），供前缀和累加找候选切点。计入 ``prefix_estimates``。
        这是一个**上界近似**：真实请求的 Token 数并不严格可加（BPE 会在拼接边界合并
        或拆分），所以它只用来选候选切点，选出的完整请求一律再走 _estimate_request_tokens
        精确/保守硬校验。
        """
        self._ensure_budget_state()
        with self._stats_lock:
            self.budget_stats.prefix_estimates += 1
        return estimate_text_tokens(unit_text)

    def _candidate_row_tokens(
        self,
        candidates: Optional[List[HeadingCandidate]],
    ) -> int:
        """估算候选表随分片增长的**内容**部分成本（不含表头固定串）。

        候选表整体由 format_candidate_table 生成；这里只需按每行文本估算增量，供前缀和
        累加。同样是上界近似，最终仍由完整请求校验兜底。
        """
        if not candidates:
            return 0
        return self._estimate_unit_tokens(format_candidate_table(candidates))

    # ── 切点优先级（P04 第 3 点）─────────────────────────────
    # 数值越小优先级越高。规划时优先在高优先级边界切，只有更高优先级无可用切点
    # 时才降级到下一档，单行分段是最后手段。
    _CUT_SECTION = 0   # 完整章节边界（窗口/骨架分隔线）
    _CUT_PAGE = 1      # 页 / 栏边界
    _CUT_PARAGRAPH = 2  # 段落边界（普通 [id] 行 / 折叠区行）
    _CUT_LINE = 3      # 单行（兜底）

    @staticmethod
    def _classify_line_boundary(line: str) -> int:
        """判断「在这一行之前切开」对应的切点优先级档位。

        依据骨架文本里已有的结构标记，不额外解析文档：
        - 章节/窗口分隔线（60 个 '='、Window/Skeleton 头、footer）-> _CUT_SECTION
        - 含 page= 提示或 <Gap> 大段跳过（页/栏级别的自然断点）-> _CUT_PAGE
        - 其余带 [id] 的骨架行 -> _CUT_PARAGRAPH
        - 其它续行文本 -> _CUT_LINE
        """
        stripped = line.strip()
        if not stripped:
            return LLMRouter._CUT_PARAGRAPH
        if stripped.startswith("=" * 8) or "Constellation Virtual Skeleton" in stripped \
                or stripped.startswith("Window ") or "Skeleton end" in stripped \
                or "end (Block" in stripped:
            return LLMRouter._CUT_SECTION
        if "<Gap:" in stripped or "page=" in stripped:
            return LLMRouter._CUT_PAGE
        if _SKELETON_ID_RE.match(line):
            return LLMRouter._CUT_PARAGRAPH
        return LLMRouter._CUT_LINE

    def fit_skeleton_chunks(
        self,
        skeleton_chunks: List[str],
        candidates: Optional[List[HeadingCandidate]] = None,
    ) -> List[str]:
        """Split skeletons until every final LLM request fits the safe budget.

        Compression windows are block-count based, while gateways enforce a
        token limit on the *complete* request.  This second-stage sharder
        accounts for the system prompt, JSON schema, user template, candidate
        table, and a worst-case bounded inherited-state hint.  It preserves
        every skeleton line, including a single oversized line split across
        continuation shards.

        P04 重构：不再逐行试装、每步重建 prompt 并对整个暂存文本重估。改为
        （1）固定段成本按 router 缓存一次；（2）对原子单元建**前缀和**，用前缀和
        在 O(log n) 内找到候选切点；（3）候选切点按「章节 > 页/栏 > 段落 > 单行」
        优先级选择；（4）每个候选切点只做**一次**完整请求硬校验，估算偏保守时再
        收缩。完整请求估算次数因此随行数亚线性增长（见开销计数用例），而前缀和
        绝不替代硬校验——真正发送的完整请求一律精确/保守校验。
        """
        if not skeleton_chunks:
            return skeleton_chunks

        self._ensure_budget_state()
        budget = self.input_token_budget
        # Reserve the largest bounded state projection used by later windows.
        reserve_hint = _build_window_hint(
            99_998,
            99_999,
            "层" * 240,
        )
        fixed_tokens = self._fixed_prompt_tokens(reserve_hint)
        if fixed_tokens >= budget:
            raise LLMRouterError(
                "LLM input budget is too small for the fixed router prompt: "
                f"estimated {fixed_tokens} tokens with safe budget {budget}. "
                "Increase LLM_MAX_INPUT_TOKENS, reduce the safety margin, or "
                "shorten the router prompts."
            )
        # 候选切点的内容预算：安全预算扣掉固定段，再留一档余量给「前缀和是可加上界
        # 近似、真实 BPE 在拼接处可能略增」以及候选表增长。硬校验仍以完整请求为准，
        # 这个内容预算只用来选候选切点。
        content_budget = max(1, budget - fixed_tokens)

        def fits(text: str) -> bool:
            """完整请求硬校验（最贵，计数）。真正的准入判据只有这一个。"""
            shard_candidates = self._candidates_for_skeleton(text, candidates)
            estimated = self._estimate_request_tokens(
                text, shard_candidates, reserve_hint,
            )
            if estimated <= budget:
                with self._stats_lock:
                    self.budget_stats.utilization_ratios.append(
                        estimated / max(1, budget)
                    )
            return estimated <= budget

        def plan_cuts(text: str) -> List[str]:
            """用前缀和 + 优先级切点把 text 切成若干候选分片（不含单行兜底切割）。

            返回的每一片都**已通过 fits() 硬校验**。无法在行边界装下的超长单行原样
            返回（交给下游的原子单行切割处理）。
            """
            lines = text.splitlines(keepends=True)
            if not lines:
                return []
            # 前缀和：prefix[i] = 前 i 行内容 Token 之和（轻量估算，累加上界）。
            unit_costs = [self._estimate_unit_tokens(line) for line in lines]
            prefix = [0] * (len(lines) + 1)
            for i, cost in enumerate(lines):
                prefix[i + 1] = prefix[i] + unit_costs[i]
            # 每行「在此行之前切」的优先级档位（第 0 行不可作为切点）。
            boundary = [self._classify_line_boundary(line) for line in lines]

            parts: List[str] = []
            start = 0
            n = len(lines)
            while start < n:
                # 1) 前缀和候选上界：最大的 end 使 (内容和) <= content_budget。
                #    prefix 单调递增，二分即可，不做任何完整请求估算。
                base = prefix[start]
                lo, hi, cand_end = start + 1, n, start + 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if prefix[mid] - base <= content_budget:
                        cand_end = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                # 单行都超内容预算 -> 交给下游原子切割。
                if cand_end == start:
                    cand_end = start + 1

                # 2) 在 (start, cand_end] 内，按优先级挑一个更「干净」的切点：
                #    优先在最高优先级（最小档位值）的边界切，同档取最大 end 以尽量填满。
                best_prio = min(
                    boundary[idx] for idx in range(start + 1, cand_end)
                ) if cand_end - 1 > start else self._CUT_LINE
                preferred_end = cand_end
                for idx in range(cand_end, start, -1):
                    # idx 是「切在第 idx 行之前」，即片含 [start, idx)。
                    if idx == n:
                        continue
                    if boundary[idx] <= best_prio:
                        preferred_end = idx
                        break
                if preferred_end == start:
                    preferred_end = start + 1

                # 3) 硬校验。前缀和是上界近似，真实 BPE 在拼接处可能略增/略减，故 fits()
                #    才是唯一准入判据。先校验优先级切点；不装下时**二分**收缩（不是逐行
                #    线性收缩），把每个分片的完整请求估算次数压到 O(log 分片行数)，从而
                #    总估算数随行数亚线性。装下且优先级切点恰是前缀和上界（无更高优先级
                #    边界可用）时，再二分尝试多吃几行，避免切得过碎浪费预算。
                if fits("".join(lines[start:preferred_end])):
                    best = preferred_end
                    # 前缀和的每行成本是保守上界，累加可能略高估，故真实切点可能比
                    # cand_end 稍靠后。仅在**很小的局部窗口**内二分尝试多吃几行（上界
                    # cand_end+8），把「填满利用率」的收益拿到手，同时把额外完整估算控
                    # 制在每分片 O(log 8) ≈ 常数次，避免退化成每分片 O(log n)。
                    if preferred_end >= cand_end:
                        lo2, hi2 = preferred_end, min(n, cand_end + 8)
                        while lo2 <= hi2:
                            mid = (lo2 + hi2) // 2
                            if mid <= start:
                                lo2 = mid + 1
                                continue
                            if fits("".join(lines[start:mid])):
                                best = mid
                                lo2 = mid + 1
                            else:
                                hi2 = mid - 1
                    parts.append("".join(lines[start:best]))
                    start = best
                    continue

                # 优先级切点装不下：二分找 (start, preferred_end) 内最大可装下的行数。
                lo2, hi2, best = start + 1, preferred_end, start + 1
                while lo2 <= hi2:
                    mid = (lo2 + hi2) // 2
                    if fits("".join(lines[start:mid])):
                        best = mid
                        lo2 = mid + 1
                    else:
                        hi2 = mid - 1
                candidate_text = "".join(lines[start:best])
                if best == start + 1 and not fits(candidate_text):
                    # 单行仍不装下 -> 原样交给原子切割。
                    parts.append(lines[start])
                    start += 1
                    continue
                parts.append(candidate_text)
                start = best
            return parts

        fitted: List[str] = []
        for original in skeleton_chunks:
            if fits(original):
                fitted.append(original)
                continue

            shard_parts: List[str] = []

            def append_part(
                part: str,
                parts: List[str] = shard_parts,
            ) -> None:
                if part:
                    parts.append(part)

            for unit in plan_cuts(original):
                if fits(unit):
                    append_part(unit)
                    continue

                # A single skeleton line can itself be huge (for example an
                # embedded table or a malformed heading style).  Structural
                # ``[id]`` / ``[start to end]`` markers are atomic: the first
                # shard must contain them completely, and later shards carry a
                # non-heading HTML comment with the same candidate range.
                unit_matches = list(_SKELETON_ID_RE.finditer(unit))
                marker_end = max((match.end() for match in unit_matches), default=0)
                range_start = min(
                    (int(match.group(1)) for match in unit_matches),
                    default=-1,
                )
                range_end = max(
                    (
                        int(match.group(2) or match.group(1))
                        for match in unit_matches
                    ),
                    default=-1,
                )
                continuation_prefix = (
                    "<!-- Constellation budget range: "
                    f"{range_start}..{range_end} -->\n"
                    if range_start >= 0 else ""
                )

                remaining = unit
                first_fragment = True
                while remaining:
                    prefix = "" if first_fragment else continuation_prefix
                    minimum = marker_end if first_fragment and marker_end else 1
                    if minimum > len(remaining) or not fits(
                        prefix + remaining[:minimum]
                    ):
                        raise LLMRouterError(
                            "LLM input budget leaves no room for an atomic "
                            "skeleton block marker and its candidate table "
                            f"(safe budget {budget} tokens)."
                        )

                    low, high, best = minimum, len(remaining), minimum
                    while low <= high:
                        mid = (low + high) // 2
                        if fits(prefix + remaining[:mid]):
                            best = mid
                            low = mid + 1
                        else:
                            high = mid - 1
                    append_part(prefix + remaining[:best])
                    remaining = remaining[best:]
                    first_fragment = False
                    marker_end = 0

            if not shard_parts:
                raise LLMRouterError(
                    "Token-budget splitting produced no routable skeleton content."
                )
            fitted.extend(shard_parts)

        with self._stats_lock:
            self.budget_stats.shards_planned = len(fitted)
        if len(fitted) != len(skeleton_chunks):
            logger.info(
                "[Router] Input-budget sharding: %d compressor chunks -> %d "
                "LLM-safe chunks (budget=%d estimated tokens, "
                "full-request estimates=%d)",
                len(skeleton_chunks), len(fitted), budget,
                self.budget_stats.full_request_estimates,
            )
        return fitted

    def plan_windows(
        self,
        skeleton_chunks: List[str],
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> List[RouterWindow]:
        """规划分片并为每片附上显式的 core_range / context_range（P04 第 5 点）。

        在 :meth:`fit_skeleton_chunks` 之上，为每个最终分片显式记录其可决策范围与
        上下文范围，不再依赖分片文本里的数字标签在下游反推控制范围。当前主路径分片
        之间不做块级重叠，故 core_range == context_range；字段分开是为将来的重叠窗口
        预留一等的「控制范围」数据。
        """
        chunks = self.fit_skeleton_chunks(skeleton_chunks, candidates=candidates)
        windows: List[RouterWindow] = []
        for chunk in chunks:
            id_range = self._chunk_id_range(chunk, max_block_id)
            windows.append(
                RouterWindow(
                    text=chunk,
                    core_range=id_range,
                    context_range=id_range,
                )
            )
        return windows

    @staticmethod
    def _chunk_id_range(chunk: str, max_block_id: int) -> tuple[int, int]:
        """推断分片代表的闭区间块 ID 范围。

        与 :meth:`CaliperParser._chunk_id_range` 采用同一口径：优先读预算续片的
        ``Constellation budget range`` 注释，否则扫描 ``[id]`` / ``[start to end]``
        标记；没有任何标记的续片返回空范围 ``(-1, -1)``。
        """
        budget_range = _BUDGET_RANGE_RE.search(chunk)
        if budget_range:
            start = int(budget_range.group(1))
            end = int(budget_range.group(2))
            return min(start, end), max(start, end)
        ids: list[int] = []
        for match in _SKELETON_ID_RE.finditer(chunk):
            ids.append(int(match.group(1)))
            if match.group(2):
                ids.append(int(match.group(2)))
        if not ids:
            return -1, -1
        return min(ids), max(ids)

    # ── Public API ─────────────────────────────────────────────

    def route(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Identify section headings in *skeleton_text*.

        Args:
            skeleton_text: Compressed skeleton produced by
                :class:`SkeletonCompressor`.

        Returns:
            Structured output containing ``doc_title``, ``doc_authors``
            and a flat ``chapters`` anchor list.

        Raises:
            LLMRouterError: On empty input or LLM call failure.
        """
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton text; cannot route.")

        user_prompt = self._user_template.format(skeleton_text=skeleton_text)
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Sending skeleton (%d chars) to LLM",
                len(skeleton_text),
            )
            result = self._call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )
            self._stamp_observation(result, skeleton_text, candidates)
            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(f"LLM routing failed: {exc}") from exc

    def route_chunk(
        self,
        skeleton_text: str,
        chunk_index: int,
        total_chunks: int,
        previous_tail_context: str = "",
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Route a single skeleton chunk (Map phase of Map-Reduce).

        For the first chunk (``chunk_index == 0``), the LLM is asked
        to extract ``doc_title`` and ``doc_authors`` as usual.  For
        subsequent chunks, those fields are explicitly set to empty
        strings so the LLM focuses solely on heading detection.
        Tail context is provided to prevent level jumps.

        Args:
            skeleton_text: One window's compressed skeleton.
            chunk_index: Zero-based window index.
            total_chunks: Total number of windows.
            previous_tail_context: Formatted string of latest
                headings from the previous chunk.

        Returns:
            :class:`LLMRouterOutput` for this chunk.

        Raises:
            LLMRouterError: On empty input or LLM call failure.
        """
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton chunk; cannot route.")

        is_first = chunk_index == 0
        window_hint = _build_window_hint(
            chunk_index, total_chunks, previous_tail_context,
        )
        user_prompt = (
            self._user_template.format(skeleton_text=skeleton_text)
            + window_hint
        )
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Sending chunk %d/%d (%d chars) to LLM",
                chunk_index + 1, total_chunks, len(skeleton_text),
            )
            result = self._call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )

            # Only the first chunk extracts document metadata
            if not is_first:
                result.doc_title = ""
                result.doc_authors = ""

            self._stamp_observation(result, skeleton_text, candidates)
            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(
                f"LLM routing failed on chunk "
                f"{chunk_index + 1}/{total_chunks}: {exc}"
            ) from exc

    # ── Async variants ─────────────────────────────────────────

    async def async_route(
        self,
        skeleton_text: str,
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Async version of :meth:`route`."""
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton text; cannot route.")

        user_prompt = self._user_template.format(skeleton_text=skeleton_text)
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Async sending skeleton (%d chars)", len(skeleton_text),
            )
            result = await self._async_call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )
            self._stamp_observation(result, skeleton_text, candidates)
            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(f"Async LLM routing failed: {exc}") from exc

    async def async_route_chunk(
        self,
        skeleton_text: str,
        chunk_index: int,
        total_chunks: int,
        previous_tail_context: str = "",
        candidates: Optional[List[HeadingCandidate]] = None,
        max_block_id: int = -1,
    ) -> LLMRouterOutput:
        """Async version of :meth:`route_chunk`."""
        if not skeleton_text:
            raise LLMRouterError("Empty skeleton chunk; cannot route.")

        is_first = chunk_index == 0
        window_hint = _build_window_hint(
            chunk_index, total_chunks, previous_tail_context,
        )
        user_prompt = (
            self._user_template.format(skeleton_text=skeleton_text)
            + window_hint
        )
        user_prompt, allowed_ids = _build_candidate_prompt(user_prompt, candidates)

        try:
            logger.info(
                "[Router] Async sending chunk %d/%d (%d chars)",
                chunk_index + 1, total_chunks, len(skeleton_text),
            )
            result = await self._async_call_with_validation_retry(
                user_prompt,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_ids,
            )

            if not is_first:
                result.doc_title = ""
                result.doc_authors = ""

            self._stamp_observation(result, skeleton_text, candidates)
            self._log_result(result)
            return result

        except LLMRouterError:
            raise
        except Exception as exc:
            raise LLMRouterError(
                f"Async LLM routing failed on chunk "
                f"{chunk_index + 1}/{total_chunks}: {exc}"
            ) from exc

    # ── Validation & retry ─────────────────────────────────────

    def _stamp_observation(self, result: LLMRouterOutput, skeleton_text: str, candidates=None) -> None:
        """Carry real half-open physical context bounds, never window ordinals."""
        ids = [int(value) for match in _SKELETON_ID_RE.finditer(skeleton_text)
               for value in match.groups() if value is not None]
        span = (min(ids), max(ids) + 1) if ids else None
        # A folded/Gap range is not an observation of its hidden paragraphs.
        result._observed_block_ids = {
            int(match.group(1)) for match in _SKELETON_ID_RE.finditer(skeleton_text)
            if match.group(2) is None
        } | {candidate.block_id for candidate in (candidates or [])}
        model = str(getattr(getattr(self, "_client", None), "model", "default"))
        for chapter in result.chapters:
            chapter._observation_context = span
            chapter._observation_model = model
            chapter._observation_prompt = ROUTER_PROMPT_VERSION

    @staticmethod
    def _validate_and_filter_anchors(
        result: LLMRouterOutput,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
        downgrade_out_of_candidate: bool = True,
    ) -> LLMRouterOutput:
        """Validate anchors; drop invalid ones, downgrade out-of-candidate ones.

        Hard checks (anchor is dropped on failure):
        - ``level`` in ``[1, 6]``
        - ``start_block_id >= 0``
        - ``title`` non-empty after stripping
        - ``start_block_id <= max_block_id`` (when ``max_block_id >= 0``)

        Soft check: anchors pointing outside ``allowed_block_ids`` are
        NOT dropped when ``downgrade_out_of_candidate`` is True.  They
        are marked ``out_of_candidate=True`` with lowered confidence and
        re-validated against physical block features by the parser
        (the candidate generator must not hold veto power over recall).
        With ``downgrade_out_of_candidate=False`` the legacy hard filter
        applies.

        Returns the (possibly filtered) :class:`LLMRouterOutput`.
        """
        valid_chapters: List[ChapterNode] = []
        dropped = 0

        for ch in result.chapters:
            if ch.level < _VALID_LEVEL_MIN or ch.level > _VALID_LEVEL_MAX:
                logger.warning(
                    "[Router] Dropped anchor: level=%d out of range [%d,%d], "
                    "title='%s'",
                    ch.level, _VALID_LEVEL_MIN, _VALID_LEVEL_MAX,
                    ch.title[:30],
                )
                dropped += 1
                continue
            if ch.start_block_id < 0:
                logger.warning(
                    "[Router] Dropped anchor: block_id=%d < 0, title='%s'",
                    ch.start_block_id, ch.title[:30],
                )
                dropped += 1
                continue
            if max_block_id >= 0 and ch.start_block_id > max_block_id:
                logger.warning(
                    "[Router] Dropped anchor: block_id=%d > max=%d, title='%s'",
                    ch.start_block_id, max_block_id, ch.title[:30],
                )
                dropped += 1
                continue
            if (
                allowed_block_ids is not None
                and ch.start_block_id not in allowed_block_ids
            ):
                if not downgrade_out_of_candidate:
                    logger.warning(
                        "[Router] Dropped anchor: block_id=%d is not in candidate set, title='%s'",
                        ch.start_block_id,
                        ch.title[:30],
                    )
                    dropped += 1
                    continue
                ch.out_of_candidate = True
                ch.confidence = min(ch.confidence, _DOWNGRADE_CONFIDENCE)
                logger.info(
                    "[Router] Downgraded out-of-candidate anchor: "
                    "block_id=%d, title='%s' (pending physical re-validation)",
                    ch.start_block_id,
                    ch.title[:30],
                )
            if not ch.title.strip():
                logger.warning(
                    "[Router] Dropped anchor: empty title, block_id=%d",
                    ch.start_block_id,
                )
                dropped += 1
                continue
            valid_chapters.append(ch)

        if dropped > 0:
            logger.warning(
                "[Router] Dropped %d invalid anchors, kept %d",
                dropped, len(valid_chapters),
            )

        result.chapters = valid_chapters
        # Declared as a PrivateAttr on LLMRouterOutput; consumed by the
        # retry logic to distinguish "no headings" from "all filtered".
        result._dropped_count = dropped
        return result

    def _record_llm_error(self, exc: LLMRouterError) -> None:
        """把一次 LLM 调用失败归类到可观测的输出侧分支（P04 第 6 点）。

        底层 llm_client 已按 finish_reason=='length'（输出截断）、空 choices/内容为
        None（拒答/内容过滤）、JSON 无法解析（格式错误）抛出**不同措辞**的
        LLMRouterError。这里按措辞归类计数——不改 llm_client（它不在可改文件内），
        而是在路由层把这些分支变成可统计信号：
        - 输出截断（max_tokens 触顶）：提示应缩小窗口或调高输出上限。
        - 格式错误：模型没吐出可解析 JSON。
        - 模型拒答 / 内容过滤：choices 为空或 content=None。
        「空标题」不是异常分支，而是在 _validate_and_filter_anchors 里被丢弃并计入
        result._dropped_count，由调用方另行累加，见下方 empty_titles 计数。
        """
        message = str(exc).lower()
        self._ensure_budget_state()
        with self._stats_lock:
            if "truncated by its output" in message or "output was truncated" in message:
                self.budget_stats.output_truncations += 1
            elif "cannot extract valid json" in message or "json" in message and "extract" in message:
                self.budget_stats.format_errors += 1
            elif (
                "empty choices" in message
                or "content=none" in message
                or "refused to generate" in message
                or "content filter" in message
            ):
                self.budget_stats.refusals += 1

    def _checkpoint_identity(
        self,
        kind: str,
        user_prompt: str,
        max_block_id: int,
        allowed_block_ids: Optional[Set[int]],
        stage_fingerprint: str,
    ) -> dict:
        return {
            "version": 2,
            "kind": kind,
            "prompt_version": ROUTER_PROMPT_VERSION,
            "stages": stage_fingerprint,
            "prompt": user_prompt,
            "system": self._system_prompt,
            "config": getattr(self._client, "_cfg", {}),
            "schema": LLMRouterOutput.model_json_schema(),
            "max_block_id": max_block_id,
            "allowed": sorted(allowed_block_ids) if allowed_block_ids is not None else None,
            "downgrade": self._downgrade_out_of_candidate,
        }

    def _legalize_raw(
        self,
        raw: LLMRouterOutput,
        max_block_id: int,
        allowed_block_ids: Optional[Set[int]],
    ) -> LLMRouterOutput:
        return self._validate_and_filter_anchors(
            raw,
            max_block_id=max_block_id,
            allowed_block_ids=allowed_block_ids,
            downgrade_out_of_candidate=self._downgrade_out_of_candidate,
        )

    def _call_with_validation_retry(
        self,
        user_prompt: str,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
    ) -> LLMRouterOutput:
        """Call the LLM and retry if all anchors are invalid after filtering.

        Sync and async share the same checkpoint protocol: validated results
        are keyed by the legalize fingerprint; raw model JSON can be reused
        when only validation rules changed.
        """
        checkpoint = current_checkpoints()
        raw_store = current_raw_checkpoints()
        validated_key = None
        raw_key = None
        if checkpoint is not None:
            validated_key = checkpoint.key(self._checkpoint_identity(
                "legalize", user_prompt, max_block_id, allowed_block_ids,
                getattr(checkpoint, "stage_fingerprint", ""),
            ))
            cached = checkpoint.load(validated_key)
            if cached is not None:
                return LLMRouterOutput.model_validate(cached)
        if raw_store is not None:
            raw_key = raw_store.key(self._checkpoint_identity(
                "route_raw", user_prompt, max_block_id, allowed_block_ids,
                getattr(raw_store, "stage_fingerprint", ""),
            ))
            raw_cached = raw_store.load(raw_key)
            if raw_cached is not None:
                raw = LLMRouterOutput.model_validate(raw_cached)
                result = self._legalize_raw(raw, max_block_id, allowed_block_ids)
                if checkpoint is not None and validated_key and (result.chapters or result._dropped_count == 0):
                    checkpoint.save(validated_key, result.model_dump(mode="json"))
                return result
        last_result: LLMRouterOutput | None = None
        last_raw: LLMRouterOutput | None = None
        for attempt in range(1 + _MAX_ANCHOR_RETRIES):
            try:
                with self._stats_lock:
                    self._actual_model_calls = getattr(self, "_actual_model_calls", 0) + 1
                result = self._client.structured_completion(
                    prompt=user_prompt,
                    response_model=LLMRouterOutput,
                    system_prompt=self._system_prompt,
                )
            except LLMRouterError as exc:
                self._record_llm_error(exc)
                raise
            last_raw = result.model_copy(deep=True)
            raw_chapter_count = len(result.chapters)
            result = self._legalize_raw(result, max_block_id, allowed_block_ids)
            last_result = result
            if result._dropped_count:
                with self._stats_lock:
                    self.budget_stats.empty_titles += result._dropped_count
            if result.chapters:
                break
            dropped = result._dropped_count
            if raw_chapter_count == 0 and dropped == 0:
                break
            if attempt < _MAX_ANCHOR_RETRIES:
                with self._stats_lock:
                    self.budget_stats.retries += 1
                time.sleep(_RETRY_BACKOFF_BASE * (2 ** attempt))
        else:
            logger.warning("[Router] No valid anchors after %d attempts", 1 + _MAX_ANCHOR_RETRIES)
        assert last_result is not None
        if raw_store is not None and raw_key and last_raw is not None and (last_result.chapters or last_result._dropped_count == 0):
            raw_store.save(raw_key, last_raw.model_dump(mode="json"))
        if checkpoint is not None and validated_key and (last_result.chapters or last_result._dropped_count == 0):
            checkpoint.save(validated_key, last_result.model_dump(mode="json"))
        return last_result

    async def _async_call_with_validation_retry(
        self,
        user_prompt: str,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
    ) -> LLMRouterOutput:
        checkpoint = current_checkpoints()
        raw_store = current_raw_checkpoints()
        validated_key = None
        raw_key = None
        if checkpoint is not None:
            validated_key = checkpoint.key(self._checkpoint_identity(
                "legalize", user_prompt, max_block_id, allowed_block_ids,
                getattr(checkpoint, "stage_fingerprint", ""),
            ))
            cached = await asyncio.to_thread(checkpoint.load, validated_key)
            if cached is not None:
                return LLMRouterOutput.model_validate(cached)
        if raw_store is not None:
            raw_key = raw_store.key(self._checkpoint_identity(
                "route_raw", user_prompt, max_block_id, allowed_block_ids,
                getattr(raw_store, "stage_fingerprint", ""),
            ))
            raw_cached = await asyncio.to_thread(raw_store.load, raw_key)
            if raw_cached is not None:
                raw = LLMRouterOutput.model_validate(raw_cached)
                result = self._legalize_raw(raw, max_block_id, allowed_block_ids)
                if checkpoint is not None and validated_key and (result.chapters or result._dropped_count == 0):
                    await asyncio.to_thread(
                        checkpoint.save, validated_key, result.model_dump(mode="json"),
                    )
                return result
        if checkpoint is None and raw_store is None:
            return await self._async_call_uncached(user_prompt, max_block_id, allowed_block_ids)
        result = await self._async_call_uncached(user_prompt, max_block_id, allowed_block_ids)
        if checkpoint is not None and validated_key and (result.chapters or result._dropped_count == 0):
            await asyncio.to_thread(checkpoint.save, validated_key, result.model_dump(mode="json"))
        return result

    async def _async_call_uncached(
        self,
        user_prompt: str,
        max_block_id: int = -1,
        allowed_block_ids: Optional[Set[int]] = None,
    ) -> LLMRouterOutput:
        """Async version of :meth:`_call_with_validation_retry`."""
        last_result: LLMRouterOutput | None = None

        for attempt in range(1 + _MAX_ANCHOR_RETRIES):
            async_client = get_async_llm_client(self._llm_config)
            try:
                with self._stats_lock:
                    self._actual_model_calls = getattr(self, "_actual_model_calls", 0) + 1
                result: LLMRouterOutput = await async_client.structured_completion(
                    prompt=user_prompt,
                    response_model=LLMRouterOutput,
                    system_prompt=self._system_prompt,
                )
            except LLMRouterError as exc:
                self._record_llm_error(exc)
                raise
            raw_result = result.model_dump(mode="json")
            raw_chapter_count = len(result.chapters)
            result = self._validate_and_filter_anchors(
                result,
                max_block_id=max_block_id,
                allowed_block_ids=allowed_block_ids,
                downgrade_out_of_candidate=self._downgrade_out_of_candidate,
            )
            last_result = result
            raw_store = current_raw_checkpoints()
            if raw_store is not None and (result.chapters or result._dropped_count == 0):
                raw_key = raw_store.key(self._checkpoint_identity(
                    "route_raw", user_prompt, max_block_id, allowed_block_ids,
                    getattr(raw_store, "stage_fingerprint", ""),
                ))
                await asyncio.to_thread(raw_store.save, raw_key, raw_result)
            if result._dropped_count:
                with self._stats_lock:
                    self.budget_stats.empty_titles += result._dropped_count

            if result.chapters:
                return result

            dropped = result._dropped_count
            if raw_chapter_count == 0 and dropped == 0:
                logger.info(
                    "[Router] LLM returned no headings (likely a "
                    "headingless document), accepting as-is"
                )
                return result

            if attempt < _MAX_ANCHOR_RETRIES:
                with self._stats_lock:
                    self.budget_stats.retries += 1
                wait = _RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "[Router] All %d anchors invalid (attempt %d/%d), "
                    "retrying in %.1fs",
                    raw_chapter_count, attempt + 1,
                    1 + _MAX_ANCHOR_RETRIES, wait,
                )
                await asyncio.sleep(wait)

        logger.warning(
            "[Router] No valid anchors after %d attempts",
            1 + _MAX_ANCHOR_RETRIES,
        )
        return last_result  # type: ignore[return-value]

    # ── Logging ────────────────────────────────────────────────

    def record_estimation_deviation(self, estimated: int, actual: int) -> None:
        """记录一次「估算 - 实际」Token 偏差样本（P04 第 7 点，离线调优依据）。

        当网关在 completion.usage 里返回真实 prompt_tokens 后，调用方（或将来在
        llm_client 回调里）把该值与请求前的估算一起传进来，这里累加偏差。正数表示
        我们高估（更安全但更碎），负数表示低估（有超限风险，应收紧 safety margin 或
        为该模型建更贴近的 tokenizer 档案）。仅用于离线分析，不影响任何在线硬校验。
        """
        with self._stats_lock:
            self.budget_stats.usage_deviations.append(int(estimated) - int(actual))

    def budget_stats_snapshot(self) -> Dict[str, object]:
        """返回预算/路由统计的只读快照（验收与离线调优读取）。"""
        with self._stats_lock:
            return self.budget_stats.snapshot()

    @staticmethod
    def _log_result(result: LLMRouterOutput) -> None:
        """Emit a debug-level summary of the routing result."""
        logger.debug(
            "[Router] title='%s', authors='%s', chapters=%d",
            result.doc_title, result.doc_authors, len(result.chapters),
        )
        for ch in result.chapters:
            indent = "  " * (ch.level - 1)
            snippet_hint = (
                f" [snippet: {ch.snippet[:20]}…" if ch.snippet else ""
            )
            logger.debug(
                "  %s[%d] L%d: %s%s",
                indent, ch.start_block_id, ch.level, ch.title, snippet_hint,
            )
