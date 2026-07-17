from __future__ import annotations

from uuid import uuid4

from src.config.settings import get_settings
from src.gateway.harness_dashboard_service import HarnessDashboardService
from src.gateway.harness_session_store import (
    PostgresHarnessSessionStore,
    initialize_harness_session_schema,
)


def _service() -> HarnessDashboardService:
    settings = get_settings()
    initialize_harness_session_schema(settings)
    store = PostgresHarnessSessionStore(settings)
    return HarnessDashboardService(store=store, settings=settings, use_postgres_checkpointer=True)


def test_postgres_dashboard_persists_default_deterministic_only_completion() -> None:
    trace_id = f"trace-phase6c-approve-{uuid4()}"
    first_service = _service()

    completed = first_service.start_session(room_id="room-dashboard-001", trace_id=trace_id)

    assert completed["status"] == "completed"
    assert completed["pending_approval"] is False
    assert completed["agent_status"] == "decision_support_disabled"

    loaded = _service().get_status(trace_id)
    legacy_approval = _service().submit_approval(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="approved",
        operator_id="operator-dashboard",
        reason="确认执行售罄处理",
    )

    assert loaded["status"] == "completed"
    assert loaded["agent_status"] == "decision_support_disabled"
    assert legacy_approval["approval_decision"] is None
    assert legacy_approval["executed_tools"] == []


def test_postgres_dashboard_legacy_rejection_cannot_reopen_session() -> None:
    trace_id = f"trace-phase6c-reject-{uuid4()}"
    service = _service()
    service.start_session(room_id="room-dashboard-001", trace_id=trace_id)

    unchanged = service.submit_approval(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="rejected",
        operator_id="operator-dashboard",
        reason="主播决定人工处理",
    )

    loaded = _service().get_status(trace_id)

    assert unchanged["status"] == "completed"
    assert loaded["status"] == "completed"
    assert loaded["approval_decision"] is None
    assert loaded["executed_tools"] == []
