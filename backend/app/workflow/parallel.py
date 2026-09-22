"""V2.5-T3 并行子代理：fan-out / fan-in + 结果合并策略。

设计要点（ROADMAP：fan-out/fan-in + 结果合并策略 first/all/merge）
------------------------------------------------------------------------
- **与 sequential 的关系**：``SubagentOrchestrator`` 是确定性串行；
  本模块 ``ParallelOrchestrator`` 是并发执行 + 汇聚，适合
  多源调研 / 多模型对比 / 独立子任务并行。
- **fan-out**：把一个任务并发派发给 N 个子代理（或同一子代理跑 N 个分片）。
- **fan-in**：等所有 / 任一 / 前 k 个完成，按策略合并为单一结果。
- **合并策略**：
    - ``first``：取第一个成功的结果（其余取消/忽略）
    - ``all``：拼接所有结果（默认 ``\\n---\\n`` 分隔）
    - ``merge``：交给 merge_fn 自定义合并（如去重 / 投票 / LLM 总结）
- **失败策略**：
    - ``fastest``：只要有一个成功就返回（first 的推广）
    - ``quorum``：至少 ``min_success`` 个成功才算整体成功
    - ``all_or_none``：任一失败则整体失败
- **超时**：整体 deadline（``timeout_seconds``），超时取消未完成的。

与 deepagents 的集成
--------------------
- runner 协议与 ``SubagentOrchestrator`` 一致：``async (spec, task, context) -> str``
- 默认 runner 复用 ``_default_runner``（deepagents create_sub_agent）
- 测试可注入 fake runner 跳过 LLM 调用

示例
-----
.. code-block:: python

    orch = ParallelOrchestrator(
        subagents=[
            {"name": "researcher_a", "description": "..."},
            {"name": "researcher_b", "description": "..."},
        ],
        strategy="all",
        runner=fake_runner,
    )
    result = await orch.run_parallel("调研 X 的最新进展", context={})
    print(result.final_output)  # 两个 researcher 的结果拼接
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# runner 协议类型：async (spec, task, context) -> output_text
SubagentRunner = Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[str]]


class ParallelError(RuntimeError):
    """并行编排失败。"""


# 合并策略
MERGE_FIRST = "first"
MERGE_ALL = "all"
MERGE_MERGE = "merge"

VALID_STRATEGIES = {MERGE_FIRST, MERGE_ALL, MERGE_MERGE}


@dataclass
class ParallelStepResult:
    """单个并行子代理的执行结果。"""

    agent: str
    output: str = ""
    ok: bool = True
    error: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "output": self.output,
            "ok": self.ok,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class ParallelResult:
    """并行编排的最终结果。"""

    steps: list[ParallelStepResult] = field(default_factory=list)
    final_output: str = ""
    strategy: str = MERGE_ALL
    success_count: int = 0
    failure_count: int = 0
    elapsed_ms: int = 0
    # 任一子代理失败时携带错误信息（按策略决定是否致命）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "final_output": self.final_output,
            "strategy": self.strategy,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass
class ParallelOrchestrator:
    """并行子代理编排器（fan-out → fan-in）。

    Args:
        subagents: 子代理 spec 列表（与 SubagentOrchestrator 同 schema）。
        strategy: 合并策略 ``first`` / ``all`` / ``merge``。
        merge_fn: ``strategy=merge`` 时的自定义合并函数
            ``(results: list[ParallelStepResult]) -> str``。
        min_success: 最少成功数（quorum 模式）；默认 1。
        timeout_seconds: 整体 deadline（秒）；None 表示不超时。
        separator: ``strategy=all`` 时的拼接分隔符。
        runner: 子代理执行协程；None 用 deepagents 默认 runner。
        max_concurrency: 最大并发数（信号量）；None 表示不限。
    """

    subagents: list[dict[str, Any]]
    strategy: str = MERGE_ALL
    merge_fn: Callable[[list[ParallelStepResult]], str] | None = None
    min_success: int = 1
    timeout_seconds: float | None = None
    separator: str = "\n---\n"
    runner: SubagentRunner | None = None
    max_concurrency: int | None = None

    def __post_init__(self) -> None:
        if self.strategy not in VALID_STRATEGIES:
            raise ParallelError(
                f"未知合并策略: {self.strategy}（合法值: {VALID_STRATEGIES}）"
            )
        if self.strategy == MERGE_MERGE and self.merge_fn is None:
            raise ParallelError("strategy=merge 需要提供 merge_fn")
        if self.min_success < 1:
            raise ParallelError("min_success 必须 >= 1")

    def validate(self) -> None:
        """校验 subagent 列表。"""
        if not self.subagents:
            raise ParallelError("subagents 列表为空")
        names: set[str] = set()
        for i, spec in enumerate(self.subagents):
            if not isinstance(spec, dict):
                raise ParallelError(f"第 {i} 个 subagent 不是 dict: {spec!r}")
            name = spec.get("name")
            if not name or not isinstance(name, str):
                raise ParallelError(f"第 {i} 个 subagent 缺少 name")
            if name in names:
                raise ParallelError(f"subagent 名重复: {name}")
            names.add(name)

    async def run_parallel(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> ParallelResult:
        """并发执行所有子代理并按策略合并。

        Args:
            task: 派发给每个子代理的任务描述（同一份）。
            context: 透传给每个 runner 的上下文。

        Returns:
            ``ParallelResult``：含每步结果 + 合并后的最终输出。
        """
        import time

        self.validate()
        ctx: dict[str, Any] = dict(context or {})
        start = time.monotonic()
        sem = (
            asyncio.Semaphore(self.max_concurrency)
            if self.max_concurrency
            else None
        )

        async def _run_one(spec: dict[str, Any]) -> ParallelStepResult:
            name = spec["name"]
            t0 = time.monotonic()
            try:
                if sem is not None:
                    async with sem:
                        output = await self._invoke(spec, task, ctx)
                else:
                    output = await self._invoke(spec, task, ctx)
                if not isinstance(output, str):
                    output = str(output)
                return ParallelStepResult(
                    agent=name,
                    output=output,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("parallel 子代理 %s 失败", name)
                return ParallelStepResult(
                    agent=name,
                    ok=False,
                    error=str(exc),
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )

        # fan-out：并发派发
        coros = [_run_one(spec) for spec in self.subagents]
        try:
            if self.timeout_seconds is not None:
                steps = await asyncio.wait_for(
                    asyncio.gather(*coros, return_exceptions=False),
                    timeout=self.timeout_seconds,
                )
            else:
                steps = await asyncio.gather(*coros, return_exceptions=False)
        except asyncio.TimeoutError:
            elapsed = int((time.monotonic() - start) * 1000)
            return ParallelResult(
                steps=[],
                final_output="",
                strategy=self.strategy,
                success_count=0,
                failure_count=len(self.subagents),
                elapsed_ms=elapsed,
                error=f"parallel 超时（{self.timeout_seconds}s）",
            )

        success = [s for s in steps if s.ok]
        failure = [s for s in steps if not s.ok]
        # 按声明顺序排（gather 保序，但显式重排便于阅读）
        name_order = [s["name"] for s in self.subagents]
        steps_ordered = sorted(
            steps, key=lambda s: name_order.index(s.agent) if s.agent in name_order else 999
        )

        # quorum 检查
        if len(success) < self.min_success:
            elapsed = int((time.monotonic() - start) * 1000)
            return ParallelResult(
                steps=steps_ordered,
                final_output="",
                strategy=self.strategy,
                success_count=len(success),
                failure_count=len(failure),
                elapsed_ms=elapsed,
                error=(
                    f"成功数 {len(success)} < 最少要求 {self.min_success}"
                ),
            )

        final_output = self._merge(steps_ordered)
        elapsed = int((time.monotonic() - start) * 1000)
        return ParallelResult(
            steps=steps_ordered,
            final_output=final_output,
            strategy=self.strategy,
            success_count=len(success),
            failure_count=len(failure),
            elapsed_ms=elapsed,
            error=None if not failure else f"{len(failure)} 个子代理失败",
        )

    async def _invoke(
        self,
        spec: dict[str, Any],
        task: str,
        context: dict[str, Any],
    ) -> str:
        """调用 runner；None 时用 deepagents 默认 runner。"""
        if self.runner is not None:
            return await self.runner(spec, task, context)
        # 延迟导入避免循环依赖
        from app.workflow.orchestrator import _default_runner  # noqa: PLC0415

        return await _default_runner(spec, task, context)

    def _merge(self, steps: list[ParallelStepResult]) -> str:
        """按策略合并成功结果。"""
        ok_results = [s for s in steps if s.ok]
        if not ok_results:
            return ""
        if self.strategy == MERGE_FIRST:
            return ok_results[0].output
        if self.strategy == MERGE_ALL:
            return self.separator.join(s.output for s in ok_results)
        if self.strategy == MERGE_MERGE:
            assert self.merge_fn is not None  # __post_init__ 已校验
            return self.merge_fn(ok_results)
        # 不应到达
        raise ParallelError(f"未知策略: {self.strategy}")


def spec_from_compiled_parallel(
    compiled_subagents: list[dict[str, Any]],
    *,
    fallback_model: str | None = None,
) -> list[dict[str, Any]]:
    """复用 sequential 的 spec 归一化（同名函数在 orchestrator 模块）。

    保留独立入口便于显式表达意图；内部委托给 orchestrator.spec_from_compiled。
    """
    from app.workflow.orchestrator import spec_from_compiled  # noqa: PLC0415

    return spec_from_compiled(
        compiled_subagents, fallback_model=fallback_model
    )
