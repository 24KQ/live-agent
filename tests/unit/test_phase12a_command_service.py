"""Phase 12A 人工命令账本、乐观校验与 TTL 的 TDD 契约测试。

命令服务不是普通状态修改接口：每条命令先以 ``command_id`` 进入 Store 权威账本，
再在同一原子边界检查版本、节点状态与固定 TTL。重复投递必须重放首次结果，任何
失败路径都不得改变节点；本文件按幂等、版本、状态、TTL 的顺序逐轮增加行为。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.commands import CommandService, PlanCommand
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanCommandType,
    PlanNodeState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.store import InMemoryPlanStore, MaterializedPlan, PlanQueryService
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str) -> CatalogProduct:
    """构造命令测试所需的完整商品快照，不连接真实 Catalog。"""
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


def _materialized_plan() -> MaterializedPlan:
    """构造含 PREPARE、手卡和 COLLECT 节点的最小完整物化计划。"""
    product_id = "p001"
    planning_input = CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-001",
        live_plan=LivePlanDraft(
            room_id="room-001",
            trace_id="trace-001",
            items=[
                LivePlanItem(
                    rank=1,
                    product_id=product_id,
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="命令契约测试",
                )
            ],
        ),
        products_by_id={product_id: _product(product_id)},
    )
    canonical = CanonicalCardBatchProposalProvider().propose_sync(planning_input)
    proposal = CandidatePlanProposal(
        provider_id=canonical.provider_id,
        provider_version=canonical.provider_version,
        nodes=canonical.nodes,
    )
    capabilities: dict[str, ResolvedPlanCapability] = {}
    for node in proposal.nodes:
        if node.logical_key == "prepare-card-batch":
            node_type = "PREPARE_CARD_BATCH"
        elif node.logical_key == "collect-card-results":
            node_type = "COLLECT_CARD_RESULTS"
        else:
            node_type = "SKILL"
        capabilities[node.logical_key] = ResolvedPlanCapability(
            node_type=node_type,
            skill_id=node.skill_id,
            skill_version="1.0.0" if node.skill_id else None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=30 if node.skill_id else None,
            resource_keys=(f"room:room-001:product:{product_id}",) if node.skill_id else (),
            max_concurrency=4,
        )
    return MaterializedPlan(
        planning_input=planning_input,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _waiting_node(
    state: PlanNodeState,
) -> tuple[InMemoryPlanStore, str, str, datetime]:
    """通过合法 RUNNING 转移建立人工命令前置状态，并返回确定性测试时钟。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    issued_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=issued_at - timedelta(seconds=2),
        lease_seconds=60,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=state,
        output={"reason": "需要人工确认"},
        now=issued_at - timedelta(seconds=1),
    )
    return store, plan_run.plan_run_id, claim.node_id, issued_at


def test_duplicate_command_id_replays_first_result_after_state_changes() -> None:
    """APPROVE 首次成功后，同一 command_id 必须重放成功而非按新状态重新失败。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(
        PlanNodeState.WAITING_APPROVAL
    )
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-approve-001",
        command_type=PlanCommandType.APPROVE,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload={"operator_id": "operator-001"},
        issued_at=issued_at,
    )

    first = service.submit(command, now=issued_at + timedelta(seconds=1))
    replay = service.submit(command, now=issued_at + timedelta(minutes=20))

    assert first.accepted is True
    assert replay == first
    assert replay.reason == "ACCEPTED"
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is PlanNodeState.READY
    assert node.ready_at == first.completed_at


def test_stale_plan_version_is_rejected_without_changing_node() -> None:
    """命令携带的 PlanVersion 与当前版本不一致时，只记拒绝结果且节点保持原状。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(
        PlanNodeState.WAITING_APPROVAL
    )
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-stale-version-001",
        command_type=PlanCommandType.APPROVE,
        plan_run_id=plan_run_id,
        expected_plan_version=2,
        node_id=node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload={"operator_id": "operator-001"},
        issued_at=issued_at,
    )

    result = service.submit(command, now=issued_at + timedelta(seconds=1))

    assert result.accepted is False
    assert result.reason == "PLAN_VERSION_MISMATCH"
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is PlanNodeState.WAITING_APPROVAL


