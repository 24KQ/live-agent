"""Phase 14 人机协同路径的启动冻结路由。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from src.config.settings import Settings


class DecisionSupportRoute(StrEnum):
    """控制是否启用受限决策支持，不改变确定性保护动作的执行。"""

    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    DECISION_SUPPORT = "DECISION_SUPPORT"


class DecisionSupportRoutePolicy(BaseModel):
    """应用启动时复制一次路由，防止运行中的配置变化改变会话权限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: DecisionSupportRoute

    @classmethod
    def from_settings(cls, settings: Settings) -> "DecisionSupportRoutePolicy":
        """从 Settings 构造不可变快照；非法值由 Settings/枚举共同拒绝。"""

        return cls(route=DecisionSupportRoute(settings.decision_support_execution_route))
