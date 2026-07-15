"""Phase 12B 售罄紧急 child DAG 的类型化输入与固定候选测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from src.plan_engine.events import (
    InventoryFactEvent,
    VerifiedIngressProvenance,
)
from src.skill_runtime.catalog import get_default_skill_catalog


def _event(
    *,
    event_id: str = "event-emergency-001",
    product_id: str = "p001",
) -> InventoryFactEvent:
    """构造已经规范化的售罄事实，避免测试绕过事件摘要校验。"""
    return InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id="room-emergency",
        product_id=product_id,
        observed_version=3,
        occurred_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        source="inventory-service",
    )


def _provenance(event: InventoryFactEvent) -> VerifiedIngressProvenance:
    """构造与事件摘要和来源闭合的可信入站来源记录。"""
    return VerifiedIngressProvenance(
        provenance_id=f"provenance-{event.event_id}",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        payload_digest=event.payload_digest,
    )


def test_emergency_input_freezes_lineage_and_rejects_wrong_expected_version() -> None:
    """紧急 child 输入必须把 root、parent、触发事件和 CAS 版本闭合为不可变事实。"""
    from src.plan_engine.models import EmergencySoldOutPlanningInput

    event = _event()
    request = EmergencySoldOutPlanningInput(
        room_id=event.room_id,
        trace_id="trace-emergency",
        root_plan_run_id="root-plan-001",
        parent_plan_run_id="root-plan-001",
        trigger_event_id=event.event_id,
        event=event,
        provenance=_provenance(event),
        expected_version=event.observed_version,
    )

    assert request.run_key
    assert request.product_id == "p001"
    invalid_payload = request.model_dump(mode="json")
    invalid_payload["expected_version"] = 4
    with pytest.raises(ValueError, match="expected_version"):
        EmergencySoldOutPlanningInput(**invalid_payload)


def test_sold_out_emergency_provider_emits_the_fixed_five_node_dag() -> None:
    """Provider 只能输出已审核的验证、售罄、备选、提示和汇总五个节点。"""
    from src.plan_engine.emergency import SoldOutEmergencyProposalProvider
    from src.plan_engine.models import EmergencySoldOutPlanningInput

    event = _event()
    request = EmergencySoldOutPlanningInput(
        room_id=event.room_id,
        trace_id="trace-emergency",
        root_plan_run_id="root-plan-001",
        parent_plan_run_id="root-plan-001",
        trigger_event_id=event.event_id,
        event=event,
        provenance=_provenance(event),
        expected_version=event.observed_version,
    )

    proposal = SoldOutEmergencyProposalProvider().propose_sync(request)

    assert [node.logical_key for node in proposal.nodes] == [
        "validate-sold-out-event",
        "mark-sold-out",
        "recommend-backup-product",
        "generate-sold-out-prompt",
        "collect-sold-out-response",
    ]
    assert [node.skill_id for node in proposal.nodes] == [
        None,
        "handle_sold_out_event",
        "recommend_backup_product",
        "generate_on_live_prompt",
        None,
    ]


def _emergency_input(
    *,
    event_id: str = "event-emergency-001",
    product_id: str = "p001",
) -> "EmergencySoldOutPlanningInput":
    """构造后续物化与调度测试共享的完整紧急计划输入。"""
    from src.plan_engine.models import EmergencySoldOutPlanningInput

    event = _event(event_id=event_id, product_id=product_id)
    return EmergencySoldOutPlanningInput(
        room_id=event.room_id,
        trace_id=f"trace-{event_id}",
        root_plan_run_id="root-plan-001",
        parent_plan_run_id="root-plan-001",
        trigger_event_id=event.event_id,
        event=event,
        provenance=_provenance(event),
        expected_version=event.observed_version,
    )


def _materialized_emergency_plan(
    *,
    event_id: str = "event-emergency-001",
    product_id: str = "p001",
) -> "MaterializedPlan":
    """通过可信 Catalog/Profile 物化固定 child DAG，不手写执行控制事实。"""
    from src.plan_engine.capabilities import PlanCapabilityProfile
    from src.plan_engine.emergency import SoldOutEmergencyProposalProvider
    from src.plan_engine.store import MaterializedPlan

    request = _emergency_input(event_id=event_id, product_id=product_id)
    proposal = SoldOutEmergencyProposalProvider().propose_sync(request)
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())
    capabilities = {
        node.logical_key: (
            profile.resolve_emergency_control_node(logical_key=node.logical_key)
            if node.skill_id is None
            else profile.resolve_emergency_skill_node(
                skill_id=node.skill_id,
                room_id=request.room_id,
                product_id=request.product_id,
            )
        )
        for node in proposal.nodes
    }
    return MaterializedPlan(
        planning_input=request,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def test_emergency_materialization_uses_exact_versions_and_product_resource_lock() -> None:
    """版本与资源锁必须由 Profile 注入，Provider 不能自行声明或覆盖。"""
    plan = _materialized_emergency_plan()

    capabilities = plan.capabilities_by_logical_key
    assert capabilities["mark-sold-out"].skill_version == "2.0.0"
    assert capabilities["recommend-backup-product"].skill_version == "1.0.0"
    assert capabilities["generate-sold-out-prompt"].skill_version == "1.0.0"
    assert capabilities["mark-sold-out"].resource_keys == (
        "room:room-emergency:product:p001",
    )
    assert capabilities["mark-sold-out"].max_concurrency == 1


def test_emergency_materialization_rejects_noncanonical_provider() -> None:
    """priority 100 入口只能接受固定 Provider 的完整五节点证据。"""
    from src.plan_engine.models import CandidatePlanProposal
    from src.plan_engine.store import MaterializedPlan, PlanStoreInvariantError

    canonical = _materialized_emergency_plan()
    forged = CandidatePlanProposal(
        provider_id="forged-emergency-provider",
        provider_version=canonical.proposal.provider_version,
        nodes=canonical.proposal.nodes,
    )

    with pytest.raises(PlanStoreInvariantError, match="固定五节点"):
        MaterializedPlan(
            planning_input=canonical.planning_input,
            proposal=forged,
            capabilities_by_logical_key=dict(
                canonical.capabilities_by_logical_key
            ),
        )


def test_emergency_plan_run_persists_kind_priority_lineage_and_ready_time() -> None:
    """紧急 child 的用途、优先级、lineage 与 READY 时刻必须成为 Store 事实。"""
    from src.plan_engine.models import PlanRunKind
    from src.plan_engine.store import InMemoryPlanStore

    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_emergency_plan())
    nodes = store.list_nodes(plan_run.plan_run_id)

    assert plan_run.plan_kind is PlanRunKind.EMERGENCY_SOLD_OUT
    assert plan_run.priority == 100
    assert plan_run.root_plan_run_id == "root-plan-001"
    assert plan_run.parent_plan_run_id == "root-plan-001"
    assert plan_run.trigger_event_id == "event-emergency-001"
    assert nodes[0].logical_key == "validate-sold-out-event"
    assert nodes[0].ready_at is not None
    assert all(node.ready_at is None for node in nodes[1:])


def test_global_claim_prefers_emergency_plan_and_uses_stable_ready_order() -> None:
    """跨 PlanRun claim 必须先看 priority，再按 READY 时刻和 node_id 稳定排序。"""
    from src.plan_engine.store import InMemoryPlanStore

    store = InMemoryPlanStore()
    emergency = store.create_or_resume(_materialized_emergency_plan())
    now = datetime(2026, 7, 15, 1, tzinfo=timezone.utc)

    claims = store.claim_next_ready_nodes(
        worker_id="worker-priority",
        now=now,
        lease_seconds=60,
        limit=1,
    )

    assert len(claims) == 1
    assert claims[0].plan_run_id == emergency.plan_run_id
    assert claims[0].node_type == "VALIDATE_SOLD_OUT_EVENT"


@pytest.mark.parametrize(
    ("second_product_id", "expected_claim_count"),
    (("p001", 1), ("p002", 2)),
)
def test_global_claim_serializes_same_product_across_emergency_plans(
    second_product_id: str,
    expected_claim_count: int,
) -> None:
    """相同商品跨 PlanRun 串行，不同商品仍可在同一批次并发执行。"""
    from src.plan_engine.models import PlanNodeState
    from src.plan_engine.store import InMemoryPlanStore

    store = InMemoryPlanStore()
    store.create_or_resume(
        _materialized_emergency_plan(event_id="event-resource-first")
    )
    store.create_or_resume(
        _materialized_emergency_plan(
            event_id="event-resource-second",
            product_id=second_product_id,
        )
    )
    now = datetime(2026, 7, 15, 1, tzinfo=timezone.utc)
    validations = store.claim_next_ready_nodes(
        worker_id="worker-validation",
        now=now,
        lease_seconds=60,
        limit=2,
    )
    assert len(validations) == 2
    for claim in validations:
        store.record_node_result(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output={"validated": True},
            now=now.replace(microsecond=1),
        )

    writes = store.claim_next_ready_nodes(
        worker_id="worker-write",
        now=now.replace(microsecond=2),
        lease_seconds=60,
        limit=2,
    )

    assert len(writes) == expected_claim_count
    assert all(claim.skill_id == "handle_sold_out_event" for claim in writes)


class _RecordingEmergencyExecutor:
    """记录 Worker 传入的完整 SkillCall，并返回身份闭合的成功结果。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def execute(self, call: Any) -> Any:
        """不访问 Adapter，只让测试检查可信上下文是否正确重建。"""
        from src.skill_runtime.models import SkillExecutionResult, SkillExecutionStatus

        self.calls.append(call)
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.SUCCESS,
            output={"handled": True},
            summary="紧急 Skill 测试成功",
        )


