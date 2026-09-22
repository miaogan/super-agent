"""V2-T4 子代理 sequential 编排器。

与 deepagents 内置 ``task`` 工具的区别
----------------------------------------
- ``task`` 工具：主 Agent 的 LLM **自主** 决定何时派发、派发给谁、是否串行
  （非确定性，适合开放任务）。
- ``SubagentOrchestrator``：按 ``CompiledConfig.subagents`` 的**声明顺序** 固定串行执行
  （确定性，适合流水线 / workflow 场景，例如
  ``coder → reviewer → summarizer``）。

每步的输出（子代理最后一条 AI 消息文本）作为下一步的输入；最后一步的输出
作为整个编排的最终结果。

设计要点
--------
- **runner 协议**：``async (spec, task, context) -> str``。
  默认 runner 用 deepagents ``create_sub_agent`` 真实执行；测试可注入 fake runner
  跳过 LLM 调用，使编排逻辑可在无 deepagents / 无 PG 环境下单元测试。
- **spec 归一化**：画布上 ``subagent`` 节点的 ``data.subagents`` 是任意 dict，
  本编排器要求每条至少有 ``name`` + ``description``，其余字段（system_prompt /
  model / tools）可选，缺省时由默认 runner 从全局配置补齐。
- **失败策略**：任一步抛异常则终止整条链，``OrchestratorResult.partial`` 标记
  未完成，``error`` 携带失败步名与异常信息。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# runner 协议类型：async (spec, task, context) -> output_text
SubagentRunner = Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[str]]


class OrchestratorError(RuntimeError):
    """编排执行失败。"""


@dataclass
class StepResult:
    """单步执行结果。"""

    agent: str
    step: int
    input: str
    output: str
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "step": self.step,
            "input": self.input,
            "output": self.output,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class OrchestratorResult:
    """整条 sequential 链的执行结果。"""

    steps: list[StepResult] = field(default_factory=list)
    final_output: str = ""
    partial: bool = False  # True = 中途失败，未跑完整条链
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "agent": s.agent,
                    "step": s.step,
                    "input": s.input,
                    "output": s.output,
                    "ok": s.ok,
                    "error": s.error,
                }
                for s in self.steps
            ],
            "final_output": self.final_output,
            "partial": self.partial,
            "error": self.error,
        }


@dataclass
class SubagentOrchestrator:
    """子代理串行编排器。

    Args:
        subagents: 编译产物 ``CompiledConfig.subagents`` 中每条形如
            ``{"name": "coder", "description": "...", "system_prompt": "...",
               "model": "auto", "tools": [...]}``。
        runner: 执行单个子代理的协程；None 时使用默认 runner
            （``_default_runner``，依赖 deepagents）。
    """

    subagents: list[dict[str, Any]]
    runner: SubagentRunner | None = None

    def validate(self) -> None:
        """校验 subagent 列表：name 唯一 + 必填字段。"""
        if not self.subagents:
            raise OrchestratorError("subagents 列表为空")
        names: set[str] = set()
        for i, spec in enumerate(self.subagents):
            if not isinstance(spec, dict):
                raise OrchestratorError(f"第 {i} 个 subagent 不是 dict: {spec!r}")
            name = spec.get("name")
            if not name or not isinstance(name, str):
                raise OrchestratorError(f"第 {i} 个 subagent 缺少 name")
            if name in names:
                raise OrchestratorError(f"subagent 名重复: {name}")
            names.add(name)
            if not spec.get("description"):
                # description 缺失会让 task 工具的可用代理列表语义不明
                logger.warning("subagent %s 缺少 description，将用空串兜底", name)

    async def run_sequential(
        self,
        task: str,
        context: dict[str, Any] | None = None,
    ) -> OrchestratorResult:
        """按声明顺序串行执行所有子代理。

        Args:
            task: 第一个子代理接收的输入任务描述。
            context: 透传给每个 runner 的上下文（如 prior_steps、tenant_id）；
                每步执行前会注入 ``prior_steps``（已完成步的结果快照）。

        Returns:
            ``OrchestratorResult``：含每步结果 + 最终输出。
        """
        self.validate()
        ctx: dict[str, Any] = dict(context or {})
        result = OrchestratorResult()
        current_input = task

        for i, spec in enumerate(self.subagents):
            name = spec["name"]
            # 把已完成步的快照注入 context，供下游 runner 引用
            ctx["prior_steps"] = [s.to_dict() if hasattr(s, "to_dict") else s for s in result.steps]
            ctx["current_step"] = i
            try:
                runner = self.runner or _default_runner
                output = await runner(spec, current_input, ctx)
                if not isinstance(output, str):
                    output = str(output)
                result.steps.append(
                    StepResult(
                        agent=name,
                        step=i,
                        input=current_input,
                        output=output,
                    )
                )
                # 链式传递：下一步的输入 = 本步输出
                current_input = output
            except Exception as exc:  # noqa: BLE001
                logger.exception("sequential 编排在第 %d 步 (%s) 失败", i, name)
                result.steps.append(
                    StepResult(
                        agent=name,
                        step=i,
                        input=current_input,
                        output="",
                        ok=False,
                        error=str(exc),
                    )
                )
                result.partial = True
                result.error = f"step {i} ({name}) 失败: {exc}"
                return result

        result.final_output = current_input
        return result


# ---------------------------------------------------------------------- #
# 默认 runner：用 deepagents create_sub_agent 真实执行
# ---------------------------------------------------------------------- #


async def _default_runner(
    spec: dict[str, Any],
    task: str,
    context: dict[str, Any],
) -> str:
    """默认 runner：编译 SubAgent spec 并 ainvoke。

    需要 deepagents + 可用的 LLM 配置；单元测试应注入 fake runner 跳过此路径。
    """
    from deepagents.middleware.subagents import create_sub_agent  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415

    sub_spec: dict[str, Any] = {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "system_prompt": spec.get("system_prompt", ""),
        # model 缺省取全局配置（load_model 同源）
        "model": spec.get("model") or settings.model,
        # sequential 子代理默认不挂工具，仅做 LLM 推理；如需工具由 spec 显式声明
        "tools": list(spec.get("tools", [])),
    }
    runnable = create_sub_agent(sub_spec)
    state = {"messages": [{"role": "user", "content": task}]}
    result = await runnable.ainvoke(state)
    msgs = result.get("messages", []) if isinstance(result, dict) else []
    # 取最后一条非空 AI 文本
    for m in reversed(msgs):
        content = getattr(m, "content", None)
        if content and getattr(m, "type", "") == "ai":
            return content if isinstance(content, str) else str(content)
    return ""


def spec_from_compiled(
    compiled_subagents: list[dict[str, Any]],
    *,
    fallback_model: str | None = None,
) -> list[dict[str, Any]]:
    """把 ``CompiledConfig.subagents`` 归一化为 runner 可用的 spec 列表。

    画布上 ``subagent`` 节点的 ``data.subagents`` 可能是字符串列表（仅名字）
    或 dict 列表；本函数统一成 ``{name, description, system_prompt, model, tools}``。
    """
    out: list[dict[str, Any]] = []
    for raw in compiled_subagents:
        if isinstance(raw, str):
            out.append(
                {
                    "name": raw,
                    "description": f"子代理 {raw}",
                    "system_prompt": "",
                    "model": fallback_model,
                    "tools": [],
                }
            )
        elif isinstance(raw, dict):
            spec = dict(raw)
            spec.setdefault("description", f"子代理 {spec.get('name', '')}")
            spec.setdefault("system_prompt", "")
            spec.setdefault("model", fallback_model)
            spec.setdefault("tools", [])
            out.append(spec)
    return out
