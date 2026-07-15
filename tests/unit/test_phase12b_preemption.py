"""Phase 12B PreemptionCoordinator 与启动冻结路由测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from typing import Any

import pytest

from src.config.settings import Settings
from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.event_state_machine import EventApplicationState, EventInboxState
from src.plan_engine.event_store import EventDelivery, InMemoryEventStore
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.replan import ReplanCoordinator
from src.plan_engine.store import InMemoryPlanStore, MaterializedPlan
from src.plan_engine.worker import PlanWorker
from src.skill_runtime.models import SkillExecutionResult, SkillExecutionStatus
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState
from src.plan_engine.side_effect_reconciliation import (
    SoldOutReconciliationResult,
    SoldOutReconciliationStatus,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def _module() -> Any:
    """延迟导入待实现模块，使 RED 明确指向 Task 10 边界。"""

    return importlib.import_module("src.plan_engine.preemption")


def _product(product_id: str) -> CatalogProduct:
    """构造可被手卡 Replan 重放的完整商品快照。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"商品 {product_id}",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["稳定卖点"],
    )


def _root_plan() -> MaterializedPlan:
    """创建三商品 CARD_BATCH，确保 p001 事件只有局部影响闭包。"""

    product_ids = ("p001", "p002", "p003")
    planning_input = CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-root-001",
        live_plan=LivePlanDraft(
            room_id="room-001",
            trace_id="trace-root-001",
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="Task 10 测试",
                )
                for index, product_id in enumerate(product_ids, start=1)
            ],
        ),
        products_by_id={product_id: _product(product_id) for product_id in product_ids},
    )
    canonical = CanonicalCardBatchProposalProvider().propose_sync(planning_input)
    proposal = CandidatePlanProposal.model_validate(canonical.model_dump(mode="json"))
    capabilities: dict[str, ResolvedPlanCapability] = {}
    for node in proposal.nodes:
        node_type = (
            "PREPARE_CARD_BATCH"
            if node.logical_key == "prepare-card-batch"
            else "COLLECT_CARD_RESULTS"
            if node.logical_key == "collect-card-results"
            else "SKILL"
        )
        product_id = node.logical_key.removeprefix("card:")
        capabilities[node.logical_key] = ResolvedPlanCapability(
            node_type=node_type,
            skill_id=node.skill_id,
            skill_version="1.0.0" if node.skill_id else None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=30 if node.skill_id else None,
            resource_keys=(
                (f"room:room-001:product:{product_id}",)
                if node.skill_id
                else ()
            ),
            max_concurrency=4,
        )
    return MaterializedPlan(
        planning_input=planning_input,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _register_verified_event(
    store: InMemoryEventStore,
    *,
    event_id: str = "event-task10-001",
    room_id: str = "room-001",
    occurrence_id: str = "occurrence-task10-001",
    offset: int = 1,
) -> InventoryFactEvent:
    """登记并验证一条可信 Kafka 售罄事实。"""

    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id=room_id,
        product_id="p001",
        observed_version=3,
        occurred_at=NOW - timedelta(seconds=2),
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id="provenance-task10-001",
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
            occurrence_id=occurrence_id,
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=offset,
            received_at=NOW - timedelta(seconds=1),
        ),
    )
    return event


