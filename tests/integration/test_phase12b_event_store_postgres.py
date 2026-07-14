"""Phase 12B PostgreSQL Event Store 的真实事务与并发契约测试。

测试只使用本地 PostgreSQL，并为每个事实生成随机身份，不依赖清库顺序。重点验证
进程内锁无法证明的跨连接语义：并发去重、冲突保真、SKIP LOCKED、lease/fencing
以及 event/root 唯一 Application。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from src.config.settings import get_settings
from src.plan_engine import event_store as event_store_module
from src.plan_engine.event_state_machine import (
    EventApplicationState,
    EventInboxState,
    EventOccurrenceKind,
)
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.store import PostgresPlanStore, initialize_plan_engine_schema


BASE_TIME = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
TASK3_EVENT_PREFIX = "phase12b-task3-event-"


def _initialize_schema(settings: Any) -> None:
    """先建立 Phase 12A 六表，再通过 Task 3 公开入口执行增量迁移。"""
    initialize_plan_engine_schema(settings)
    initializer = getattr(event_store_module, "initialize_event_store_schema", None)
    assert initializer is not None, "尚未实现 initialize_event_store_schema"
    initializer(settings)
    # claim_next 是全局队列。只删除本文件专用前缀的历史行，使失败后重跑不受旧
    # VERIFIED 事实影响；其他测试、开发数据和 Phase 12A 计划一律不触碰。
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            parameters = {"prefix": f"{TASK3_EVENT_PREFIX}%"}
            cursor.execute(
                "DELETE FROM plan_event_applications WHERE event_id LIKE %(prefix)s;",
                parameters,
            )
            cursor.execute(
                "DELETE FROM plan_event_occurrences WHERE event_id LIKE %(prefix)s;",
                parameters,
            )
            cursor.execute(
                "DELETE FROM plan_event_inbox WHERE event_id LIKE %(prefix)s;",
                parameters,
            )
        connection.commit()


def _postgres_store(settings: Any) -> Any:
    """延迟取得生产 Store，让未实现能力表现为可读红灯而非收集错误。"""
    store_type = getattr(event_store_module, "PostgresEventStore", None)
    assert store_type is not None, "尚未实现 PostgresEventStore"
    return store_type(settings)


def _event(
    event_id: str,
    *,
    observed_version: int = 3,
    product_id: str = "product-001",
) -> InventoryFactEvent:
    """构造摘要闭合且不依赖 Kafka 的售罄事实。"""
    return InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id="room-phase12b-postgres",
        product_id=product_id,
        observed_version=observed_version,
        occurred_at=BASE_TIME,
        source="inventory-service",
    )


def _provenance(event: InventoryFactEvent, *, received_at: datetime = BASE_TIME) -> VerifiedIngressProvenance:
    """构造与事件摘要、来源和传输坐标闭合的已验证 provenance。"""
    return VerifiedIngressProvenance(
        provenance_id=f"provenance-{uuid4().hex}",
        profile_id="inventory-trust-v1",
        transport="KAFKA",
        topic="inventory-facts",
        source=event.source,
        received_at=received_at,
        payload_digest=event.payload_digest,
    )


def _delivery(index: int, *, received_at: datetime | None = None) -> Any:
    """为并发投递生成互不冲突的 Kafka 坐标。"""
    return event_store_module.EventDelivery(
        occurrence_id=f"occurrence-{uuid4().hex}-{index}",
        transport="KAFKA",
        topic="inventory-facts",
        # 集成数据库不会在每次测试前清空；随机 partition 让同一测试反复执行时仍使用
        # 全新传输坐标，而 exact replay 场景会复用同一个 EventDelivery 对象。
        partition=int(uuid4().hex[:7], 16),
        offset=index,
        received_at=received_at or BASE_TIME + timedelta(seconds=index),
    )


def _insert_phase12a_shaped_root(settings: Any) -> tuple[str, int]:
    """省略全部 Phase 12B 新列写入旧形状行，验证迁移默认值与 lineage 外键。"""
    plan_run_id = str(uuid4())
    plan_version_id = str(uuid4())
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO plan_runs (
                    plan_run_id, room_id, trace_id, run_key, plan_digest,
                    current_version, execution_route, state, planning_input
                ) VALUES (
                    %(plan_run_id)s::uuid, %(room_id)s, %(trace_id)s, %(run_key)s,
                    %(plan_digest)s, 1, 'PLAN_ENGINE', 'ACTIVE', '{}'::jsonb
                );
                """,
                {
                    "plan_run_id": plan_run_id,
                    "room_id": f"room-{plan_run_id}",
                    "trace_id": f"trace-{plan_run_id}",
                    "run_key": f"run-{plan_run_id}",
                    "plan_digest": "a" * 64,
                },
            )
            cursor.execute(
                """
                INSERT INTO plan_versions (
                    plan_version_id, plan_run_id, version_number,
                    provider_id, provider_version, proposal
                ) VALUES (
                    %(plan_version_id)s::uuid, %(plan_run_id)s::uuid, 1,
                    'phase12b-test-provider', '1.0.0', '{}'::jsonb
                );
                """,
                {
                    "plan_version_id": plan_version_id,
                    "plan_run_id": plan_run_id,
                },
            )
        connection.commit()
    return plan_run_id, 1


