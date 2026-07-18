"""Phase 14 Task 6 PostgreSQL 售罄保护事实链与重启读取。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.models import LiveSessionWorkspace, WorkspaceView
from src.decision_support.sold_out_flow import HumanGuidedSoldOutFlow, SoldOutFlowStatus
from src.decision_support.store import PostgresDecisionSupportStore, WorkspaceConflictError
from src.plan_engine.event_state_machine import EventInboxState
from src.plan_engine.event_store import EventDelivery, InMemoryEventStore
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.preemption import PreemptionResult, PreemptionStatus


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def _isolated_phase14_task6_schema():
    """Task 6 使用独立 schema，避免 PostgreSQL 事实与其他阶段互相污染。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase14_task6_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    try:
        yield SimpleNamespace(
            postgres_connection_kwargs={
                **base_kwargs,
                "options": f"-c search_path={schema_name}",
            }
        )
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.commit()


def _workspace(session_id: str) -> LiveSessionWorkspace:
    """构造从 PREPARE 进入 LIVE 的唯一会话身份。"""

    return LiveSessionWorkspace(
        live_session_id=session_id,
        run_key=f"run-{session_id}",
        room_id=f"room-{session_id}",
        trace_id=f"trace-{session_id}",
        anchor_id="anchor-task6",
        root_plan_run_id=f"root-{session_id}",
        event_inbox_scope_id=f"inbox-{session_id}",
        decision_trace_scope_id=f"decision-{session_id}",
        replay_scope_id=f"replay-{session_id}",
        evaluation_scope_id=f"evaluation-{session_id}",
    )


def _event_store(*, event_id: str, room_id: str) -> InMemoryEventStore:
    """使用真实 Event 模型登记可信售罄事实。"""

    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id=room_id,
        product_id="p001",
        observed_version=4,
        occurred_at=NOW - timedelta(seconds=2),
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id=f"provenance-{event_id}",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=NOW - timedelta(seconds=1),
        payload_digest=event.payload_digest,
    )
    store = InMemoryEventStore()
    store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id=f"occurrence-{event_id}",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=1,
            received_at=NOW - timedelta(seconds=1),
        ),
    )
    return store


class _FakeProtection:
    """只返回 Phase 12B 已完成保护的摘要，不接触平台或 Runtime。"""

    async def run_next(self, *, root_plan_run_id: str, now: datetime) -> PreemptionResult:
        return PreemptionResult(
            status=PreemptionStatus.APPLIED,
            event_id="event-task6-postgres",
            root_plan_run_id=root_plan_run_id,
        )
    async def reconcile_waiting(
        self,
        *,
        event_id: str,
        root_plan_run_id: str,
        now: datetime,
    ) -> PreemptionResult:
        return PreemptionResult(
            status=PreemptionStatus.RETRY_PENDING,
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
        )


class _NoopCommandService:
    """Task 6 PostgreSQL 保护测试不允许意外进入经营恢复命令。"""

    def submit(self, command, *, now):
        raise AssertionError("automatic protection must not submit recovery command")


def _enter_live(store: PostgresDecisionSupportStore, session_id: str) -> None:
    """通过正式 operator lease 推进 Workspace，而不是直接改数据库状态。"""

    current = store.get_workspace(session_id)
    lease = store.acquire_operator_lock(session_id, "task6-transition", 60)
    store.advance_view(
        session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=current.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )
    store.release_operator_lock(
        session_id,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )


def test_postgres_protection_persists_incident_and_root_lookup_survives_restart(
    _isolated_phase14_task6_schema,
) -> None:
    """自动保护事实在 PostgreSQL 重启后的 Store 中仍按 root 唯一读取。"""

    settings = _isolated_phase14_task6_schema
    session_id = f"session-{uuid4().hex}"
    event_id = "event-task6-postgres"
    root_plan_run_id = f"root-{session_id}"
    store = PostgresDecisionSupportStore(settings)
    store.initialize_schema()
    store.create_workspace(_workspace(session_id))
    _enter_live(store, session_id)
    flow = HumanGuidedSoldOutFlow(
        workspace_store=store,
        event_store=_event_store(event_id=event_id, room_id=f"room-{session_id}"),
        protection_coordinator=_FakeProtection(),
        command_service=_NoopCommandService(),
    )

    result = asyncio.run(
        flow.handle_verified_event(
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
            now=NOW,
        )
    )

    assert result.status is SoldOutFlowStatus.PROTECTED
    restarted = PostgresDecisionSupportStore(settings)
    assert restarted.get_workspace_by_root_plan(root_plan_run_id).live_session_id == session_id
    incidents = restarted.list_incidents(session_id)
    assert len(incidents) == 1
    assert incidents[0].snapshot["payload_digest"]


def test_postgres_flow_rejects_room_scope_before_protection(
    _isolated_phase14_task6_schema,
) -> None:
    """PostgreSQL Workspace 的 room 事实不允许调用方用错误事件触发保护。"""

    settings = _isolated_phase14_task6_schema
    session_id = f"session-{uuid4().hex}"
    store = PostgresDecisionSupportStore(settings)
    store.initialize_schema()
    store.create_workspace(_workspace(session_id))
    _enter_live(store, session_id)
    flow = HumanGuidedSoldOutFlow(
        workspace_store=store,
        event_store=_event_store(event_id="event-task6-postgres-wrong", room_id="room-other"),
        protection_coordinator=_FakeProtection(),
        command_service=_NoopCommandService(),
    )

    with pytest.raises(WorkspaceConflictError, match="room"):
        asyncio.run(
            flow.handle_verified_event(
                event_id="event-task6-postgres-wrong",
                root_plan_run_id=f"root-{session_id}",
                now=NOW,
            )
        )
