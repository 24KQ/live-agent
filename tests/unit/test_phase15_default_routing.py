"""Phase 15 Task 11 显式 Release 与默认路由晋升的 TDD 契约。

测试固定两次 Release 的安全边界：第一次只能通过显式新 Runtime profile，第二次
只有技术结论 PASS 才能生成新默认；Copilot 是否开启始终由独立 Promotion 结论决定。
本文件不连接 PostgreSQL、GitHub 或真实模型。
"""

from __future__ import annotations

import pytest

from src.config.settings import Settings
from src.decision_support.routing import DecisionSupportRoute, DecisionSupportRoutePolicy
from src.plan_engine.routing import PlanExecutionPolicy, PlanExecutionRoute
from src.release_gates.decisions import PromotionStatus, TechnicalReleaseDecision, TechnicalReleaseStatus
from src.release_gates.routing import (
    ReleaseRouteMode,
    ReleaseRoutePromotionError,
    build_explicit_release_profile,
    build_verified_default_profile,
)
from src.skill_runtime.routing import RouteConfig
from src.skill_runtime.routing import RoutePolicy


def _technical(status: TechnicalReleaseStatus) -> TechnicalReleaseDecision:
    """构造最小技术结论，保持计数和状态符合 Release 模型不变量。"""

    complete = status is TechnicalReleaseStatus.PASS
    return TechnicalReleaseDecision(
        release_run_id="phase15-release-task11",
        status=status,
        expected_case_count=48,
        completed_case_count=48,
        passed_case_count=48 if complete else 0,
        failed_case_count=0 if complete or status is TechnicalReleaseStatus.BLOCKED else 48,
        blocked_case_count=48 if status is TechnicalReleaseStatus.BLOCKED else 0,
        severe_violation_count=0,
        blocking_gate_count=0 if complete else 1,
        case_results_digest="a" * 64,
        reason_codes=() if complete else ("EXTERNAL_EVIDENCE_MISSING",),
    )


def test_explicit_release_forces_new_runtime_but_keeps_copilot_independent() -> None:
    """第一次 Release 不依赖默认值，且缺少 Promotion 时 Copilot 仍关闭。"""

    profile = build_explicit_release_profile(
        release_run_id="phase15-explicit-v1.0.0-rc1",
        promotion_status=PromotionStatus.BLOCKED,
    )

    assert profile.mode is ReleaseRouteMode.EXPLICIT_RELEASE
    assert profile.skill_policy.batch1 is RouteConfig.SKILL_RUNTIME
    assert profile.skill_policy.batch2 is RouteConfig.SKILL_RUNTIME
    assert profile.skill_policy.batch3 is RouteConfig.SKILL_RUNTIME
    assert profile.plan_policy.route is PlanExecutionRoute.PLAN_ENGINE
    assert profile.decision_support_policy.route is DecisionSupportRoute.DETERMINISTIC_ONLY


def test_verified_defaults_require_technical_pass_and_promotion_only_controls_copilot() -> None:
    """技术 PASS 后切换确定性默认；Promotion 非 PROMOTE 不能开启 Copilot。"""

    disabled = build_verified_default_profile(
        technical=_technical(TechnicalReleaseStatus.PASS),
        promotion_status=PromotionStatus.KEEP_DISABLED,
    )
    promoted = build_verified_default_profile(
        technical=_technical(TechnicalReleaseStatus.PASS),
        promotion_status=PromotionStatus.PROMOTE,
    )

    assert disabled.mode is ReleaseRouteMode.VERIFIED_DEFAULTS
    assert disabled.skill_policy.batch1 is RouteConfig.SKILL_RUNTIME
    assert disabled.plan_policy.route is PlanExecutionRoute.PLAN_ENGINE
    assert disabled.decision_support_policy.route is DecisionSupportRoute.DETERMINISTIC_ONLY
    assert promoted.decision_support_policy.route is DecisionSupportRoute.DECISION_SUPPORT


def test_verified_defaults_reject_technical_fail_or_blocked() -> None:
    """技术失败或外部证据 BLOCKED 时不能生成新默认路由。"""

    for status in (TechnicalReleaseStatus.FAIL, TechnicalReleaseStatus.BLOCKED):
        with pytest.raises(ReleaseRoutePromotionError, match="technical release must PASS"):
            build_verified_default_profile(
                technical=_technical(status),
                promotion_status=PromotionStatus.PROMOTE,
            )


def test_settings_default_is_legacy_and_profile_is_startup_frozen() -> None:
    """没有发布晋升证据时 Settings 仍是 Legacy，运行中修改不能影响已冻结策略。"""

    settings = Settings(_env_file=None)
    assert settings.phase15_route_profile == "LEGACY_DEFAULT"
    assert settings.phase15_decision_support_promotion == "BLOCKED"

    with pytest.raises(ReleaseRoutePromotionError, match="technical release must PASS"):
        build_verified_default_profile(
            technical=_technical(TechnicalReleaseStatus.BLOCKED),
            promotion_status=PromotionStatus.BLOCKED,
        )


def test_settings_release_profile_resolves_all_three_frozen_route_policies() -> None:
    """部署 profile 必须同时驱动三类路由，且 Promotion 不能被独立字段伪造。"""

    explicit = Settings(_env_file=None, PHASE15_ROUTE_PROFILE="EXPLICIT_RELEASE")
    assert RoutePolicy.from_settings(explicit).batch1 is RouteConfig.SKILL_RUNTIME
    assert RoutePolicy.from_settings(explicit).batch2 is RouteConfig.SKILL_RUNTIME
    assert PlanExecutionPolicy.from_settings(explicit).route is PlanExecutionRoute.PLAN_ENGINE
    assert DecisionSupportRoutePolicy.from_settings(explicit).route is DecisionSupportRoute.DETERMINISTIC_ONLY

    promoted = Settings(
        _env_file=None,
        PHASE15_ROUTE_PROFILE="VERIFIED_DEFAULTS",
        PHASE15_DECISION_SUPPORT_PROMOTION="PROMOTE",
    )
    assert DecisionSupportRoutePolicy.from_settings(promoted).route is DecisionSupportRoute.DECISION_SUPPORT

    forged = Settings(
        _env_file=None,
        PHASE15_ROUTE_PROFILE="VERIFIED_DEFAULTS",
        DECISION_SUPPORT_EXECUTION_ROUTE="DECISION_SUPPORT",
        PHASE15_DECISION_SUPPORT_PROMOTION="BLOCKED",
    )
    with pytest.raises(ValueError, match="Promotion PROMOTE"):
        DecisionSupportRoutePolicy.from_settings(forged)
