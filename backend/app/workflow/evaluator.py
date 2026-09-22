"""V2-T8 评测面板：TestCase + golden + 运行 + 通过率。

设计要点
--------
- **三种断言方式**：
  - ``contains``：actual 包含 expected 子串（默认，最宽松）
  - ``regex``：actual 匹配 expected 正则
  - ``similarity``：actual 与 expected 的 Jaccard 词集合相似度 ≥ 阈值
- **runner 协议**：``async (case_input, context) -> actual_output``。
  默认 runner 调真实 Agent；测试可注入 fake runner 跳过 LLM。
- **批量运行**：``run_batch`` 顺序跑所有 case，回写 TestRun + TestCase 实际结果，
  返回通过率。失败 case 不阻断后续。
- **隔离**：每个 tenant 的 TestCase 独立；workflow_id 为空时跑默认 Agent。

与 deepagents 的关系
------------------------
默认 runner 复用 build_agent，按 CompiledConfig 组装；
测试链路注入 fake runner，纯逻辑断言可在无 LLM 环境下运行。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# runner 协议：async (case_input, context) -> actual_output
EvalRunner = Callable[[str, dict[str, Any]], Awaitable[str]]


class EvalError(RuntimeError):
    """评测执行失败。"""


@dataclass
class CaseResult:
    """单 case 运行结果。"""

    case_id: str
    case_name: str
    passed: bool
    actual: str
    error: str | None = None
    elapsed_ms: int = 0


@dataclass
class BatchResult:
    """批量运行结果。"""

    total: int = 0
    passed: int = 0
    results: list[CaseResult] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "results": [
                {
                    "case_id": r.case_id,
                    "case_name": r.case_name,
                    "passed": r.passed,
                    "actual": r.actual,
                    "error": r.error,
                    "elapsed_ms": r.elapsed_ms,
                }
                for r in self.results
            ],
            "elapsed_ms": self.elapsed_ms,
        }


def assert_case(
    actual: str, expected: str, assertion: str = "contains"
) -> bool:
    """断言 actual 是否符合 expected。

    Args:
        actual: 实际输出。
        expected: 期望输出（contains / regex 时是子串或正则；similarity 时是参照文本）。
        assertion: 断言方式。

    Returns:
        是否通过。
    """
    if not actual:
        return False
    if assertion == "regex":
        try:
            return re.search(expected, actual) is not None
        except re.error as exc:
            logger.warning("正则无效 %r：%s", expected, exc)
            return False
    if assertion == "similarity":
        return _jaccard_similarity(actual, expected) >= 0.3
    # 默认 contains
    return expected.lower() in actual.lower() if expected else bool(actual)


def _jaccard_similarity(a: str, b: str) -> float:
    """Jaccard 词集合相似度（简单实现，无分词库依赖）。"""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union)


async def run_batch(
    cases: list[dict[str, Any]],
    runner: EvalRunner,
    *,
    context: dict[str, Any] | None = None,
) -> BatchResult:
    """顺序运行一批 test case，返回通过率。

    Args:
        cases: 每个 case 至少含 ``id`` + ``input`` + ``expected`` + ``assertion``。
        runner: 执行单个 case 的协程；默认 runner 调真实 Agent。
        context: 透传给 runner 的上下文（tenant_id / workflow_id 等）。

    Returns:
        ``BatchResult``：含每 case 结果 + 通过率。
    """
    result = BatchResult(total=len(cases))
    batch_start = time.monotonic()
    ctx = dict(context or {})

    for case in cases:
        case_id = case.get("id", "")
        case_name = case.get("name", case_id)
        case_input = case.get("input", "")
        expected = case.get("expected", "")
        assertion = case.get("assertion", "contains")
        case_start = time.monotonic()
        try:
            actual = await runner(case_input, ctx)
            if not isinstance(actual, str):
                actual = str(actual)
            passed = assert_case(actual, expected, assertion)
            elapsed = int((time.monotonic() - case_start) * 1000)
            result.results.append(
                CaseResult(
                    case_id=case_id,
                    case_name=case_name,
                    passed=passed,
                    actual=actual,
                    elapsed_ms=elapsed,
                )
            )
            if passed:
                result.passed += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("case %s 运行失败", case_id)
            elapsed = int((time.monotonic() - case_start) * 1000)
            result.results.append(
                CaseResult(
                    case_id=case_id,
                    case_name=case_name,
                    passed=False,
                    actual="",
                    error=str(exc),
                    elapsed_ms=elapsed,
                )
            )

    result.elapsed_ms = int((time.monotonic() - batch_start) * 1000)
    return result


async def _default_runner(case_input: str, context: dict[str, Any]) -> str:
    """默认 runner：复用 build_agent 跑真实 Agent。

    需要 deepagents + 可用的 LLM；测试应注入 fake runner 跳过此路径。
    """
    from app.agent import build_agent  # noqa: PLC0415
    from app.sandbox_backend import OpenSandboxBackend  # noqa: PLC0415

    backend = OpenSandboxBackend.create()
    agent = build_agent(backend=backend)
    state = {"messages": [{"role": "user", "content": case_input}]}
    result = await agent.ainvoke(state)
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    for m in reversed(msgs):
        content = getattr(m, "content", None)
        if content and getattr(m, "type", "") == "ai":
            return content if isinstance(content, str) else str(content)
    return ""