def test_phase12b_migration_keeps_phase12a_insert_shape_and_defaults() -> None:
    """旧代码省略 lineage 列时仍能写入，并得到 CARD_BATCH 的安全默认事实。"""
    settings = get_settings()
    _initialize_schema(settings)
    plan_run_id, version = _insert_phase12a_shaped_root(settings)

    with psycopg.connect(
        **settings.postgres_connection_kwargs,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT plan_kind, priority, root_plan_run_id, parent_plan_run_id,
                       trigger_event_id
                FROM plan_runs WHERE plan_run_id = %(plan_run_id)s::uuid;
                """,
                {"plan_run_id": plan_run_id},
            )
            run_row = cursor.fetchone()
            cursor.execute(
                """
                SELECT change_reason, source_event_ids
                FROM plan_versions
                WHERE plan_run_id = %(plan_run_id)s::uuid
                  AND version_number = %(version)s;
                """,
                {"plan_run_id": plan_run_id, "version": version},
            )
            version_row = cursor.fetchone()

    assert run_row == {
        "plan_kind": "CARD_BATCH",
        "priority": 0,
        "root_plan_run_id": None,
        "parent_plan_run_id": None,
        "trigger_event_id": None,
    }
    assert version_row == {"change_reason": "INITIAL", "source_event_ids": []}

    # 物理列不能只供临时 SQL 查询；PlanStore 的公开冻结视图也必须携带 lineage，
    # 后续紧急计划调度和 Replan 才不会绕过权威 Store 自行读表。
    run_view = PostgresPlanStore(settings).get_plan_run(plan_run_id)
    version_view = PostgresPlanStore(settings).get_plan_version(plan_run_id, version)
    assert run_view.plan_kind == "CARD_BATCH"
    assert run_view.priority == 0
    assert run_view.root_plan_run_id is None
    assert run_view.parent_plan_run_id is None
    assert run_view.trigger_event_id is None
    assert version_view.change_reason == "INITIAL"
    assert version_view.source_event_ids == ()


def test_postgres_concurrent_duplicate_registration_has_one_accepted_fact() -> None:
    """八个独立连接并发登记时，只能有一个首次事实，其余均形成 DUPLICATE。"""
    settings = get_settings()
    _initialize_schema(settings)
    event = _event(f"{TASK3_EVENT_PREFIX}{uuid4().hex}")

    def register(index: int) -> Any:
        """每个线程使用独立 Store/连接，避免进程内对象掩盖数据库竞态。"""
        received_at = BASE_TIME + timedelta(seconds=index)
        return _postgres_store(settings).register_event(
            event,
            _provenance(event, received_at=received_at),
            _delivery(index, received_at=received_at),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(1, 9)))

    classifications = [result.occurrence.classification for result in results]
    assert classifications.count(EventOccurrenceKind.ACCEPTED) == 1
    assert classifications.count(EventOccurrenceKind.DUPLICATE) == 7
    store = _postgres_store(settings)
    assert len(store.list_occurrences(event.event_id)) == 8
    assert store.get_inbox(event.event_id).event == event


def test_postgres_exact_delivery_replay_and_digest_conflict_preserve_first_fact() -> None:
    """崩溃重放不追加 occurrence；同 ID 不同摘要追加冲突但不覆盖首次 payload。"""
    settings = get_settings()
    _initialize_schema(settings)
    store = _postgres_store(settings)
    event_id = f"{TASK3_EVENT_PREFIX}{uuid4().hex}"
    first_event = _event(event_id)
    delivery = _delivery(10)
    provenance = _provenance(first_event, received_at=delivery.received_at)

    first = store.register_event(first_event, provenance, delivery)
    replay = _postgres_store(settings).register_event(first_event, provenance, delivery)
    conflicting = _event(event_id, observed_version=4)
    conflict_delivery = _delivery(11)
    conflict = store.register_event(
        conflicting,
        _provenance(conflicting, received_at=conflict_delivery.received_at),
        conflict_delivery,
    )

    assert replay.created is False
    assert replay.occurrence == first.occurrence
    assert conflict.occurrence.classification is EventOccurrenceKind.CONFLICT
    assert conflict.inbox.state is EventInboxState.CONFLICT
    assert conflict.inbox.event == first_event
    assert len(store.list_occurrences(event_id)) == 2


def test_postgres_claim_lease_and_fencing_reject_late_worker() -> None:
    """跨 Store claim 只能有一个胜者，过期 Worker 不得晚到提交，新 claim 递增 token。"""
    settings = get_settings()
    _initialize_schema(settings)
    event = _event(f"{TASK3_EVENT_PREFIX}{uuid4().hex}")
    delivery = _delivery(20)
    _postgres_store(settings).register_event(
        event,
        _provenance(event, received_at=delivery.received_at),
        delivery,
    )
    claimed_at = delivery.received_at + timedelta(seconds=1)

    def claim(worker_id: str) -> Any:
        """独立连接竞争同一条 VERIFIED Inbox。"""
        return _postgres_store(settings).claim_next(
            worker_id,
            now=claimed_at,
            lease_seconds=10,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    winners = [claim_result for claim_result in claims if claim_result is not None]
    assert len(winners) == 1
    first = winners[0]

    with pytest.raises(event_store_module.EventLeaseError):
        _postgres_store(settings).transition_inbox(
            event.event_id,
            expected_state=EventInboxState.PROCESSING,
            target_state=EventInboxState.APPLIED,
            now=claimed_at + timedelta(seconds=11),
            worker_id=first.record.lease_owner,
            fencing_token=first.fencing_token,
        )

    second = _postgres_store(settings).claim_next(
        "worker-c",
        now=claimed_at + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    applied = _postgres_store(settings).transition_inbox(
        event.event_id,
        expected_state=EventInboxState.PROCESSING,
        target_state=EventInboxState.APPLIED,
        now=claimed_at + timedelta(seconds=12),
        worker_id="worker-c",
        fencing_token=second.fencing_token,
    )
    assert applied.state is EventInboxState.APPLIED


def test_postgres_heartbeat_persists_monotonic_lease_across_store_instances() -> None:
    """heartbeat 必须跨连接延长 lease，回拨时间不能缩短租约或更新时间。"""
    settings = get_settings()
    _initialize_schema(settings)
    event = _event(f"{TASK3_EVENT_PREFIX}{uuid4().hex}")
    delivery = _delivery(25)
    _postgres_store(settings).register_event(
        event,
        _provenance(event, received_at=delivery.received_at),
        delivery,
    )
    claimed_at = delivery.received_at + timedelta(seconds=1)
    claim = _postgres_store(settings).claim_next(
        "worker-heartbeat",
        now=claimed_at,
        lease_seconds=10,
    )
    assert claim is not None

    extended = _postgres_store(settings).heartbeat(
        event.event_id,
        worker_id="worker-heartbeat",
        fencing_token=claim.fencing_token,
        now=claimed_at + timedelta(seconds=5),
        lease_seconds=20,
    )
    backdated = _postgres_store(settings).heartbeat(
        event.event_id,
        worker_id="worker-heartbeat",
        fencing_token=claim.fencing_token,
        now=claimed_at + timedelta(seconds=4),
        lease_seconds=5,
    )

    assert extended.lease_expires_at == claimed_at + timedelta(seconds=25)
    assert backdated.lease_expires_at == extended.lease_expires_at
    assert backdated.updated_at == extended.updated_at
    with pytest.raises(event_store_module.EventLeaseError):
        _postgres_store(settings).heartbeat(
            event.event_id,
            worker_id="worker-other",
            fencing_token=claim.fencing_token,
            now=claimed_at + timedelta(seconds=6),
            lease_seconds=20,
        )


def test_postgres_application_is_unique_per_event_and_root_across_connections() -> None:
    """并发相同意图复用一个 Application，不同 source version 必须 fail-closed。"""
    settings = get_settings()
    _initialize_schema(settings)
    root_plan_run_id, source_version = _insert_phase12a_shaped_root(settings)
    event = _event(f"{TASK3_EVENT_PREFIX}{uuid4().hex}")
    delivery = _delivery(30)
    _postgres_store(settings).register_event(
        event,
        _provenance(event, received_at=delivery.received_at),
        delivery,
    )
    now = delivery.received_at + timedelta(seconds=1)

    def create_application(_: int) -> Any:
        """每个线程从独立事务创建相同 event/root 意图。"""
        return _postgres_store(settings).create_application(
            event.event_id,
            root_plan_run_id=root_plan_run_id,
            source_plan_version=source_version,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create_application, range(2)))

    assert sum(result.created for result in results) == 1
    assert results[0].application == results[1].application
    assert len(
        _postgres_store(settings).list_applications(
            root_plan_run_id=root_plan_run_id
        )
    ) == 1
    with pytest.raises(event_store_module.EventStoreInvariantError, match="source_plan_version"):
        _postgres_store(settings).create_application(
            event.event_id,
            root_plan_run_id=root_plan_run_id,
            source_plan_version=source_version + 1,
            now=now + timedelta(seconds=1),
        )

    freezing = _postgres_store(settings).transition_application(
        event.event_id,
        root_plan_run_id,
        expected_state=EventApplicationState.PENDING,
        target_state=EventApplicationState.FREEZING,
        now=now + timedelta(seconds=2),
        impact_analysis={"scope": "PRODUCT", "affected_node_ids": []},
    )
    assert freezing.state is EventApplicationState.FREEZING
    reloaded = _postgres_store(settings).get_application(
        event.event_id,
        root_plan_run_id,
    )
    assert reloaded == freezing
    with pytest.raises(
        event_store_module.EventStoreInvariantError,
        match="impact_analysis",
    ):
        _postgres_store(settings).transition_application(
            event.event_id,
            root_plan_run_id,
            expected_state=EventApplicationState.FREEZING,
            target_state=EventApplicationState.EMERGENCY_RUNNING,
            now=now + timedelta(seconds=3),
            impact_analysis={"scope": "ROOM", "affected_node_ids": []},
        )
