"""Phase 12A PlanStore 的并发事实与不可变查询契约测试。

本文件按 TDD 顺序逐步建立 Store 的行为边界。首组测试只覆盖冻结计划幂等创建、
摘要冲突、初始节点状态和首次 claim；租约、fencing 与防御复制会在首组转绿后追加，
从而让每一轮失败都能明确指向尚未实现的生产行为。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanNodeState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.store import (
    InMemoryPlanStore,
    MaterializedPlan,
    PlanQueryService,
    PlanStoreInvariantError,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str) -> CatalogProduct:
    """构造完整商品快照，避免 Store 测试依赖外部 Catalog 或数据库。"""
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


def _planning_input(*, trace_id: str = "trace-001") -> CardBatchPlanningInput:
    """构造稳定的三商品冻结输入，使重复调用拥有同一个 ``run_key``。"""
    product_ids = ("p001", "p002", "p003")
    items = [
        LivePlanItem(
            rank=index,
            product_id=product_id,
            product_name=f"商品 {product_id}",
            role="引流款",
            reason="Store 契约测试",
        )
        for index, product_id in enumerate(product_ids, start=1)
    ]
    return CardBatchPlanningInput(
        room_id="room-001",
        trace_id=trace_id,
        live_plan=LivePlanDraft(room_id="room-001", trace_id=trace_id, items=items),
        products_by_id={product_id: _product(product_id) for product_id in product_ids},
    )


def _capability_for(logical_key: str) -> ResolvedPlanCapability:
    """按规范候选节点补齐可信能力事实，候选本身无权声明这些执行约束。"""
    if logical_key == "prepare-card-batch":
        return ResolvedPlanCapability(
            node_type="PREPARE_CARD_BATCH",
            skill_id=None,
            skill_version=None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=None,
            resource_keys=(),
            max_concurrency=4,
        )
    if logical_key == "collect-card-results":
        return ResolvedPlanCapability(
            node_type="COLLECT_CARD_RESULTS",
            skill_id=None,
            skill_version=None,
            lifecycle=frozenset(),
            risk_level=None,
            max_attempt_seconds=None,
            resource_keys=(),
            max_concurrency=4,
        )
    product_id = logical_key.removeprefix("card:")
    return ResolvedPlanCapability(
        node_type="SKILL",
        skill_id="generate_product_card",
        skill_version="1.0.0",
        lifecycle=frozenset(),
        risk_level=None,
        max_attempt_seconds=30,
        resource_keys=(f"room:room-001:product:{product_id}",),
        max_concurrency=4,
    )


def _materialized_plan(
    *,
    provider_version: str = "1.0.0",
    trace_id: str = "trace-001",
) -> MaterializedPlan:
    """物化冻结输入、候选 DAG 与可信能力映射，作为 Store 唯一创建参数。"""
    planning_input = _planning_input(trace_id=trace_id)
    canonical = CanonicalCardBatchProposalProvider().propose_sync(planning_input)
    proposal = CandidatePlanProposal(
        provider_id=canonical.provider_id,
        provider_version=provider_version,
        nodes=canonical.nodes,
    )
    return MaterializedPlan(
        planning_input=planning_input,
        proposal=proposal,
        capabilities_by_logical_key={
            node.logical_key: _capability_for(node.logical_key)
            for node in proposal.nodes
        },
    )


def test_create_or_resume_reuses_same_frozen_plan() -> None:
    """同一冻结输入及同一摘要必须复用首次 PlanRun，版本始终为 1。"""
    store = InMemoryPlanStore()
    plan = _materialized_plan()

    first = store.create_or_resume(plan)
    replay = store.create_or_resume(plan)

    assert replay.plan_run_id == first.plan_run_id
    assert replay.current_version == first.current_version == 1


def test_create_or_resume_rejects_same_run_key_with_different_digest() -> None:
    """相同 ``run_key`` 若携带不同候选事实，必须 fail-closed 而非覆盖旧计划。"""
    store = InMemoryPlanStore()
    store.create_or_resume(_materialized_plan(provider_version="1.0.0"))

    with pytest.raises(PlanStoreInvariantError, match="摘要"):
        store.create_or_resume(_materialized_plan(provider_version="1.0.1"))


def test_create_or_resume_marks_prepare_ready_and_other_nodes_pending() -> None:
    """首次持久化只开放 PREPARE 节点，其余节点等待依赖闭合。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())

    nodes = {node.logical_key: node for node in store.list_nodes(plan_run.plan_run_id)}

    assert nodes["prepare-card-batch"].state is PlanNodeState.READY
    assert all(
        node.state is PlanNodeState.PENDING
        for logical_key, node in nodes.items()
        if logical_key != "prepare-card-batch"
    )


