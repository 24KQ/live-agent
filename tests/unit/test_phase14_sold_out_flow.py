"""Phase 14 Task 6 售罄自动保护与人工恢复的 TDD 契约。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.decision_support.models import (
    DecisionKind,
    LiveSessionWorkspace,
    OperatorDecision,
    WorkspaceView,
)
from src.plan_engine.event_state_machine import EventInboxState
from src.plan_engine.event_store import EventDelivery, InMemoryEventStore
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.preemption import (
    PreemptionEvidenceRef,
    PreemptionResult,
    PreemptionStatus,
)
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState
from src.decision_support.store import InMemoryDecisionSupportStore, WorkspaceConflictError
from src.plan_engine.commands import PlanCommand

from src.decision_support.sold_out_flow import (
    HumanGuidedSoldOutFlow,
    SoldOutFlowBoundaryError,
    SoldOutFlowStatus,
)
from src.decision_support.commands import CompiledOperatorDecision


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
SESSION_ID = "live-session-task6"
ROOM_ID = "room-task6"
ROOT_PLAN_ID = "plan-root-task6"


def _workspace_store() -> InMemoryDecisionSupportStore:
    """创建真实 Workspace Store，并通过操作员锁推进到 LIVE。"""

    store = InMemoryDecisionSupportStore()
    store.create_workspace(
        LiveSessionWorkspace(
            live_session_id=SESSION_ID,
            run_key="run-task6",
            room_id=ROOM_ID,
            trace_id="trace-task6",
            anchor_id="anchor-task6",
            root_plan_run_id=ROOT_PLAN_ID,
            event_inbox_scope_id="inbox-task6",
            decision_trace_scope_id="decision-trace-task6",
            replay_scope_id="replay-task6",
            evaluation_scope_id="evaluation-task6",
            view=WorkspaceView.PREPARE,
        )
    )
    lease = store.acquire_operator_lock(SESSION_ID, "operator-task6", 60, now=NOW)
    store.advance_view(
        SESSION_ID,
        target_view=WorkspaceView.LIVE,
        expected_version=1,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=NOW,
    )
    store.release_operator_lock(
        SESSION_ID,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=NOW,
    )
    return store


def _event_store(event_id: str = "event-task6") -> InMemoryEventStore:
    """写入已验证的库存事实；测试不伪造私有可信标记。"""

    store = InMemoryEventStore()
    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id=ROOM_ID,
        product_id="p001",
        observed_version=3,
        occurred_at=NOW - timedelta(seconds=2),
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id=f"provenance-{event_id}",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=NOW - timedelta(seconds=1),
        payload_digest=event.payload_digest,
    )
    store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id=f"occurrence-{event_id}",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=1,
            received_at=NOW - timedelta(seconds=1),
        ),
    )
    return store


class _FakeProtectionCoordinator:
    """只模拟已验证 Phase 12B 协调端口，不执行平台或 Skill。"""

    def __init__(self, result: PreemptionResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def run_next(self, *, root_plan_run_id: str, now: datetime) -> PreemptionResult:
        self.calls.append(("run_next", root_plan_run_id))
        return self.result

    async def reconcile_waiting(
        self,
        *,
        event_id: str,
        root_plan_run_id: str,
        now: datetime,
    ) -> PreemptionResult:
        self.calls.append(("reconcile_waiting", event_id))
        return self.result


class _FailIfCalledCommandService:
    """无人工决定的测试替身；任何命令调用都代表越过人类门禁。"""

    def submit(self, command: PlanCommand, *, now: datetime):
        raise AssertionError("没有 OperatorDecision 时不得提交 PlanCommand")


def _applied_result(event_id: str = "event-task6") -> PreemptionResult:
    """构造与确定性事实摘要闭合的保护成功结果。"""

    evidence = PreemptionEvidenceRef.create(
        event_id=event_id,
        root_plan_run_id=ROOT_PLAN_ID,
        application_state="APPLIED",
        emergency_plan_run_id="plan-emergency-task6",
        applied_plan_version=2,
        final_suggestion_fact="售罄已被确定性控制面处理，等待运营决定恢复经营动作",
    )
    return PreemptionResult(
        status=PreemptionStatus.APPLIED,
        event_id=event_id,
        root_plan_run_id=ROOT_PLAN_ID,
        evidence_ref=evidence,
    )


def test_verified_sold_out_runs_automatic_protection_and_appends_incident() -> None:
    """可信事件只能在同一 LIVE Workspace 运行保护，并持久化父 Incident。"""

    event_id = "event-task6"
    protection = _FakeProtectionCoordinator(_applied_result(event_id))
    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=_event_store(event_id),
        protection_coordinator=protection,
        command_service=_FailIfCalledCommandService(),
    )

    result = asyncio.run(
        flow.handle_verified_event(
            event_id=event_id,
            root_plan_run_id=ROOT_PLAN_ID,
            now=NOW,
        )
    )

    assert result.status is SoldOutFlowStatus.PROTECTED
    assert protection.calls == [("run_next", ROOT_PLAN_ID)]
    incident = flow.workspace_store.list_incidents(SESSION_ID)[0]
    assert incident.snapshot["event_id"] == event_id
    assert incident.snapshot["product_id"] == "p001"


def test_unknown_side_effect_stays_waiting_reconciliation() -> None:
    """SIDE_EFFECT_UNKNOWN 不得变成成功，也不能触发人工恢复命令。"""

    event_id = "event-task6-unknown"
    failure = FailureFact(
        category=FailureCategory.SIDE_EFFECT_UNKNOWN,
        external_code="platform.timeout",
        side_effect_state=SideEffectState.UNKNOWN,
        attempt_id="attempt-task6-unknown",
    )
    protection = _FakeProtectionCoordinator(
        PreemptionResult(
            status=PreemptionStatus.WAITING_RECONCILIATION,
            event_id=event_id,
            root_plan_run_id=ROOT_PLAN_ID,
            failure=failure,
        )
    )
    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=_event_store(event_id),
        protection_coordinator=protection,
        command_service=_FailIfCalledCommandService(),
    )

    result = asyncio.run(
        flow.handle_verified_event(
            event_id=event_id,
            root_plan_run_id=ROOT_PLAN_ID,
            now=NOW,
        )
    )

    assert result.status is SoldOutFlowStatus.WAITING_RECONCILIATION
    assert result.failure is not None
    assert result.failure.category is FailureCategory.SIDE_EFFECT_UNKNOWN


def test_applied_event_is_replayed_without_second_protection_call() -> None:
    """响应丢失后的 APPLIED 重放只读取 Incident，不再次调用协调器。"""

    event_id = "event-task6-applied"
    event_store = _event_store(event_id)
    claim = event_store.claim_next_for_room(
        "task6-replay-worker",
        room_id=ROOM_ID,
        now=NOW,
        lease_seconds=60,
    )
    assert claim is not None
    event_store.transition_inbox(
        event_id,
        expected_state=EventInboxState.PROCESSING,
        target_state=EventInboxState.APPLIED,
        now=NOW,
        worker_id="task6-replay-worker",
        fencing_token=claim.fencing_token,
    )
    protection = _FakeProtectionCoordinator(_applied_result(event_id))
    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=event_store,
        protection_coordinator=protection,
        command_service=_FailIfCalledCommandService(),
    )

    result = asyncio.run(
        flow.handle_verified_event(
            event_id=event_id,
            root_plan_run_id=ROOT_PLAN_ID,
            now=NOW,
        )
    )

    assert result.status is SoldOutFlowStatus.PROTECTED
    assert protection.calls == []


def test_reconcile_waiting_uses_read_only_coordinator_port() -> None:
    """等待对账入口只能调用只读 reconcile_waiting，不会走新事件保护。"""

    event_id = "event-task6-reconcile"
    protection = _FakeProtectionCoordinator(
        PreemptionResult(
            status=PreemptionStatus.RETRY_PENDING,
            event_id=event_id,
            root_plan_run_id=ROOT_PLAN_ID,
        )
    )
    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=_event_store(event_id),
        protection_coordinator=protection,
        command_service=_FailIfCalledCommandService(),
    )

    result = asyncio.run(
        flow.reconcile_waiting(
            event_id=event_id,
            root_plan_run_id=ROOT_PLAN_ID,
            now=NOW,
        )
    )

    assert result.status is SoldOutFlowStatus.PROTECTION_PENDING
    assert protection.calls == [("reconcile_waiting", event_id)]


def test_event_from_another_room_is_rejected_before_coordinator() -> None:
    """事件 room 与 Workspace 不一致时必须在保护端口前 fail-closed。"""

    store = _event_store("event-task6-wrong-room")
    event = store.get_inbox("event-task6-wrong-room").event
    wrong_event = event.model_copy(update={"room_id": "room-other"})
    # 通过真实模型重新计算摘要后写入另一条事实，避免测试依赖非法绕过实例。
    wrong_event = InventoryFactEvent.create_sold_out(
        event_id=wrong_event.event_id,
        room_id=wrong_event.room_id,
        product_id=wrong_event.product_id,
        observed_version=wrong_event.observed_version,
        occurred_at=wrong_event.occurred_at,
        source=wrong_event.source,
    )
    wrong_store = InMemoryEventStore()
    provenance = VerifiedIngressProvenance(
        provenance_id="provenance-wrong-room",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=wrong_event.source,
        received_at=NOW - timedelta(seconds=1),
        payload_digest=wrong_event.payload_digest,
    )
    wrong_store.register_event(
        wrong_event,
        provenance,
        EventDelivery(
            occurrence_id="occurrence-wrong-room",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=1,
            received_at=NOW - timedelta(seconds=1),
        ),
    )
    protection = _FakeProtectionCoordinator(_applied_result("event-task6-wrong-room"))
    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=wrong_store,
        protection_coordinator=protection,
        command_service=_FailIfCalledCommandService(),
    )

    with pytest.raises(WorkspaceConflictError, match="room"):
        asyncio.run(
            flow.handle_verified_event(
                event_id="event-task6-wrong-room",
                root_plan_run_id=ROOT_PLAN_ID,
                now=NOW,
            )
        )
    assert protection.calls == []


def test_recovery_requires_compiled_operator_decision() -> None:
    """原始 PlanCommand 不能作为售罄经营恢复入口。"""

    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=_event_store(),
        protection_coordinator=_FakeProtectionCoordinator(_applied_result()),
        command_service=_FailIfCalledCommandService(),
    )
    raw_command = PlanCommand(
        command_id="raw-recovery-command",
        command_type="APPROVE",
        plan_run_id=ROOT_PLAN_ID,
        expected_plan_version=1,
        node_id="node-waiting-approval",
        expected_node_status="WAITING_APPROVAL",
        payload={"backup_product_id": "p002"},
        issued_at=NOW,
    )

    with pytest.raises(SoldOutFlowBoundaryError, match="OperatorDecision"):
        flow.submit_raw_recovery_command(raw_command, now=NOW)


def test_compiled_recovery_requires_incident_binding_from_authoritative_store() -> None:
    """同一会话内没有已持久化 Incident 时也不能执行恢复。"""

    flow = HumanGuidedSoldOutFlow(
        workspace_store=_workspace_store(),
        event_store=_event_store(),
        protection_coordinator=_FakeProtectionCoordinator(_applied_result()),
        command_service=_FailIfCalledCommandService(),
    )
    decision = OperatorDecision(
        decision_id="decision-without-incident",
        live_session_id=SESSION_ID,
        proposal_id="proposal-task6",
        idempotency_key="decision-without-incident-idem",
        expected_proposal_version=1,
        operator_id="operator-task6",
        decision_kind=DecisionKind.REJECT,
        reason_code="EVIDENCE_CONFLICT",
        snapshot={"option_id": None},
        created_at=NOW,
    )

    with pytest.raises(SoldOutFlowBoundaryError, match="incident_id"):
        flow.submit_compiled_recovery(
            compiled=CompiledOperatorDecision(operator_decision=decision),
            expected_workspace_version=1,
            operator_id="operator-task6",
            fencing_token=1,
            now=NOW,
        )
