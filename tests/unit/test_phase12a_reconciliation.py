"""Phase 12A PlanStore 与 LangGraph checkpoint 一致性契约测试。

这些测试只通过 PlanStore 和 checkpointer 的公开接口构造事实。PlanStore 成功而
checkpoint 落后时必须复用结果；checkpoint 声称成功但 PlanStore 缺少证据时，
必须持久化 INTERNAL_INVARIANT 并冻结计划，不能补造 NodeRun。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pytest
from pydantic import ValidationError

from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.commands import CommandService, PlanCommand
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanCommandType,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.store import InMemoryPlanStore, MaterializedPlan
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


class _CheckpointState(TypedDict, total=False):
    """最小 checkpoint state，只保存 Task 6 允许的计划引用。"""

    plan_checkpoint_reference: dict[str, Any]


def _reconciliation_api() -> Any:
    """延迟导入待实现模块，使 RED 以明确断言失败而不是收集错误呈现。"""
    try:
        return importlib.import_module("src.plan_engine.reconciliation")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 src.plan_engine.reconciliation", pytrace=False)


def _product(product_id: str) -> CatalogProduct:
    """构造不依赖数据库或外部平台的完整商品快照。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"商品 {product_id}",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["可审计卖点"],
    )


def _materialized_plan(*, trace_id: str) -> MaterializedPlan:
    """构造单商品三节点规范 DAG，便于精确控制 PlanStore 状态。"""
    product_id = "p001"
    room_id = f"room-{trace_id}"
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
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="checkpoint 一致性测试",
                )
            ],
        ),
        products_by_id={product_id: _product(product_id)},
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


def _complete_plan(store: InMemoryPlanStore, plan_run_id: str) -> None:
    """只经公开 claim/result API 顺序闭合准备、手卡和汇总节点。"""
    now = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    for sequence in range(3):
        claims = store.claim_ready_nodes(
            plan_run_id=plan_run_id,
            worker_id=f"worker-{sequence}",
            now=now + timedelta(seconds=sequence * 2),
            lease_seconds=60,
            limit=4,
        )
        assert len(claims) == 1
        claim = claims[0]
        if claim.node_type == "PREPARE_CARD_BATCH":
            output: dict[str, Any] = {"prepared_product_ids": ["p001"]}
        elif claim.node_type == "SKILL":
            output = {"product_id": "p001", "title": "商品 p001 手卡"}
        else:
            output = {
                "cards": [{"product_id": "p001", "title": "商品 p001 手卡"}]
            }
        store.record_node_result(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output=output,
            now=now + timedelta(seconds=sequence * 2 + 1),
        )
    assert store.get_plan_run(plan_run_id).state is PlanRunState.SUCCEEDED


def _write_checkpoint(
    checkpointer: Any,
    *,
    thread_id: str,
    reference: dict[str, Any],
) -> None:
    """通过 LangGraph 公开编译/调用路径写 checkpoint，不操作内部存储表。"""
    builder = StateGraph(_CheckpointState)
    builder.add_node("persist-reference", lambda _state: {})
    builder.add_edge(START, "persist-reference")
    builder.add_edge("persist-reference", END)
    graph = builder.compile(checkpointer=checkpointer)
    graph.invoke(
        {"plan_checkpoint_reference": reference},
        config={"configurable": {"thread_id": thread_id}},
    )


def test_planstore_success_without_checkpoint_returns_replay_reuse() -> None:
    """PlanStore 领先时返回已成功卡片，且不需要任何 Skill 再次执行。"""
    api = _reconciliation_api()
    store = InMemoryPlanStore()
    plan = store.create_or_resume(_materialized_plan(trace_id="trace-store-ahead"))
    _complete_plan(store, plan.plan_run_id)

    outcome = api.PlanReconciliationService(
        store=store,
        checkpointer=InMemorySaver(),
    ).reconcile(plan.plan_run_id)

    assert outcome.category == "REPLAY_REUSE"
    assert outcome.plan_state == "SUCCEEDED"
    assert outcome.cards_snapshot == (
        {"product_id": "p001", "title": "商品 p001 手卡"},
    )
    assert outcome.audit_summary["reused_node_runs"] == 1
    assert store.get_plan_run(plan.plan_run_id).reconciliation_required is False


