"""播前业务服务幂等性测试。

Phase 2F 引入 LangGraph interrupt 后，Graph resume 会从当前节点开头重新执行。
因此高风险建播成功这类副作用必须具备幂等保护，避免 checkpoint 写入前崩溃导致
恢复时重复写成功审计，后续接真实平台 API 时也能避免重复建播。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.audit import tool_call_audit
from src.audit.tool_call_audit import AuditEvent
from src.core.human_approval import HumanApprovalDecision, HumanApprovalRequest, HumanApprovalResponse
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.state.models import RiskLevel


class FakeAuditStore:
    """在内存中模拟生产 Store 的工具级全局幂等冲突语义。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_event(self, event: AuditEvent) -> str:
        """等价重放返回旧 ID，任何审计事实不同则抛出受控冲突。"""

        for existing in self.events:
            if existing["tool_name"] != event.tool_name:
                continue
            if existing["idempotency_key"] != event.idempotency_key or event.idempotency_key is None:
                continue
            expected = {
                "trace_id": event.trace_id,
                "room_id": event.room_id,
                "tool_name": event.tool_name,
                "action_type": event.action_type,
                "risk_level": event.risk_level,
                "gate_decision": event.gate_decision,
                "operator_decision": event.operator_decision,
                "idempotency_key": event.idempotency_key,
                "request_payload": event.request_payload,
                "result_payload": event.result_payload,
            }
            if all(existing[field] == value for field, value in expected.items()):
                return existing["audit_id"]
            raise tool_call_audit.IdempotencyConflictError("conflicting audit idempotency replay")

        audit_id = f"audit-{len(self.events) + 1}"
        self.events.append(
            {
                "audit_id": audit_id,
                "trace_id": event.trace_id,
                "room_id": event.room_id,
                "tool_name": event.tool_name,
                "action_type": event.action_type,
                "risk_level": event.risk_level,
                "gate_decision": event.gate_decision,
                "request_payload": event.request_payload,
                "result_payload": event.result_payload,
                "operator_decision": event.operator_decision,
                "idempotency_key": event.idempotency_key,
            }
        )
        return audit_id

    def list_events_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """按 trace_id 返回审计链路，模拟真实审计 Store 的查询接口。"""

        return [event for event in self.events if event["trace_id"] == trace_id]


def _sample_plan(trace_id: str) -> LivePlanDraft:
    """构造最小排品方案，用于触发建播成功审计。"""

    return LivePlanDraft(
        room_id="room-demo-001",
        trace_id=trace_id,
        items=[
            LivePlanItem(
                rank=1,
                product_id="p001",
                product_name="轻盈保温杯",
                role="引流款",
                reason="幂等性测试固定排品理由",
            )
        ],
    )


def test_setup_live_session_reuses_existing_success_audit_for_same_idempotency_key() -> None:
    """同一 trace 的建播成功重放应复用已有 audit_id，而不是重复写审计。"""

    audit_store = FakeAuditStore()
    service = PreLiveBusinessFlowService(catalog_repository=object(), audit_store=audit_store)  # type: ignore[arg-type]
    trace_id = "trace-phase2f-setup-idempotent"
    plan = _sample_plan(trace_id)

    first_gate, first_audit_id = service.setup_live_session(
        room_id="room-demo-001",
        plan=plan,
        trace_id=trace_id,
        confirmed_setup=True,
    )
    second_gate, second_audit_id = service.setup_live_session(
        room_id="room-demo-001",
        plan=plan,
        trace_id=trace_id,
        confirmed_setup=True,
    )

    assert first_gate.allowed is True
    assert second_gate.allowed is True
    assert first_audit_id == "audit-1"
    assert second_audit_id == first_audit_id
    assert len(audit_store.events) == 1
    assert audit_store.events[0]["tool_name"] == "setup_live_session"
    assert audit_store.events[0]["idempotency_key"] == f"{trace_id}:setup_live_session"
    assert audit_store.events[0]["request_payload"]["idempotency_key"] == f"{trace_id}:setup_live_session"


