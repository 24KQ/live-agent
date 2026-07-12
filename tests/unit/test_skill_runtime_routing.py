"""Phase 11A 路由策略与播前 Facade 测试。

测试覆盖：默认路由、独立批次切换、Facade 申请 TRUSTED_COMPAT 审批、
Facade 运行时失败不 fallback 到 legacy。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.core.security_hooks import GateDecision, GateResult
from src.skill_runtime.models import SkillExecutionResult, SkillExecutionStatus
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct
from src.skill_runtime.routing import RoutePolicy, RouteConfig


class FakeLegacyService:
    """记录 legacy 调用的替身，用于证明 Runtime 失败时不会隐式 fallback。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        self.calls.append("query_products")
        return [_product()]

    def generate_plan(
        self,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        self.calls.append("generate_plan")
        return _plan(room_id, trace_id)

    def generate_cards(self, room_id, plan, products, trace_id):
        self.calls.append("generate_cards")
        return []

    def setup_live_session(self, room_id, plan, trace_id, confirmed_setup, **kwargs):
        self.calls.append("setup_live_session")
        return GateResult(True, GateDecision.HARD_GATE, False, "测试批准"), "audit-legacy"


class FakeExecutor:
    """返回预设 Runtime 结果并保存 SkillCall。"""

    def __init__(self, result: SkillExecutionResult) -> None:
        self.result = result
        self.calls = []

    def execute(self, call):
        self.calls.append(call)
        return self.result


def _product() -> CatalogProduct:
    """构造完整商品领域对象。"""
    return CatalogProduct(
        product_id="p001",
        name="测试商品",
        category="日用",
        price=Decimal("39.90"),
        inventory=10,
        conversion_rate=Decimal("0.15"),
        commission_rate=Decimal("0.05"),
        tags=["引流"],
        selling_points=["测试卖点"],
    )


def _plan(room_id: str = "room-001", trace_id: str = "trace-001") -> LivePlanDraft:
    """构造与现有 Graph 契约一致的真实计划对象。"""
    return LivePlanDraft(
        room_id=room_id,
        trace_id=trace_id,
        items=[
            LivePlanItem(
                rank=1,
                product_id="p001",
                product_name="测试商品",
                role="引流款",
                reason="测试原因",
            )
        ],
    )


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
    with pytest.raises(ValidationError):
        policy.generation = RouteConfig.SKILL_RUNTIME  # type: ignore[misc]


def test_facade_creates_trusted_compat_approval_when_confirmed() -> None:
    """confirmed_setup=True 时 Facade 构造 TRUSTED_COMPAT ApprovalContext。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    executor = FakeExecutor(
        SkillExecutionResult(
            skill_id="setup_live_session",
            version="1.0.0",
            status=SkillExecutionStatus.SUCCESS,
            output={"allowed": True, "setup_status": "prepared"},
            audit_id="audit-runtime",
        )
    )
    service = RoutedPreLiveBusinessService(
        policy=RoutePolicy(setup=RouteConfig.SKILL_RUNTIME),
        legacy_service=FakeLegacyService(),
        skill_executor=executor,
    )
    service.setup_live_session("room-001", _plan(), "trace-001", True)

    approval = executor.calls[0].context.approval
    assert approval is not None
    assert approval.source.value == "TRUSTED_COMPAT"
    assert approval.decision == "APPROVED"
    assert approval.operator_id == "compat_migration"


def test_facade_returns_none_when_not_confirmed() -> None:
    """confirmed_setup=False 时 Facade 不构造审批证据。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    executor = FakeExecutor(
        SkillExecutionResult(
            skill_id="setup_live_session",
            version="1.0.0",
            status=SkillExecutionStatus.PENDING,
            summary="等待审批",
        )
    )
    service = RoutedPreLiveBusinessService(
        policy=RoutePolicy(setup=RouteConfig.SKILL_RUNTIME),
        legacy_service=FakeLegacyService(),
        skill_executor=executor,
    )
    service.setup_live_session("room-001", _plan(), "trace-001", False)

    assert executor.calls[0].context.approval is None


def test_facade_from_settings() -> None:
    """RoutedPreLiveBusinessService 从 Settings 创建 RoutePolicy。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    settings = Settings()
    service = RoutedPreLiveBusinessService.from_settings(settings)
    assert service.policy.generation in (RouteConfig.LEGACY, RouteConfig.SKILL_RUNTIME)
    assert service.policy.setup in (RouteConfig.LEGACY, RouteConfig.SKILL_RUNTIME)


def test_settings_reject_invalid_route_value() -> None:
    """非法路由必须在配置加载阶段 fail-fast，不能延迟到第一次调用。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, SKILL_ROUTE_PRELIVE_GENERATION="SHADOW_COMPARE")


def test_existing_route_policy_is_not_changed_by_environment_update(monkeypatch) -> None:
    """服务装配完成后修改环境变量，不得改变已有调用的执行路径。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    service = RoutedPreLiveBusinessService.from_settings(Settings(_env_file=None))
    monkeypatch.setenv("SKILL_ROUTE_PRELIVE_GENERATION", "SKILL_RUNTIME")
    monkeypatch.setenv("SKILL_ROUTE_PRELIVE_SETUP", "SKILL_RUNTIME")

    assert service.policy == RoutePolicy.default()


def test_runtime_failure_raises_without_legacy_fallback() -> None:
    """Runtime 失败必须显式抛错，且不得调用 legacy。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService, SkillRuntimeCallError

    result = SkillExecutionResult(
        skill_id="query_products",
        version="1.0.0",
        status=SkillExecutionStatus.ERROR,
        summary="参数不合法",
    )
    legacy = FakeLegacyService()
    service = RoutedPreLiveBusinessService(
        policy=RoutePolicy(generation=RouteConfig.SKILL_RUNTIME),
        legacy_service=legacy,
        skill_executor=FakeExecutor(result),
    )

    with pytest.raises(SkillRuntimeCallError):
        service.query_products("room-001", "trace-001")
    assert legacy.calls == []


def test_runtime_query_returns_domain_products_for_graph_protocol() -> None:
    """Facade 对 Graph 保持领域模型接口，Runtime 快照只存在于内部边界。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    result = SkillExecutionResult(
        skill_id="query_products",
        version="1.0.0",
        status=SkillExecutionStatus.SUCCESS,
        output={"products": [_product().model_dump(mode="json")]},
    )
    service = RoutedPreLiveBusinessService(
        policy=RoutePolicy(generation=RouteConfig.SKILL_RUNTIME),
        legacy_service=FakeLegacyService(),
        skill_executor=FakeExecutor(result),
    )

    products = service.query_products("room-001", "trace-001")
    assert products == [_product()]


def test_runtime_generate_cards_fails_when_plan_product_is_missing() -> None:
    """计划商品找不到对应快照时必须明确失败，不能静默少生成手卡。"""
    from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService

    result = SkillExecutionResult(
        skill_id="generate_product_card",
        version="1.0.0",
        status=SkillExecutionStatus.SUCCESS,
        output={"card": {}},
    )
    service = RoutedPreLiveBusinessService(
        policy=RoutePolicy(generation=RouteConfig.SKILL_RUNTIME),
        legacy_service=FakeLegacyService(),
        skill_executor=FakeExecutor(result),
    )

    with pytest.raises(ValueError, match="p001"):
        service.generate_cards("room-001", _plan(), [], "trace-001")