def test_checkpoint_success_without_planstore_evidence_freezes_and_persists() -> None:
    """checkpoint 领先必须冻结计划并保存可跨重启读取的事故事实。"""
    api = _reconciliation_api()
    store = InMemoryPlanStore()
    plan = store.create_or_resume(_materialized_plan(trace_id="trace-checkpoint-ahead"))
    checkpointer = InMemorySaver()
    reference = api.PlanCheckpointReference(
        plan_run_id=plan.plan_run_id,
        plan_version=1,
        control_position="CARD_BATCH_SUCCEEDED",
    )
    _write_checkpoint(
        checkpointer,
        thread_id=plan.trace_id,
        reference=reference.model_dump(mode="json"),
    )
    service = api.PlanReconciliationService(store=store, checkpointer=checkpointer)

    first = service.reconcile(plan.plan_run_id)
    second = service.reconcile(plan.plan_run_id)

    assert first.category == "INTERNAL_INVARIANT"
    assert first.plan_state == "FROZEN"
    assert second.failure_signature == first.failure_signature
    persisted = store.get_plan_run(plan.plan_run_id)
    assert persisted.state is PlanRunState.FROZEN
    assert persisted.reconciliation_required is True
    assert persisted.reconciliation_failure["category"] == "INTERNAL_INVARIANT"
    assert persisted.reconciliation_failure["reason"] == "CHECKPOINT_AHEAD_OF_PLANSTORE"
    assert persisted.reconciliation_signature == first.failure_signature
    assert persisted.reconciliation_attempt_count == 2
    assert persisted.last_reconciled_at is not None
    assert store.list_node_runs(plan.plan_run_id) == ()


def test_consistent_checkpoint_clears_existing_incident_without_resetting_history() -> None:
    """事故证据闭合后清除当前阻断，但保留累计对账次数供审计。"""
    api = _reconciliation_api()
    store = InMemoryPlanStore()
    plan = store.create_or_resume(_materialized_plan(trace_id="trace-clear-incident"))
    _complete_plan(store, plan.plan_run_id)
    now = datetime(2026, 7, 15, 8, 30, tzinfo=timezone.utc)
    store.record_reconciliation_failure(
        plan_run_id=plan.plan_run_id,
        failure={"category": "INTERNAL_INVARIANT", "reason": "TEST_INCIDENT"},
        signature="a" * 64,
        now=now,
    )
    checkpointer = InMemorySaver()
    reference = api.PlanCheckpointReference(
        plan_run_id=plan.plan_run_id,
        plan_version=1,
        control_position="CARD_BATCH_SUCCEEDED",
    )
    _write_checkpoint(
        checkpointer,
        thread_id=plan.trace_id,
        reference=reference.model_dump(mode="json"),
    )

    outcome = api.PlanReconciliationService(
        store=store,
        checkpointer=checkpointer,
    ).reconcile(plan.plan_run_id)

    assert outcome.category == "CONSISTENT"
    persisted = store.get_plan_run(plan.plan_run_id)
    assert persisted.reconciliation_required is False
    assert persisted.reconciliation_failure is None
    assert persisted.reconciliation_signature is None
    assert persisted.reconciliation_attempt_count == 1


def test_reference_rejects_unknown_position_and_non_strict_version() -> None:
    """checkpoint 引用必须使用受控位置和精确正整数版本。"""
    api = _reconciliation_api()

    with pytest.raises(ValidationError):
        api.PlanCheckpointReference(
            plan_run_id="plan-001",
            plan_version=1,
            control_position="CARD_BATCH_PENDING",
        )
    with pytest.raises(ValidationError):
        api.PlanCheckpointReference(
            plan_run_id="plan-001",
            plan_version=True,
            control_position="CARD_BATCH_SUCCEEDED",
        )


def test_invalid_checkpoint_reference_is_persisted_as_internal_invariant() -> None:
    """checkpoint 已存在但引用 Schema 损坏时必须留证冻结，不能只抛校验异常。"""
    api = _reconciliation_api()
    store = InMemoryPlanStore()
    plan = store.create_or_resume(_materialized_plan(trace_id="trace-invalid-reference"))
    checkpointer = InMemorySaver()
    _write_checkpoint(
        checkpointer,
        thread_id=plan.trace_id,
        reference={
            "plan_run_id": plan.plan_run_id,
            "plan_version": 1,
            "control_position": "CARD_BATCH_PENDING",
        },
    )

    outcome = api.PlanReconciliationService(
        store=store,
        checkpointer=checkpointer,
    ).reconcile(plan.plan_run_id)

    assert outcome.category == "INTERNAL_INVARIANT"
    persisted = store.get_plan_run(plan.plan_run_id)
    assert persisted.state is PlanRunState.FROZEN
    assert persisted.reconciliation_failure["reason"] == "INVALID_CHECKPOINT_REFERENCE"


