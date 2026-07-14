"""Phase 11B 三批启动冻结路由测试。

这些测试只验证装配期路由事实：配置如何映射到不可变 RoutePolicy，以及
AgentToolExecutor 是否按 Skill 批次选择 Legacy 或 Runtime。测试不引入真实
平台、数据库或重试逻辑，避免把 Phase 11B 的路由边界扩展到后续任务。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.config.tool_registry import get_default_tool_registry
from src.core.agent_tool_executor import AgentToolExecutor
from src.core.security_hooks import GateDecision, GateResult
from src.skill_runtime.models import (
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillExecutionResult,
    SkillExecutionStatus,
    SkillErrorCode,
)
from src.skill_runtime.routing import RouteConfig, RoutePolicy, skill_batch_for
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str = "p001") -> CatalogProduct:
    """构造完整商品，供 legacy query/plan 和兼容 Runtime 输入补全使用。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"测试商品-{product_id}",
        category="日用",
        price=Decimal("39.90"),
        inventory=100,
        conversion_rate=Decimal("0.15"),
        commission_rate=Decimal("0.05"),
        tags=["引流"],
        selling_points=["耐用"],
        is_active=True,
    )


class RecordingLegacyService:
    """记录 legacy 服务调用，证明 Runtime 路由失败时不会回退执行旧路径。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.products = [_product("p001"), _product("p002")]

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """模拟旧播前货盘查询。"""
        self.calls.append(("query_products", room_id, trace_id))
        return self.products

    def generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str):
        """模拟旧排品生成，并记录是否被 fallback 调用。"""
        from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem

        self.calls.append(("generate_plan", room_id, trace_id))
        return LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product.product_id,
                    product_name=product.name,
                    role="引流款",
                    reason="legacy plan",
                )
                for index, product in enumerate(products, start=1)
            ],
        )

    def generate_card(self, room_id: str, product: CatalogProduct, trace_id: str):
        """模拟旧单商品手卡入口。"""
        from src.skills.product_card_generator import ProductCard

        self.calls.append(("generate_card", room_id, trace_id, product.product_id))
        return ProductCard(
            product_id=product.product_id,
            title=f"{product.name}手卡",
            talking_points=["卖点1"],
            opening_script="开场",
            price_hint="价格提示",
            risk_tips=[],
        )

    def setup_live_session(
        self,
        room_id: str,
        plan: Any,
        trace_id: str,
        confirmed_setup: bool,
        **kwargs: Any,
    ):
        """模拟旧建播入口。"""
        self.calls.append(("setup_live_session", room_id, trace_id, confirmed_setup))
        return GateResult(
            allowed=confirmed_setup,
            decision=GateDecision.HARD_GATE,
            requires_confirmation=not confirmed_setup,
            reason="legacy setup",
        ), ("audit-legacy" if confirmed_setup else None)


class RecordingRuntimeExecutor:
    """记录 Runtime SkillCall，并返回预设结果。"""

    def __init__(self, result: SkillExecutionResult) -> None:
        self.result = result
        self.calls: list[Any] = []

    def execute(self, call: Any) -> SkillExecutionResult:
        """保存调用快照，证明路由只执行一次 Runtime。"""
        self.calls.append(call)
        return self.result


def _executor(
    *,
    policy: RoutePolicy,
    runtime_result: SkillExecutionResult,
    legacy: RecordingLegacyService | None = None,
) -> tuple[AgentToolExecutor, RecordingLegacyService, RecordingRuntimeExecutor]:
    """装配带记录能力的 AgentToolExecutor。"""
    legacy_service = legacy or RecordingLegacyService()
    runtime = RecordingRuntimeExecutor(runtime_result)
    return (
        AgentToolExecutor(
            registry=get_default_tool_registry(),
            pre_live_service=legacy_service,
            skill_executor=runtime,
            route_policy=policy,
        ),
        legacy_service,
        runtime,
    )


def test_phase11b_routes_default_to_legacy() -> None:
    """新三批路由默认全为 LEGACY，避免启动后自动切换新执行链。"""
    policy = RoutePolicy.from_settings(Settings(_env_file=None))
    assert (policy.batch1, policy.batch2, policy.batch3) == (RouteConfig.LEGACY,) * 3


def test_legacy_phase11a_route_aliases_feed_batch1_and_batch2() -> None:
    """新配置缺席时，旧 generation/setup 配置继续控制前两批。"""
    policy = RoutePolicy.from_settings(
        Settings(
            _env_file=None,
            SKILL_ROUTE_PRELIVE_GENERATION="SKILL_RUNTIME",
            SKILL_ROUTE_PRELIVE_SETUP="SKILL_RUNTIME",
        )
    )

    assert policy.batch1 == RouteConfig.SKILL_RUNTIME
    assert policy.batch2 == RouteConfig.SKILL_RUNTIME
    assert policy.batch3 == RouteConfig.LEGACY
    assert policy.generation == RouteConfig.SKILL_RUNTIME
    assert policy.setup == RouteConfig.SKILL_RUNTIME


def test_explicit_phase11b_routes_override_legacy_aliases() -> None:
    """显式 Phase 11B 批次配置优先于旧别名，支持按批次回滚。"""
    policy = RoutePolicy.from_settings(
        Settings(
            _env_file=None,
            SKILL_ROUTE_PRELIVE_GENERATION="SKILL_RUNTIME",
            SKILL_ROUTE_PRELIVE_SETUP="SKILL_RUNTIME",
            SKILL_ROUTE_PHASE11B_BATCH1="LEGACY",
            SKILL_ROUTE_PHASE11B_BATCH2="LEGACY",
            SKILL_ROUTE_PHASE11B_BATCH3="SKILL_RUNTIME",
        )
    )

    assert policy == RoutePolicy(
        batch1=RouteConfig.LEGACY,
        batch2=RouteConfig.LEGACY,
        batch3=RouteConfig.SKILL_RUNTIME,
    )


def test_route_policy_is_frozen_after_construction() -> None:
    """RoutePolicy 是启动冻结快照，构造后不能被环境或调用方原地修改。"""
    policy = RoutePolicy.from_settings(
        Settings(_env_file=None, SKILL_ROUTE_PHASE11B_BATCH1="SKILL_RUNTIME")
    )
    with pytest.raises(ValidationError):
        policy.batch1 = RouteConfig.LEGACY  # type: ignore[misc]


@pytest.mark.parametrize(
    ("skill_id", "expected_batch"),
    [
        ("query_products", "batch1"),
        ("suggest_price_change", "batch1"),
        ("setup_live_session", "batch2"),
        ("handle_sold_out_event", "batch2"),
        ("set_product_price", "batch3"),
    ],
)
def test_skill_batch_mapping_is_explicit(skill_id: str, expected_batch: str) -> None:
    """13 个 Skill 的批次归属由显式表维护，不能依赖字符串前缀猜测。"""
    assert skill_batch_for(skill_id) == expected_batch


def test_runtime_failure_never_runs_legacy_for_same_batch1_call() -> None:
    """批次一路由到 Runtime 后，即使失败也不得在同次调用中 fallback legacy。"""
    executor, legacy, runtime = _executor(
        policy=RoutePolicy(batch1=RouteConfig.SKILL_RUNTIME),
        runtime_result=SkillExecutionResult(
            skill_id="suggest_price_change",
            version="1.0.0",
            status=SkillExecutionStatus.ERROR,
            error_code=SkillErrorCode.HANDLER_FAILED,
            summary="runtime failed",
        ),
    )

    observation = executor.execute(
        "suggest_price_change",
        {"product_id": "p001", "suggested_price": "35.90"},
        "room-1",
        "trace-1",
        lifecycle="PRE_LIVE",
    )

    assert observation.status == "error"
    assert observation.summary == "HANDLER_FAILED: runtime failed"
    assert [call.skill_id for call in runtime.calls] == ["suggest_price_change"]
    assert legacy.calls == []


def test_default_runtime_adapter_installs_batch1_handlers() -> None:
    """未注入测试替身时，AgentToolExecutor 也必须能执行批次一 Runtime Handler。"""
    legacy = RecordingLegacyService()
    executor = AgentToolExecutor(
        registry=get_default_tool_registry(),
        pre_live_service=legacy,
        route_policy=RoutePolicy(batch1=RouteConfig.SKILL_RUNTIME),
    )

    observation = executor.execute(
        "suggest_price_change",
        {"product_id": "p001", "suggested_price": "35.90"},
        "room-1",
        "trace-1",
        lifecycle="PRE_LIVE",
    )

    assert observation.status == "success"
    assert observation.summary == "执行成功"
    assert legacy.calls == []


def test_default_legacy_route_uses_legacy_service_for_core_query() -> None:
    """默认 LEGACY 下，四个播前核心工具仍能走旧服务，不被强制送入 Runtime。"""
    executor, legacy, runtime = _executor(
        policy=RoutePolicy.default(),
        runtime_result=SkillExecutionResult(
            skill_id="query_products",
            version="1.0.0",
            status=SkillExecutionStatus.ERROR,
            summary="runtime should not run",
        ),
    )

    observation = executor.execute(
        "query_products",
        {"room_id": "room-legacy"},
        "room-legacy",
        "trace-legacy",
    )

    assert observation.status == "success"
    assert "queried products" in observation.summary
    assert legacy.calls == [("query_products", "room-legacy", "trace-legacy")]
    assert runtime.calls == []


def test_failure_fact_is_sanitized_into_agent_observation() -> None:
    """Runtime FailureFact 映射到旧 Observation 时保留分类与 attempt/audit 证据。"""
    executor, legacy, runtime = _executor(
        policy=RoutePolicy(batch1=RouteConfig.SKILL_RUNTIME),
        runtime_result=SkillExecutionResult(
            skill_id="suggest_price_change",
            version="1.0.0",
            status=SkillExecutionStatus.ERROR,
            error_code=SkillErrorCode.HANDLER_FAILED,
            summary="adapter failed",
            audit_id="audit-runtime-1",
            attempt_id="attempt-1",
            failure=FailureFact(
                category=FailureCategory.RATE_LIMITED,
                external_code="fake.rate_limited",
                side_effect_state=SideEffectState.NOT_SENT,
                attempt_id="attempt-1",
                retry_after_seconds=3,
            ),
        ),
    )

    observation = executor.execute(
        "suggest_price_change",
        {"product_id": "p001", "suggested_price": "35.90"},
        "room-1",
        "trace-1",
        lifecycle="PRE_LIVE",
    )

    assert observation.status == "error"
    assert "HANDLER_FAILED" in observation.summary
    assert "RATE_LIMITED" in observation.summary
    assert "attempt-1" in observation.summary
    assert observation.audit_id == "audit-runtime-1"
    assert runtime.calls
    assert legacy.calls == []


def test_batch3_runtime_uses_catalog_version_and_moves_idempotency_to_context() -> None:
    """批次三兼容入口必须钉住单活版本，且不泄漏幂等键到业务参数。"""
    executor, legacy, runtime = _executor(
        policy=RoutePolicy(batch3=RouteConfig.SKILL_RUNTIME),
        runtime_result=SkillExecutionResult(
            skill_id="set_product_price",
            version="1.1.0",
            status=SkillExecutionStatus.PENDING,
            error_code=SkillErrorCode.APPROVAL_REQUIRED,
            summary="高风险 Skill 需要审批",
        ),
    )

    observation = executor.execute(
        "set_product_price",
        {
            "product_id": "p001",
            "price": "35.90",
            "expected_version": 1,
            "idempotency_key": "idem-price-001",
        },
        "room-1",
        "trace-1",
        lifecycle="PRE_LIVE",
    )

    assert observation.status == "pending"
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call.version == "1.1.0"
    assert call.arguments == {
        "product_id": "p001",
        "price": "35.90",
        "expected_version": 1,
    }
    assert call.context.idempotency_key == "idem-price-001"
    assert call.context.approval is None
    assert legacy.calls == []


def test_batch3_legacy_route_does_not_dispatch_runtime() -> None:
    """批次三回滚只由启动冻结 LEGACY 路由控制，不能在同次调用内隐式回退。"""
    executor, legacy, runtime = _executor(
        policy=RoutePolicy(batch3=RouteConfig.LEGACY),
        runtime_result=SkillExecutionResult(
            skill_id="set_product_price",
            version="1.1.0",
            status=SkillExecutionStatus.SUCCESS,
            summary="runtime should not run",
        ),
    )

    observation = executor.execute(
        "set_product_price",
        {
            "product_id": "p001",
            "price": "35.90",
            "expected_version": 1,
            "idempotency_key": "idem-price-legacy",
        },
        "room-1",
        "trace-1",
        lifecycle="PRE_LIVE",
    )

    assert observation.status == "pending"
    assert runtime.calls == []
    assert legacy.calls == []
