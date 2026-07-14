"""Phase 11B 批次三内部批准改价集成测试。

本模块不经过 AgentToolExecutor，直接证明受控 SkillCall 与 ApprovalContext 可以在隔离
Fake 平台上形成完整高风险写入闭环；不连接真实淘宝 API、数据库、Kafka 或 LLM。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.skill_runtime.attempt_store import InMemoryAttemptStore
from src.skill_runtime.executor import SkillExecutor
from src.skill_runtime.fake_platform import (
    FakeFaultKind,
    FakeFaultRule,
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import (
    FailureCategory,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_human_interrupt_approval,
)


class CountingPlatform(FakeLiveCommercePlatform):
    """在不修改生产 Fake 观测面的前提下记录真实改价调用次数。"""

    def __init__(self, fixture: FakePlatformFixture) -> None:
        super().__init__(fixture)
        self.set_price_calls = 0

    async def set_price(self, request):
        """记录一次 Port 调用后委托原始有状态 Fake。"""
        self.set_price_calls += 1
        return await super().set_price(request)


def _fixture(*, faults: tuple[FakeFaultRule, ...] = ()) -> FakePlatformFixture:
    """创建互相独立的高风险改价平台状态。"""
    return FakePlatformFixture(
        room_id="room-11b-price-integration",
        products=(
            FakePlatformProduct(
                product_id="p001",
                name="集成测试商品",
                price=Decimal("39.90"),
                inventory=10,
                version=1,
            ),
        ),
        faults=faults,
    )


def _runtime(platform: CountingPlatform) -> SkillExecutor:
    """为每个场景装配独立 Handler 与 Attempt Store，禁止跨用例状态泄漏。"""
    return SkillExecutor(
        handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform)),
        attempt_store=InMemoryAttemptStore(),
    )


def _call(*, expected_version: int = 1, idempotency_key: str = "idem-price-integration") -> SkillCall:
    """构造只由受控人工批准才能执行的内部改价调用。"""
    return SkillCall(
        skill_id="set_product_price",
        version="1.1.0",
        context=SkillExecutionContext(
            room_id="room-11b-price-integration",
            trace_id="trace-11b-price-integration",
            lifecycle="PRE_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=idempotency_key,
            approval=_build_human_interrupt_approval(
                decision="APPROVED",
                operator_id="operator-11b-integration",
                approval_audit_id="approval-audit-11b-integration",
            ),
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
        ),
        arguments={
            "product_id": "p001",
            "price": "35.90",
            "expected_version": expected_version,
        },
    )


def test_price_success_replays_first_terminal_fact_once() -> None:
    """成功改价重放同一终态，不能向平台产生第二次写请求。"""
    platform = CountingPlatform(_fixture())
    runtime = _runtime(platform)
    call = _call()

    first = asyncio.run(runtime.execute(call))
    second = asyncio.run(runtime.execute(call))

    assert first.status == SkillExecutionStatus.SUCCESS
    assert second.attempt_id == first.attempt_id
    assert platform.set_price_calls == 1
    assert platform.product("p001").version == 2


def test_rate_limited_price_replays_first_terminal_fact_once() -> None:
    """限流 FailureFact 也必须稳定重放，Handler 不得自行重试。"""
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="set_price",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.RATE_LIMITED,
                    retry_after_seconds=4,
                ),
            )
        )
    )
    runtime = _runtime(platform)
    call = _call()

    first = asyncio.run(runtime.execute(call))
    second = asyncio.run(runtime.execute(call))

    assert first.failure is not None
    assert first.failure.category == FailureCategory.RATE_LIMITED
    assert second.attempt_id == first.attempt_id
    assert platform.set_price_calls == 1


def test_unknown_price_write_replays_first_terminal_fact_once() -> None:
    """发送后未知必须保留首次终态，重放禁止再次发送潜在副作用。"""
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="set_price",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            )
        )
    )
    runtime = _runtime(platform)
    call = _call()

    first = asyncio.run(runtime.execute(call))
    second = asyncio.run(runtime.execute(call))

    assert first.failure is not None
    assert first.failure.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert second.attempt_id == first.attempt_id
    assert platform.set_price_calls == 1


def test_price_resource_version_conflict_is_not_a_skill_version_error() -> None:
    """商品 CAS 冲突来自 Adapter FailureFact，不能被误归类为 Skill 版本错误。"""
    platform = CountingPlatform(_fixture())
    runtime = _runtime(platform)

    result = asyncio.run(runtime.execute(_call(expected_version=2)))

    assert result.status == SkillExecutionStatus.ERROR
    assert result.failure is not None
    assert result.failure.category == FailureCategory.VERSION_CONFLICT
    assert platform.set_price_calls == 1
    assert platform.product("p001").version == 1
