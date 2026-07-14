"""Phase 12A PlanStore 数据库迁移顺序与 DDL 契约测试。

这些测试只读取迁移注册表和 SQL 文件，不连接数据库。真实并发语义由 integration
测试证明；本文件负责尽早拦截漏注册、表缺失、约束退化以及误耦合官方 checkpoint
私有表等结构性问题。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.run_db_migrations import MIGRATIONS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PHASE12A_SQL = PROJECT_ROOT / "docker" / "init_phase12a_plan_engine.sql"


def _normalized_sql() -> str:
    """以严格 UTF-8 读取并规范空白，避免断言依赖 SQL 的排版细节。"""
    return re.sub(r"\s+", " ", PHASE12A_SQL.read_text(encoding="utf-8")).lower()


def test_phase12a_migration_follows_phase11b_and_is_required() -> None:
    """PlanStore 依赖 Skill Attempt 事实，因此迁移必须紧跟 Phase 11B。"""
    phases = [step.phase for step in MIGRATIONS]

    assert phases.index("phase12a") == phases.index("phase11b") + 1
    phase12a = next(step for step in MIGRATIONS if step.phase == "phase12a")
    assert phase12a.sql_file == "init_phase12a_plan_engine.sql"
    assert phase12a.required is True


def test_phase12a_sql_defines_all_six_authoritative_tables() -> None:
    """DDL 必须显式建立 D-067 固定的六张权威关系表。"""
    sql = _normalized_sql()

    for table_name in (
        "plan_runs",
        "plan_versions",
        "plan_nodes",
        "plan_node_dependencies",
        "node_runs",
        "plan_commands",
    ):
        assert f"create table if not exists {table_name}" in sql


def test_phase12a_sql_keeps_identity_state_and_json_evidence_constraints() -> None:
    """并发身份、状态白名单与 JSONB 证据不能只依赖 Python 进程校验。"""
    sql = _normalized_sql()

    assert "unique (plan_run_id, version_number)" in sql
    assert "unique (plan_version_id, logical_key)" in sql
    assert "unique (node_id, attempt_number)" in sql
    assert "unique (node_id, claim_version)" in sql
    assert "planning_input jsonb not null" in sql
    assert "proposal jsonb not null" in sql
    assert "input_snapshot jsonb not null" in sql
    assert "command_id text primary key" in sql
    assert "check (state in" in sql


def test_phase12a_sql_persists_checkpoint_reconciliation_facts() -> None:
    """checkpoint 事故必须落在 PlanRun 权威行，不能只存在进程内日志。"""
    sql = _normalized_sql()

    for column in (
        "reconciliation_required boolean not null default false",
        "reconciliation_failure jsonb",
        "reconciliation_signature text",
        "reconciliation_attempt_count integer not null default 0",
        "last_reconciled_at timestamptz",
    ):
        assert column in sql

    assert "alter table plan_runs" in sql


def test_phase12a_sql_has_ready_lease_dependency_and_resource_indexes() -> None:
    """READY 调度、依赖推进、lease 回收与跨计划资源冲突都必须有索引入口。"""
    sql = _normalized_sql()

    for index_name in (
        "plan_nodes_ready_retry_idx",
        "plan_node_dependencies_dependency_idx",
        "node_runs_active_lease_idx",
        "node_runs_resource_keys_gin_idx",
    ):
        assert f"create index if not exists {index_name}" in sql


def test_phase12a_sql_does_not_reference_postgressaver_private_tables() -> None:
    """PlanStore 与官方 PostgresSaver 只共享实例，禁止通过私有表外键伪造原子性。"""
    sql = _normalized_sql()

    for private_table in (
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    ):
        assert f"references {private_table}" not in sql

    # Skill Attempt 只是可空审计关联，不以外键把两个独立事实存储绑定生命周期。
    assert "skill_attempt_id uuid" in sql
    assert "references skill_execution_attempts" not in sql
