"""Phase 12B 售罄 2.0.0 Handler、CAS 失败和严格只读对账测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from typing import Any

from src.skill_runtime.attempt_store import InMemoryAttemptStore
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.fake_platform import (
    FakeFaultKind,
    FakeFaultRule,
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import (
    EventAuthorizationContext,
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_verified_event_authorization,
)


class CountingPlatform(FakeLiveCommercePlatform):
    """记录写与只读对账调用次数，证明未知副作用不会触发第二次写。"""

    def __init__(self, fixture: FakePlatformFixture) -> None:
        super().__init__(fixture)
        self.mark_sold_out_calls = 0
        self.resolve_product_context_calls = 0

    async def mark_sold_out(self, request):
        """记录单次 CAS 写调用。"""
        self.mark_sold_out_calls += 1
        return await super().mark_sold_out(request)

    async def resolve_product_context(self, request):
        """记录只读对账调用。"""
        self.resolve_product_context_calls += 1
        return await super().resolve_product_context(request)


def _fixture(
    *,
    faults: tuple[FakeFaultRule, ...] = (),
    inventory: int = 5,
    version: int = 3,
    is_active: bool = True,
) -> FakePlatformFixture:
    """构造版本为 3 的售罄目标及独立备选商品。"""
    return FakePlatformFixture(
        room_id="room-phase12b-sold-out",
        products=(
            FakePlatformProduct(
                product_id="p001",
                name="售罄目标",
                price=Decimal("39.90"),
                inventory=inventory,
                version=version,
                is_active=is_active,
            ),
            FakePlatformProduct(
                product_id="p002",
                name="备选商品",
                price=Decimal("59.90"),
                inventory=8,
                version=1,
            ),
        ),
        faults=faults,
    )


def _authorization(observed_version: int = 3) -> EventAuthorizationContext:
    """构造与售罄 CAS 版本闭合的可信事件授权。"""
    return _build_verified_event_authorization(
        event_id="event-phase12b-sold-out",
        provenance_id="provenance-phase12b-sold-out",
        payload_digest="d" * 64,
        observed_version=observed_version,
    )


def _call(
    *,
    expected_version: int = 3,
    idempotency_key: str = "event-phase12b-sold-out:root-001:handle_sold_out_event",
) -> SkillCall:
    """构造只含显式 CAS 业务参数的 2.0.0 调用。"""
    return SkillCall(
        skill_id="handle_sold_out_event",
        version="2.0.0",
        context=SkillExecutionContext(
            room_id="room-phase12b-sold-out",
            trace_id="trace-phase12b-sold-out",
            lifecycle="ON_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=idempotency_key,
            event_authorization=_authorization(expected_version),
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
        ),
        arguments={"product_id": "p001", "expected_version": expected_version},
    )


def _runtime(platform: CountingPlatform) -> SyncSkillExecutorAdapter:
    """使用真实 Handler 和内存 Attempt Store 装配同步测试桥。"""
    return SyncSkillExecutorAdapter(
        SkillExecutor(
            handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform)),
            attempt_store=InMemoryAttemptStore(),
        )
    )


def test_sold_out_cas_success_and_replay_call_port_once() -> None:
    """CAS 成功返回前后版本；相同幂等键只重放原 Attempt。"""
    platform = CountingPlatform(_fixture())
    runtime = _runtime(platform)
    call = _call()

    first = runtime.execute(call)
    second = runtime.execute(call)

    assert first.status is SkillExecutionStatus.SUCCESS
    assert second.status is SkillExecutionStatus.SUCCESS
    assert first.attempt_id == second.attempt_id
    assert platform.mark_sold_out_calls == 1
    assert first.output is not None
    assert first.output["previous_version"] == 3
    assert first.output["new_version"] == 4
    assert first.output["sold_out_product"]["inventory"] == 0
    assert set(first.output) == {"sold_out_product", "previous_version", "new_version"}


def test_sold_out_native_version_conflict_does_not_mutate_product() -> None:
    """事件观察版本过期时 Adapter 返回 VERSION_CONFLICT/NOT_SENT，状态保持原样。"""
    platform = CountingPlatform(_fixture(version=3))
    before = platform.product("p001")

    result = _runtime(platform).execute(_call(expected_version=2))

    assert result.failure is not None
    assert result.failure.category is FailureCategory.VERSION_CONFLICT
    assert result.failure.side_effect_state is SideEffectState.NOT_SENT
    assert platform.product("p001") == before
    assert platform.mark_sold_out_calls == 1


def test_sold_out_rate_limit_preserves_retry_after_without_mutation() -> None:
    """发送前限流保留 Retry-After，并且不修改商品版本或库存。"""
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="mark_sold_out",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.RATE_LIMITED,
                    retry_after_seconds=7,
                ),
            )
        )
    )
    before = platform.product("p001")

    result = _runtime(platform).execute(_call())

    assert result.failure is not None
    assert result.failure.category is FailureCategory.RATE_LIMITED
    assert result.failure.retry_after_seconds == 7
    assert result.failure.side_effect_state is SideEffectState.NOT_SENT
    assert platform.product("p001") == before


def test_sold_out_unknown_after_send_replays_without_second_write() -> None:
    """发送后未知保留实际售罄事实，相同 Operation 重放不得再次调用 Port。"""
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="mark_sold_out",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            )
        )
    )
    runtime = _runtime(platform)
    call = _call()

    first = runtime.execute(call)
    second = runtime.execute(call)

    assert first.failure is not None
    assert first.failure.category is FailureCategory.SIDE_EFFECT_UNKNOWN
    assert second.attempt_id == first.attempt_id
    assert platform.mark_sold_out_calls == 1
    assert platform.product("p001").version == 4


def _reconciliation_module() -> Any:
    """延迟导入严格对账模块，使缺少实现形成可读红灯。"""
    return importlib.import_module("src.plan_engine.side_effect_reconciliation")


def _unknown_failure(attempt_id: str = "attempt-sold-out-unknown") -> FailureFact:
    """构造只能通过只读事实恢复的原始未知副作用。"""
    return FailureFact(
        category=FailureCategory.SIDE_EFFECT_UNKNOWN,
        external_code="fake.unknown_after_send",
        side_effect_state=SideEffectState.UNKNOWN,
        attempt_id=attempt_id,
    )


def _reconciliation_request(module: Any, failure: FailureFact) -> Any:
    """构造绑定原 Attempt、事件版本和商品的严格对账请求。"""
    return module.SoldOutReconciliationRequest(
        room_id="room-phase12b-sold-out",
        trace_id="trace-phase12b-sold-out",
        product_id="p001",
        expected_version=3,
        event_authorization=_authorization(3),
        original_failure=failure,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
    )


def test_strict_reconciliation_confirms_closed_fact_without_second_write() -> None:
    """未知写后只读商品已售罄且版本递增时，结果关联原 Attempt 并确认成功。"""
    module = _reconciliation_module()
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="mark_sold_out",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            )
        )
    )
    unknown = _runtime(platform).execute(_call())
    assert unknown.failure is not None

    result = asyncio.run(
        module.SoldOutSideEffectReconciler(platform).reconcile(
            _reconciliation_request(module, unknown.failure)
        )
    )

    assert result.status is module.SoldOutReconciliationStatus.CONFIRMED_SUCCESS
    assert result.original_attempt_id == unknown.attempt_id
    assert result.evidence is not None
    assert result.evidence["confirmed_version"] == 4
    assert platform.mark_sold_out_calls == 1
    assert platform.resolve_product_context_calls == 1


def test_strict_reconciliation_keeps_waiting_when_evidence_is_insufficient() -> None:
    """商品仍可售或版本未递增时保持 WAITING，不得把未知 Attempt 猜成成功。"""
    module = _reconciliation_module()
    platform = CountingPlatform(_fixture())
    failure = _unknown_failure()

    result = asyncio.run(
        module.SoldOutSideEffectReconciler(platform).reconcile(
            _reconciliation_request(module, failure)
        )
    )

    assert result.status is module.SoldOutReconciliationStatus.WAITING_RECONCILIATION
    assert result.original_attempt_id == failure.attempt_id
    assert result.evidence is None
    assert platform.mark_sold_out_calls == 0
    assert platform.resolve_product_context_calls == 1