def test_materialized_plan_rejects_capability_that_does_not_match_candidate() -> None:
    """物化边界必须复核候选与能力事实，不能只检查 logical_key 集合相同。"""
    plan = _materialized_plan()
    capabilities = dict(plan.capabilities_by_logical_key)
    card_key = "card:p001"
    capabilities[card_key] = replace(
        capabilities[card_key],
        skill_id="query_products",
    )

    with pytest.raises(PlanStoreInvariantError, match="能力"):
        MaterializedPlan(
            planning_input=plan.planning_input,
            proposal=plan.proposal,
            capabilities_by_logical_key=capabilities,
        )


@pytest.mark.parametrize("invalid_version", [True, 1.0, 0, -1])
def test_version_queries_reject_non_positive_or_non_integer_versions(
    invalid_version: object,
) -> None:
    """PlanVersion 查询必须使用精确正整数，不能利用 Python 相等语义命中版本 1。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    query = PlanQueryService(store)

    with pytest.raises(PlanStoreInvariantError, match="版本"):
        query.get_plan_version(plan_run.plan_run_id, invalid_version)  # type: ignore[arg-type]
    with pytest.raises(PlanStoreInvariantError, match="版本"):
        query.list_nodes(  # type: ignore[arg-type]
            plan_run.plan_run_id,
            version_number=invalid_version,
        )


def test_claim_ready_nodes_creates_independent_node_run() -> None:
    """首次 claim 必须创建可审计 NodeRun，并保存 worker、租约与 fencing 事实。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)

    claims = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
        limit=1,
    )

    assert len(claims) == 1
    assert claims[0].node_run_id
    assert claims[0].attempt_number == 1
    assert claims[0].claim_version == 1
    assert claims[0].worker_id == "worker-001"
    assert claims[0].state is PlanNodeState.RUNNING
    assert claims[0].lease_until > claimed_at


def test_claim_ready_nodes_excludes_resources_locked_by_another_plan() -> None:
    """不同 PlanRun 也不能同时 claim 同一房间商品的有效资源锁。"""
    store = InMemoryPlanStore()
    first = store.create_or_resume(_materialized_plan(trace_id="trace-001"))
    second = store.create_or_resume(_materialized_plan(trace_id="trace-002"))
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)

    # 两张计划先各自完成无资源锁的 PREPARE，使三张相同商品手卡同时 READY。
    for plan_run in (first, second):
        prepare = store.claim_ready_nodes(
            plan_run_id=plan_run.plan_run_id,
            worker_id=f"prepare-{plan_run.plan_run_id}",
            now=started_at,
            lease_seconds=30,
        )[0]
        store.record_node_result(
            node_run_id=prepare.node_run_id,
            worker_id=prepare.worker_id,
            claim_version=prepare.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output={"prepared": True},
            now=started_at + timedelta(seconds=1),
        )

    first_cards = store.claim_ready_nodes(
        plan_run_id=first.plan_run_id,
        worker_id="worker-first",
        now=started_at + timedelta(seconds=2),
        lease_seconds=30,
        limit=3,
    )
    second_cards = store.claim_ready_nodes(
        plan_run_id=second.plan_run_id,
        worker_id="worker-second",
        now=started_at + timedelta(seconds=2),
        lease_seconds=30,
        limit=3,
    )

    assert len(first_cards) == 3
    assert second_cards == ()


