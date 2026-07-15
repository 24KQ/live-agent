"""Phase 12B PostgreSQL Attempt 与售罄未知副作用严格对账集成测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from src.config.settings import get_settings
from src.plan_engine.side_effect_reconciliation import (
    SoldOutReconciliationRequest,
    SoldOutReconciliationStatus,
    SoldOutSideEffectReconciler,
)
from src.skill_runtime.attempt_store import (
    PostgresAttemptStore,
    initialize_skill_execution_attempt_schema,
)
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
    FailureCategory,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    _build_verified_event_authorization,
)


class CountingPlatform(FakeLiveCommercePlatform):
    """记录真实 Runtime 与对账服务是否产生第二次售罄写。"""

    def __init__(self, fixture: FakePlatformFixture) -> None:
        super().__init__(fixture)
        self.mark_sold_out_calls = 0
        self.resolve_product_context_calls = 0

    async def mark_sold_out(self, request):
        """记录 CAS 写次数。"""
        self.mark_sold_out_calls += 1
        return await super().mark_sold_out(request)

    async def resolve_product_context(self, request):
        """记录只读对账次数。"""
        self.resolve_product_context_calls += 1
        return await super().resolve_product_context(request)


def test_postgres_unknown_attempt_is_replayed_and_reconciled_without_second_write() -> None:
    """PostgreSQL 保存唯一未知 Attempt；只读对账确认事实但不创建第二个 Operation。"""
    settings = get_settings()
    initialize_skill_execution_attempt_schema(settings)
    suffix = uuid4().hex
    room_id = f"room-phase12b-reconcile-{suffix}"
    event_id = f"event-phase12b-reconcile-{suffix}"
    idempotency_key = f"{event_id}:root-{suffix}:handle_sold_out_event"
    authorization = _build_verified_event_authorization(
        event_id=event_id,
        provenance_id=f"provenance-{suffix}",
        payload_digest="e" * 64,
        observed_version=3,
    )
    platform = CountingPlatform(
        FakePlatformFixture(
            room_id=room_id,
            products=(
                FakePlatformProduct(
                    product_id="p001",
                    name="售罄目标",
                    price=Decimal("39.90"),
                    inventory=5,
                    version=3,
                ),
            ),
            faults=(
                FakeFaultRule(
                    operation_name="mark_sold_out",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            ),
        )
    )
    runtime = SyncSkillExecutorAdapter(
        SkillExecutor(
            handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform)),
            attempt_store=PostgresAttemptStore(settings),
        )
    )
    deadline = datetime.now(timezone.utc) + timedelta(seconds=15)
    call = SkillCall(
        skill_id="handle_sold_out_event",
        version="2.0.0",
        context=SkillExecutionContext(
            room_id=room_id,
            trace_id=f"trace-{suffix}",
            lifecycle="ON_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=idempotency_key,
            event_authorization=authorization,
            deadline_at=deadline,
        ),
        arguments={"product_id": "p001", "expected_version": 3},
    )

    first = runtime.execute(call)
    replay = runtime.execute(call)
    assert first.failure is not None
    assert first.failure.category is FailureCategory.SIDE_EFFECT_UNKNOWN
    assert replay.attempt_id == first.attempt_id

    reconciled = asyncio.run(
        SoldOutSideEffectReconciler(platform).reconcile(
            SoldOutReconciliationRequest(
                room_id=room_id,
                trace_id=f"trace-{suffix}",
                product_id="p001",
                expected_version=3,
                event_authorization=authorization,
                original_failure=first.failure,
                deadline_at=deadline,
            )
        )
    )

    assert reconciled.status is SoldOutReconciliationStatus.CONFIRMED_SUCCESS
    assert reconciled.original_attempt_id == first.attempt_id
    assert platform.mark_sold_out_calls == 1
    assert platform.resolve_product_context_calls == 1
    with psycopg.connect(
        **settings.postgres_connection_kwargs,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) AS operation_count
                FROM skill_execution_operations
                WHERE skill_id = 'handle_sold_out_event'
                  AND room_id = %(room_id)s
                  AND idempotency_key = %(idempotency_key)s;
                """,
                {"room_id": room_id, "idempotency_key": idempotency_key},
            )
            operation_count = int(cursor.fetchone()["operation_count"])
            cursor.execute(
                """
                SELECT count(*) AS attempt_count, min(state) AS state
                FROM skill_execution_attempts AS attempt
                JOIN skill_execution_operations AS operation
                  ON operation.operation_id = attempt.operation_id
                WHERE operation.room_id = %(room_id)s
                  AND operation.idempotency_key = %(idempotency_key)s;
                """,
                {"room_id": room_id, "idempotency_key": idempotency_key},
            )
            attempt_row = cursor.fetchone()
    assert operation_count == 1
    assert int(attempt_row["attempt_count"]) == 1
    assert attempt_row["state"] == "SIDE_EFFECT_UNKNOWN"
