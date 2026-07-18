"""Phase 16 Task 9 配对评估的 PostgreSQL 重放证据。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.multi_agent_evaluation import (
    generate_phase16_controlled_multi_agent_dataset,
    load_phase16_controlled_multi_agent_dataset,
    run_phase16_scripted_evaluation,
)
from src.decision_support.store import PostgresDecisionSupportStore


@pytest.fixture()
def postgres_phase16_store_factory():
    """创建独立 schema；评估绝不连接开发库默认 public schema。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_evaluation_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {};").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    PostgresDecisionSupportStore(settings).initialize_schema()
    try:
        # 每次返回新的 Store 对象但复用同一隔离 schema，模拟进程重启后以持久事实恢复。
        yield lambda: PostgresDecisionSupportStore(settings)
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE;").format(sql.Identifier(schema_name)))
            connection.commit()


def test_phase16_scripted_evaluation_replays_full_lineage_after_postgres_restart(
    tmp_path: Path,
    postgres_phase16_store_factory,
) -> None:
    """真实 PostgreSQL 必须保留 READY/DEGRADED 父链，重放不得增加模型发送。"""

    root = tmp_path / "phase16-dataset"
    generate_phase16_controlled_multi_agent_dataset(root)
    dataset = load_phase16_controlled_multi_agent_dataset(root)

    report = run_phase16_scripted_evaluation(
        dataset,
        store_factory=postgres_phase16_store_factory,
        restart_store_factory=postgres_phase16_store_factory,
    )

    assert report.total_cases == 48
    assert report.ready_outcomes == 24
    assert report.degraded_outcomes == 6
    assert report.replay_identity_correct_cases == 48
    assert report.analyst_calls == 30
    assert report.planner_calls == 26
    assert report.lineage_identity_correct_cases == 48
