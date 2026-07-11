from __future__ import annotations

from fastapi.testclient import TestClient

from src.gateway import api_server
from src.gateway.harness_dashboard_service import create_in_memory_harness_dashboard_service


api_server.set_harness_dashboard_service(create_in_memory_harness_dashboard_service())


client = TestClient(api_server.app)


def test_harness_start_endpoint_returns_pending_status() -> None:
    resp = client.post(
        "/api/agent/harness/start",
        json={"room_id": "room-dashboard-001", "trace_id": "trace-api-start"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["trace_id"] == "trace-api-start"
    assert data["status"] == "pending_human"
    assert data["pending_approval"] is True


def test_harness_status_endpoint_returns_saved_node_path() -> None:
    client.post(
        "/api/agent/harness/start",
        json={"room_id": "room-dashboard-001", "trace_id": "trace-api-status"},
    )

    resp = client.get("/api/agent/harness/status", params={"trace_id": "trace-api-status"})

    assert resp.status_code == 200
    data = resp.json()
    assert "completed_nodes" in data
    assert "load_context" in data["completed_nodes"]


def test_harness_approval_endpoint_approves_pending_tool() -> None:
    client.post(
        "/api/agent/harness/start",
        json={"room_id": "room-dashboard-001", "trace_id": "trace-api-approval"},
    )

    resp = client.post(
        "/api/agent/harness/approval",
        json={
            "trace_id": "trace-api-approval",
            "room_id": "room-dashboard-001",
            "tool_name": "handle_sold_out_event",
            "decision": "approved",
            "operator_id": "operator-dashboard",
            "reason": "确认执行",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["approval_decision"] == "approved"


def test_harness_approval_rejects_invalid_decision() -> None:
    resp = client.post(
        "/api/agent/harness/approval",
        json={
            "trace_id": "trace-api-invalid",
            "room_id": "room-dashboard-001",
            "tool_name": "handle_sold_out_event",
            "decision": "maybe",
            "operator_id": "operator-dashboard",
            "reason": "非法决策",
        },
    )

    assert resp.status_code == 422
