"""Phase 12B PostgreSQL 协作式冻结、claim 竞态与 late result 集成测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from src.config.settings import get_settings
from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.event_store import (
    EventDelivery,
    EventInboxRecord,
    PostgresEventStore,
    initialize_event_store_schema,
)
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import (
    CandidatePlanProposal,
    CardBatchPlanningInput,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
import src.plan_engine.store as store_module
from src.plan_engine.store import MaterializedPlan, PlanStoreInvariantError
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


BASE_TIME = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)
TASK5_EVENT_PREFIX = "phase12b-impact-event-"


def _impact_module() -> Any:
    """延迟导入 Task 5 模块，使缺少实现形成明确红灯。"""
    import importlib

    return importlib.import_module("src.plan_engine.impact")


def _store(settings: Any) -> Any:
    """通过生产 PostgreSQL Store 验证跨连接事务语义。"""
    return store_module.PostgresPlanStore(settings)


def _initialize(settings: Any) -> None:
    """按迁移依赖顺序建立 Phase 12A 与 Phase 12B 结构。"""
    store_module.initialize_plan_engine_schema(settings)
    initialize_event_store_schema(settings)


def _quarantine_test_events(settings: Any) -> None:
    """把本文件遗留 Inbox 移出全局可 claim 集合，但保留 NodeRun 外键证据。

    Task 5 的 superseded NodeRun 必须永久引用原 event_id，因此不能像独立 Event Store
    测试那样直接删除 Inbox。这里仅把仍可被全局 Worker 领取的测试事件改成 APPLIED，
    避免失败重跑或后续测试误领；CONFLICT 本身不可 claim，继续保留原安全事实。
    """
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE plan_event_inbox
                SET state = 'APPLIED',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = GREATEST(updated_at, now())
                WHERE event_id LIKE %(prefix)s
                  AND state IN ('VERIFIED', 'PROCESSING');
                """,
                {"prefix": f"{TASK5_EVENT_PREFIX}%"},
            )
        connection.commit()


@pytest.fixture(autouse=True)
def _isolate_global_event_queue() -> Any:
    """每个测试前后隔离 Task 5 事件，防止污染 Event Inbox 的全局 claim 顺序。"""
    settings = get_settings()
    _initialize(settings)
    _quarantine_test_events(settings)
    yield
    _quarantine_test_events(settings)


