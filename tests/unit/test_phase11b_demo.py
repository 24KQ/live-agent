"""Phase 11B 六场景无外部依赖 Demo 契约测试。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any


EXPECTED_SCENARIOS = [
    "setup_success",
    "sold_out",
    "rate_limited",
    "version_conflict",
    "deadline",
    "side_effect_unknown",
]


def test_demo_emits_six_fixed_scenarios(monkeypatch: Any) -> None:
    """内存 Demo 必须按固定顺序返回六种契约结果，且不得尝试连接 PostgreSQL。"""
    from src.skill_runtime import attempt_store

    def reject_postgres_connection(*args: Any, **kwargs: Any) -> None:
        """一旦 Demo 误触数据库连接就立即失败，避免本机服务掩盖外部依赖。"""
        raise AssertionError("Phase 11B demo must not connect to PostgreSQL")

    monkeypatch.setattr(attempt_store.psycopg, "connect", reject_postgres_connection)

    from scripts.run_phase11b_platform_contract_demo import run_demo_scenarios

    rows = run_demo_scenarios(emit=False)

    assert [row["scenario"] for row in rows] == EXPECTED_SCENARIOS
    assert [row["status"] for row in rows] == [
        "success",
        "success",
        "error",
        "error",
        "error",
        "error",
    ]
    assert [row["failure_category"] for row in rows] == [
        None,
        None,
        "RATE_LIMITED",
        "VERSION_CONFLICT",
        "TRANSIENT_INFRA",
        "SIDE_EFFECT_UNKNOWN",
    ]

    # 除名称、状态和失败分类外，还要逐场景核对决定其业务含义的关键事实。这样即使
    # 两个场景函数被错误接线，只要 Attempt、SideEffect 或平台状态不同就会立即失败。
    (
        setup_success,
        sold_out,
        rate_limited,
        version_conflict,
        deadline,
        side_effect_unknown,
    ) = rows

    assert setup_success["output"]["setup_status"] == "prepared"
    assert setup_success["output"]["allowed"] is True
    assert setup_success["attempt_state"] == "SUCCEEDED"
    assert setup_success["side_effect_state"] == "CONFIRMED"
    assert setup_success["platform_state"]["products"]["p001"] == {
        "product_id": "p001",
        "name": "主推商品",
        "price": "39.90",
        "inventory": 10,
        "version": 1,
        "is_active": True,
    }

    sold_out_product = sold_out["platform_state"]["products"]["p001"]
    assert sold_out_product["inventory"] == 0
    assert sold_out_product["is_active"] is False
    assert sold_out["attempt_state"] == "SUCCEEDED"
    assert sold_out["side_effect_state"] == "CONFIRMED"

    assert rate_limited["attempt_state"] == "FAILED"
    assert rate_limited["side_effect_state"] == "NOT_SENT"
    assert rate_limited["retry_after_seconds"] == 7
    assert rate_limited["platform_state"]["products"]["p001"]["price"] == "39.90"

    assert version_conflict["attempt_state"] == "FAILED"
    assert version_conflict["side_effect_state"] == "NOT_SENT"
    assert version_conflict["platform_state"]["products"]["p001"]["price"] == "39.90"

    assert deadline["attempt_state"] == "FAILED"
    assert deadline["side_effect_state"] == "NOT_SENT"
    assert deadline["platform_state"]["products"]["p001"]["price"] == "39.90"

    # 发送后未知必须同时证明副作用已经发生；价格和版本共同变化说明不能自动重试。
    unknown_product = side_effect_unknown["platform_state"]["products"]["p001"]
    assert side_effect_unknown["attempt_state"] == "SIDE_EFFECT_UNKNOWN"
    assert side_effect_unknown["side_effect_state"] == "UNKNOWN"
    assert unknown_product["price"] == "35.90"
    assert unknown_product["version"] == 2


def test_demo_script_prints_exactly_six_json_rows() -> None:
    """直接脚本入口应只打印六条可机器读取的场景摘要并以零退出。"""
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "run_phase11b_platform_contract_demo.py"

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
    assert len(rows) == 6


def test_run_all_phase11b_demo_only_delegates_to_new_script(monkeypatch: Any) -> None:
    """统一入口只委托新脚本，不探测数据库，也不改变其他命令实现。"""
    from scripts import run_all

    calls: list[tuple[str, tuple[str, ...]]] = []

    def record_run(script_name: str, *args: str) -> int:
        """记录委托参数并返回固定退出码，隔离真实子进程。"""
        calls.append((script_name, args))
        return 17

    monkeypatch.setattr(run_all, "_run_python", record_run)

    assert run_all.main(["phase11b-demo"]) == 17
    assert calls == [("run_phase11b_platform_contract_demo.py", ())]
