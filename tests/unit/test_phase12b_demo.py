"""Phase 12B Task 11 业务闭环 Demo 的可重复性与安全边界测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_phase12b_preemption_demo.py"


def test_business_loop_demo_writes_trace_and_report(tmp_path: Path) -> None:
    """固定场景必须输出机器 Trace 和人工可读报告。"""

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--scenario",
            "live-session-p001-sold-out-v1",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    trace = json.loads((tmp_path / "business-loop-trace.json").read_text(encoding="utf-8"))
    report = (tmp_path / "business-loop-report.md").read_text(encoding="utf-8")
    assert trace["scenario"] == "live-session-p001-sold-out-v1"
    assert trace["status"] == "APPLIED"
    assert trace["event"]["inbox_state"] == "APPLIED"
    assert trace["event"]["offset_commit_after_store"] is True
    assert trace["replan"]["plan_version"] == 2
    assert trace["replan"]["reused_products"] == ["p002", "p003"]
    assert trace["evidence"]["application_state"] == "APPLIED"
    assert trace["external_writes"] == 1
    assert "Phase 12B 业务闭环" in report
    assert "不声称真实 GMV" in report


def test_business_loop_demo_is_byte_stable(tmp_path: Path) -> None:
    """同一隔离 Fixture 重复执行时，规范化 Trace 必须字节一致。"""

    first = tmp_path / "first"
    second = tmp_path / "second"
    for output_dir in (first, second):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--scenario",
                "live-session-p001-sold-out-v1",
                "--output-dir",
                str(output_dir),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    assert (first / "business-loop-trace.json").read_bytes() == (
        second / "business-loop-trace.json"
    ).read_bytes()


def test_default_demo_prints_eight_acceptance_scenarios() -> None:
    """无参数入口必须按冻结顺序展示八类抢占与恢复证据。"""

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [row["scenario"] for row in rows] == [
        "trusted_sold_out_replan",
        "kafka_duplicate_idempotency",
        "event_digest_conflict",
        "late_result_superseded",
        "side_effect_unknown_confirmed",
        "reconciliation_waiting_human",
        "multi_event_merge_reuse",
        "replan_budget_exhausted",
    ]
    assert all(row["verified"] is True for row in rows)
