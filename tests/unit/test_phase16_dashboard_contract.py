"""Phase 16 Task 8 受控多 Agent 播中工作台的无浏览器 RED/GREEN 契约。"""

from __future__ import annotations

from pathlib import Path


INDEX = Path(__file__).parents[2] / "front" / "index.html"


def _html() -> str:
    """以 UTF-8 读取真实静态入口，避免测试副本掩盖页面与服务端协议漂移。"""

    return INDEX.read_text(encoding="utf-8")


def test_live_workspace_shows_only_server_backed_multi_agent_facts() -> None:
    """升级区必须展示 route、trigger、analysis 与 outcome，不能要求运营手填 Bundle ID。"""

    html = _html()

    for token in (
        "multi-agent-escalation-panel",
        "multi-agent-route",
        "multi-agent-triggers",
        "multi-agent-analysis",
        "multi-agent-outcome",
        "evidence_bundles",
        "renderMultiAgentEscalation",
    ):
        assert token in html
    assert 'id="multi-agent-bundle-id"' not in html


def test_live_workspace_posts_only_narrow_canonical_escalation_request() -> None:
    """浏览器只从服务端 Bundle 摘要构造 canonical key、ID 与 Workspace CAS。"""

    html = _html()

    for token in (
        "MULTI_AGENT_ESCALATION_SUFFIX",
        "requestMultiAgentEscalation",
        "phase16-escalation:operator_requested:",
        "evidence_bundle_id: bundle.evidence_bundle_id",
        "expected_workspace_version: workspace.version",
        '"X-Idempotency-Key": idempotencyKey',
    ):
        assert token in html
    assert "trigger_codes:" not in html[html.index("function requestMultiAgentEscalation"):]
    assert "fencing_token:" not in html[html.index("function requestMultiAgentEscalation"):]


def test_live_workspace_uses_short_lived_subscription_ticket_not_operator_token_in_url() -> None:
    """浏览器必须先经认证 REST 签发票据，再把不透明值放入 WebSocket subprotocol。"""

    html = _html()

    for token in (
        "SUBSCRIPTION_TICKET_SUFFIX",
        "requestDecisionSupportSubscriptionTicket",
        "liveagent.ticket.",
        "subprotocols",
    ):
        assert token in html
    websocket_start = html.index("function connectDecisionSupportSocket")
    websocket_end = html.index('document.getElementById("live-session-id")', websocket_start)
    websocket_body = html[websocket_start:websocket_end]
    assert "X-Operator-Token" not in websocket_body
    assert "token-input" not in websocket_body


def test_live_workspace_invalidates_stale_ticket_requests_and_reconnects_after_auth_changes() -> None:
    """旧会话票据返回不得覆盖新会话，输入新的认证信息必须重新取得权威订阅。"""

    html = _html()

    for token in (
        "connectionGeneration",
        "generation !== state.connectionGeneration",
        "targetSession !== sessionId()",
        "reconnectDecisionSupportWorkspace",
        "authRefreshTimer",
    ):
        assert token in html
    escalation_start = html.index("function requestMultiAgentEscalation")
    escalation_end = html.index("function requestDecisionSupportSubscriptionTicket", escalation_start)
    assert 'textContent = "DEGRADED：" + error.message' not in html[escalation_start:escalation_end]


def test_operator_execution_controls_stay_disabled_without_matching_ready_outcome() -> None:
    """多 Agent Proposal 没有同一 Proposal ID 的 READY Outcome 时，UI 不得开放经营决定。"""

    html = _html()

    for token in (
        "matchingReadyMultiAgentOutcome",
        "MULTI_AGENT",
        "Multi-Agent Proposal 尚未通过 READY 校验",
        "DEGRADED：仅展示确定性事实摘要",
    ):
        assert token in html


def test_current_escalation_cannot_fall_back_to_unrelated_single_copilot_proposal() -> None:
    """当前高冲突事实尚未 READY 时，最后一条无关 Proposal 不得解锁运营决定。"""

    html = _html()
    proposal_start = html.index("function currentProposal")
    proposal_end = html.index("function isMultiAgentProposal", proposal_start)
    proposal_body = html[proposal_start:proposal_end]

    for token in (
        "currentMultiAgentState",
        "facts.escalation",
        'facts.outcome.status !== "READY"',
        "return null",
    ):
        assert token in proposal_body


def test_workspace_read_failure_uses_unavailable_state_not_fabricated_degraded_outcome() -> None:
    """HTTP/认证读取错误必须 fail-closed，但不能冒充服务端 MultiAgentOutcome。"""

    html = _html()
    load_start = html.index("async function loadWorkspace")
    load_end = html.index("function renderWorkspace", load_start)
    load_body = html[load_start:load_end]

    assert 'state.dataState = "UNAVAILABLE"' in load_body
    assert 'state.dataState = "DEGRADED"' not in load_body


def test_operator_decision_write_failure_does_not_fabricate_degraded_outcome() -> None:
    """决定提交失败必须保持服务端事实不变，不能被客户端伪造成模型终态。"""

    html = _html()
    submit_start = html.index("async function submitDecision")
    submit_end = html.index("async function appendCurrentProposal", submit_start)
    submit_body = html[submit_start:submit_end]

    assert 'textContent = "提交失败：" + error.message' in submit_body
    assert 'textContent = "DEGRADED：" + error.message' not in submit_body
