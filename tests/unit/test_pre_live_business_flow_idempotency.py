"""播前业务服务幂等性测试。

Phase 2F 引入 LangGraph interrupt 后，Graph resume 会从当前节点开头重新执行。
因此高风险建播成功这类副作用必须具备幂等保护，避免 checkpoint 写入前崩溃导致
恢复时重复写成功审计，后续接真实平台 API 时也能避免重复建播。
"""

from __future__ import annotations

from typing import Any

from src.audit.tool_call_audit import AuditEvent
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem


class FakeAuditStore:
    """只在内存中保存审计事件的 Store 替身。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record_event(self, event: AuditEvent) -> str:
        """模拟 PostgreSQL 写入并返回稳定递增的审计 ID。"""

        audit_id = f"audit-{len(self.events) + 1}"
        self.events.append(
            {
                "audit_id": audit_id,
                "trace_id": event.trace_id,
                "tool_name": event.tool_name,
                "request_payload": event.request_payload,
                "operator_decision": event.operator_decision,
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
    assert audit_store.events[0]["request_payload"]["idempotency_key"] == "setup-explicit-001"
