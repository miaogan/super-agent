"""V3-T7 adversarial 子代理：judge agent + 投票合并。

在 V2.5-T3 并行编排（fan-out / fan-in）基础上，新增两种对抗式合并策略：

- ``vote``：多数投票。每个子代理的输出经 ``vote_fn`` 提取一个「投票键」
  （如答案首行、结构化字段），取出现次数最多的那个对应的原始输出。
  平票时取第一个成功结果。无需额外 LLM 调用。
- ``judge``：法官代理。将任务 + 所有子代理输出交给一个 ``judge_spec``
  定义的 judge agent，由其综合评判、挑选或合成最终答案。需要一次额外的
  LLM 调用（``judge_runner``）。

这两种策略适合需要「多智能体一致性 / 质量把关」的场景，例如：
- 多模型对比评测（vote）
- 代码审查交叉验证（judge）
- 高风险决策前的双盲复核（judge）

设计原则
--------
- 不重写 fan-out：直接复用 ``ParallelOrchestrator`` 的并发执行逻辑，
  只替换 ``_merge`` 阶段。
- ``vote_fn`` 可注入：默认按「输出归一化后的全文」投票（适合答案唯一的场景），
  调用方可自定义提取结构化答案（如 JSON ``answer`` 字段）。
- ``judge_runner`` 可注入：默认复用 deepagents runner，测试可注入 fake。
- 不引入新依赖：纯标准库 + dataclass。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.workflow.parallel import (
    MERGE_ALL,
    MERGE_FIRST,
    MERGE_MERGE,
    ParallelError,
    ParallelOrchestrator,
    ParallelResult,
    ParallelStepResult,
    SubagentRunner,
)

logger = logging.getLogger(__name__)

# 新增合并策略常量
MERGE_VOTE = "vote"
MERGE_JUDGE = "judge"

# 全部合法策略（含 V2.5 的 first/all/merge）
ADVERSARIAL_STRATEGIES = {MERGE_FIRST, MERGE_ALL, MERGE_MERGE, MERGE_VOTE, MERGE_JUDGE}

# 投票键提取函数：(step: ParallelStepResult) -> str
VoteFn = Callable[[ParallelStepResult], str]
# 法官 runner：(judge_spec, task, outputs, context) -> str
JudgeRunner = Callable[
    [dict[str, Any], str, list[ParallelStepResult], dict[str, Any]],
    Awaitable[str],
]


def default_vote_fn(step: ParallelStepResult) -> str:
    """默认投票键：归一化输出全文（去首尾空白、压缩内部空白）。

    适合答案高度一致的场景（如 1+1=?）。若输出是结构化 JSON，
    调用方应提供自定义 vote_fn 提取 answer 字段。
    """
    text = (step.output or "").strip()
    # 压缩多余空白，避免格式差异导致同票分散
    text = re.sub(r"\s+", " ", text)
    return text


@dataclass
class AdversarialResult(ParallelResult):
    """adversarial 编排结果，在 ParallelResult 基础上附加投票/法官信息。"""

    # vote 策略：各投票键的计数
    vote_counts: dict[str, int] = field(default_factory=dict)
    # vote 策略：胜出的投票键
    winner_vote: str | None = None
    # judge 策略：法官代理名
    judge_agent: str | None = None
    # judge 策略：法官输出的原始文本（若 final_output 是从 judge 输出中提取的）
    judge_raw: str | None = None


@dataclass
class AdversarialOrchestrator:
    """对抗式子代理编排器（并行执行 + vote/judge 合并）。

    Args:
        subagents: 子代理 spec 列表（与 ParallelOrchestrator 同 schema）。
        strategy: 合并策略；除 V2.5 的 ``first``/``all``/``merge`` 外，
            新增 ``vote``（多数投票）/ ``judge``（法官代理）。
        vote_fn: ``strategy=vote`` 时的投票键提取函数；None 用
            ``default_vote_fn``（输出全文归一化）。
        judge_spec: ``strategy=judge`` 时的法官代理 spec（含 name/system_prompt/model）；
            None 时用内建默认法官。
        judge_runner: 法官执行协程；None 时用 deepagents 默认 runner。
        min_success: 最少成功数（quorum）；默认 1。vote 需要至少 2 个成功才有意义，
            但不强制（1 个成功时直接返回该结果）。
        timeout_seconds: 整体 deadline（秒）；None 不超时。
        runner: 子代理执行协程；None 用 deepagents 默认 runner。
        max_concurrency: 最大并发数；None 不限。
    """

    subagents: list[dict[str, Any]]
    strategy: str = MERGE_VOTE
    vote_fn: VoteFn | None = None
    judge_spec: dict[str, Any] | None = None
    judge_runner: JudgeRunner | None = None
    min_success: int = 1
    timeout_seconds: float | None = None
    runner: SubagentRunner | None = None
    max_concurrency: int | None = None

    def __post_init__(self) -> None:
        if self.strategy not in ADVERSARIAL_STRATEGIES:
            raise ParallelError(
                f"未知合并策略: {self.strategy}（合法值: {ADVERSARIAL_STRATEGIES}）"
            )
        if self.strategy == MERGE_VOTE:
            # vote_fn 可选，None 时用 default_vote_fn
            pass
        if self.strategy == MERGE_JUDGE:
            # judge_spec 可选，None 时用默认法官
            if self.judge_spec is None:
                self.judge_spec = {
                    "name": "judge",
                    "description": "综合评判多个子代理输出，挑选最佳答案",
                    "system_prompt": (
                        "你是公正的评审专家。给定任务和多个子代理的输出，"
                        "请综合评估准确性、完整性、逻辑性，挑选最佳答案；"
                        "若各有优劣可融合互补。直接输出最终答案，不要解释评审过程。"
                    ),
                    "model": "",
                }
        if self.min_success < 1:
            raise ParallelError("min_success 必须 >= 1")

    async def run_adversarial(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> AdversarialResult:
        """执行对抗式编排。"""
        ctx: dict[str, Any] = dict(context or {})

        # 1. fan-out：复用 ParallelOrchestrator 并行执行
        #    对 vote/judge 策略，底层用 first 取第一个成功作为占位（实际合并在后面做）
        base_strategy = (
            MERGE_ALL if self.strategy in (MERGE_VOTE, MERGE_JUDGE) else self.strategy
        )
        orch = ParallelOrchestrator(
            subagents=self.subagents,
            strategy=base_strategy,
            min_success=self.min_success,
            timeout_seconds=self.timeout_seconds,
            runner=self.runner,
            max_concurrency=self.max_concurrency,
        )
        base_result = await orch.run_parallel(task, ctx)

        # 若底层失败（超时 / quorum 不足），直接返回失败结果
        if base_result.error and base_result.success_count < self.min_success:
            return AdversarialResult(
                steps=base_result.steps,
                final_output="",
                strategy=self.strategy,
                success_count=base_result.success_count,
                failure_count=base_result.failure_count,
                elapsed_ms=base_result.elapsed_ms,
                error=base_result.error,
            )

        ok_steps = [s for s in base_result.steps if s.ok]
        if not ok_steps:
            return AdversarialResult(
                steps=base_result.steps,
                final_output="",
                strategy=self.strategy,
                success_count=0,
                failure_count=base_result.failure_count,
                elapsed_ms=base_result.elapsed_ms,
                error="无成功子代理输出",
            )

        # 2. fan-in：按对抗策略合并
        if self.strategy == MERGE_VOTE:
            final, winner, counts = self._merge_vote(ok_steps)
            return AdversarialResult(
                steps=base_result.steps,
                final_output=final,
                strategy=self.strategy,
                success_count=base_result.success_count,
                failure_count=base_result.failure_count,
                elapsed_ms=base_result.elapsed_ms,
                error=None if not base_result.failure_count else base_result.error,
                vote_counts=counts,
                winner_vote=winner,
            )

        if self.strategy == MERGE_JUDGE:
            final, judge_raw = await self._merge_judge(task, ok_steps, ctx)
            return AdversarialResult(
                steps=base_result.steps,
                final_output=final,
                strategy=self.strategy,
                success_count=base_result.success_count,
                failure_count=base_result.failure_count,
                elapsed_ms=base_result.elapsed_ms,
                error=None if not base_result.failure_count else base_result.error,
                judge_agent=self.judge_spec["name"] if self.judge_spec else "judge",
                judge_raw=judge_raw,
            )

        # first/all/merge 由底层已处理，直接透传
        return AdversarialResult(
            steps=base_result.steps,
            final_output=base_result.final_output,
            strategy=self.strategy,
            success_count=base_result.success_count,
            failure_count=base_result.failure_count,
            elapsed_ms=base_result.elapsed_ms,
            error=base_result.error,
        )

    def _merge_vote(
        self, ok_steps: list[ParallelStepResult]
    ) -> tuple[str, str | None, dict[str, int]]:
        """多数投票合并：按 vote_fn 提取投票键，取频次最高的输出。

        Returns:
            (final_output, winner_vote_key, vote_counts)
        """
        vote_fn = self.vote_fn or default_vote_fn
        keys = [vote_fn(s) for s in ok_steps]
        counts = Counter(keys)
        # 取频次最高；平票时取第一个出现的（稳定）
        winner_key = counts.most_common(1)[0][0]
        # 返回第一个投给 winner_key 的原始输出
        for s, k in zip(ok_steps, keys):
            if k == winner_key:
                return s.output, winner_key, dict(counts)
        # 兜底
        return ok_steps[0].output, winner_key, dict(counts)

    async def _merge_judge(
        self,
        task: str,
        ok_steps: list[ParallelStepResult],
        context: dict[str, Any],
    ) -> tuple[str, str]:
        """法官代理合并：把任务 + 所有输出交给 judge，返回最终答案。

        Returns:
            (final_output, judge_raw_output)
        """
        assert self.judge_spec is not None  # __post_init__ 已保证
        # 构造法官的 prompt：任务 + 各子代理输出
        outputs_text = "\n\n".join(
            f"【子代理 {i+1}：{s.agent}】\n{s.output}"
            for i, s in enumerate(ok_steps)
        )
        judge_task = (
            f"任务：{task}\n\n"
            f"以下是 {len(ok_steps)} 个子代理的回答，请综合评判并输出最佳答案：\n\n"
            f"{outputs_text}\n\n"
            f"最佳答案："
        )
        if self.judge_runner is not None:
            raw = await self.judge_runner(self.judge_spec, judge_task, context)
        else:
            from app.workflow.orchestrator import _default_runner  # noqa: PLC0415

            raw = await _default_runner(self.judge_spec, judge_task, context)
        if not isinstance(raw, str):
            raw = str(raw)
        # final_output 直接取法官输出（法官应直接给出最终答案）
        return raw, raw
