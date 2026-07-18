"""Phase 16 Task 7 受治理人工升级 API 的 RED/GREEN 契约。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.decision_support.models import WorkspaceView
from src.decision_support.multi_agent import HighConflictCoordinationResult
from src.decision_support.store import WorkspaceConflictError, WorkspaceLeaseError
from src.gateway import api_server
from src.gateway.decision_support_service import (
    DecisionSupportService,
    DecisionSupportServiceUnavailable,
    MultiAgentEscalationRequest,
)
from src.gateway.operator_auth import OperatorAuthError, OperatorIdentity, OperatorRole


SESSION_ID = "live-session-phase16-api"
BUNDLE_ID = "bundle-phase16-api"
IDEMPOTENCY_KEY = f"phase16-escalation:operator_requested:{BUNDLE_ID}"


class _RecordingEscalationService:
    """记录 HTTP 门面调用，证明路由不会直接接触 Coordinator 或 Store。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def get_workspace_payload(self, live_session_id: str) -> dict[str, Any]:
        """提供稳定全量投影，供写后 WebSocket 契约验证不泄漏临时协调返回值。"""

        return {
            "live_session_id": live_session_id,
            "version": 8,
            "view": "LIVE",
            "incidents": [],
            "escalations": [],
            "conflict_analyses": [],
            "multi_agent_outcomes": [],
            "proposals": [],
            "operator_decisions": [],
            "execution_commands": [],
        }

    async def request_multi_agent_escalation(
        self,
        *,
        live_session_id: str,
        request: MultiAgentEscalationRequest,
        operator_id: str,
        request_idempotency_key: str,
    ) -> dict[str, Any]:
        """回显最小安全输入，避免测试替身伪造分析、方案或经营执行事实。"""

        self.calls.append(
            (live_session_id, request, operator_id, request_idempotency_key)
        )
        return {
            "accepted": True,
            "request_idempotency_key": request_idempotency_key,
            "escalation_id": f"escalation:{request.evidence_bundle_id}",
        }


@pytest.fixture
def client() -> tuple[TestClient, _RecordingEscalationService]:
    """隔离模块级 API 门面，避免本任务的 HTTP RED 影响旧 Decision Support 回归。"""

    service = _RecordingEscalationService()
    api_server.set_decision_support_service(service)
    yield TestClient(api_server.app, raise_server_exceptions=False), service
    api_server.set_decision_support_service(None)


def _operator(monkeypatch: pytest.MonkeyPatch) -> None:
    """让本测试聚焦冻结请求边界；身份仍以现有 OperatorIdentity 形状传入路由。"""

    # 成功与幂等冲突用例必须显式模拟安全装配已开启，避免无意依赖旧 API 的本地
    # 默认管理员兼容；认证关闭的 fail-closed 行为由独立测试固定。
    monkeypatch.setattr(
        api_server,
        "get_settings",
        lambda: SimpleNamespace(operator_auth_enabled=True),
    )
    monkeypatch.setattr(
        api_server,
        "authenticate_request",
        lambda _headers: OperatorIdentity(
            "operator-phase16-api", OperatorRole.OPERATOR, "测试运营"
        ),
    )
    monkeypatch.setattr(api_server, "authorize_action", lambda _identity, _role: None)


