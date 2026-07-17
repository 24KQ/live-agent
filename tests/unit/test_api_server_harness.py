from __future__ import annotations

from fastapi.testclient import TestClient

from src.gateway import api_server
from src.gateway.harness_dashboard_service import create_in_memory_harness_dashboard_service
from src.plan_engine.preemption import PreemptionEvidenceRef


api_server.set_harness_dashboard_service(create_in_memory_harness_dashboard_service())


client = TestClient(api_server.app)


def test_harness_start_endpoint_returns_deterministic_only_status() -> None:
    resp = client.post(
        "/api/agent/harness/start",
        json={"room_id": "room-dashboard-001", "trace_id": "trace-api-start"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["trace_id"] == "trace-api-start"
    assert data["status"] == "completed"
    assert data["pending_approval"] is False
    assert data["agent_status"] == "decision_support_disabled"


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


def test_legacy_harness_approval_endpoint_cannot_execute_without_pending_request() -> None:
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
    assert data["approval_decision"] is None
    assert data["executed_tools"] == []
    assert data["agent_status"] == "decision_support_disabled"


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


def test_harness_start_forwards_preemption_evidence_to_service(monkeypatch) -> None:
    """真实 HTTP 入口必须把 EvidenceRef 与最终建议转发到启动冻结的 Graph Service。"""

    class _RecordingService:
        def __init__(self) -> None:
            self.kwargs = None

        def start_session(self, **kwargs):
            self.kwargs = kwargs
            return {"trace_id": kwargs["trace_id"], "status": "completed"}

    service = _RecordingService()
    monkeypatch.setattr(api_server, "get_harness_dashboard_service", lambda: service)
    evidence = PreemptionEvidenceRef.create(
        event_id="event-api-evidence",
        root_plan_run_id="root-api-evidence",
        application_state="APPLIED",
        emergency_plan_run_id="child-api-evidence",
        applied_plan_version=2,
        final_suggestion_fact="已完成售罄处理，请切换商品",
    )

    response = client.post(
        "/api/agent/harness/start",
        json={
            "room_id": "room-api-evidence",
            "trace_id": "trace-api-evidence",
            "preemption_evidence_refs": [evidence.model_dump(mode="json")],
            "final_suggestion_fact": evidence.final_suggestion_fact,
        },
    )

    assert response.status_code == 200
    assert service.kwargs is not None
    assert service.kwargs["preemption_evidence_refs"][0].event_id == evidence.event_id
    assert service.kwargs["final_suggestion_fact"] == evidence.final_suggestion_fact
