"""Phase 12B Event Store 与计划 lineage 的数据库迁移契约测试。

本文件只读取迁移注册表和 SQL，不连接数据库。它负责固定表、列、唯一约束、外键和
索引这些结构性不变量；跨连接并发、lease 与 fencing 由 integration 测试证明。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.run_db_migrations import MIGRATIONS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHASE12B_SQL = PROJECT_ROOT / "docker" / "init_phase12b_preemption.sql"


def _normalized_sql() -> str:
    """严格以 UTF-8 读取并压平空白，让断言不依赖 SQL 排版。"""
    assert PHASE12B_SQL.exists(), "尚未创建 Phase 12B 迁移文件"
    return re.sub(r"\s+", " ", PHASE12B_SQL.read_text(encoding="utf-8")).lower()


def test_phase12b_migration_is_required_and_follows_phase12a() -> None:
    """事件表依赖 Phase 12A PlanStore，因此必须紧跟其后且属于必需迁移。"""
    phases = [step.phase for step in MIGRATIONS]

    assert "phase12b" in phases
    assert phases.index("phase12b") == phases.index("phase12a") + 1
    phase12b = next(step for step in MIGRATIONS if step.phase == "phase12b")
    assert phase12b.sql_file == "init_phase12b_preemption.sql"
    assert phase12b.required is True


def test_phase12b_sql_defines_three_authoritative_event_tables() -> None:
    """Inbox、Occurrence 与 Application 必须是独立关系事实，不能塞入 Plan JSON。"""
    sql = _normalized_sql()

    for table_name in (
        "plan_event_inbox",
        "plan_event_occurrences",
        "plan_event_applications",
    ):
        assert f"create table if not exists {table_name}" in sql

    assert "event_id text primary key" in sql
    assert "payload_digest text not null" in sql
    assert "provenance jsonb not null" in sql
    assert "failure_fact jsonb" in sql


def test_phase12b_sql_enforces_delivery_application_and_lease_identity() -> None:
    """跨进程幂等、唯一应用和 lease 形状必须由数据库约束守住。"""
    sql = _normalized_sql()

    assert "occurrence_id text primary key" in sql
    assert "unique nulls not distinct (transport, topic, partition, transport_offset)" in sql
    assert "unique (event_id, root_plan_run_id)" in sql
    assert "fencing_token bigint not null default 0" in sql
    assert "lease_owner is not null and lease_expires_at is not null" in sql
    assert "lease_owner is null and lease_expires_at is null" in sql
    assert "references plan_event_inbox(event_id)" in sql
    assert "references plan_runs(plan_run_id)" in sql


def test_phase12b_sql_extends_plan_lineage_without_rewriting_phase12a_rows() -> None:
    """新增列必须有兼容默认值，旧 CARD_BATCH 行无需回填脚本即可继续读取。"""
    sql = _normalized_sql()

    assert "alter table plan_runs" in sql
    for column in (
        "plan_kind text not null default 'card_batch'",
        "priority integer not null default 0",
        "root_plan_run_id uuid",
        "parent_plan_run_id uuid",
        "trigger_event_id text",
    ):
        assert column in sql

    assert "alter table plan_versions" in sql
    assert "change_reason text not null default 'initial'" in sql
    assert "source_event_ids text[] not null default array[]::text[]" in sql
    assert "'emergency_sold_out'" in sql
    assert "add column if not exists ready_at timestamptz" in sql


def test_phase12b_sql_has_processing_and_lineage_query_indexes() -> None:
    """claim、root 聚合、事件 occurrence 与 child lineage 都要有稳定索引入口。"""
    sql = _normalized_sql()

    for index_name in (
        "plan_event_inbox_claim_idx",
        "plan_event_occurrences_event_idx",
        "plan_event_applications_root_idx",
        "plan_runs_root_priority_idx",
        "plan_runs_trigger_event_idx",
        "plan_nodes_global_ready_priority_idx",
    ):
        assert f"create index if not exists {index_name}" in sql

    assert "priority desc" in sql
    assert "ready_at" in sql


def test_phase12b_sql_does_not_reference_postgressaver_private_tables() -> None:
    """事件事实和 Plan lineage 不能通过官方 checkpoint 私表伪造事务一致性。"""
    sql = _normalized_sql()

    for private_table in (
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    ):
        assert f"references {private_table}" not in sql