class _RecordingReconciler:
    """记录命令前对账调用，证明 CommandService 不绕过统一入口。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def reconcile_before_command(self, command: PlanCommand) -> None:
        self.calls.append(command.command_id)


def test_command_service_reconciles_before_store_and_incident_blocks_resume() -> None:
    """普通恢复命令必须先对账，并被持久化事故 fail-closed 拒绝。"""
    api = _reconciliation_api()
    store = InMemoryPlanStore()
    plan = store.create_or_resume(_materialized_plan(trace_id="trace-command-guard"))
    store.record_reconciliation_failure(
        plan_run_id=plan.plan_run_id,
        failure={"category": "INTERNAL_INVARIANT", "reason": "CHECKPOINT_AHEAD"},
        signature="b" * 64,
        now=datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc),
    )
    reconciler = _RecordingReconciler()
    command = PlanCommand(
        command_id="command-resume-blocked",
        command_type=PlanCommandType.RESUME,
        plan_run_id=plan.plan_run_id,
        expected_plan_version=1,
        issued_at=datetime(2026, 7, 15, 9, 1, tzinfo=timezone.utc),
    )

    result = CommandService(store=store, reconciler=reconciler).submit(
        command,
        now=datetime(2026, 7, 15, 9, 1, 1, tzinfo=timezone.utc),
    )

    assert reconciler.calls == [command.command_id]
    assert result.accepted is False
    assert result.reason == "RECONCILIATION_REQUIRED"
    assert store.get_plan_run(plan.plan_run_id).state is PlanRunState.FROZEN
    assert api.RECONCILIATION_INTERVAL_SECONDS == 30


def test_startup_periodic_and_command_entrypoints_share_reconcile_logic() -> None:
    """三类触发入口必须委托同一 reconcile，不复制状态修复规则。"""
    api = _reconciliation_api()
    store = InMemoryPlanStore()
    first = store.create_or_resume(_materialized_plan(trace_id="trace-scan-first"))
    second = store.create_or_resume(_materialized_plan(trace_id="trace-scan-second"))
    service = api.PlanReconciliationService(store=store, checkpointer=InMemorySaver())

    startup = service.reconcile_startup()
    periodic = service.reconcile_active_plans_once()
    command = PlanCommand(
        command_id="command-scan",
        command_type=PlanCommandType.RESUME,
        plan_run_id=first.plan_run_id,
        expected_plan_version=1,
        issued_at=datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc),
    )
    before_command = service.reconcile_before_command(command)

    assert {item.plan_run_id for item in startup} == {
        first.plan_run_id,
        second.plan_run_id,
    }
    assert {item.plan_run_id for item in periodic} == {
        first.plan_run_id,
        second.plan_run_id,
    }
    assert before_command.plan_run_id == first.plan_run_id
    assert all(item.category == "NO_CHECKPOINT" for item in startup + periodic)


class _LifecycleReconciler:
    """记录服务装配触发，避免测试依赖真实后台线程或 sleep。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def reconcile_startup(self) -> tuple[str, ...]:
        self.calls.append("startup")
        return ("startup-result",)

    def reconcile_active_plans_once(self) -> tuple[str, ...]:
        self.calls.append("periodic")
        return ("periodic-result",)


def test_plan_engine_service_exposes_startup_and_periodic_entries() -> None:
    """装配服务只复用 Reconciliation Service，不复制扫描实现或启动线程。"""
    try:
        service_module = importlib.import_module("src.plan_engine.service")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 src.plan_engine.service", pytrace=False)
    reconciler = _LifecycleReconciler()
    service = service_module.PlanEngineService(reconciler=reconciler)

    assert service.startup() == ("startup-result",)
    assert service.run_reconciliation_tick() == ("periodic-result",)
    assert reconciler.calls == ["startup", "periodic"]
    assert service.reconciliation_interval_seconds == 30
