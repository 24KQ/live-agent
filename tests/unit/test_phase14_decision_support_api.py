"""Phase 14 Task 7 API、鉴权和 WebSocket 协议的 TDD 契约。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.gateway import api_server
from src.gateway.websocket_manager import WebSocketManager
from src.gateway.operator_auth import OperatorAuthError, OperatorIdentity, OperatorRole
from src.decision_support.commands import DecisionExecutionContext, OperatorDecisionDraft
from src.decision_support.models import DecisionKind, Proposal
from src.plan_engine.models import PlanNodeState

from src.gateway.decision_support_service import DecisionSupportProposalRequest


NOW = datetime(2026, 7, 18, 13, 0, tzinfo=timezone.utc)
SESSION_ID = "live-session-task7-api"


def _proposal() -> Proposal:
    """构造 API schema 所需的最小结构化 Proposal 快照。"""

    return Proposal(
        proposal_id="proposal-task7-api",
        live_session_id=SESSION_ID,
        incident_id="incident-task7-api",
        evidence_bundle_id="evidence-task7-api",
        idempotency_key="proposal-task7-api-idem",
        proposal_key="sold-out-response",
        proposal_version=1,
        profile_id="live_ops_decision_support",
        profile_version="1.0.0",
        snapshot={"options": []},
        created_at=NOW,
    )


def _draft() -> OperatorDecisionDraft:
    """构造只包含结构化字段的人工决定请求。"""

    return OperatorDecisionDraft(
        decision_id="decision-task7-api",
        proposal_id="proposal-task7-api",
        expected_proposal_version=1,
        operator_id="operator-task7",
        decision_kind=DecisionKind.REJECT,
        reason_code="EVIDENCE_CONFLICT",
        idempotency_key="decision-task7-api-idem",
    )


class _RecordingService:
    """验证 HTTP 层只传递已解析身份和结构化输入，不直接碰 Store。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def get_workspace_payload(self, live_session_id: str) -> dict[str, Any]:
        self.calls.append(("workspace", live_session_id))
        return {"live_session_id": live_session_id, "view": "LIVE", "version": 3}

    def create_proposal(self, request: DecisionSupportProposalRequest, *, operator_id: str):
        self.calls.append(("proposal", operator_id, request))
        return {"proposal_id": request.proposal.proposal_id, "accepted": True}

    def submit_decision(self, *, live_session_id: str, request, operator_id: str):
        self.calls.append(("decision", live_session_id, operator_id, request))
        return {"status": "RECOVERY_REJECTED", "decision_id": request.draft.decision_id}


@pytest.fixture
def client():
    """每个测试还原 API 门面，避免全局应用状态污染其他 API 回归。"""

    service = _RecordingService()
    api_server.set_decision_support_service(service)
    yield TestClient(api_server.app), service
    api_server.set_decision_support_service(None)


def _operator(monkeypatch) -> None:
    """让 HTTP 测试聚焦协议，认证结果仍通过现有 OperatorIdentity。"""

    monkeypatch.setattr(
        api_server,
        "authenticate_request",
        lambda headers: OperatorIdentity("operator-task7", OperatorRole.OPERATOR, "测试运营"),
    )
    monkeypatch.setattr(api_server, "authorize_action", lambda identity, role: None)


def test_workspace_endpoint_requires_operator_and_returns_service_snapshot(client, monkeypatch) -> None:
    """Workspace 查询必须经过 Operator 身份，并返回门面权威快照。"""

    test_client, service = client
    _operator(monkeypatch)
    response = test_client.get(
        f"/api/decision-support/workspaces/{SESSION_ID}",
        headers={"x-operator-id": "operator-task7"},
    )
    assert response.status_code == 200
    assert response.json()["view"] == "LIVE"
    assert service.calls[0] == ("workspace", SESSION_ID)


def test_proposal_endpoint_passes_idempotency_and_operator_identity(client, monkeypatch) -> None:
    """Proposal API 只交给门面，不在 HTTP 层直接创建命令或执行副作用。"""

    test_client, service = client
    _operator(monkeypatch)
    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/proposals",
        headers={"x-operator-id": "operator-task7", "x-idempotency-key": "proposal-task7-api-idem"},
        json={"proposal": _proposal().model_dump(mode="json"), "expected_workspace_version": 3},
    )
    assert response.status_code == 200
    assert service.calls[0][0] == "proposal"
    assert service.calls[0][1] == "operator-task7"


def test_decision_endpoint_binds_header_operator_and_cas_context(client, monkeypatch) -> None:
    """Decision API 不信任 body 中的 operator_id，必须与认证身份一致。"""

    test_client, service = client
    _operator(monkeypatch)
    context = DecisionExecutionContext(
        plan_run_id="root-task7-api",
        expected_plan_version=2,
        node_id="node-task7-api",
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
    )
    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/decisions",
        headers={"x-operator-id": "operator-task7", "x-idempotency-key": "decision-task7-api-idem"},
        json={"draft": _draft().model_dump(mode="json"), "execution_context": context.model_dump(mode="json")},
    )
    assert response.status_code == 200
    assert service.calls[0][0] == "decision"
    assert service.calls[0][2] == "operator-task7"


def test_decision_endpoint_rejects_body_operator_mismatch(client, monkeypatch) -> None:
    """HTTP 身份与决定事实不一致时在门面前 fail-closed。"""

    test_client, _ = client
    _operator(monkeypatch)
    draft = OperatorDecisionDraft.model_validate(
        {**_draft().model_dump(mode="json"), "operator_id": "operator-other"}
    )
    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/decisions",
        headers={"x-operator-id": "operator-task7"},
        json={
            "draft": draft.model_dump(mode="json"),
            "execution_context": {
                "plan_run_id": "root-task7-api",
                "expected_plan_version": 2,
                "node_id": "node-task7-api",
                "expected_node_status": "WAITING_APPROVAL",
            },
        },
    )
    assert response.status_code == 409


def test_decision_support_websocket_uses_distinct_event_type_and_old_harness_remains() -> None:
    """新 WebSocket 路由必须和旧 agent_harness_update 事件类型分离。"""

    assert api_server.DECISION_SUPPORT_EVENT_TYPE == "decision_support_workspace_update"
    assert api_server.HARNESS_EVENT_TYPE == "agent_harness_update"


def test_websocket_manager_scopes_decision_updates_without_changing_legacy_broadcast() -> None:
    """同一广播池中，Phase 14 更新不能泄露到其他 session。"""

    class _Socket:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.messages.append(payload)

    manager = WebSocketManager()
    first = _Socket()
    second = _Socket()
    legacy = _Socket()
    manager.connect(first, scope="session-1")
    manager.connect(second, scope="session-2")
    manager.connect(legacy)

    asyncio.run(manager.broadcast({"type": "decision_support_workspace_update"}, scope="session-1"))
    assert len(first.messages) == 1
    assert second.messages == []
    assert legacy.messages == []

    asyncio.run(manager.broadcast({"type": "agent_harness_update"}))
    assert len(first.messages) == 2
    assert len(second.messages) == 1
    assert len(legacy.messages) == 1


def test_auth_error_is_not_converted_to_a_business_success(client, monkeypatch) -> None:
    """认证失败必须返回 401，不进入门面。"""

    test_client, service = client
    monkeypatch.setattr(
        api_server,
        "authenticate_request",
        lambda headers: (_ for _ in ()).throw(OperatorAuthError("missing token")),
    )
    response = test_client.get(f"/api/decision-support/workspaces/{SESSION_ID}")
    assert response.status_code == 401
    assert service.calls == []
