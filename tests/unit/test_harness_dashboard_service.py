from __future__ import annotations

from src.gateway.harness_dashboard_service import HarnessDashboardService
from src.gateway.harness_session_store import InMemoryHarnessSessionStore


def test_start_creates_pending_human_session() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)

    status = service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-start")

    assert status["trace_id"] == "trace-dashboard-start"
    assert status["status"] == "pending_human"
    assert status["pending_approval"] is True
    assert status["interrupt_payload"]["tool_name"] == "handle_sold_out_event"
    assert store.get("trace-dashboard-start").status == "pending_human"


def test_approve_resumes_graph_and_completes_session() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)
    service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-approve")

    status = service.submit_approval(
        trace_id="trace-dashboard-approve",
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="approved",
        operator_id="operator-dashboard",
        reason="确认售罄处理",
    )

    assert status["status"] == "completed"
    assert status["pending_approval"] is False
    assert status["approval_decision"] == "approved"
    assert status["executed_tools"][0]["tool_name"] == "handle_sold_out_event"
    assert status["observations"][0]["tool_name"] == "handle_sold_out_event"
    assert status["final_suggestion"]


def test_reject_resumes_graph_without_executing_tool() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)
    service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-reject")

    status = service.submit_approval(
        trace_id="trace-dashboard-reject",
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="rejected",
        operator_id="operator-dashboard",
        reason="主播决定人工处理",
    )

    assert status["status"] == "rejected"
    assert status["approval_decision"] == "rejected"
    assert status["executed_tools"] == []
    assert status["agent_status"] == "rejected_by_human"


def test_mismatched_approval_fails_closed() -> None:
    store = InMemoryHarnessSessionStore()
    service = HarnessDashboardService(store=store, use_postgres_checkpointer=False)
    service.start_session(room_id="room-dashboard-001", trace_id="trace-dashboard-mismatch")

    status = service.submit_approval(
        trace_id="trace-dashboard-mismatch",
        room_id="room-dashboard-001",
        tool_name="wrong_tool",
        decision="approved",
        operator_id="operator-dashboard",
        reason="错误工具名",
    )

    assert status["status"] == "error"
    assert "tool_name" in status["error"]
    assert status["executed_tools"] == []