def test_manual_escalation_endpoint_only_forwards_canonical_bundle_request(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 只能提交 Bundle/CAS/规范幂等身份，禁止在边界伪造 Coordinator 控制字段。"""

    test_client, service = client
    _operator(monkeypatch)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={
            "x-operator-id": "operator-phase16-api",
            "x-idempotency-key": IDEMPOTENCY_KEY,
        },
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 200
    assert len(service.calls) == 1
    live_session_id, request, operator_id, request_idempotency_key = service.calls[0]
    assert live_session_id == SESSION_ID
    assert request.evidence_bundle_id == BUNDLE_ID
    assert request.expected_workspace_version == 7
    assert request_idempotency_key == IDEMPOTENCY_KEY
    assert operator_id == "operator-phase16-api"


def test_manual_escalation_endpoint_rejects_noncanonical_idempotency_key(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任意自选幂等键不能改变单 Bundle 的唯一升级身份或绕过重放语义。"""

    test_client, service = client
    _operator(monkeypatch)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={
            "x-operator-id": "operator-phase16-api",
            "x-idempotency-key": "caller-chosen-key",
        },
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 409
    assert service.calls == []


def test_manual_escalation_endpoint_rejects_idempotency_from_json_body(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-153 只允许 header 承载规范幂等身份，JSON 不能携带任何并发控制字段。"""

    test_client, service = client
    _operator(monkeypatch)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={
            "x-operator-id": "operator-phase16-api",
            "x-idempotency-key": IDEMPOTENCY_KEY,
        },
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
            "request_idempotency_key": IDEMPOTENCY_KEY,
        },
    )

    assert response.status_code == 422
    assert service.calls == []


def test_manual_escalation_endpoint_requires_idempotency_header(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络重试必须显式绑定单 Bundle 事实；缺少 header 不得进入 Service。"""

    test_client, service = client
    _operator(monkeypatch)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={"x-operator-id": "operator-phase16-api"},
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 409
    assert service.calls == []


def test_manual_escalation_endpoint_rejects_client_trigger_injection(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile、触发码、作用域和 lease 等 Coordinator 控制字段都不是公开 JSON 契约。"""

    test_client, service = client
    _operator(monkeypatch)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={
            "x-operator-id": "operator-phase16-api",
            "x-idempotency-key": IDEMPOTENCY_KEY,
        },
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
            "trigger_codes": ["RHYTHM_PAUSE_REQUIRED"],
        },
    )

    assert response.status_code == 422
    assert service.calls == []


def test_manual_escalation_endpoint_maps_authentication_failure_before_service(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """认证错误保持全局 401 语义，不能被升级端点转换成业务成功或 lease 请求。"""

    test_client, service = client
    monkeypatch.setattr(
        api_server,
        "get_settings",
        lambda: SimpleNamespace(operator_auth_enabled=True),
    )
    monkeypatch.setattr(
        api_server,
        "authenticate_request",
        lambda _headers: (_ for _ in ()).throw(OperatorAuthError("missing token")),
    )

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={"x-idempotency-key": IDEMPOTENCY_KEY},
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 401
    assert service.calls == []


def test_manual_escalation_endpoint_rejects_when_operator_authentication_is_disabled(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧本地默认管理员不能获得受控多 Agent 升级的请求权。"""

    test_client, service = client
    monkeypatch.setattr(
        api_server,
        "get_settings",
        lambda: SimpleNamespace(operator_auth_enabled=False),
    )
    monkeypatch.setattr(
        api_server,
        "authenticate_request",
        lambda _headers: OperatorIdentity(
            "legacy-default-admin", OperatorRole.ADMIN, "历史兼容管理员"
        ),
    )
    monkeypatch.setattr(api_server, "authorize_action", lambda _identity, _role: None)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={"x-idempotency-key": IDEMPOTENCY_KEY},
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 503
    assert service.calls == []


def test_manual_escalation_endpoint_rejects_malformed_body_when_authentication_is_disabled(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-157：安全装配缺失优先于 JSON 校验，畸形负载不能探测端点的 Schema。"""

    test_client, service = client
    monkeypatch.setattr(
        api_server,
        "get_settings",
        lambda: SimpleNamespace(operator_auth_enabled=False),
    )

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        json={"trigger_codes": ["RHYTHM_PAUSE_REQUIRED"]},
    )

    assert response.status_code == 503
    assert service.calls == []


def test_manual_escalation_write_broadcasts_authoritative_workspace_projection(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """订阅者只能看到 Store 事实投影，不能把一次请求的临时响应当作 Workspace 状态。"""

    test_client, _ = client
    _operator(monkeypatch)
    broadcasts: list[dict[str, Any]] = []

    async def _record_broadcast(*, live_session_id: str, payload: dict[str, Any]) -> None:
        """记录原本将发往当前 session 的 payload，不连接真实 WebSocket。"""

        broadcasts.append({"live_session_id": live_session_id, "payload": payload})

    monkeypatch.setattr(api_server, "_broadcast_decision_support_status", _record_broadcast)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={
            "x-operator-id": "operator-phase16-api",
            "x-idempotency-key": IDEMPOTENCY_KEY,
        },
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 200
    assert broadcasts == [
        {
            "live_session_id": SESSION_ID,
            "payload": {
                "workspace": {
                    "live_session_id": SESSION_ID,
                    "version": 8,
                    "view": "LIVE",
                    "incidents": [],
                    "escalations": [],
                    "conflict_analyses": [],
                    "multi_agent_outcomes": [],
                    "proposals": [],
                    "operator_decisions": [],
                    "execution_commands": [],
                }
            },
        }
    ]


class _WorkspaceStore:
    """提供 Service 需要的最小权威投影，所有调用都被记录以验证装配顺序。"""

    def __init__(self) -> None:
        self.bundle = SimpleNamespace(
            evidence_bundle_id=BUNDLE_ID,
            live_session_id=SESSION_ID,
        )
        self.workspace = SimpleNamespace(
            live_session_id=SESSION_ID,
            version=7,
            view=WorkspaceView.LIVE,
            model_dump=lambda mode: {
                "live_session_id": SESSION_ID,
                "version": 7,
                "view": WorkspaceView.LIVE.value,
            },
        )
        self.lease = SimpleNamespace(
            operator_id="operator-phase16-api",
            fencing_token=11,
        )
        self.calls: list[tuple[str, Any]] = []

    def get_evidence_bundle(self, evidence_bundle_id: str) -> Any:
        """模拟 append-only Store 的 Bundle 重载，拒绝由 HTTP 直接传递快照。"""

        self.calls.append(("bundle", evidence_bundle_id))
        return self.bundle

    def get_workspace(self, live_session_id: str) -> Any:
        """返回当前 Workspace 版本与 LIVE 状态，供 Service 在调用模型前验证。"""

        self.calls.append(("workspace", live_session_id))
        return self.workspace

    def acquire_operator_lock(
        self,
        live_session_id: str,
        operator_id: str,
        lease_seconds: int,
    ) -> Any:
        """记录仅服务端持有的 lease/fencing 装配，不接受 HTTP 控制字段。"""

        self.calls.append(("lease", live_session_id, operator_id, lease_seconds))
        return self.lease

    def list_incidents(self, _live_session_id: str) -> tuple[object, ...]:
        return ()

    def list_escalations(self, _live_session_id: str) -> tuple[object, ...]:
        return ()

    def list_conflict_analyses(self, _live_session_id: str) -> tuple[object, ...]:
        return ()

    def list_multi_agent_outcomes(self, _live_session_id: str) -> tuple[object, ...]:
        return ()

    def list_proposals(self, _live_session_id: str) -> tuple[object, ...]:
        return ()

    def list_operator_decisions(self, _live_session_id: str) -> tuple[object, ...]:
        return ()

    def list_execution_commands(self, _live_session_id: str) -> tuple[object, ...]:
        return ()


class _RecordingCoordinator:
    """记录 Service 传入的权威对象，绝不调用真实 Runner 或模型。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_operator_requested(self, bundle: Any, **kwargs: Any) -> HighConflictCoordinationResult:
        """回显调用边界，确认 Coordinator 仅接收服务端 Bundle/lease/CAS。"""

        self.calls.append({"bundle": bundle, **kwargs})
        return HighConflictCoordinationResult(selected=False)


def _service_request() -> MultiAgentEscalationRequest:
    """构造只含公开 JSON 字段的最小服务请求，header 身份另行传递。"""

    return MultiAgentEscalationRequest(
        evidence_bundle_id=BUNDLE_ID,
        expected_workspace_version=7,
    )


def test_service_reloads_bundle_and_uses_server_lease_for_manual_escalation() -> None:
    """Service 必须只向 Coordinator 传递 Store 重载的 Bundle、当前 CAS 和 server-side fencing。"""

    store = _WorkspaceStore()
    coordinator = _RecordingCoordinator()
    service = DecisionSupportService(store=store, multi_agent_coordinator=coordinator)

    result = asyncio.run(
        service.request_multi_agent_escalation(
            live_session_id=SESSION_ID,
            request=_service_request(),
            operator_id="operator-phase16-api",
            request_idempotency_key=IDEMPOTENCY_KEY,
        )
    )

    assert result["accepted"] is False
    assert result["request_idempotency_key"] == IDEMPOTENCY_KEY
    assert coordinator.calls == [
        {
            "bundle": store.bundle,
            "expected_workspace_version": 7,
            "operator_id": "operator-phase16-api",
            "fencing_token": 11,
        }
    ]
    assert store.calls[:3] == [
        ("bundle", BUNDLE_ID),
        ("workspace", SESSION_ID),
        ("lease", SESSION_ID, "operator-phase16-api", 60),
    ]


def test_service_rejects_multi_agent_escalation_without_explicit_coordinator() -> None:
    """默认确定性装配不能因 HTTP 请求自动创建 Coordinator、Runner 或模型路径。"""

    store = _WorkspaceStore()
    service = DecisionSupportService(store=store)

    with pytest.raises(DecisionSupportServiceUnavailable):
        asyncio.run(
            service.request_multi_agent_escalation(
                live_session_id=SESSION_ID,
                request=_service_request(),
                operator_id="operator-phase16-api",
                request_idempotency_key=IDEMPOTENCY_KEY,
            )
        )
    assert store.calls == []


@pytest.mark.parametrize(
    ("bundle_session_id", "workspace_version", "expected_error"),
    [
        ("other-session", 7, "escalation bundle does not belong to workspace"),
        (SESSION_ID, 8, "workspace version conflict"),
    ],
)
def test_service_rejects_cross_scope_bundle_and_stale_cas_before_coordinator(
    bundle_session_id: str,
    workspace_version: int,
    expected_error: str,
) -> None:
    """父作用域和 CAS 必须在获取 lease、调用 Coordinator 或产生模型成本之前完成验证。"""

    store = _WorkspaceStore()
    store.bundle.live_session_id = bundle_session_id
    store.workspace.version = workspace_version
    coordinator = _RecordingCoordinator()
    service = DecisionSupportService(store=store, multi_agent_coordinator=coordinator)

    with pytest.raises(WorkspaceConflictError, match=expected_error):
        asyncio.run(
            service.request_multi_agent_escalation(
                live_session_id=SESSION_ID,
                request=_service_request(),
                operator_id="operator-phase16-api",
                request_idempotency_key=IDEMPOTENCY_KEY,
            )
        )

    assert coordinator.calls == []
    assert not any(call[0] == "lease" for call in store.calls)


def test_manual_escalation_endpoint_maps_lease_conflict_to_fail_closed_response(
    client: tuple[TestClient, _RecordingEscalationService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一名运营持有 lease 时必须返回稳定冲突，不得泄漏为 500 或重试 Coordinator。"""

    class _LeaseConflictService(_RecordingEscalationService):
        async def request_multi_agent_escalation(self, **_kwargs: Any) -> dict[str, Any]:
            """模拟 Store 的当前 lease 拒绝，确保 HTTP 层保留 fail-closed 语义。"""

            raise WorkspaceLeaseError("workspace locked by another operator")

    test_client, _ = client
    api_server.set_decision_support_service(_LeaseConflictService())
    _operator(monkeypatch)

    response = test_client.post(
        f"/api/decision-support/workspaces/{SESSION_ID}/multi-agent-escalations",
        headers={
            "x-operator-id": "operator-phase16-api",
            "x-idempotency-key": IDEMPOTENCY_KEY,
        },
        json={
            "evidence_bundle_id": BUNDLE_ID,
            "expected_workspace_version": 7,
        },
    )

    assert response.status_code == 409


def test_workspace_payload_projects_all_phase16_append_only_facts() -> None:
    """WebSocket 与后续工作台只能读取完整的服务端事实投影，不读取临时协调结果。"""

    payload = DecisionSupportService(store=_WorkspaceStore()).get_workspace_payload(SESSION_ID)

    assert payload["escalations"] == []
    assert payload["conflict_analyses"] == []
    assert payload["multi_agent_outcomes"] == []
