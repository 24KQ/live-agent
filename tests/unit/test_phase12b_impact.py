"""Phase 12B ImpactAnalyzer 与内存协作式冻结契约测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from typing import Any

import pytest

from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.event_state_machine import EventInboxState
from src.plan_engine.event_store import EventInboxRecord
from src.plan_engine.events import (
    ImpactScope,
    InventoryFactEvent,
    VerifiedIngressProvenance,
)
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanNodeKind,
    PlanNodeState,
    PlanNodeView,
    PlanRunState,
    PlanRunView,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.state_machine import PlanStateMachine
from src.plan_engine.store import (
    InMemoryPlanStore,
    MaterializedPlan,
    PlanStoreInvariantError,
)
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


BASE_TIME = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def _impact_module() -> Any:
    """延迟导入 Task 5 模块，使缺少实现形成可读红灯。"""
    return importlib.import_module("src.plan_engine.impact")


def _product(product_id: str) -> CatalogProduct:
    """构造不依赖数据库的完整商品快照。"""
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


def _materialized_plan() -> MaterializedPlan:
    """构造三商品规范手卡 DAG，供冻结依赖闭包测试。"""
    product_ids = ("p001", "p002", "p003")
    planning_input = CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-impact-001",
        live_plan=LivePlanDraft(
            room_id="room-001",
            trace_id="trace-impact-001",
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="ImpactAnalyzer 测试",
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
        if node.logical_key == "prepare-card-batch":
            node_type = "PREPARE_CARD_BATCH"
        elif node.logical_key == "collect-card-results":
            node_type = "COLLECT_CARD_RESULTS"
        else:
            node_type = "SKILL"
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


def _inbox(
    *,
    event_id: str = "event-sold-out-001",
    product_id: str = "p001",
    state: EventInboxState = EventInboxState.VERIFIED,
) -> EventInboxRecord:
    """构造已验证或冲突的权威 Inbox 快照。"""
    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id="room-001",
        product_id=product_id,
        observed_version=3,
        occurred_at=BASE_TIME,
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id=f"provenance-{event_id}",
        profile_id="inventory-kafka-v1",
        transport="KAFKA",
        topic="inventory-facts",
        source=event.source,
        received_at=BASE_TIME,
        payload_digest=event.payload_digest,
    )
    return EventInboxRecord(
        event=event,
        provenance=provenance,
        state=state,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _manual_nodes() -> tuple[PlanNodeView, ...]:
    """提供包含依赖与资源键的冻结节点快照，隔离 Analyzer 纯函数测试。"""
    common = {"plan_run_id": "plan-001", "version_number": 1}
    return (
        PlanNodeView(
            **common,
            node_id="node-prepare",
            logical_key="prepare-card-batch",
            node_kind=PlanNodeKind.CONTROL,
            state=PlanNodeState.SUCCEEDED,
            depends_on=(),
            resource_keys=(),
        ),
        PlanNodeView(
            **common,
            node_id="node-p001",
            logical_key="card:p001",
            node_kind=PlanNodeKind.SKILL,
            state=PlanNodeState.RUNNING,
            skill_id="generate_product_card",
            depends_on=("prepare-card-batch",),
            resource_keys=("room:room-001:product:p001",),
        ),
        PlanNodeView(
            **common,
            node_id="node-p002",
            logical_key="card:p002",
            node_kind=PlanNodeKind.SKILL,
            state=PlanNodeState.READY,
            skill_id="generate_product_card",
            depends_on=("prepare-card-batch",),
            resource_keys=("room:room-001:product:p002",),
        ),
        PlanNodeView(
            **common,
            node_id="node-collect",
            logical_key="collect-card-results",
            node_kind=PlanNodeKind.CONTROL,
            state=PlanNodeState.PENDING,
            depends_on=("card:p001", "card:p002"),
            resource_keys=(),
        ),
    )


def _plan_run() -> PlanRunView:
    """构造与手工节点同版本、同房间的活动计划。"""
    return PlanRunView(
        plan_run_id="plan-001",
        room_id="room-001",
        trace_id="trace-001",
        run_key="run-key-001",
        current_version=1,
        state=PlanRunState.ACTIVE,
    )


def _platform_failure() -> FailureFact:
    """构造只表达平台级基础设施事实、没有写副作用的 FailureFact。"""
    return FailureFact(
        category=FailureCategory.TRANSIENT_INFRA,
        external_code="platform.inventory.unavailable",
        side_effect_state=SideEffectState.NOT_SENT,
        attempt_id="platform-failure-attempt",
    )


def _complete_prepare(store: InMemoryPlanStore, plan_run_id: str) -> None:
    """合法闭合 PREPARE，使三个商品手卡节点进入 READY。"""
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run_id,
        worker_id="prepare-worker",
        now=BASE_TIME,
        lease_seconds=60,
        limit=1,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"prepared_product_ids": ["p001", "p002", "p003"]},
        now=BASE_TIME + timedelta(seconds=1),
    )


def test_product_scope_uses_resource_match_and_downstream_dependency_closure() -> None:
    """p001 售罄只影响 p001 手卡及其汇总后继，不冻结无关 p002。"""
    module = _impact_module()

    analysis = module.ImpactAnalyzer().analyze(
        inbox=_inbox(),
        plan_run=_plan_run(),
        nodes=_manual_nodes(),
    )

    assert analysis.scope is ImpactScope.PRODUCT
    assert analysis.affected_logical_keys == ("card:p001", "collect-card-results")
    assert analysis.affected_node_ids == ("node-collect", "node-p001")
    assert analysis.resource_keys == ("room:room-001:product:p001",)
    assert "SOLD_OUT_PRODUCT_MATCH" in analysis.reason_codes
    assert len(analysis.analysis_digest) == 64


def test_conflict_or_unresolved_product_escalates_to_room_and_platform_fact_to_platform() -> None:
    """无法证明商品边界时影响全房间，只有受控平台失败事实才能提升 PLATFORM。"""
    module = _impact_module()
    analyzer = module.ImpactAnalyzer()
    nodes = _manual_nodes()

    conflict = analyzer.analyze(
        inbox=_inbox(state=EventInboxState.CONFLICT),
        plan_run=_plan_run(),
        nodes=nodes,
    )
    unresolved = analyzer.analyze(
        inbox=_inbox(product_id="missing-product"),
        plan_run=_plan_run(),
        nodes=nodes,
    )
    platform = analyzer.analyze(
        inbox=_inbox(),
        plan_run=_plan_run(),
        nodes=nodes,
        platform_failure=_platform_failure(),
    )

    all_keys = tuple(sorted(node.logical_key for node in nodes))
    assert conflict.scope is ImpactScope.ROOM
    assert conflict.affected_logical_keys == all_keys
    assert "EVENT_IDENTITY_CONFLICT" in conflict.reason_codes
    assert unresolved.scope is ImpactScope.ROOM
    assert "PRODUCT_RESOURCE_UNRESOLVED" in unresolved.reason_codes
    assert platform.scope is ImpactScope.PLATFORM
    assert platform.affected_logical_keys == all_keys
    assert "PLATFORM_FAILURE_FACT" in platform.reason_codes


def test_analysis_digest_is_order_independent_and_rejects_wrong_plan_or_fake_platform_fact() -> None:
    """相同事实不同输入顺序产生同摘要，跨房间或伪平台错误不能被静默升级。"""
    module = _impact_module()
    analyzer = module.ImpactAnalyzer()
    nodes = _manual_nodes()

    first = analyzer.analyze(
        inbox=_inbox(),
        plan_run=_plan_run(),
        nodes=nodes,
    )
    reordered = analyzer.analyze(
        inbox=_inbox(),
        plan_run=_plan_run(),
        nodes=tuple(reversed(nodes)),
    )
    assert first.analysis_digest == reordered.analysis_digest

    wrong_room = _plan_run().model_copy(update={"room_id": "room-other"})
    with pytest.raises(module.ImpactAnalysisError, match="room"):
        analyzer.analyze(inbox=_inbox(), plan_run=wrong_room, nodes=nodes)
    fake_platform = _platform_failure().model_copy(
        update={"external_code": "adapter.timeout"}
    )
    with pytest.raises(module.ImpactAnalysisError, match="平台"):
        analyzer.analyze(
            inbox=_inbox(),
            plan_run=_plan_run(),
            nodes=nodes,
            platform_failure=fake_platform,
        )


def test_state_machine_allows_not_started_nodes_to_freeze_without_cancelling_running() -> None:
    """PENDING/READY 可协作冻结；RUNNING 是否受影响由 Store 标记结果而非强停。"""
    assert (
        PlanStateMachine.transition_node(PlanNodeState.PENDING, PlanNodeState.FROZEN)
        is PlanNodeState.FROZEN
    )
    assert (
        PlanStateMachine.transition_node(PlanNodeState.READY, PlanNodeState.FROZEN)
        is PlanNodeState.FROZEN
    )


def test_product_freeze_keeps_unrelated_branch_runnable_and_marks_late_result_superseded() -> None:
    """局部冻结不阻塞 p002/p003，p001 在途结果保存但标记 superseded。"""
    module = _impact_module()
    store = InMemoryPlanStore()
    run = store.create_or_resume(_materialized_plan())
    _complete_prepare(store, run.plan_run_id)
    claims = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="card-worker",
        now=BASE_TIME + timedelta(seconds=2),
        lease_seconds=60,
        limit=2,
    )
    nodes_before = {node.node_id: node for node in store.list_nodes(run.plan_run_id)}
    claims_by_key = {
        nodes_before[claim.node_id].logical_key: claim for claim in claims
    }
    analysis = module.ImpactAnalyzer().analyze(
        inbox=_inbox(),
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )

    store.apply_impact_freeze(
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        event_id="event-sold-out-001",
        analysis=analysis,
        now=BASE_TIME + timedelta(seconds=3),
    )

    nodes = {node.logical_key: node for node in store.list_nodes(run.plan_run_id)}
    assert store.get_plan_run(run.plan_run_id).state is PlanRunState.ACTIVE
    assert nodes["card:p001"].state is PlanNodeState.RUNNING
    assert nodes["collect-card-results"].state is PlanNodeState.FROZEN
    assert nodes["card:p002"].state is PlanNodeState.RUNNING
    assert nodes["card:p003"].state is PlanNodeState.READY
    affected_claim = claims_by_key["card:p001"]
    unaffected_claim = claims_by_key["card:p002"]
    marked = store.list_node_runs(run.plan_run_id, affected_claim.node_id)[0]
    assert marked.superseded is True
    assert marked.superseded_by_event_id == "event-sold-out-001"

    completed = store.record_node_result(
        node_run_id=affected_claim.node_run_id,
        worker_id=affected_claim.worker_id,
        claim_version=affected_claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"card": "p001 late result"},
        now=BASE_TIME + timedelta(seconds=4),
    )
    unaffected = store.record_node_result(
        node_run_id=unaffected_claim.node_run_id,
        worker_id=unaffected_claim.worker_id,
        claim_version=unaffected_claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"card": "p002 reusable result"},
        now=BASE_TIME + timedelta(seconds=4),
    )
    next_claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="card-worker-next",
        now=BASE_TIME + timedelta(seconds=5),
        lease_seconds=60,
        limit=3,
    )

    assert completed.output == {"card": "p001 late result"}
    assert completed.superseded is True
    assert unaffected.superseded is False
    assert [
        {node.node_id: node.logical_key for node in store.list_nodes(run.plan_run_id)}[
            claim.node_id
        ]
        for claim in next_claim
    ] == ["card:p003"]


def test_room_freeze_blocks_new_claims_but_allows_running_result_to_close_without_unfreeze() -> None:
    """ROOM 风险冻结整计划；在途结果可闭合，但不能把 PlanRun 改回 ACTIVE/SUCCEEDED。"""
    module = _impact_module()
    store = InMemoryPlanStore()
    run = store.create_or_resume(_materialized_plan())
    _complete_prepare(store, run.plan_run_id)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="room-freeze-worker",
        now=BASE_TIME + timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )[0]
    analysis = module.ImpactAnalyzer().analyze(
        inbox=_inbox(state=EventInboxState.CONFLICT),
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )

    store.apply_impact_freeze(
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        event_id="event-sold-out-001",
        analysis=analysis,
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert store.get_plan_run(run.plan_run_id).state is PlanRunState.FROZEN
    assert store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="blocked-worker",
        now=BASE_TIME + timedelta(seconds=4),
        lease_seconds=60,
        limit=4,
    ) == ()

    completed = store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"card": "late but audited"},
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert completed.superseded is True
    assert store.get_plan_run(run.plan_run_id).state is PlanRunState.FROZEN


def _frozen_product_claim() -> tuple[InMemoryPlanStore, Any]:
    """创建已被 PRODUCT 事件命中的在途 claim，供重试与回收边界复用。"""
    module = _impact_module()
    store = InMemoryPlanStore()
    run = store.create_or_resume(_materialized_plan())
    _complete_prepare(store, run.plan_run_id)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="superseded-worker",
        now=BASE_TIME + timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )[0]
    analysis = module.ImpactAnalyzer().analyze(
        inbox=_inbox(),
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )
    store.apply_impact_freeze(
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        event_id=analysis.event_id,
        analysis=analysis,
        now=BASE_TIME + timedelta(seconds=3),
    )
    return store, claim


def test_superseded_running_node_cannot_schedule_another_retry() -> None:
    """冻结前已开始的 attempt 可闭合，但瞬态失败不得派生第二次外部调用。"""
    store, claim = _frozen_product_claim()

    with pytest.raises(PlanStoreInvariantError, match="superseded"):
        store.schedule_retry(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version,
            now=BASE_TIME + timedelta(seconds=4),
            retry_at=BASE_TIME + timedelta(seconds=10),
        )

    runs = store.list_node_runs(claim.plan_run_id, claim.node_id)
    assert len(runs) == 1
    assert runs[0].state is PlanNodeState.RUNNING


def test_superseded_expired_node_cannot_be_reclaimed_as_new_attempt() -> None:
    """受影响 attempt 即使租约过期也只能等待后续 Replan，不能被普通回收器重跑。"""
    store, claim = _frozen_product_claim()

    with pytest.raises(PlanStoreInvariantError, match="superseded"):
        store.reclaim_expired_node(
            node_run_id=claim.node_run_id,
            worker_id="replacement-worker",
            now=BASE_TIME + timedelta(seconds=63),
            lease_seconds=60,
        )

    assert len(store.list_node_runs(claim.plan_run_id, claim.node_id)) == 1


def test_superseded_failure_does_not_fail_unaffected_product_branches() -> None:
    """受影响旧 attempt 的失败仍需审计，但不能把 PRODUCT 局部风险升级为整批失败。"""
    store, claim = _frozen_product_claim()

    completed = store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.FAILED,
        output={"failure": {"code": "adapter.timeout"}},
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert completed.state is PlanNodeState.FAILED
    assert completed.superseded is True
    assert store.get_plan_run(claim.plan_run_id).state is PlanRunState.ACTIVE
    unaffected_claims = store.claim_ready_nodes(
        plan_run_id=claim.plan_run_id,
        worker_id="unaffected-worker",
        now=BASE_TIME + timedelta(seconds=5),
        lease_seconds=60,
        limit=3,
    )
    assert len(unaffected_claims) == 2


def test_phase12b_migration_persists_superseded_node_run_evidence() -> None:
    """superseded 不能只存在内存视图，Phase 12B DDL 必须可重启恢复。"""
    sql_path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "docker"
        / "init_phase12b_preemption.sql"
    )
    sql = " ".join(sql_path.read_text(encoding="utf-8").lower().split())
    for fragment in (
        "superseded boolean not null default false",
        "superseded_by_event_id text",
        "superseded_at timestamptz",
        "node_runs_superseded_idx",
    ):
        assert fragment in sql
