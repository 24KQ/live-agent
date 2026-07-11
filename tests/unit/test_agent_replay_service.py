from __future__ import annotations

from datetime import datetime, timezone

from src.core.agent_replay import AgentReplayService, ReplayTimelineItem


class FakeSessionStore:
    def get(self, trace_id: str):
        assert trace_id == "trace-replay-001"
        return type(
            "Record",
            (),
            {
                "trace_id": trace_id,
                "room_id": "room-dashboard-001",
                "status": "completed",
                "latest_state": {
                    "completed_nodes": ["load_context", "agent_reasoning", "write_audit"],
                    "executed_tools": [{"tool_name": "generate_on_live_prompt", "status": "success"}],
                    "observations": [{"tool_name": "generate_on_live_prompt", "summary": "已生成话术"}],
                    "approval_decision": "approved",
                },
                "audit_ids": ["audit-001"],
                "decision_trace_ids": ["decision-001"],
                "updated_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
            },
        )()


class FakeAuditStore:
    def list_events_by_trace_id(self, trace_id: str):
        assert trace_id == "trace-replay-001"
        return [
            {
                "audit_id": "audit-001",
                "tool_name": "generate_on_live_prompt",
                "gate_decision": "AUTO",
                "operator_decision": "auto",
                "created_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
            }
        ]


def test_replay_degrades_from_session_when_checkpoint_unavailable() -> None:
    service = AgentReplayService(
        session_store=FakeSessionStore(),
        audit_store=FakeAuditStore(),
        checkpointer=None,
    )

    replay = service.build_replay("trace-replay-001")

    assert replay.trace_id == "trace-replay-001"
    assert replay.replay_fidelity == "degraded"
    assert [item.node_name for item in replay.timeline][:3] == [
        "load_context",
        "agent_reasoning",
        "write_audit",
    ]
    assert replay.timeline[-1].tool_call["tool_name"] == "generate_on_live_prompt"
    assert replay.timeline[-1].evidence_ids == ["audit-001", "decision-001"]


def test_replay_timeline_item_sanitizes_sensitive_state() -> None:
    item = ReplayTimelineItem(
        sequence=1,
        node_name="agent_reasoning",
        phase="on_live",
        status="completed",
        state_delta={"api_key": "secret", "path": "D:\\Users\\someone\\.env"},
    )

    dumped = item.model_dump(mode="json")

    assert dumped["state_delta"]["api_key"] == "<redacted>"
    assert "<redacted" in dumped["state_delta"]["path"]


class FakeHighRiskAuditStore:
    def list_events_by_trace_id(self, trace_id: str):
        return [
            {
                "audit_id": "audit-high-risk",
                "tool_name": "set_product_price",
                "risk_level": "HIGH",
                "operator_decision": "approved",
                "gate_decision": "HARD_GATE",
                "created_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
            }
        ]


def test_audit_degraded_replay_preserves_risk_and_approval() -> None:
    service = AgentReplayService(
        session_store=FakeSessionStore(),
        audit_store=FakeHighRiskAuditStore(),
    )

    replay = service.build_replay("trace-replay-001")
    audit_item = replay.timeline[-1]

    assert audit_item.tool_call["risk_level"] == "HIGH"
    assert audit_item.approval["decision"] == "approved"