def test_expected_node_status_mismatch_is_rejected_without_changing_node() -> None:
    """节点实际状态与命令快照不一致时，禁止把过时人工判断应用到新状态。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(
        PlanNodeState.WAITING_APPROVAL
    )
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-stale-node-001",
        command_type=PlanCommandType.APPROVE,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=PlanNodeState.READY,
        payload={"operator_id": "operator-001"},
        issued_at=issued_at,
    )

    result = service.submit(command, now=issued_at + timedelta(seconds=1))

    assert result.accepted is False
    assert result.reason == "NODE_STATUS_MISMATCH"
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is PlanNodeState.WAITING_APPROVAL


@pytest.mark.parametrize(
    ("command_type", "waiting_state", "elapsed"),
    [
        (
            PlanCommandType.APPROVE,
            PlanNodeState.WAITING_APPROVAL,
            timedelta(minutes=10, microseconds=1),
        ),
        (
            PlanCommandType.RECONCILE,
            PlanNodeState.WAITING_RECONCILIATION,
            timedelta(minutes=30, microseconds=1),
        ),
    ],
    ids=("approval-10-minutes", "reconciliation-30-minutes"),
)
def test_expired_commands_fail_closed_without_changing_node(
    command_type: PlanCommandType,
    waiting_state: PlanNodeState,
    elapsed: timedelta,
) -> None:
    """审批十分钟、对账三十分钟后均失效，过期命令只记拒绝账本。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(waiting_state)
    service = CommandService(store)
    command = PlanCommand(
        command_id=f"command-expired-{command_type.value.lower()}",
        command_type=command_type,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=waiting_state,
        payload={"operator_id": "operator-001"},
        issued_at=issued_at,
    )

    result = service.submit(command, now=issued_at + elapsed)

    assert result.accepted is False
    assert result.reason == "COMMAND_EXPIRED"
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is waiting_state


@pytest.mark.parametrize(
    ("command_type", "waiting_state", "ttl"),
    [
        (
            PlanCommandType.APPROVE,
            PlanNodeState.WAITING_APPROVAL,
            timedelta(minutes=10),
        ),
        (
            PlanCommandType.RECONCILE,
            PlanNodeState.WAITING_RECONCILIATION,
            timedelta(minutes=30),
        ),
    ],
)
def test_command_at_exact_ttl_fails_closed(
    command_type: PlanCommandType,
    waiting_state: PlanNodeState,
    ttl: timedelta,
) -> None:
    """到达 TTL 截止时刻即视为过期，不能再推进节点状态。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(waiting_state)
    command = PlanCommand(
        command_id=f"command-boundary-{command_type.value.lower()}",
        command_type=command_type,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=waiting_state,
        payload={"outcome": "SUCCEEDED"},
        issued_at=issued_at,
    )

    result = CommandService(store).submit(command, now=issued_at + ttl)

    assert result.accepted is False
    assert result.reason == "COMMAND_EXPIRED"
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is waiting_state


def test_future_issued_command_fails_closed_without_changing_node() -> None:
    """未来签发时间不能借时钟偏差延长命令有效期或提前应用人工决定。"""
    store, plan_run_id, node_id, now = _waiting_node(PlanNodeState.WAITING_APPROVAL)
    command = PlanCommand(
        command_id="command-future-issued",
        command_type=PlanCommandType.APPROVE,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload={"operator_id": "operator-001"},
        issued_at=now + timedelta(seconds=1),
    )

    result = CommandService(store).submit(command, now=now)

    assert result.accepted is False
    assert result.reason == "COMMAND_NOT_YET_VALID"
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is PlanNodeState.WAITING_APPROVAL


@pytest.mark.parametrize("invalid_version", [True, 1.0, 0, -1])
def test_plan_command_requires_strict_positive_plan_version(
    invalid_version: object,
) -> None:
    """人工命令的乐观版本必须是精确正整数，禁止 Pydantic 隐式转换。"""
    with pytest.raises(ValidationError, match="expected_plan_version"):
        PlanCommand(
            command_id="command-invalid-version",
            command_type=PlanCommandType.RESUME,
            plan_run_id="plan-run-001",
            expected_plan_version=invalid_version,  # type: ignore[arg-type]
            issued_at=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
        )


def test_reject_transitions_waiting_approval_to_failed() -> None:
    """REJECT 只能闭合 WAITING_APPROVAL，并同步把 PlanRun 标记为失败。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(
        PlanNodeState.WAITING_APPROVAL
    )
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-reject-001",
        command_type=PlanCommandType.REJECT,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload={"operator_id": "operator-001", "reason": "审批拒绝"},
        issued_at=issued_at,
    )

    result = service.submit(command, now=issued_at + timedelta(seconds=1))

    assert result.accepted is True
    assert result.resulting_node_status is PlanNodeState.FAILED
    assert store.get_plan_run(plan_run_id).state.value == "FAILED"