def test_reclaim_rejects_resource_acquired_by_another_plan_after_expiry() -> None:
    """旧 lease 到期并释放资源后，不得从新持有者手中再次 reclaim 同一资源。"""
    store = InMemoryPlanStore()
    first = store.create_or_resume(_materialized_plan(trace_id="trace-001"))
    second = store.create_or_resume(_materialized_plan(trace_id="trace-002"))
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)

    for plan_run in (first, second):
        prepare = store.claim_ready_nodes(
            plan_run_id=plan_run.plan_run_id,
            worker_id=f"prepare-{plan_run.plan_run_id}",
            now=started_at,
            lease_seconds=30,
        )[0]
        store.record_node_result(
            node_run_id=prepare.node_run_id,
            worker_id=prepare.worker_id,
            claim_version=prepare.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output={"prepared": True},
            now=started_at + timedelta(seconds=1),
        )

    expired = store.claim_ready_nodes(
        plan_run_id=first.plan_run_id,
        worker_id="worker-expired",
        now=started_at + timedelta(seconds=2),
        lease_seconds=30,
        limit=1,
    )[0]
    acquired = store.claim_ready_nodes(
        plan_run_id=second.plan_run_id,
        worker_id="worker-current",
        now=expired.lease_until,
        lease_seconds=30,
        limit=1,
    )
    assert len(acquired) == 1

    with pytest.raises(PlanStoreInvariantError, match="资源"):
        store.reclaim_expired_node(
            node_run_id=expired.node_run_id,
            worker_id="worker-reclaim",
            now=expired.lease_until,
            lease_seconds=30,
        )


def test_heartbeat_rejects_wrong_claim_version_without_extending_lease() -> None:
    """heartbeat 必须同时匹配 NodeRun、worker 和 fencing token，错误 token 不得续租。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]

    with pytest.raises(PlanStoreInvariantError, match="fencing"):
        store.heartbeat_node_run(
            node_run_id=claim.node_run_id,
            worker_id=claim.worker_id,
            claim_version=claim.claim_version + 1,
            now=claimed_at + timedelta(seconds=5),
            lease_seconds=30,
        )

    # 失败路径之后重新查询权威记录，证明 Store 没有先写 lease 再做 token 校验。
    persisted = store.list_node_runs(plan_run.plan_run_id)[0]
    assert persisted.lease_until == claim.lease_until


def test_reclaim_rejects_unexpired_lease_without_creating_attempt() -> None:
    """当前租约尚未过期时，其他 worker 不能抢占节点或制造额外 NodeRun。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]

    with pytest.raises(PlanStoreInvariantError, match="尚未过期"):
        store.reclaim_expired_node(
            node_run_id=claim.node_run_id,
            worker_id="worker-002",
            now=claimed_at + timedelta(seconds=29),
            lease_seconds=30,
        )

    persisted = store.list_node_runs(plan_run.plan_run_id)
    assert len(persisted) == 1
    assert persisted[0].worker_id == "worker-001"


def test_reclaim_expired_lease_creates_new_node_run_and_higher_fencing_token() -> None:
    """过期回收必须追加新 attempt，旧 NodeRun 作为历史保留且 fencing 单调递增。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    original = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]

    reclaimed = store.reclaim_expired_node(
        node_run_id=original.node_run_id,
        worker_id="worker-002",
        now=original.lease_until,
        lease_seconds=45,
    )

    assert reclaimed.node_run_id != original.node_run_id
    assert reclaimed.node_id == original.node_id
    assert reclaimed.attempt_number == original.attempt_number + 1
    assert reclaimed.claim_version > original.claim_version
    assert reclaimed.worker_id == "worker-002"
    assert [item.node_run_id for item in store.list_node_runs(plan_run.plan_run_id)] == [
        original.node_run_id,
        reclaimed.node_run_id,
    ]


def test_reclaimed_old_node_run_cannot_record_terminal_result() -> None:
    """新 fencing token 签发后，旧 worker 即使提交成功结果也不能改写节点终态。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    original = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]
    store.reclaim_expired_node(
        node_run_id=original.node_run_id,
        worker_id="worker-002",
        now=original.lease_until,
        lease_seconds=30,
    )

    with pytest.raises(PlanStoreInvariantError, match="fencing"):
        store.record_node_result(
            node_run_id=original.node_run_id,
            worker_id=original.worker_id,
            claim_version=original.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output={"prepared": True},
            now=original.lease_until,
        )

    prepare = next(
        node
        for node in store.list_nodes(plan_run.plan_run_id)
        if node.logical_key == "prepare-card-batch"
    )
    assert prepare.state is PlanNodeState.RUNNING


