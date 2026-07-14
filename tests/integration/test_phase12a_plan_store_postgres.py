"""Phase 12A PostgreSQL PlanStore 的真实事务与并发契约测试。

测试使用本地 PostgreSQL，但所有计划身份都带随机后缀，避免依赖清库顺序。重点验证
内存锁无法证明的跨连接语义：幂等物化、SKIP LOCKED、跨计划资源排他、lease 与
fencing。测试不读取或修改 LangGraph PostgresSaver 的任何私有表。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from src.config.settings import get_settings
from src.plan_engine.commands import CommandService, PlanCommand
from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanCommandType,
    PlanNodeState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
import src.plan_engine.store as store_module
from src.plan_engine.store import MaterializedPlan, PlanStoreInvariantError
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _initialize_schema(settings: Any) -> None:
    """通过生产公开初始化入口建表，让缺少 Phase 12A DDL 成为明确红灯。"""
    initializer = getattr(store_module, "initialize_plan_engine_schema", None)
    assert initializer is not None, "尚未实现 initialize_plan_engine_schema"
    initializer(settings)


def _postgres_store(settings: Any) -> Any:
    """延迟取得生产 Store，避免缺少实现时在 pytest 收集阶段直接报错。"""
    store_type = getattr(store_module, "PostgresPlanStore", None)
    assert store_type is not None, "尚未实现 PostgresPlanStore"
    return store_type(settings)


def _product(product_id: str) -> CatalogProduct:
    """构造不依赖外部 Catalog 的完整商品快照。"""
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


def _materialized_plan(*, room_id: str, trace_id: str) -> MaterializedPlan:
    """构造单商品规范 DAG，使每个测试只关注数据库协调而非业务规模。"""
    product_id = "p001"
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
                    reason="PostgreSQL Store 契约测试",
                )
            ],
        ),
        products_by_id={product_id: _product(product_id)},
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


def _complete_prepare(store: Any, plan_run_id: str, now: datetime) -> None:
    """通过公开 API 合法闭合 PREPARE，令手卡节点进入 READY。"""
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run_id,
        worker_id=f"prepare-{uuid4()}",
        now=now,
        lease_seconds=30,
        limit=1,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"prepared_product_ids": ["p001"]},
        now=now + timedelta(seconds=1),
    )


def test_postgres_create_or_resume_persists_relational_plan_snapshot() -> None:
    """一次物化必须原子写入 Run、Version、Node 和依赖边，并可安全重放。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    store = _postgres_store(settings)
    plan = _materialized_plan(
        room_id=f"room-phase12a-{suffix}",
        trace_id=f"trace-phase12a-{suffix}",
    )

    created = store.create_or_resume(plan)
    replay = _postgres_store(settings).create_or_resume(plan)

    assert replay == created
    assert len(store.list_nodes(created.plan_run_id)) == 3
    with psycopg.connect(
        **settings.postgres_connection_kwargs,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM plan_versions WHERE plan_run_id = %(plan_run_id)s::uuid)
                        AS version_count,
                    (SELECT count(*) FROM plan_nodes WHERE plan_run_id = %(plan_run_id)s::uuid)
                        AS node_count,
                    (SELECT count(*)
                     FROM plan_node_dependencies
                     WHERE plan_run_id = %(plan_run_id)s::uuid) AS dependency_count;
                """,
                {"plan_run_id": created.plan_run_id},
            )
            row = cursor.fetchone()
    assert row == {"version_count": 1, "node_count": 3, "dependency_count": 2}


def test_two_postgres_connections_claim_one_ready_node_once() -> None:
    """两个独立连接同时 claim 时，只能有一个 NodeRun 获得 READY 节点。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    run = _postgres_store(settings).create_or_resume(
        _materialized_plan(
            room_id=f"room-claim-{suffix}",
            trace_id=f"trace-claim-{suffix}",
        )
    )
    now = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)

    def claim(worker_id: str) -> tuple[Any, ...]:
        return _postgres_store(settings).claim_ready_nodes(
            plan_run_id=run.plan_run_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=60,
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = tuple(pool.map(claim, ("worker-a", "worker-b")))

    claims = tuple(item for batch in batches for item in batch)
    assert len(claims) == 1
    assert len(_postgres_store(settings).list_node_runs(run.plan_run_id)) == 1


def test_postgres_claim_excludes_resource_held_by_another_plan() -> None:
    """不同 PlanRun 的 READY 节点使用同一资源键时也必须串行。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    room_id = f"room-resource-{suffix}"
    store = _postgres_store(settings)
    first = store.create_or_resume(
        _materialized_plan(room_id=room_id, trace_id=f"trace-a-{suffix}")
    )
    second = store.create_or_resume(
        _materialized_plan(room_id=room_id, trace_id=f"trace-b-{suffix}")
    )
    now = datetime(2026, 7, 14, 9, 10, tzinfo=timezone.utc)
    _complete_prepare(store, first.plan_run_id, now)
    _complete_prepare(store, second.plan_run_id, now)

    first_claim = _postgres_store(settings).claim_ready_nodes(
        plan_run_id=first.plan_run_id,
        worker_id="worker-resource-a",
        now=now + timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )
    second_claim = _postgres_store(settings).claim_ready_nodes(
        plan_run_id=second.plan_run_id,
        worker_id="worker-resource-b",
        now=now + timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )

    assert len(first_claim) == 1
    assert second_claim == ()


def test_postgres_reclaim_preserves_history_and_rejects_stale_fencing() -> None:
    """lease 到期后追加新 NodeRun；旧 worker 永久不能 heartbeat 或提交终态。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    store = _postgres_store(settings)
    run = store.create_or_resume(
        _materialized_plan(
            room_id=f"room-fencing-{suffix}",
            trace_id=f"trace-fencing-{suffix}",
        )
    )
    now = datetime(2026, 7, 14, 9, 20, tzinfo=timezone.utc)
    original = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="worker-old",
        now=now,
        lease_seconds=30,
        limit=1,
    )[0]

    with pytest.raises(PlanStoreInvariantError):
        _postgres_store(settings).heartbeat_node_run(
            node_run_id=original.node_run_id,
            worker_id=original.worker_id,
            claim_version=original.claim_version + 1,
            now=now + timedelta(seconds=1),
            lease_seconds=30,
        )

    reclaimed = _postgres_store(settings).reclaim_expired_node(
        node_run_id=original.node_run_id,
        worker_id="worker-new",
        now=original.lease_until,
        lease_seconds=45,
    )

    with pytest.raises(PlanStoreInvariantError):
        store.record_node_result(
            node_run_id=original.node_run_id,
            worker_id=original.worker_id,
            claim_version=original.claim_version,
            state=PlanNodeState.SUCCEEDED,
            output={"prepared": True},
            now=original.lease_until,
        )

    history = store.list_node_runs(run.plan_run_id)
    assert [item.node_run_id for item in history] == [
        original.node_run_id,
        reclaimed.node_run_id,
    ]
    assert reclaimed.claim_version > original.claim_version


