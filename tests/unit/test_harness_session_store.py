from __future__ import annotations

import pytest

from src.gateway.harness_session_store import (
    HarnessSessionNotFoundError,
    HarnessSessionRecord,
    InMemoryHarnessSessionStore,
)


def _pending_record(trace_id: str = "trace-store-001") -> HarnessSessionRecord:
    return HarnessSessionRecord(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        anchor_id="anchor-demo",
        status="pending_human",
        approval_request={"tool_name": "handle_sold_out_event"},
        interrupt_payload={"risk_level": "HIGH"},
        latest_state={"completed_nodes": ["load_context"]},
    )


def test_pending_session_can_be_saved_and_loaded() -> None:
    store = InMemoryHarnessSessionStore()

    store.save_pending(_pending_record())
    loaded = store.get("trace-store-001")

    assert loaded.trace_id == "trace-store-001"
    assert loaded.status == "pending_human"
    assert loaded.approval_request["tool_name"] == "handle_sold_out_event"
    assert loaded.interrupt_payload["risk_level"] == "HIGH"


def test_terminal_session_is_created_without_pending_transition() -> None:
    """无审批终态必须一次创建，并对相同 trace 保持首个事实不变。"""

    store = InMemoryHarnessSessionStore()
    first = store.save_terminal(
        HarnessSessionRecord(
            trace_id="trace-terminal-001",
            room_id="room-dashboard-001",
            status="completed",
            latest_state={"agent_status": "decision_support_disabled"},
        )
    )
    repeated = store.save_terminal(
        HarnessSessionRecord(
            trace_id="trace-terminal-001",
            room_id="room-other",
            status="error",
            latest_state={"agent_status": "error"},
        )
    )

    assert first.status == "completed"
    assert repeated == first
    assert store.latest_for_room("room-dashboard-001") == [first]
    assert store.latest_for_room("room-other") == []


def test_terminal_create_rejects_pending_status() -> None:
    """原子终态接口不能被旧审批流程误用来创建 pending_human。"""

    store = InMemoryHarnessSessionStore()

    with pytest.raises(ValueError, match="terminal"):
        store.save_terminal(_pending_record("trace-terminal-invalid"))


def test_approval_update_is_idempotent_for_completed_session() -> None:
    store = InMemoryHarnessSessionStore()
    store.save_pending(_pending_record())

    first = store.save_final_state(
        trace_id="trace-store-001",
        status="completed",
        latest_state={"agent_status": "final_answer"},
        approval_decision="approved",
        operator_id="operator-dashboard",
        reason="确认执行",
        audit_status="recorded",
        audit_ids=["audit-001"],
        decision_trace_ids=["decision-001"],
    )
    second = store.save_final_state(
        trace_id="trace-store-001",
        status="completed",
        latest_state={"agent_status": "final_answer"},
        approval_decision="approved",
        operator_id="operator-dashboard",
        reason="重复提交",
        audit_status="recorded",
        audit_ids=["audit-001"],
        decision_trace_ids=["decision-001"],
    )

    assert first.trace_id == second.trace_id
    assert second.status == "completed"
    assert second.approval_decision == "approved"
    assert second.reason == "确认执行"


def test_unknown_trace_raises_clear_error() -> None:
    store = InMemoryHarnessSessionStore()

    with pytest.raises(HarnessSessionNotFoundError, match="trace-missing"):
        store.get("trace-missing")


def test_latest_for_room_returns_newest_first() -> None:
    store = InMemoryHarnessSessionStore()
    store.save_pending(_pending_record("trace-old"))
    store.save_pending(_pending_record("trace-new"))

    latest = store.latest_for_room("room-dashboard-001", limit=2)

    assert [item.trace_id for item in latest] == ["trace-new", "trace-old"]
