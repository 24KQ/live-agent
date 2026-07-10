"""Phase 2F 人工审批输入模型测试。

这些测试先约束人审数据的最小合规边界：审批结果只能是批准或拒绝，
且恢复 payload 必须和 interrupt 时发出的审批请求属于同一条 trace、同一直播间和同一工具。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.human_approval import (
    HumanApprovalDecision,
    HumanApprovalRequest,
    HumanApprovalResponse,
    validate_human_approval_response,
)
from src.state.models import RiskLevel


def _approval_request(trace_id: str = "trace-phase2f-approval") -> HumanApprovalRequest:
    """构造标准审批请求，便于不同测试只覆盖自己关心的字段。"""

    return HumanApprovalRequest(
        trace_id=trace_id,
        room_id="room-demo-001",
        tool_name="setup_live_session",
        risk_level=RiskLevel.HIGH,
        action="confirm_setup_live_session",
        plan_item_ids=["p001", "p002"],
        message="请确认是否按当前排品方案模拟建播。",
    )


def test_human_approval_response_accepts_approved_and_rejected_decisions() -> None:
    """人工审批恢复输入只接受 approved/rejected 两种明确决策。"""

    approved = HumanApprovalResponse(
        trace_id="trace-phase2f-approval",
        room_id="room-demo-001",
        tool_name="setup_live_session",
        decision=HumanApprovalDecision.APPROVED,
        operator_id="operator-demo",
        reason="确认建播配置无误。",
    )
    rejected = HumanApprovalResponse(
        trace_id="trace-phase2f-approval",
        room_id="room-demo-001",
        tool_name="setup_live_session",
        decision="rejected",
        operator_id="operator-demo",
        reason="需要调整排品后再建播。",
    )

    assert approved.decision == HumanApprovalDecision.APPROVED
    assert rejected.decision == HumanApprovalDecision.REJECTED


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("trace_id", " "),
        ("room_id", ""),
        ("tool_name", " "),
        ("operator_id", ""),
        ("reason", " "),
    ],
)
def test_human_approval_response_rejects_blank_required_fields(field_name: str, field_value: str) -> None:
    """审批恢复输入中的关键字段不能为空，避免审计链路无法回放。"""

    payload = {
        "trace_id": "trace-phase2f-approval",
        "room_id": "room-demo-001",
        "tool_name": "setup_live_session",
        "decision": "approved",
        "operator_id": "operator-demo",
        "reason": "确认建播配置无误。",
    }
    payload[field_name] = field_value

    with pytest.raises(ValidationError):
        HumanApprovalResponse.model_validate(payload)


def test_human_approval_response_rejects_unknown_decision() -> None:
    """未知审批决策必须 fail-closed，不能被当成默认批准或默认拒绝。"""

    with pytest.raises(ValidationError):
        HumanApprovalResponse.model_validate(
            {
                "trace_id": "trace-phase2f-approval",
                "room_id": "room-demo-001",
                "tool_name": "setup_live_session",
                "decision": "maybe",
                "operator_id": "operator-demo",
                "reason": "未知决策不应通过。",
            }
        )


def test_validate_human_approval_response_rejects_trace_mismatch() -> None:
    """恢复 payload 的 trace_id 必须和 pending interrupt 请求一致。"""

    request = _approval_request(trace_id="trace-expected")
    response = HumanApprovalResponse(
        trace_id="trace-other",
        room_id=request.room_id,
        tool_name=request.tool_name,
        decision="approved",
        operator_id="operator-demo",
        reason="trace 不一致时不能恢复。",
    )

    with pytest.raises(ValueError, match="trace_id"):
        validate_human_approval_response(request, response)


def test_validate_human_approval_response_returns_clean_response_when_matched() -> None:
    """审批请求和恢复输入一致时，应返回已标准化的审批响应对象。"""

    request = _approval_request()
    response = HumanApprovalResponse(
        trace_id=request.trace_id,
        room_id=request.room_id,
        tool_name=request.tool_name,
        decision="approved",
        operator_id="operator-demo",
        reason="确认建播配置无误。",
    )

    assert validate_human_approval_response(request, response) == response


def test_human_approval_request_supports_on_live_tool_context_defaults() -> None:
    """Phase 5I：播中审批新增字段应有默认值，避免破坏播前旧调用。"""

    request = _approval_request()

    assert request.tool_arguments == {}
    assert request.context_summary is None


def test_human_approval_request_serializes_on_live_tool_context() -> None:
    """Phase 5I：播中审批 payload 应能携带工具参数和压缩上下文，供副屏/CLI 展示。"""

    request = HumanApprovalRequest(
        trace_id="trace-on-live-approval",
        room_id="room-demo-001",
        tool_name="handle_sold_out_event",
        risk_level=RiskLevel.HIGH,
        action="approve_on_live_tool_call",
        message="是否允许 Agent 执行售罄处理工具？",
        tool_arguments={"product_id": "p001"},
        context_summary="库存售罄，弹幕集中询问能否继续购买。",
    )

    payload = request.model_dump(mode="json")

    assert payload["tool_arguments"] == {"product_id": "p001"}
    assert payload["context_summary"] == "库存售罄，弹幕集中询问能否继续购买。"