def _product(product_id: str) -> CatalogProduct:
    """构造完整商品快照。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"商品 {product_id}",
        category="家居",
        price=Decimal("29.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["测试"],
        selling_points=["可复现"],
    )


def _plan(suffix: str) -> MaterializedPlan:
    """构造两商品手卡计划，足以区分受影响和未受影响分支。"""
    room_id = f"room-impact-{suffix}"
    trace_id = f"trace-impact-{suffix}"
    product_ids = ("p001", "p002")
    planning_input = CardBatchPlanningInput(
        room_id=room_id,
        trace_id=trace_id,
        live_plan=LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="PostgreSQL cooperative freeze",
                )
                for index, product_id in enumerate(product_ids, start=1)
            ],
        ),
        products_by_id={product_id: _product(product_id) for product_id in product_ids},
    )
    proposal = CandidatePlanProposal.model_validate(
        CanonicalCardBatchProposalProvider()
        .propose_sync(planning_input)
        .model_dump(mode="json")
    )
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
                (f"room:{room_id}:product:{product_id}",)
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
    settings: Any,
    room_id: str,
    *,
    conflict: bool = False,
) -> EventInboxRecord:
    """把售罄事实真实登记到 PostgreSQL，并返回权威 Inbox 视图。"""
    event_id = f"{TASK5_EVENT_PREFIX}{uuid4().hex}"
    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id=room_id,
        product_id="p001",
        observed_version=3,
        occurred_at=BASE_TIME,
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
            provenance_id=f"provenance-{event_id}",
            profile_id="impact-test-v1",
            transport="KAFKA",
            topic="inventory-facts",
            source=event.source,
            received_at=BASE_TIME,
            payload_digest=event.payload_digest,
    )
    event_store = PostgresEventStore(settings)
    registered = event_store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id=f"occurrence-{event_id}-accepted",
            transport="KAFKA",
            topic="inventory-facts",
            partition=int(uuid4().hex[:7], 16),
            offset=0,
            received_at=BASE_TIME,
        ),
    )
    if not conflict:
        return registered.inbox

    # 同一 event_id 的不同规范摘要必须由 Event Store 自己提升为 CONFLICT；直接构造
    # Pydantic 视图无法证明数据库中的原始 payload 和冲突 occurrence 都已保留。
    conflicting_event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id=room_id,
        product_id="p001",
        observed_version=4,
        occurred_at=BASE_TIME,
        source="inventory-service",
    )
    conflict_result = event_store.register_event(
        conflicting_event,
        VerifiedIngressProvenance(
            provenance_id=f"provenance-{event_id}-conflict",
            profile_id="impact-test-v1",
            transport="KAFKA",
            topic="inventory-facts",
            source=conflicting_event.source,
            received_at=BASE_TIME + timedelta(microseconds=1),
            payload_digest=conflicting_event.payload_digest,
        ),
        EventDelivery(
            occurrence_id=f"occurrence-{event_id}-conflict",
            transport="KAFKA",
            topic="inventory-facts",
            partition=int(uuid4().hex[:7], 16),
            offset=1,
            received_at=BASE_TIME + timedelta(microseconds=1),
        ),
    )
    return conflict_result.inbox


def _complete_prepare(store: Any, plan_run_id: str) -> None:
    """闭合 PREPARE，使商品节点可参与冻结/claim 竞态。"""
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run_id,
        worker_id=f"prepare-{uuid4().hex}",
        now=BASE_TIME,
        lease_seconds=120,
        limit=1,
    )[0]
    store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"prepared": ["p001", "p002"]},
        now=BASE_TIME + timedelta(seconds=1),
    )


def test_postgres_product_freeze_and_claim_race_never_leaves_affected_run_reusable() -> None:
    """claim 可先或后赢得计划锁，但受影响 p001 最终必须冻结或 superseded。"""
    settings = get_settings()
    _initialize(settings)
    store = _store(settings)
    run = store.create_or_resume(_plan(uuid4().hex))
    _complete_prepare(store, run.plan_run_id)
    inbox = _inbox(settings, run.room_id)
    analysis = _impact_module().ImpactAnalyzer().analyze(
        inbox=inbox,
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )
    barrier = Barrier(2)

    def freeze() -> Any:
        """与 claim 同时竞争 PlanRun 行锁。"""
        barrier.wait()
        return _store(settings).apply_impact_freeze(
            plan_run_id=run.plan_run_id,
            expected_plan_version=1,
            event_id=inbox.event.event_id,
            analysis=analysis,
            now=BASE_TIME + timedelta(seconds=2),
        )

    def claim() -> Any:
        """尝试一次领取全部 READY 商品节点。"""
        barrier.wait()
        return _store(settings).claim_ready_nodes(
            plan_run_id=run.plan_run_id,
            worker_id="race-worker",
            now=BASE_TIME + timedelta(seconds=2),
            lease_seconds=120,
            limit=4,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        freeze_future = pool.submit(freeze)
        claim_future = pool.submit(claim)
        freeze_future.result(timeout=5)
        claim_future.result(timeout=5)

    nodes = {node.logical_key: node for node in store.list_nodes(run.plan_run_id)}
    p001_runs = store.list_node_runs(run.plan_run_id, nodes["card:p001"].node_id)
    assert nodes["card:p001"].state in {
        PlanNodeState.FROZEN,
        PlanNodeState.RUNNING,
    }
    assert all(node_run.superseded for node_run in p001_runs)
    assert nodes["collect-card-results"].state is PlanNodeState.FROZEN
    assert store.get_plan_run(run.plan_run_id).state is PlanRunState.ACTIVE
    assert all(
        claim.node_id != nodes["card:p001"].node_id
        for claim in store.claim_ready_nodes(
            plan_run_id=run.plan_run_id,
            worker_id="post-freeze-worker",
            now=BASE_TIME + timedelta(seconds=3),
            lease_seconds=120,
            limit=4,
        )
    )


def test_postgres_late_result_persists_output_and_superseded_facts() -> None:
    """已 RUNNING 的受影响调用可以闭合，但数据库必须保留 superseded 关联。"""
    settings = get_settings()
    _initialize(settings)
    store = _store(settings)
    run = store.create_or_resume(_plan(uuid4().hex))
    _complete_prepare(store, run.plan_run_id)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="late-result-worker",
        now=BASE_TIME + timedelta(seconds=2),
        lease_seconds=120,
        limit=1,
    )[0]
    inbox = _inbox(settings, run.room_id)
    analysis = _impact_module().ImpactAnalyzer().analyze(
        inbox=inbox,
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )
    store.apply_impact_freeze(
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        event_id=inbox.event.event_id,
        analysis=analysis,
        now=BASE_TIME + timedelta(seconds=3),
    )

    completed = store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"card": "late result"},
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert completed.output == {"card": "late result"}
    assert completed.superseded is True
    assert completed.superseded_by_event_id == inbox.event.event_id
    assert completed.superseded_at == BASE_TIME + timedelta(seconds=3)
    with psycopg.connect(
        **settings.postgres_connection_kwargs,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT superseded, superseded_by_event_id, superseded_at, output
                FROM node_runs WHERE node_run_id = %(node_run_id)s::uuid;
                """,
                {"node_run_id": claim.node_run_id},
            )
            row = cursor.fetchone()
    assert row == {
        "superseded": True,
        "superseded_by_event_id": inbox.event.event_id,
        "superseded_at": BASE_TIME + timedelta(seconds=3),
        "output": {"card": "late result"},
    }


