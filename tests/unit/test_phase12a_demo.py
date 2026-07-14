"""Phase 12A DAG PlanEngine 五场景无外部依赖 Demo 契约测试。"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest


EXPECTED_SCENARIOS = [
    "three_cards_parallel",
    "rate_limited_retry",
    "unrecoverable_failure",
    "planstore_ahead_recovery",
    "duplicate_command",
]


def _demo_module() -> Any:
    """延迟导入待实现脚本，使 RED 以清晰断言呈现。"""
    try:
        return importlib.import_module("scripts.run_phase12a_dag_plan_engine_demo")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Phase 12A DAG PlanEngine Demo", pytrace=False)


def test_demo_emits_five_isolated_runtime_scenarios(monkeypatch: Any) -> None:
    """五个场景必须真实使用内存 Runtime，且不得尝试连接 PostgreSQL。"""
    from src.plan_engine import store as store_module

    def reject_postgres_connection(*args: Any, **kwargs: Any) -> None:
        """任何数据库探测都违反无外部依赖 Demo 契约。"""
        raise AssertionError("Phase 12A demo must not connect to PostgreSQL")

    monkeypatch.setattr(store_module.psycopg, "connect", reject_postgres_connection)
    rows = _demo_module().run_demo_scenarios(emit=False)

    assert [row["scenario"] for row in rows] == EXPECTED_SCENARIOS
    assert [row["plan_status"] for row in rows[:4]] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
    ]

    parallel, retry, failed, recovered, command = rows
    assert parallel["card_count"] == 3
    assert parallel["skill_calls"] == 3
    assert parallel["parallel_claim_count"] == 3

    assert retry["retry_wait_observed"] is True
    assert retry["skill_calls"] == 2
    assert retry["attempt_count"] == 2
    assert retry["retry_after_seconds"] == 7

    assert failed["skill_calls"] == 3
    assert failed["succeeded_card_nodes"] == 2
    assert failed["failed_card_nodes"] == 1
    assert failed["collect_runs"] == 0

    assert recovered["reconciliation"] == "REPLAY_REUSE"
    assert recovered["skill_calls_before_restart"] == 3
    assert recovered["skill_calls_after_restart"] == 0
    assert recovered["card_count"] == 3

    assert command["first_accepted"] is True
    assert command["second_accepted"] is True
    assert command["replayed"] is True
    assert command["ledger_reason"] == "ACCEPTED"
    assert command["resulting_node_status"] == "READY"


def test_demo_scenarios_do_not_share_plan_or_command_identity() -> None:
    """每个场景必须重新装配 Store，随机内部 ID 不得在场景间形成共享状态。"""
    rows = _demo_module().run_demo_scenarios(emit=False)

    plan_ids = [row["plan_run_id"] for row in rows if row.get("plan_run_id")]
    assert len(plan_ids) == len(set(plan_ids))
    assert rows[-1]["command_id"] == "phase12a-demo-approve"
    assert all(row["external_dependencies"] == [] for row in rows)


def test_demo_script_prints_exactly_five_json_rows() -> None:
    """直接脚本只能输出五条机器可读 JSON，不能夹带统一入口日志。"""
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "run_phase12a_dag_plan_engine_demo.py"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [row["scenario"] for row in rows] == EXPECTED_SCENARIOS
    assert len(rows) == 5


def test_run_all_phase12a_demo_only_delegates_to_new_script(monkeypatch: Any) -> None:
    """统一入口只委托内存脚本，不探测数据库或改变其他命令。"""
    from scripts import run_all

    calls: list[tuple[str, tuple[str, ...]]] = []

    def record_run(script_name: str, *args: str) -> int:
        """记录唯一子进程委托并返回可观察退出码。"""
        calls.append((script_name, args))
        return 23

    monkeypatch.setattr(run_all, "_run_python", record_run)

    assert run_all.main(["phase12a-demo"]) == 23
    assert calls == [("run_phase12a_dag_plan_engine_demo.py", ())]
