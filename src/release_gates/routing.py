"""Phase 15 两次 Release 的启动冻结路由状态机。

技术 Runtime、PlanEngine 和 Decision Support 必须作为三个独立快照装配。第一次
Release 使用显式新路径；只有技术结论 PASS 才能生成 VERIFIED_DEFAULTS；Copilot
仍由独立 Promotion 结论控制，不能因技术发布成功自动开启。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.decision_support.routing import DecisionSupportRoute, DecisionSupportRoutePolicy
from src.plan_engine.routing import PlanExecutionPolicy, PlanExecutionRoute
from src.release_gates.decisions import PromotionStatus, TechnicalReleaseDecision, TechnicalReleaseStatus
from src.skill_runtime.routing import RouteConfig, RoutePolicy


class ReleaseRouteMode(StrEnum):
    """Release 路由快照的生命周期。"""

    EXPLICIT_RELEASE = "EXPLICIT_RELEASE"
    VERIFIED_DEFAULTS = "VERIFIED_DEFAULTS"


class ReleaseRoutePromotionError(ValueError):
    """Release 结论不足以生成新默认路由时抛出的稳定错误。"""


class ReleaseRouteProfile(BaseModel):
    """绑定一次 Release 身份的三路不可变配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_run_id: str = Field(..., min_length=1)
    mode: ReleaseRouteMode
    skill_policy: RoutePolicy
    plan_policy: PlanExecutionPolicy
    decision_support_policy: DecisionSupportRoutePolicy
    technical_status: TechnicalReleaseStatus | None = None
    promotion_status: PromotionStatus

    @model_validator(mode="after")
    def _check_profile_invariants(self) -> "ReleaseRouteProfile":
        """防止技术状态、Copilot 状态和实际路由形成自相矛盾快照。"""

        if self.mode is ReleaseRouteMode.VERIFIED_DEFAULTS and self.technical_status is not TechnicalReleaseStatus.PASS:
            raise ReleaseRoutePromotionError("technical release must PASS before default promotion")
        expected_support = (
            DecisionSupportRoute.DECISION_SUPPORT
            if self.promotion_status is PromotionStatus.PROMOTE
            else DecisionSupportRoute.DETERMINISTIC_ONLY
        )
        if self.decision_support_policy.route is not expected_support:
            raise ReleaseRoutePromotionError("Decision Support route does not match Promotion status")
        if self.skill_policy != RoutePolicy(
            batch1=RouteConfig.SKILL_RUNTIME,
            batch2=RouteConfig.SKILL_RUNTIME,
            batch3=RouteConfig.SKILL_RUNTIME,
        ):
            raise ReleaseRoutePromotionError("Release profile must use SKILL_RUNTIME for all batches")
        if self.plan_policy.route is not PlanExecutionRoute.PLAN_ENGINE:
            raise ReleaseRoutePromotionError("Release profile must use PLAN_ENGINE")
        return self


def _build_profile(
    *,
    release_run_id: str,
    mode: ReleaseRouteMode,
    promotion_status: PromotionStatus,
    technical_status: TechnicalReleaseStatus | None,
) -> ReleaseRouteProfile:
    """用统一构造逻辑生成第一次和第二次 Release 的冻结 profile。"""

    decision_route = (
        DecisionSupportRoute.DECISION_SUPPORT
        if promotion_status is PromotionStatus.PROMOTE
        else DecisionSupportRoute.DETERMINISTIC_ONLY
    )
    return ReleaseRouteProfile(
        release_run_id=release_run_id,
        mode=mode,
        skill_policy=RoutePolicy(
            batch1=RouteConfig.SKILL_RUNTIME,
            batch2=RouteConfig.SKILL_RUNTIME,
            batch3=RouteConfig.SKILL_RUNTIME,
        ),
        plan_policy=PlanExecutionPolicy(route=PlanExecutionRoute.PLAN_ENGINE),
        decision_support_policy=DecisionSupportRoutePolicy(route=decision_route),
        technical_status=technical_status,
        promotion_status=promotion_status,
    )


def build_explicit_release_profile(
    *,
    release_run_id: str,
    promotion_status: PromotionStatus,
) -> ReleaseRouteProfile:
    """构造第一次显式 Release profile，不宣称技术门禁已经通过。"""

    return _build_profile(
        release_run_id=release_run_id,
        mode=ReleaseRouteMode.EXPLICIT_RELEASE,
        promotion_status=promotion_status,
        technical_status=None,
    )


def build_verified_default_profile(
    *,
    technical: TechnicalReleaseDecision,
    promotion_status: PromotionStatus,
) -> ReleaseRouteProfile:
    """技术 PASS 后生成第二次 Release 使用的已验证默认 profile。"""

    if technical.status is not TechnicalReleaseStatus.PASS:
        raise ReleaseRoutePromotionError("technical release must PASS before default promotion")
    return _build_profile(
        release_run_id=f"{technical.release_run_id}:verified-defaults",
        mode=ReleaseRouteMode.VERIFIED_DEFAULTS,
        promotion_status=promotion_status,
        technical_status=technical.status,
    )


__all__ = [
    "ReleaseRouteMode",
    "ReleaseRouteProfile",
    "ReleaseRoutePromotionError",
    "build_explicit_release_profile",
    "build_verified_default_profile",
]
