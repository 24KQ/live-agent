"""Phase 12B PostgreSQL root 级 Replan CAS 与不可变版本集成测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from src.config.settings import get_settings
from src.plan_engine.event_store import initialize_event_store_schema
from src.plan_engine.store import PostgresPlanStore, initialize_plan_engine_schema
from tests.integration.test_phase12a_plan_store_postgres import _materialized_plan


def test_two_postgres_replan_workers_create_only_one_next_version() -> None:
    """PlanRun 行锁与 expected version CAS 必须把并发请求收敛到同一版本 2。"""
    settings = get_settings()
    initialize_plan_engine_schema(settings)
    initialize_event_store_schema(settings)
    suffix = uuid4().hex
    plan = _materialized_plan(
        room_id=f"room-replan-{suffix}",
        trace_id=f"trace-replan-{suffix}",
    )
    root = PostgresPlanStore(settings).create_or_resume(plan)
    now = datetime(2026, 7, 15, 4, tzinfo=timezone.utc)

    def create() -> tuple[int, bool]:
        """每个线程使用独立连接，避免进程内对象掩盖数据库竞争。"""
        version, created = PostgresPlanStore(settings).create_replan_version(
            plan_run_id=root.plan_run_id,
            expected_plan_version=1,
            plan=plan,
            source_event_ids=(f"event-replan-{suffix}",),
            failure_signature="d" * 64,
            input_fingerprint=plan.planning_input.run_key,
            reused_from_by_logical_key={},
            now=now,
        )
        return version.version_number, created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: create(), range(2)))

    assert sorted(results) == [(2, False), (2, True)]
    store = PostgresPlanStore(settings)
    assert store.get_plan_run(root.plan_run_id).current_version == 2
    assert store.get_plan_version(root.plan_run_id, 2).source_event_ids == (
        f"event-replan-{suffix}",
    )
    assert all(
        store.list_node_runs(root.plan_run_id, node.node_id) == ()
        for node in store.list_nodes(root.plan_run_id, 2)
    )