def _registered_event_store(*, conflicting: bool = False) -> "InMemoryEventStore":
    """登记冻结输入对应事件；可选追加同 ID 不同摘要以制造权威冲突。"""
    from src.plan_engine.event_store import EventDelivery, InMemoryEventStore

    event = _event()
    store = InMemoryEventStore()
    store.register_event(
        event,
        _provenance(event),
        EventDelivery(
            occurrence_id="occurrence-emergency-001",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=1,
            received_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        ),
    )
    if conflicting:
        conflict = InventoryFactEvent.create_sold_out(
            event_id=event.event_id,
            room_id=event.room_id,
            product_id=event.product_id,
            observed_version=event.observed_version + 1,
            occurred_at=event.occurred_at,
            source=event.source,
        )
        store.register_event(
            conflict,
            _provenance(conflict),
            EventDelivery(
                occurrence_id="occurrence-emergency-conflict",
                transport="kafka",
                topic="inventory.sold-out",
                partition=0,
                offset=2,
                received_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            ),
        )
    return store


def test_worker_rebuilds_event_authorization_only_after_validation() -> None:
    """验证控制节点成功后，售罄写才能取得可信事件授权与稳定幂等键。"""
    from src.plan_engine.store import InMemoryPlanStore
    from src.plan_engine.worker import PlanWorker

    plan_store = InMemoryPlanStore()
    plan_run = plan_store.create_or_resume(_materialized_emergency_plan())
    executor = _RecordingEmergencyExecutor()
    worker = PlanWorker(
        store=plan_store,
        event_store=_registered_event_store(),
        skill_executor=executor,
        worker_id="worker-emergency",
        clock=lambda: datetime(2026, 7, 15, 0, 0, 1, tzinfo=timezone.utc),
    )

    validation = asyncio.run(worker.run_next_once())
    write = asyncio.run(worker.run_next_once())

    assert validation.succeeded == 1
    assert write.succeeded == 1
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call.skill_id == "handle_sold_out_event"
    assert call.context.event_authorization is not None
    assert call.context.event_authorization.event_id == "event-emergency-001"
    assert call.context.idempotency_key

    recommendation = asyncio.run(worker.run_next_once())
    assert recommendation.succeeded == 1
    assert executor.calls[1].skill_id == "recommend_backup_product"
    assert executor.calls[1].context.event_authorization is None