def test_postgres_room_freeze_keeps_plan_frozen_after_running_result() -> None:
    """整计划冻结与在途闭合使用同一 PlanRun-first 锁序，结果不能意外解冻。"""
    settings = get_settings()
    _initialize(settings)
    store = _store(settings)
    run = store.create_or_resume(_plan(uuid4().hex))
    _complete_prepare(store, run.plan_run_id)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="room-running-worker",
        now=BASE_TIME + timedelta(seconds=2),
        lease_seconds=120,
        limit=1,
    )[0]
    inbox = _inbox(settings, run.room_id, conflict=True)
    analysis = _impact_module().ImpactAnalyzer().analyze(
        inbox=inbox,
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )
    store.apply_impact_freeze(
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        event_id=inbox.event.event_id,
        analysis=analysis,
        now=BASE_TIME + timedelta(seconds=3),
    )
    completed = store.record_node_result(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"card": "room-frozen-late"},
        now=BASE_TIME + timedelta(seconds=4),
    )

    assert completed.superseded is True
    assert store.get_plan_run(run.plan_run_id).state is PlanRunState.FROZEN
    assert store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="room-blocked-worker",
        now=BASE_TIME + timedelta(seconds=5),
        lease_seconds=120,
        limit=4,
    ) == ()


def _postgres_frozen_product_claim(settings: Any) -> tuple[Any, Any]:
    """创建 PostgreSQL 中已被 PRODUCT 事件标记的最新 RUNNING NodeRun。"""
    store = _store(settings)
    run = store.create_or_resume(_plan(uuid4().hex))
    _complete_prepare(store, run.plan_run_id)
    claim = store.claim_ready_nodes(
        plan_run_id=run.plan_run_id,
        worker_id="postgres-superseded-worker",
        now=BASE_TIME + timedelta(seconds=2),
        lease_seconds=120,
        limit=1,
    )[0]
    inbox = _inbox(settings, run.room_id)
    analysis = _impact_module().ImpactAnalyzer().analyze(
        inbox=inbox,
        plan_run=store.get_plan_run(run.plan_run_id),
        nodes=store.list_nodes(run.plan_run_id),
    )
    store.apply_impact_freeze(
        plan_run_id=run.plan_run_id,
        expected_plan_version=1,
        event_id=inbox.event.event_id,
        analysis=analysis,
        now=BASE_TIME + timedelta(seconds=3),
    )
    return store, claim


def test_postgres_superseded_run_cannot_schedule_retry() -> None:
    """PostgreSQL 重试事务必须在写 RETRY_WAIT 前拒绝 superseded NodeRun。"""
    settings = get_settings()
    store, claim = _postgres_frozen_product_claim(settings)

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


def test_postgres_superseded_expired_run_cannot_be_reclaimed() -> None:
    """PostgreSQL 回收器不得把已废弃的过期 attempt 复制成新外部请求。"""
    settings = get_settings()
    store, claim = _postgres_frozen_product_claim(settings)

    with pytest.raises(PlanStoreInvariantError, match="superseded"):
        store.reclaim_expired_node(
            node_run_id=claim.node_run_id,
            worker_id="postgres-replacement-worker",
            now=BASE_TIME + timedelta(seconds=123),
            lease_seconds=120,
        )

    assert len(store.list_node_runs(claim.plan_run_id, claim.node_id)) == 1


def test_postgres_superseded_failure_keeps_product_plan_active() -> None:
    """数据库聚合不得让受影响旧 attempt 的失败阻断无关商品节点。"""
    settings = get_settings()
    store, claim = _postgres_frozen_product_claim(settings)

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
    assert store.claim_ready_nodes(
        plan_run_id=claim.plan_run_id,
        worker_id="postgres-unaffected-worker",
        now=BASE_TIME + timedelta(seconds=5),
        lease_seconds=120,
        limit=2,
    )
