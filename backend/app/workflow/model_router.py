"""V2-T6 模型路由：auto / cost / quality / balanced 四策略 + 任务类型映射。

当 ``CompiledConfig.model == "auto"`` 时，由 ``ModelRouter`` 根据策略 + 任务
类型解析到具体模型。非 ``auto`` 的 model 透传（用户在画布上显式指定的优先）。

四策略
------
- ``AUTO``      默认：按任务类型查表，缺省用 default_model
- ``COST``      总用最便宜（适合大批量、低风险任务）
- ``QUALITY``   总用最强（适合关键决策、复杂推理）
- ``BALANCED``   平衡（性价比）

任务类型映射
------------
画布上 ``agent`` 节点的 ``data.task_type`` 可选标注（如 ``coding`` / ``summary``
/ ``review``），路由器据此选策略。例如 ``coding → quality``、``summary → cost``。

设计要点
--------
- 纯逻辑模块，不依赖 deepagents / LLM，可独立单元测试。
- ``resolve()`` 幂等、无副作用，便于在 build_agent / orchestrate 入口统一调用。
- 默认路由表从环境变量读，便于不同部署环境切换模型池。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ModelStrategy(str, Enum):
    """模型选择策略。"""

    AUTO = "auto"
    COST = "cost"
    QUALITY = "quality"
    BALANCED = "balanced"

    @classmethod
    def parse(cls, raw: str | None) -> "ModelStrategy":
        if not raw:
            return cls.AUTO
        try:
            return cls(raw.lower().strip())
        except ValueError:
            logger.warning("未知模型策略 %r，回退 AUTO", raw)
            return cls.AUTO


# 任务类型 → 默认策略映射（用户可覆盖）
DEFAULT_TASK_STRATEGY: dict[str, ModelStrategy] = {
    "coding": ModelStrategy.QUALITY,      # 写代码要强模型
    "review": ModelStrategy.QUALITY,      # 复核要强模型
    "reasoning": ModelStrategy.QUALITY,    # 复杂推理
    "summary": ModelStrategy.COST,        # 总结用便宜模型
    "chat": ModelStrategy.BALANCED,        # 日常对话平衡
    "data": ModelStrategy.BALANCED,        # 数据分析平衡
}


@dataclass
class ModelRouter:
    """模型路由器。

    Args:
        default_model: AUTO 策略兜底模型（也即全站默认）。
        cost_model: COST 策略用的便宜模型。
        quality_model: QUALITY 策略用的强模型。
        balanced_model: BALANCED 策略用的平衡模型。
        task_strategy_map: 任务类型 → 策略映射；覆盖默认表。
    """

    default_model: str
    cost_model: str = ""
    quality_model: str = ""
    balanced_model: str = ""
    task_strategy_map: dict[str, ModelStrategy] = field(
        default_factory=lambda: dict(DEFAULT_TASK_STRATEGY)
    )

    def __post_init__(self) -> None:
        # 缺省字段用 default_model 兜底，避免空串
        if not self.cost_model:
            self.cost_model = self.default_model
        if not self.quality_model:
            self.quality_model = self.default_model
        if not self.balanced_model:
            self.balanced_model = self.default_model

    def resolve(
        self,
        model_spec: str | None,
        task_type: str | None = None,
        strategy: ModelStrategy | str | None = None,
    ) -> str:
        """解析最终模型。

        Args:
            model_spec: ``CompiledConfig.model`` 或 agent 节点 data.model。
                非 ``auto`` 且非空 → 直接透传（用户显式指定优先）。
            task_type: agent 节点 data.task_type（可选）。
            strategy: 强制策略（覆盖 task 映射）；None 时按 task_type 查表。

        Returns:
            具体模型字符串（如 ``"openai:gpt-4o-mini"``）。
        """
        # 1. 用户在画布上显式指定了具体模型 → 透传
        if model_spec and model_spec.strip().lower() != "auto":
            return model_spec.strip()

        # 2. 解析策略
        if strategy is None:
            strat = ModelStrategy.AUTO
        else:
            strat = (
                strategy
                if isinstance(strategy, ModelStrategy)
                else ModelStrategy.parse(str(strategy))
            )

        # AUTO 策略：按任务类型查表
        if strat == ModelStrategy.AUTO and task_type:
            strat = self.task_strategy_map.get(
                task_type.strip().lower(), ModelStrategy.AUTO
            )

        return self._by_strategy(strat)

    def _by_strategy(self, strategy: ModelStrategy) -> str:
        if strategy == ModelStrategy.COST:
            return self.cost_model
        if strategy == ModelStrategy.QUALITY:
            return self.quality_model
        if strategy == ModelStrategy.BALANCED:
            return self.balanced_model
        return self.default_model


def default_router_from_env() -> ModelRouter:
    """从环境变量构造默认路由器（便于不同部署切换模型池）。

    环境变量：
    - ``MODEL_DEFAULT``  兜底模型（缺省取 settings.model）
    - ``MODEL_COST``     便宜模型
    - ``MODEL_QUALITY``  强模型
    - ``MODEL_BALANCED`` 平衡模型
    """
    from app.config import settings  # noqa: PLC0415

    return ModelRouter(
        default_model=os.getenv("MODEL_DEFAULT", settings.model),
        cost_model=os.getenv("MODEL_COST", ""),
        quality_model=os.getenv("MODEL_QUALITY", ""),
        balanced_model=os.getenv("MODEL_BALANCED", ""),
    )
