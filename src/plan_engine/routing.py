"""Phase 12A 播前手卡 PlanEngine 的启动冻结路由。

该路由独立于 Phase 11B Skill 批次路由：前者决定播前 Graph 的手卡节点是否交给
确定性 DAG Runtime，后者只决定单个 Skill 走 Legacy 还是统一 Executor。策略对象在
应用装配时复制 Settings 值，运行期间不再读取环境变量或可变配置。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanExecutionRoute(StrEnum):
    """播前手卡节点允许的两条互斥执行路径。"""

    LEGACY = "LEGACY"
    PLAN_ENGINE = "PLAN_ENGINE"


class PlanExecutionPolicy(BaseModel):
    """应用启动时冻结的手卡执行策略。

    本对象只保存一个枚举值，不引用 Settings 实例。即使测试或管理代码随后修改
    Settings，已经编译的 Graph 仍保持原路由，避免同一次调用中途切换执行器。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: PlanExecutionRoute = Field(default=PlanExecutionRoute.LEGACY)

    @classmethod
    def from_settings(cls, settings: Any) -> "PlanExecutionPolicy":
        """从已校验 Settings 复制路由值，未知值由 Pydantic fail-fast 拒绝。"""
        release_profile = getattr(settings, "phase15_route_profile", "LEGACY_DEFAULT")
        if release_profile in {"EXPLICIT_RELEASE", "VERIFIED_DEFAULTS"}:
            return cls(route=PlanExecutionRoute.PLAN_ENGINE)
        return cls(route=settings.plan_engine_card_execution_route)

    @classmethod
    def default(cls) -> "PlanExecutionPolicy":
        """返回 fail-safe 的 Legacy 默认策略。"""
        return cls()


__all__ = ["PlanExecutionPolicy", "PlanExecutionRoute"]