def test_postgres_retry_requires_current_fencing_and_creates_new_attempt_at_due_time() -> None:
    """RETRY_WAIT 调度必须匹配当前 token，且到期后才创建新的 NodeRun。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    store = _postgres_store(settings)
    run = store.create_or_resume(
        _materialized_plan(
            room_id=f"room-retry-{suffix}",
            trace_id=f"trace-retry-{suffix}",
        )
    )
    now = datetime(2026, 7, 14, 9, 30, tzinfo=timezone.utc)
    original = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="worker-retry-old",
        now=now,
        lease_seconds=30,
        limit=1,
    )[0]
    retry_at = now + timedelta(seconds=10)

    with pytest.raises(PlanStoreInvariantError):
        store.schedule_retry(
            node_run_id=original.node_run_id,
            worker_id=original.worker_id,
            claim_version=original.claim_version + 1,
            now=now + timedelta(seconds=1),
            retry_at=retry_at,
        )

    store.schedule_retry(
        node_run_id=original.node_run_id,
        worker_id=original.worker_id,
        claim_version=original.claim_version,
        now=now + timedelta(seconds=1),
        retry_at=retry_at,
    )
    assert store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="worker-too-early",
        now=retry_at - timedelta(microseconds=1),
        lease_seconds=30,
        limit=1,
    ) == ()

    retried = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="worker-retry-new",
        now=retry_at,
        lease_seconds=30,
        limit=1,
    )[0]
    assert retried.attempt_number == original.attempt_number + 1
    assert retried.claim_version > original.claim_version


def test_postgres_claim_waits_for_plan_freeze_transaction() -> None:
    """claim 必须锁定 PlanRun，不能在并发冻结尚未提交时读取旧 ACTIVE 后偷跑。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    store = _postgres_store(settings)
    run = store.create_or_resume(
        _materialized_plan(
            room_id=f"room-freeze-race-{suffix}",
            trace_id=f"trace-freeze-race-{suffix}",
        )
    )
    now = datetime(2026, 7, 14, 9, 40, tzinfo=timezone.utc)

    # 先在独立事务写入 FROZEN 但不提交。正确 claim 必须等待同一 PlanRun 行锁；
    # 若只做普通 SELECT，它会看到旧 ACTIVE 并错误创建 NodeRun。
    locking_connection = psycopg.connect(**settings.postgres_connection_kwargs)
    try:
        with locking_connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE plan_runs
                SET state = 'FROZEN'
                WHERE plan_run_id = %(plan_run_id)s::uuid;
                """,
                {"plan_run_id": run.plan_run_id},
            )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                _postgres_store(settings).claim_ready_nodes,
                plan_run_id=run.plan_run_id,
                worker_id="worker-freeze-race",
                now=now,
                lease_seconds=30,
                limit=1,
            )
            try:
                with pytest.raises(FuturesTimeoutError):
                    future.result(timeout=0.3)
            finally:
                locking_connection.commit()
            assert future.result(timeout=2) == ()
    finally:
        locking_connection.close()

    assert store.list_node_runs(run.plan_run_id) == ()


def test_postgres_result_waits_for_plan_lock_before_locking_node() -> None:
    """结果提交必须遵守 PlanRun -> NodeRun -> Node，避免与命令路径锁顺序反转。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    store = _postgres_store(settings)
    run = store.create_or_resume(
        _materialized_plan(
            room_id=f"room-lock-order-{suffix}",
            trace_id=f"trace-lock-order-{suffix}",
        )
    )
    now = datetime(2026, 7, 14, 9, 45, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="worker-lock-order",
        now=now,
        lease_seconds=60,
        limit=1,
    )[0]

    locking_connection = psycopg.connect(**settings.postgres_connection_kwargs)
    try:
        with locking_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT plan_run_id
                FROM plan_runs
                WHERE plan_run_id = %(plan_run_id)s::uuid
                FOR UPDATE;
                """,
                {"plan_run_id": run.plan_run_id},
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    store.record_node_result,
                    node_run_id=claim.node_run_id,
                    worker_id=claim.worker_id,
                    claim_version=claim.claim_version,
                    state=PlanNodeState.SUCCEEDED,
                    output={"prepared": True},
                    now=now + timedelta(seconds=1),
                )
                try:
                    with pytest.raises(FuturesTimeoutError):
                        future.result(timeout=0.3)

                    # 若后台结果路径先锁了节点，此 NOWAIT 会立即抛 LockNotAvailable；
                    # 正确实现应仍阻塞在 PlanRun，因此当前事务可以取得节点锁。
                    cursor.execute(
                        """
                        SELECT node_id
                        FROM plan_nodes
                        WHERE node_id = %(node_id)s::uuid
                        FOR UPDATE NOWAIT;
                        """,
                        {"node_id": claim.node_id},
                    )
                finally:
                    locking_connection.commit()
                completed = future.result(timeout=2)
    finally:
        locking_connection.close()

    assert completed.state is PlanNodeState.SUCCEEDED


def test_postgres_command_ledger_replays_json_array_payload() -> None:
    """命令 payload 是任意 JSON-safe 值，数组查询和重放不能被强制转成对象。"""
    settings = get_settings()
    _initialize_schema(settings)
    suffix = uuid4().hex
    store = _postgres_store(settings)
    run = store.create_or_resume(
        _materialized_plan(
            room_id=f"room-command-{suffix}",
            trace_id=f"trace-command-{suffix}",
        )
    )
    issued_at = datetime(2026, 7, 14, 9, 50, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="worker-command",
        now=issued_at - timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.WAITING_APPROVAL,
        output={"reason": "需要人工确认"},
        now=issued_at - timedelta(seconds=1),
    )
    command = PlanCommand(
        command_id=f"command-array-{suffix}",
        command_type=PlanCommandType.APPROVE,
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        node_id=claim.node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload=[{"operator_id": "operator-001"}],
        issued_at=issued_at,
    )
    service = CommandService(store)

    first = service.submit(command, now=issued_at + timedelta(seconds=1))
    replay = service.submit(command, now=issued_at + timedelta(minutes=20))
    ledger = store.get_command(command.command_id)

    assert first.accepted is True
    assert replay == first
    assert ledger.payload == [{"operator_id": "operator-001"}]
