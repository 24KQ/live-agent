"""Phase 14 人机协同路径的启动冻结路由。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from src.config.settings import Settings
from src.release_gates.decisions import PromotionStatus


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
        release_profile = getattr(settings, "phase15_route_profile", "LEGACY_DEFAULT")
        if release_profile in {"EXPLICIT_RELEASE", "VERIFIED_DEFAULTS"}:
            promotion = PromotionStatus(
                getattr(settings, "phase15_decision_support_promotion", "BLOCKED")
            )
            requested = DecisionSupportRoute(settings.decision_support_execution_route)
            expected = (
                DecisionSupportRoute.DECISION_SUPPORT
                if promotion is PromotionStatus.PROMOTE
                else DecisionSupportRoute.DETERMINISTIC_ONLY
            )
            # Release profile 下禁止用独立环境变量伪造 Copilot Promotion；它必须与
            # 已持久化的 Promotion 状态一致，避免技术默认晋升顺便打开经营建议。
            if requested is DecisionSupportRoute.DECISION_SUPPORT and expected is not requested:
                raise ValueError("DECISION_SUPPORT requires Phase 15 Promotion PROMOTE")
            return cls(route=expected)
        return cls(route=DecisionSupportRoute(settings.decision_support_execution_route))
