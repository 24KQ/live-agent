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


def test_postgres_harness_dashboard_approve_flow_recovers_from_checkpoint() -> None:
    trace_id = f"trace-phase6c-approve-{uuid4()}"
    first_service = _service()

    pending = first_service.start_session(room_id="room-dashboard-001", trace_id=trace_id)

    assert pending["status"] == "pending_human"
    assert pending["interrupt_payload"]["tool_name"] == "handle_sold_out_event"

    resumed_service = _service()
    completed = resumed_service.submit_approval(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="approved",
        operator_id="operator-dashboard",
        reason="确认执行售罄处理",
    )

    assert completed["status"] == "completed"
    assert completed["approval_decision"] == "approved"
    assert completed["executed_tools"][0]["tool_name"] == "handle_sold_out_event"
    assert completed["audit_status"] in {"dry_run", "recorded", None}


def test_postgres_harness_dashboard_reject_flow_persists_decision() -> None:
    trace_id = f"trace-phase6c-reject-{uuid4()}"
    service = _service()
    service.start_session(room_id="room-dashboard-001", trace_id=trace_id)

    rejected = service.submit_approval(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="rejected",
        operator_id="operator-dashboard",
        reason="主播决定人工处理",
    )

    loaded = _service().get_status(trace_id)

    assert rejected["status"] == "rejected"
    assert loaded["status"] == "rejected"
    assert loaded["approval_decision"] == "rejected"
    assert loaded["executed_tools"] == []