class _SuccessfulEmergencyExecutor:
    """返回固定售罄、备选和主播提示事实，不访问任何外部平台。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, call: Any) -> SkillExecutionResult:
        """按 Skill ID 返回下游绑定所需的最小完整输出。"""

        self.calls.append(call.skill_id)
        outputs = {
            "handle_sold_out_event": {"sold_out_product": {"product_id": "p001"}},
            "recommend_backup_product": {"backup_product": {"product_id": "p002"}},
            "generate_on_live_prompt": {"prompt": {"message": "商品已售罄，请切换 p002"}},
        }
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.SUCCESS,
            output=outputs[call.skill_id],
            summary="测试成功",
        )


class _UnknownThenSuccessfulExecutor(_SuccessfulEmergencyExecutor):
    """首次售罄写返回副作用未知，严格对账后其余节点正常执行。"""

    async def execute(self, call: Any) -> SkillExecutionResult:
        if call.skill_id == "handle_sold_out_event":
            self.calls.append(call.skill_id)
            failure = FailureFact(
                category=FailureCategory.SIDE_EFFECT_UNKNOWN,
                external_code="fake.sold_out_unknown",
                side_effect_state=SideEffectState.UNKNOWN,
                attempt_id="attempt-task10-unknown",
            )
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                summary="结果未知",
                failure=failure,
                attempt_id=failure.attempt_id,
            )
        return await super().execute(call)


class _ConfirmingReconciler:
    """只读返回已确认事实，记录请求以验证引用原 Attempt。"""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def reconcile(self, request: Any) -> SoldOutReconciliationResult:
        self.requests.append(request)
        return SoldOutReconciliationResult(
            status=SoldOutReconciliationStatus.CONFIRMED_SUCCESS,
            original_attempt_id=request.original_failure.attempt_id,
            evidence={
                "event_id": request.event_authorization.event_id,
                "product_id": request.product_id,
                "confirmed_version": request.expected_version + 1,
            },
            reason_code="SOLD_OUT_FACT_CONFIRMED",
        )


def test_sold_out_route_defaults_to_legacy_and_freezes_settings_snapshot() -> None:
    """默认路由不改变生产行为，Settings 后续变化不能改写已装配策略。"""

    module = _module()
    settings = Settings(_env_file=None)
    policy = module.SoldOutRoutePolicy.from_settings(settings)

    assert settings.sold_out_execution_route == "LEGACY"
    assert policy.route is module.SoldOutExecutionRoute.LEGACY
    settings.sold_out_execution_route = "PLAN_ENGINE"
    assert policy.route is module.SoldOutExecutionRoute.LEGACY


def test_coordinator_runs_verified_event_through_child_and_replan_without_fallback() -> None:
    """可信事件必须形成冻结、紧急计划、Replan 与 EvidenceRef 的完整单次链路。"""

    module = _module()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan())
    event_store = InMemoryEventStore()
    event = _register_verified_event(event_store)
    executor = _SuccessfulEmergencyExecutor()
    worker = PlanWorker(
        store=plan_store,
        event_store=event_store,
        skill_executor=executor,
        worker_id="worker-task10",
        clock=lambda: NOW + timedelta(seconds=1),
        max_claims=1,
    )
    coordinator = module.PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=worker,
        replan_coordinator=ReplanCoordinator(
            plan_store=plan_store,
            event_store=event_store,
        ),
        worker_id="coordinator-task10",
        clock=lambda: NOW + timedelta(seconds=2),
    )

    result = asyncio.run(
        coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW)
    )

    assert result.status is module.PreemptionStatus.APPLIED
    assert result.evidence_ref is not None
    assert result.evidence_ref.event_id == event.event_id
    assert result.evidence_ref.applied_plan_version == 2
    assert "切换 p002" in result.evidence_ref.final_suggestion_fact
    assert executor.calls == [
        "handle_sold_out_event",
        "recommend_backup_product",
        "generate_on_live_prompt",
    ]
    assert event_store.get_inbox(event.event_id).state is EventInboxState.APPLIED
    assert plan_store.get_plan_run(root.plan_run_id).current_version == 2
    child = plan_store.get_plan_run(result.evidence_ref.emergency_plan_run_id)
    assert child.state is PlanRunState.SUCCEEDED
    replay = asyncio.run(
        coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW)
    )
    assert replay.status is module.PreemptionStatus.IDLE


def test_evidence_ref_rejects_digest_rebinding() -> None:
    """EvidenceRef 不能把新建议绑定到旧摘要，避免 Harness 消费伪造事实。"""

    module = _module()
    evidence = module.PreemptionEvidenceRef.create(
        event_id="event-digest-001",
        root_plan_run_id="root-digest-001",
        application_state="APPLIED",
        emergency_plan_run_id="child-digest-001",
        applied_plan_version=2,
        final_suggestion_fact="原始建议",
    )
    payload = evidence.model_dump(mode="json")
    payload["final_suggestion_fact"] = "被篡改建议"

    try:
        module.PreemptionEvidenceRef.model_validate(payload)
    except ValueError:
        pass
    else:
        raise AssertionError("EvidenceRef 必须拒绝摘要重绑定")


def test_evidence_ref_rejects_non_applied_application() -> None:
    """等待对账或执行中的 Application 不能伪装成 Harness 最终建议证据。"""

    module = _module()
    with pytest.raises(ValueError, match="APPLIED"):
        module.PreemptionEvidenceRef.create(
            event_id="event-waiting-evidence",
            root_plan_run_id="root-waiting-evidence",
            application_state="WAITING_RECONCILIATION",
            emergency_plan_run_id="child-waiting-evidence",
            applied_plan_version=1,
            final_suggestion_fact="尚未确认的建议",
        )


def test_coordinator_has_no_legacy_fallback_dependency() -> None:
    """Coordinator 的公开构造器不能接受同次 Legacy fallback 回调。"""

    module = _module()
    try:
        module.PreemptionCoordinator(
            plan_store=InMemoryPlanStore(),
            event_store=InMemoryEventStore(),
            emergency_worker=object(),
            replan_coordinator=object(),
            worker_id="coordinator-no-fallback",
            legacy_fallback=lambda: None,
        )
    except TypeError:
        pass
    else:
        raise AssertionError("PreemptionCoordinator must not accept legacy_fallback")


def test_coordinator_claims_only_events_for_selected_root_room() -> None:
    """其他直播间更早的事件必须保持 VERIFIED，不能被错误绑定或永久失败。"""

    module = _module()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan())
    event_store = InMemoryEventStore()
    other = _register_verified_event(
        event_store,
        event_id="event-other-room",
        room_id="room-other",
        occurrence_id="occurrence-other-room",
        offset=0,
    )
    selected = _register_verified_event(event_store)
    executor = _SuccessfulEmergencyExecutor()
    worker = PlanWorker(
        store=plan_store,
        event_store=event_store,
        skill_executor=executor,
        worker_id="worker-room-filter",
        clock=lambda: NOW + timedelta(seconds=1),
        max_claims=1,
    )
    coordinator = module.PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=worker,
        replan_coordinator=ReplanCoordinator(plan_store=plan_store, event_store=event_store),
        worker_id="coordinator-room-filter",
        clock=lambda: NOW + timedelta(seconds=2),
    )

    result = asyncio.run(coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW))

    assert result.event_id == selected.event_id
    assert event_store.get_inbox(other.event_id).state is EventInboxState.VERIFIED


def test_root_ambiguity_after_claim_releases_event_with_current_fencing(monkeypatch) -> None:
    """检查与 claim 间出现第二个 root 时，事件必须退回 VERIFIED 且不建 Application。"""

    module = _module()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan())
    event_store = InMemoryEventStore()
    event = _register_verified_event(event_store)
    coordinator = module.PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=object(),  # type: ignore[arg-type]
        replan_coordinator=object(),  # type: ignore[arg-type]
        worker_id="coordinator-root-race",
        clock=lambda: NOW + timedelta(seconds=1),
    )
    snapshots = iter(((root.plan_run_id,), (root.plan_run_id, "root-raced")))
    monkeypatch.setattr(
        coordinator,
        "_active_card_batch_roots",
        lambda room_id: next(snapshots),
    )

    result = asyncio.run(coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW))

    assert result.status is module.PreemptionStatus.RETRY_PENDING
    assert event_store.get_inbox(event.event_id).state is EventInboxState.VERIFIED
    assert event_store.list_applications(root_plan_run_id=root.plan_run_id) == ()


def test_unknown_side_effect_requires_read_only_reconciliation_before_replan() -> None:
    """未知副作用不得重发售罄写，确认后复用原 Attempt 并继续 child。"""

    module = _module()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan())
    event_store = InMemoryEventStore()
    event = _register_verified_event(event_store)
    executor = _UnknownThenSuccessfulExecutor()
    reconciler = _ConfirmingReconciler()
    worker = PlanWorker(
        store=plan_store,
        event_store=event_store,
        skill_executor=executor,
        worker_id="worker-task10-reconcile",
        clock=lambda: NOW + timedelta(seconds=1),
        max_claims=1,
    )
    coordinator = module.PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=worker,
        replan_coordinator=ReplanCoordinator(plan_store=plan_store, event_store=event_store),
        reconciliation_service=reconciler,
        worker_id="coordinator-task10-reconcile",
        clock=lambda: NOW + timedelta(seconds=2),
    )

    waiting = asyncio.run(coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW))
    resumed = asyncio.run(
        coordinator.reconcile_waiting(
            event_id=event.event_id,
            root_plan_run_id=root.plan_run_id,
            now=NOW + timedelta(seconds=2),
        )
    )
    completed = asyncio.run(
        coordinator.run_next(
            root_plan_run_id=root.plan_run_id,
            now=NOW + timedelta(seconds=3),
        )
    )

    assert waiting.status is module.PreemptionStatus.WAITING_RECONCILIATION
    assert resumed.status is module.PreemptionStatus.RETRY_PENDING
    assert completed.status is module.PreemptionStatus.APPLIED
    assert executor.calls.count("handle_sold_out_event") == 1
    assert len(reconciler.requests) == 1
    assert reconciler.requests[0].original_failure.attempt_id == "attempt-task10-unknown"


@pytest.mark.parametrize("application_already_resumed", [False, True])
def test_reconciliation_crash_windows_are_idempotently_recovered(
    application_already_resumed: bool,
) -> None:
    """NodeRun 或 Application 单独提交后崩溃，恢复不得再次读平台或重发写。"""

    module = _module()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan())
    event_store = InMemoryEventStore()
    event = _register_verified_event(event_store)
    executor = _UnknownThenSuccessfulExecutor()
    reconciler = _ConfirmingReconciler()
    worker = PlanWorker(
        store=plan_store,
        event_store=event_store,
        skill_executor=executor,
        worker_id="worker-reconcile-crash",
        clock=lambda: NOW + timedelta(seconds=1),
        max_claims=1,
    )
    coordinator = module.PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=worker,
        replan_coordinator=ReplanCoordinator(plan_store=plan_store, event_store=event_store),
        reconciliation_service=reconciler,
        worker_id="coordinator-reconcile-crash",
        clock=lambda: NOW + timedelta(seconds=2),
    )
    waiting = asyncio.run(coordinator.run_next(root_plan_run_id=root.plan_run_id, now=NOW))
    application = event_store.get_application(event.event_id, root.plan_run_id)
    child_id = application.emergency_plan_run_id
    assert child_id is not None
    mark_node = next(
        node for node in plan_store.list_nodes(child_id) if node.logical_key == "mark-sold-out"
    )
    plan_store.reconcile_plan_reference(
        plan_run_id=child_id,
        node_id=mark_node.node_id,
        outcome=PlanNodeState.SUCCEEDED,
        reference={"simulated_crash_window": True},
    )
    if application_already_resumed:
        event_store.transition_application(
            event.event_id,
            root.plan_run_id,
            expected_state=EventApplicationState.WAITING_RECONCILIATION,
            target_state=EventApplicationState.EMERGENCY_RUNNING,
            now=NOW + timedelta(seconds=1),
        )

    resumed = asyncio.run(
        coordinator.reconcile_waiting(
            event_id=event.event_id,
            root_plan_run_id=root.plan_run_id,
            now=NOW + timedelta(seconds=2),
        )
    )

    assert waiting.evidence_ref is None
    assert resumed.status is module.PreemptionStatus.RETRY_PENDING
    assert resumed.evidence_ref is None
    assert reconciler.requests == []
    assert event_store.get_inbox(event.event_id).state is EventInboxState.VERIFIED


def test_failed_application_recovery_closes_inbox_without_success_evidence() -> None:
    """Application 已失败而 Inbox 未闭合时，重领只能补写 FAILED，不能返回 APPLIED。"""

    module = _module()
    plan_store = InMemoryPlanStore()
    root = plan_store.create_or_resume(_root_plan())
    event_store = InMemoryEventStore()
    event = _register_verified_event(event_store)
    claim = event_store.claim_next_for_room(
        "crashed-coordinator",
        room_id="room-001",
        now=NOW,
        lease_seconds=60,
    )
    assert claim is not None
    event_store.create_application(
        event.event_id,
        root_plan_run_id=root.plan_run_id,
        source_plan_version=1,
        now=NOW,
    )
    failure = FailureFact(
        category=FailureCategory.INTERNAL_INVARIANT,
        external_code="test.preemption_failed",
        side_effect_state=SideEffectState.NOT_SENT,
        attempt_id="attempt-preemption-failed",
    )
    event_store.transition_application(
        event.event_id,
        root.plan_run_id,
        expected_state=EventApplicationState.PENDING,
        target_state=EventApplicationState.FAILED,
        now=NOW,
        failure=failure,
    )
    coordinator = module.PreemptionCoordinator(
        plan_store=plan_store,
        event_store=event_store,
        emergency_worker=object(),  # type: ignore[arg-type]
        replan_coordinator=object(),  # type: ignore[arg-type]
        worker_id="recovery-coordinator",
        clock=lambda: NOW + timedelta(seconds=62),
    )

    result = asyncio.run(
        coordinator.run_next(
            root_plan_run_id=root.plan_run_id,
            now=NOW + timedelta(seconds=61),
        )
    )

    assert result.status is module.PreemptionStatus.FAILED
    assert result.evidence_ref is None
    assert event_store.get_inbox(event.event_id).state is EventInboxState.FAILED
