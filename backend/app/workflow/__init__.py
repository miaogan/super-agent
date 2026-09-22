"""V2 Workflow 模块：编译器 + 子代理编排。

节点类型（与前端画布对齐）:
- ``start``     入口节点（接收用户消息）
- ``agent``     LLM 节点（system_prompt + model + tools 子集）
- ``tool``      工具节点（sandbox/skill/memory/task）
- ``subagent``  子代理节点（sequential 串行编排）
- ``end``       出口节点（返回最终回复）

连线 ``edge`` 表示数据流：``{source, target, source_handle?, target_handle?}``
编译器把 DAG 编译为 deepagents ``create_deep_agent`` 的 kwargs JSON。

子代理编排（V2-T4）:
- ``SubagentOrchestrator`` 按声明顺序串行执行 ``CompiledConfig.subagents``，
  每步输出作为下一步输入；与 deepagents 内置 ``task`` 工具（LLM 自主派发）
  互补，适合确定性流水线场景。
"""

from app.workflow.compiler import (
    NODE_TYPES,
    CompileError,
    CompiledConfig,
    compile_workflow,
    validate_definition,
)
from app.workflow.orchestrator import (
    OrchestratorError,
    OrchestratorResult,
    StepResult,
    SubagentOrchestrator,
    SubagentRunner,
    spec_from_compiled,
)
from app.workflow.model_router import (
    DEFAULT_TASK_STRATEGY,
    ModelRouter,
    ModelStrategy,
    default_router_from_env,
)
from app.workflow.evaluator import (
    BatchResult,
    CaseResult,
    EvalError,
    EvalRunner,
    assert_case,
    run_batch,
)
from app.workflow.observability import (
    Span,
    TraceCollector,
    traced_call,
)

__all__ = [
    # 编译器
    "NODE_TYPES",
    "CompileError",
    "CompiledConfig",
    "compile_workflow",
    "validate_definition",
    # 子代理编排
    "OrchestratorError",
    "OrchestratorResult",
    "StepResult",
    "SubagentOrchestrator",
    "SubagentRunner",
    "spec_from_compiled",
    # 模型路由
    "DEFAULT_TASK_STRATEGY",
    "ModelRouter",
    "ModelStrategy",
    "default_router_from_env",
    # 评测
    "BatchResult",
    "CaseResult",
    "EvalError",
    "EvalRunner",
    "assert_case",
    "run_batch",
    # 可观测性
    "Span",
    "TraceCollector",
    "traced_call",
]