def test_setup_live_session_uses_explicit_idempotency_key_for_replay() -> None:
    """Runtime 提供的显式幂等键必须写入审计，并用于复用同一成功结果。"""
    audit_store = FakeAuditStore()
    service = PreLiveBusinessFlowService(catalog_repository=object(), audit_store=audit_store)  # type: ignore[arg-type]
    trace_id = "trace-phase11a-explicit-key"
    plan = _sample_plan(trace_id)

    first = service.setup_live_session(
        room_id="room-demo-001",
        plan=plan,
        trace_id=trace_id,
        confirmed_setup=True,
        idempotency_key="setup-explicit-001",
    )
    second = service.setup_live_session(
        room_id="room-demo-001",
        plan=plan,
        trace_id=trace_id,
        confirmed_setup=True,
        idempotency_key="setup-explicit-001",
    )

    assert first[1] == second[1] == "audit-1"
    assert len(audit_store.events) == 1
    assert audit_store.events[0]["idempotency_key"] == "setup-explicit-001"
    assert audit_store.events[0]["request_payload"]["idempotency_key"] == "setup-explicit-001"


def test_setup_live_session_rejects_same_trace_and_key_with_different_plan() -> None:
    """同 trace、同幂等键若排品结果不同，Service 不得直接复用旧审计 ID。"""

    audit_store = FakeAuditStore()
    service = PreLiveBusinessFlowService(catalog_repository=object(), audit_store=audit_store)  # type: ignore[arg-type]
    trace_id = "trace-phase11a-conflicting-plan"
    original_plan = _sample_plan(trace_id)
    conflicting_plan = LivePlanDraft(
        room_id=original_plan.room_id,
        trace_id=trace_id,
        items=[
            LivePlanItem(
                rank=1,
                product_id="p002",
                product_name="冲突测试商品",
                role="利润款",
                reason="同一调用键不得承载不同排品结果",
            )
        ],
    )

    service.setup_live_session(
        room_id="room-demo-001",
        plan=original_plan,
        trace_id=trace_id,
        confirmed_setup=True,
        idempotency_key="setup-conflicting-plan",
    )

    with pytest.raises(tool_call_audit.IdempotencyConflictError):
        service.setup_live_session(
            room_id="room-demo-001",
            plan=conflicting_plan,
            trace_id=trace_id,
            confirmed_setup=True,
            idempotency_key="setup-conflicting-plan",
        )

    assert len(audit_store.events) == 1
    assert audit_store.events[0]["result_payload"]["plan_item_ids"] == ["p001"]


def _approval_request() -> HumanApprovalRequest:
    """构造最小建播审批请求，集中约束 approval 审计键的输入事实。"""

    return HumanApprovalRequest(
        trace_id="trace-phase11a-approval-idempotency",
        room_id="room-demo-001",
        tool_name="setup_live_session",
        risk_level=RiskLevel.HIGH,
        action="confirm_setup_live_session",
        plan_item_ids=["p001"],
        message="请确认建播。",
    )


def test_record_setup_approval_event_sets_explicit_idempotency_key() -> None:
    """pending 与 resumed 审批事件都必须把派生键写入 AuditEvent 独立字段。"""

    audit_store = FakeAuditStore()
    service = PreLiveBusinessFlowService(catalog_repository=object(), audit_store=audit_store)  # type: ignore[arg-type]
    request = _approval_request()
    response = HumanApprovalResponse(
        trace_id=request.trace_id,
        room_id=request.room_id,
        tool_name=request.tool_name,
        decision=HumanApprovalDecision.APPROVED,
        operator_id="operator-phase11a",
        reason="审批内容已核对。",
    )

    service.record_setup_approval_event(request, None)
    service.record_setup_approval_event(request, response)

    assert [event["idempotency_key"] for event in audit_store.events] == [
        f"{request.trace_id}:{request.tool_name}:approval:pending",
        f"{request.trace_id}:{request.tool_name}:approval:approved",
    ]
    assert all(
        event["request_payload"]["idempotency_key"] == event["idempotency_key"]
        for event in audit_store.events
    )


def test_record_setup_approval_event_rejects_same_key_with_different_response() -> None:
    """相同审批状态生成同一键时，操作员或原因变化不得复用旧审批审计。"""

    audit_store = FakeAuditStore()
    service = PreLiveBusinessFlowService(catalog_repository=object(), audit_store=audit_store)  # type: ignore[arg-type]
    request = _approval_request()
    first_response = HumanApprovalResponse(
        trace_id=request.trace_id,
        room_id=request.room_id,
        tool_name=request.tool_name,
        decision=HumanApprovalDecision.APPROVED,
        operator_id="operator-first",
        reason="首次批准。",
    )
    conflicting_response = first_response.model_copy(
        update={"operator_id": "operator-second", "reason": "不同审批事实。"}
    )
    service.record_setup_approval_event(request, first_response)

    with pytest.raises(tool_call_audit.IdempotencyConflictError):
        service.record_setup_approval_event(request, conflicting_response)

    assert len(audit_store.events) == 1
    assert audit_store.events[0]["result_payload"]["operator_id"] == "operator-first"