def test_worker_fails_closed_before_skill_when_event_is_conflicted() -> None:
    """Inbox 已冲突时验证节点失败，且不得创建任何售罄 Skill 调用。"""
    from src.plan_engine.models import PlanRunState
    from src.plan_engine.store import InMemoryPlanStore
    from src.plan_engine.worker import PlanWorker

    plan_store = InMemoryPlanStore()
    plan_run = plan_store.create_or_resume(_materialized_emergency_plan())
    executor = _RecordingEmergencyExecutor()
    worker = PlanWorker(
        store=plan_store,
        event_store=_registered_event_store(conflicting=True),
        skill_executor=executor,
        worker_id="worker-emergency-conflict",
        clock=lambda: datetime(2026, 7, 15, 0, 0, 1, tzinfo=timezone.utc),
    )

    result = asyncio.run(worker.run_next_once())

    assert result.failed == 1
    assert plan_store.get_plan_run(plan_run.plan_run_id).state is PlanRunState.FAILED
    assert executor.calls == []


def test_worker_rechecks_event_before_write_when_conflict_arrives_late() -> None:
    """验证节点后到达的摘要冲突仍必须在售罄写派发前被二次拦截。"""
    from src.plan_engine.event_store import EventDelivery
    from src.plan_engine.models import PlanRunState
    from src.plan_engine.store import InMemoryPlanStore
    from src.plan_engine.worker import PlanWorker

    plan_store = InMemoryPlanStore()
    plan_run = plan_store.create_or_resume(_materialized_emergency_plan())
    event_store = _registered_event_store()
    executor = _RecordingEmergencyExecutor()
    worker = PlanWorker(
        store=plan_store,
        event_store=event_store,
        skill_executor=executor,
        worker_id="worker-emergency-late-conflict",
        clock=lambda: datetime(2026, 7, 15, 0, 0, 1, tzinfo=timezone.utc),
    )
    assert asyncio.run(worker.run_next_once()).succeeded == 1
    original = _event()
    conflict = InventoryFactEvent.create_sold_out(
        event_id=original.event_id,
        room_id=original.room_id,
        product_id=original.product_id,
        observed_version=original.observed_version + 1,
        occurred_at=original.occurred_at,
        source=original.source,
    )
    event_store.register_event(
        conflict,
        _provenance(conflict),
        EventDelivery(
            occurrence_id="occurrence-emergency-late-conflict",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=3,
            received_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        ),
    )

    result = asyncio.run(worker.run_next_once())

    assert result.failed == 1
    assert plan_store.get_plan_run(plan_run.plan_run_id).state is PlanRunState.FAILED
    assert executor.calls == []