def test_record_current_success_updates_node_and_readies_satisfied_dependents() -> None:
    """当前 fencing 成功提交后，应闭合 NodeRun/节点并仅开放依赖已满足的后继节点。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]

    completed = store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"prepared": True},
        now=claimed_at + timedelta(seconds=1),
    )

    nodes = {node.logical_key: node for node in store.list_nodes(plan_run.plan_run_id)}
    assert completed.state is PlanNodeState.SUCCEEDED
    assert completed.output == {"prepared": True}
    assert nodes["prepare-card-batch"].state is PlanNodeState.SUCCEEDED
    assert all(
        nodes[f"card:{product_id}"].state is PlanNodeState.READY
        for product_id in ("p001", "p002", "p003")
    )
    assert nodes["collect-card-results"].state is PlanNodeState.PENDING


def test_query_service_returns_defensive_json_safe_copies() -> None:
    """查询服务只能经 Store 读取冻结视图，修改导出副本不得污染权威计划事实。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"prepared": {"count": 3}},
        now=claimed_at + timedelta(seconds=1),
    )
    query = PlanQueryService(store)

    run_dump = query.get_plan_run(plan_run.plan_run_id).model_dump(mode="json")
    version_dump = query.get_plan_version(plan_run.plan_run_id, 1).model_dump(mode="json")
    node_run_dump = query.list_node_runs(plan_run.plan_run_id)[0].model_dump(mode="json")
    run_dump["planning_input"]["room_id"] = "tampered-room"
    version_dump["proposal"]["provider_id"] = "tampered-provider"
    node_run_dump["output"]["prepared"]["count"] = 999

    assert query.get_plan_run(plan_run.plan_run_id).planning_input["room_id"] == "room-001"
    assert query.get_plan_version(plan_run.plan_run_id, 1).proposal["provider_id"] == "canonical-card-batch"
    assert query.list_node_runs(plan_run.plan_run_id)[0].output == {
        "prepared": {"count": 3}
    }


def test_schedule_retry_uses_d015_and_claims_only_after_retry_time() -> None:
    """重试必须先从 RUNNING 进入 RETRY_WAIT，到期后才能产生更高 token 的新 attempt。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    original = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]
    retry_at = claimed_at + timedelta(seconds=10)

    waiting = store.schedule_retry(
        node_run_id=original.node_run_id,
        worker_id=original.worker_id,
        claim_version=original.claim_version,
        now=claimed_at + timedelta(seconds=1),
        retry_at=retry_at,
    )

    assert waiting.state is PlanNodeState.RETRY_WAIT
    assert store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-002",
        now=retry_at - timedelta(microseconds=1),
        lease_seconds=30,
    ) == ()
    retried = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-002",
        now=retry_at,
        lease_seconds=30,
    )[0]
    assert retried.attempt_number == original.attempt_number + 1
    assert retried.claim_version > original.claim_version


def test_reconcile_plan_reference_closes_only_waiting_reconciliation() -> None:
    """Store 对账原语必须按 D-015 闭合节点，并防御复制外部引用事实。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    claimed_at = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=claimed_at,
        lease_seconds=30,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.WAITING_RECONCILIATION,
        output={"external_state": "UNKNOWN"},
        now=claimed_at + timedelta(seconds=1),
    )
    reference = {"external_id": "external-001"}

    reconciled = store.reconcile_plan_reference(
        plan_run_id=plan_run.plan_run_id,
        node_id=claim.node_id,
        outcome=PlanNodeState.SUCCEEDED,
        reference=reference,
    )
    reference["external_id"] = "tampered"

    assert reconciled.state is PlanNodeState.SUCCEEDED
    persisted_run = store.list_node_runs(plan_run.plan_run_id, claim.node_id)[0]
    assert persisted_run.output["reconciliation"]["reference"]["external_id"] == "external-001"
