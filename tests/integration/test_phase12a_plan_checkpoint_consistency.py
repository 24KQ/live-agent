"""Phase 12A 使用真实 PostgreSQL 与官方 PostgresSaver 的一致性测试。

测试刻意让 PlanStore 和 checkpoint 在两个独立连接中提交，证明恢复依赖有序事实与
幂等对账，而不是不存在的跨连接事务。所有 checkpoint 都通过官方 LangGraph API 写入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from src.config.settings import get_settings
from src.core.langgraph_checkpoint import (
    create_postgres_checkpointer,
    initialize_postgres_checkpointer,
)
from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.reconciliation import (
    PlanCheckpointReference,
    PlanReconciliationService,
)
from src.plan_engine.store import (
    MaterializedPlan,
    PostgresPlanStore,
    initialize_plan_engine_schema,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


class _CheckpointState(TypedDict, total=False):
    """真实 PostgresSaver 测试使用的最小 Graph state。"""

    plan_checkpoint_reference: dict[str, Any]


def _materialized_plan(*, room_id: str, trace_id: str) -> MaterializedPlan:
    """为每个集成场景创建身份隔离的单商品规范 DAG。"""
    product_id = "p001"
    product = CatalogProduct(
        product_id=product_id,
        name="商品 p001",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["可审计卖点"],
    )
    planning_input = CardBatchPlanningInput(
        room_id=room_id,
        trace_id=trace_id,
        live_plan=LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=1,
                    product_id=product_id,
                    product_name=product.name,
                    role="引流款",
                    reason="真实 checkpoint 一致性测试",
                )
            ],
        ),
        products_by_id={product_id: product},
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
        capabilities[node.logical_key] = ResolvedPlanCapability(
            node_type=node_type,
            skill_id=node.skill_id,
            skill_version="1.0.0" if node.skill_id else None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=30 if node.skill_id else None,
            resource_keys=(f"room:{room_id}:product:{product_id}",) if node.skill_id else (),
            max_concurrency=4,
        )
    return MaterializedPlan(
        planning_input=planning_input,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _complete_plan(store: PostgresPlanStore, plan_run_id: str) -> None:
    """通过真实事务逐节点闭合计划，生成可复用的卡片 NodeRun 证据。"""
    now = datetime.now(timezone.utc)
    for sequence in range(3):
        claim = store.claim_ready_nodes(
            plan_run_id=plan_run_id,
            worker_id=f"checkpoint-worker-{uuid4().hex}",
            now=now + timedelta(seconds=sequence * 2),
            lease_seconds=60,
            limit=4,
        )[0]
        output = (
            {"prepared_product_ids": ["p001"]}
            if claim.node_type == "PREPARE_CARD_BATCH"
            else {"product_id": "p001", "title": "商品 p001 手卡"}
            if claim.node_type == "SKILL"
            else {"cards": [{"product_id": "p001", "title": "商品 p001 手卡"}]}
        )
        store.record_node_result(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output=output,
            now=now + timedelta(seconds=sequence * 2 + 1),
        )


def _write_success_checkpoint(checkpointer: Any, reference: PlanCheckpointReference, trace_id: str) -> None:
    """使用官方 Graph compile/invoke 路径写入成功引用。"""
    builder = StateGraph(_CheckpointState)
    builder.add_node("persist-reference", lambda _state: {})
    builder.add_edge(START, "persist-reference")
    builder.add_edge("persist-reference", END)
    graph = builder.compile(checkpointer=checkpointer)
    graph.invoke(
        {"plan_checkpoint_reference": reference.model_dump(mode="json")},
        config={"configurable": {"thread_id": trace_id}},
    )


def test_postgres_planstore_success_without_checkpoint_reuses_cards() -> None:
    """PlanStore 先提交成功而 checkpoint 尚未写入时，只读取原 NodeRun 结果。"""
    settings = get_settings()
    initialize_plan_engine_schema(settings)
    initialize_postgres_checkpointer(settings)
    suffix = uuid4().hex
    trace_id = f"trace-store-ahead-{suffix}"
    store = PostgresPlanStore(settings)
    plan = store.create_or_resume(
        _materialized_plan(room_id=f"room-{suffix}", trace_id=trace_id)
    )
    _complete_plan(store, plan.plan_run_id)

    with create_postgres_checkpointer(settings) as checkpointer:
        outcome = PlanReconciliationService(
            store=store,
            checkpointer=checkpointer,
        ).reconcile(plan.plan_run_id)

    assert outcome.category == "REPLAY_REUSE"
    assert outcome.cards_snapshot[0]["product_id"] == "p001"
    assert len(store.list_node_runs(plan.plan_run_id)) == 3


def test_postgres_checkpoint_ahead_persists_fail_closed_incident() -> None:
    """checkpoint 先声称成功时，事故和冻结状态必须可由新 Store 实例读取。"""
    settings = get_settings()
    initialize_plan_engine_schema(settings)
    initialize_postgres_checkpointer(settings)
    suffix = uuid4().hex
    trace_id = f"trace-checkpoint-ahead-{suffix}"
    store = PostgresPlanStore(settings)
    plan = store.create_or_resume(
        _materialized_plan(room_id=f"room-{suffix}", trace_id=trace_id)
    )
    reference = PlanCheckpointReference(
        plan_run_id=plan.plan_run_id,
        plan_version=1,
        control_position="CARD_BATCH_SUCCEEDED",
    )

    with create_postgres_checkpointer(settings) as checkpointer:
        _write_success_checkpoint(checkpointer, reference, trace_id)
        outcome = PlanReconciliationService(
            store=store,
            checkpointer=checkpointer,
        ).reconcile(plan.plan_run_id)

    persisted = PostgresPlanStore(settings).get_plan_run(plan.plan_run_id)
    assert outcome.category == "INTERNAL_INVARIANT"
    assert persisted.state is PlanRunState.FROZEN
    assert persisted.reconciliation_required is True
    assert persisted.reconciliation_failure["reason"] == "CHECKPOINT_AHEAD_OF_PLANSTORE"
    assert persisted.reconciliation_signature == outcome.failure_signature
    assert persisted.reconciliation_attempt_count == 1
    assert PostgresPlanStore(settings).list_node_runs(plan.plan_run_id) == ()
