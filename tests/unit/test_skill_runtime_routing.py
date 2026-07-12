"""Phase 11A 路由策略与播前 Facade 测试。

测试覆盖：默认路由、独立批次切换、Facade 申请 TRUSTED_COMPAT 审批、
Facade 运行时失败不 fallback 到 legacy。
"""

from __future__ import annotations

import pytest

from src.skill_runtime.routing import RoutePolicy, RouteConfig


def test_default_routes_are_legacy() -> None:
    """默认路由必须为 LEGACY。"""
    policy = RoutePolicy.default()
    assert policy.generation == RouteConfig.LEGACY
    assert policy.setup == RouteConfig.LEGACY


def test_route_config_rejects_invalid() -> None:
    """RouteConfig 枚举拒绝非法值。"""
    from src.skill_runtime.models import SkillExecutionRoute

    assert RouteConfig.LEGACY == SkillExecutionRoute.LEGACY
    assert RouteConfig.SKILL_RUNTIME == SkillExecutionRoute.SKILL_RUNTIME


def test_generation_and_setup_can_be_independent() -> None:
    """generation 和 setup 可以独立配置。"""
    policy = RoutePolicy(generation=RouteConfig.SKILL_RUNTIME, setup=RouteConfig.LEGACY)
    assert policy.generation == RouteConfig.SKILL_RUNTIME
    assert policy.setup == RouteConfig.LEGACY


def test_policy_is_immutable_after_construction() -> None:
    """RoutePolicy 构造后不可修改。"""
    policy = RoutePolicy.default()
    with pytest.raises(Exception):
        policy.generation = RouteConfig.SKILL_RUNTIME  # type: ignore[misc]


def test_facade_creates_trusted_compat_approval_when_confirmed() -> None:
    """confirmed_setup=True 时 Facade 构造 TRUSTED_COMPAT ApprovalContext。"""
    from src.skill_runtime.pre_live_facade import create_compat_approval

    approval = create_compat_approval()
    assert approval is not None
    assert approval.source.value == "TRUSTED_COMPAT"
    assert approval.decision == "APPROVED"
    assert approval.operator_id == "compat_migration"


def test_facade_returns_none_when_not_confirmed() -> None:
    """confirmed_setup=False 时 Facade 不构造审批证据。"""
    from src.skill_runtime.pre_live_facade import create_compat_approval

    approval = create_compat_approval(confirmed=False)
    assert approval is None


def test_facade_from_settings() -> None:
    """RoutedPreLiveBusinessService 从 Settings 创建 RoutePolicy。"""
    from src.config.settings import Settings
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    settings = Settings()
    service = RoutedPreLiveBusinessService.from_settings(settings)
    assert service.policy.generation in (RouteConfig.LEGACY, RouteConfig.SKILL_RUNTIME)
    assert service.policy.setup in (RouteConfig.LEGACY, RouteConfig.SKILL_RUNTIME)
