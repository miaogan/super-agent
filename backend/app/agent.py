"""组装 deep agent：deepagents + OpenSandbox 后端 + PostgreSQL 持久化。

- 多轮对话：checkpointer（AsyncPostgresSaver）按 thread_id 保存完整状态
- 长期记忆：store（AsyncPostgresStore）+ 记忆注入 middleware + 记忆工具
- 沙箱执行：backend（OpenSandboxBackend）提供 execute + 全套文件工具
"""

from __future__ import annotations

import logging

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel

from app.config import settings
from app.memory import MemoryInjectionMiddleware, create_memory_tools
from app.sandbox_backend import OpenSandboxBackend

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个强大的深度智能体（Deep Agent），运行在隔离沙箱之上。

## 能力
- **多轮对话**：当前会话（thread）内的全部上下文自动保持，无需用户重复背景。
- **长期记忆**：跨会话的用户事实通过 `manage_memory`（写入）/ `search_memory`（检索）
  管理；用户偏好和背景会在每次回复前自动注入，善用它们提供个性化回答。
- **沙箱执行**：`execute` 工具在隔离的 OpenSandbox 容器中执行任意 shell 命令
  （Python 脚本、系统命令、pip 安装等），命令输出与退出码会完整返回。
- **文件操作**：`ls` / `read_file` / `write_file` / `edit_file` / `glob` / `grep`
  直接作用于沙箱文件系统。
- **子代理**：复杂任务用 `task` 工具派发子代理（{subagent_names}）分工处理，
  多个独立子任务可分别派发给不同子代理。
- **任务规划**：多步骤任务先用 `write_todos` 制定计划，随进展更新。

## 工作准则
1. 涉及代码/数据处理的任务：先写文件（write_file），再执行（execute）验证，
   把真实运行结果反馈给用户，不要凭空猜测输出。
2. 长期信息（用户偏好、项目背景、关键决定）主动调用 `manage_memory` 保存。
3. 用户提到的事实与本会话历史冲突时，以最近一次说明为准。
4. 回答使用用户的语言（中文问题用中文回答）。
"""

SUBAGENTS = [
    {
        "name": "coder",
        "description": (
            "编写并运行代码的子代理。适合需要编写脚本、执行验证、"
            "调试排错、把想法快速落成可运行代码的子任务。"
        ),
        "system_prompt": (
            "你是资深工程师。工作流：write_file 写代码 → execute 运行验证 → "
            "出错就读输出修复重试（最多 3 轮）→ 返回代码与真实运行结果摘要。"
            "优先写单文件、自包含、可立即运行的脚本。"
        ),
    },
    {
        "name": "data-analyst",
        "description": (
            "数据分析子代理。适合统计分析、数据清洗、图表生成、"
            "CSV/JSON 数据探索类子任务。"
        ),
        "system_prompt": (
            "你是数据分析师。需要第三方库时先 execute 'pip install pandas' 之类安装；"
            "分析前先用 read_file/ls 查看数据结构；"
            "结论必须给出关键数字与计算过程，禁止编造数据。"
        ),
    },
    {
        "name": "researcher",
        "description": (
            "资料调研子代理。适合需要查阅沙箱文件、整理多方信息、"
            "对比归纳、写综述摘要的子任务。"
        ),
        "system_prompt": (
            "你是严谨的调研助理。用 ls/glob/grep/read_file 收集材料，"
            "交叉验证后归纳结论，明确区分「事实」与「推测」，并注明信息缺口。"
        ),
    },
    {
        "name": "reviewer",
        "description": (
            "质量复核子代理。适合对代码、方案或分析结果做审查、找漏洞、"
            "提改进建议的子任务。"
        ),
        "system_prompt": (
            "你是挑剔的技术评审。审查给定代码/结论时：先自行 execute 验证可疑之处，"
            "按「严重 / 建议 / 可选」三级输出问题清单，每条附理由与修改建议。"
        ),
    },
]

SUBAGENT_NAMES = ", ".join(f"`{s['name']}`" for s in SUBAGENTS)
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{subagent_names}", SUBAGENT_NAMES)


def load_model() -> str | BaseChatModel:
    """加载 LLM：
    - 配置了 OPENAI_BASE_URL：用 ChatOpenAI 指向兼容端点（火山方舟 ARK / DeepSeek / vLLM 等）
    - 否则：直接传 "provider:model" 字符串，由 init_chat_model 解析
    """
    if settings.openai_base_url:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.model,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )
    return settings.model


def build_agent(
    *,
    backend: OpenSandboxBackend,
    checkpointer=None,
    store=None,
    user_id: str | None = None,
    model: str | BaseChatModel | None = None,
    skills_dirs: list[str] | None = None,
    subagents: list[dict] | None = None,
    system_prompt: str | None = None,
):
    """组装 deep agent。

    Args:
        backend: OpenSandbox 沙箱后端。
        checkpointer: LangGraph Checkpointer（多轮对话）；None 则不持久化。
        store: LangGraph BaseStore（长期记忆）；None 则记忆工具不可用。
        user_id: 长期记忆归属用户（默认取配置）。
        model: 覆盖默认模型（测试注入 fake 模型用）。
        skills_dirs: Skill 目录列表（V1：global + tenant 隔离目录）；空则不加载 skill。
        subagents: V2 子代理列表（来自 CompiledConfig.subagents）；None 时用内置
            ``SUBAGENTS``。每条需含 ``name`` + ``description``，可选 ``system_prompt``
            / ``model`` / ``tools``。传入后主 Agent 通过 deepagents 内置 ``task``
            工具由 LLM 自主派发（与 ``SubagentOrchestrator`` 的确定性串行互补）。
        system_prompt: V2 覆盖系统提示（来自 CompiledConfig.system_prompt）；
            None 时用内置 ``SYSTEM_PROMPT``。
    """
    user_id = user_id or settings.user_id
    tools = create_memory_tools(user_id, store) if store is not None else []
    kwargs: dict = dict(
        model=model or load_model(),
        system_prompt=system_prompt or SYSTEM_PROMPT,
        tools=tools,
        backend=backend,
        middleware=[
            TodoListMiddleware(),
            MemoryInjectionMiddleware(user_id=user_id),
        ],
        subagents=subagents if subagents is not None else SUBAGENTS,
        checkpointer=checkpointer,
        store=store,
    )
    # V1：传入 skills 目录，deepagents 自动扫描 SKILL.md 注入能力
    if skills_dirs:
        kwargs["skills"] = skills_dirs
    return create_deep_agent(**kwargs)