def test_reconcile_closes_waiting_reconciliation_with_explicit_outcome() -> None:
    """RECONCILE 只能依据显式 outcome 闭合对账节点，并保留外部引用载荷。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(
        PlanNodeState.WAITING_RECONCILIATION
    )
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-reconcile-001",
        command_type=PlanCommandType.RECONCILE,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=PlanNodeState.WAITING_RECONCILIATION,
        payload={
            "operator_id": "operator-001",
            "outcome": "SUCCEEDED",
            "reference": {"external_id": "external-001"},
        },
        issued_at=issued_at,
    )

    result = service.submit(command, now=issued_at + timedelta(minutes=20))

    assert result.accepted is True
    assert result.resulting_node_status is PlanNodeState.SUCCEEDED
    node = next(item for item in store.list_nodes(plan_run_id) if item.node_id == node_id)
    assert node.state is PlanNodeState.SUCCEEDED
    node_run = store.list_node_runs(plan_run_id, node_id)[0]
    assert node_run.output["reconciliation"]["reference"]["external_id"] == "external-001"


def test_resume_reactivates_frozen_plan_without_rewriting_nodes() -> None:
    """RESUME 只解除 PlanRun 的人工冻结，节点事实保持冻结前状态并继续可调度。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    issued_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    before_nodes = store.list_nodes(plan_run.plan_run_id)
    store.freeze_plan(plan_run_id=plan_run.plan_run_id)
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-resume-001",
        command_type=PlanCommandType.RESUME,
        plan_run_id=plan_run.plan_run_id,
        expected_plan_version=1,
        node_id=None,
        expected_node_status=None,
        payload={"operator_id": "operator-001"},
        issued_at=issued_at,
    )

    result = service.submit(command, now=issued_at + timedelta(seconds=1))

    assert result.accepted is True
    assert store.get_plan_run(plan_run.plan_run_id).state.value == "ACTIVE"
    assert store.list_nodes(plan_run.plan_run_id) == before_nodes


def test_query_service_returns_defensive_command_ledger_view() -> None:
    """Command 查询必须来自 Store 首次账本，导出副本的修改不能污染后续读取。"""
    store, plan_run_id, node_id, issued_at = _waiting_node(
        PlanNodeState.WAITING_APPROVAL
    )
    service = CommandService(store)
    command = PlanCommand(
        command_id="command-query-001",
        command_type=PlanCommandType.APPROVE,
        plan_run_id=plan_run_id,
        expected_plan_version=1,
        node_id=node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload={"operator": {"id": "operator-001"}},
        issued_at=issued_at,
    )
    service.submit(command, now=issued_at + timedelta(seconds=1))
    query = PlanQueryService(store)

    exported = query.get_command(command.command_id).model_dump(mode="json")
    exported["payload"]["operator"]["id"] = "tampered"

    persisted = query.get_command(command.command_id)
    assert persisted.payload["operator"]["id"] == "operator-001"
    assert persisted.accepted is True
    assert persisted.reason == "ACCEPTED"
