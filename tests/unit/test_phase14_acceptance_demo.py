"""Phase 14 Task 12 三场景 Demo 与 Acceptance 的 RED 契约。

这些测试只验证对外可回放的阶段事实：同一直播会话贯穿三视图，可信售罄
保护与人工经营决定分离，记忆晋升经过规则和人工确认，正式模型证据不足时
保持 INCONCLUSIVE。Demo 不连接外部模型、平台 API 或数据库。
"""

from __future__ import annotations

from pathlib import Path

from scripts.run_phase14_human_support_demo import (
    DEMO_SESSION_ID,
    DemoStatus,
    render_demo_report,
    run_demo,
    write_acceptance_report,
)


def test_demo_replays_one_session_through_prepare_live_review() -> None:
    """播前、播中、播后必须共享一个可重放的 live_session_id。"""

    result = run_demo(Path("evaluation") / "phase14_human_support")

    assert result.status is DemoStatus.INCONCLUSIVE
    assert result.live_session_id == DEMO_SESSION_ID
    assert result.views == ("PREPARE", "LIVE", "REVIEW")
    assert result.replay_live_session_id == DEMO_SESSION_ID
    assert result.replay_stable is True
    assert result.route == "DECISION_SUPPORT"
    assert result.production_default_route == "DETERMINISTIC_ONLY"


def test_demo_keeps_automatic_protection_separate_from_operator_recovery() -> None:
    """自动保护可完成，但经营恢复必须留下结构化人工决定证据。"""

    result = run_demo(Path("evaluation") / "phase14_human_support")

    assert result.automatic_protection_status == "APPLIED"
    assert result.operator_decision_required is True
    assert result.operator_decision_kind == "MODIFY"
    assert result.operator_decision_evidence_ids
    assert result.execution_command_submitted is False
    assert "no_operator_decision_no_recovery" in result.safety_invariants


def test_demo_replays_governed_memory_and_honestly_reports_external_gap(tmp_path: Path) -> None:
    """双证据记忆闭环可回放，真实模型未运行时 Acceptance 不能伪装通过。"""

    result = run_demo(tmp_path)

    assert result.memory_promotion_status == "APPLIED"
    assert result.memory_replay_status == "APPLIED"
    assert result.formal_evaluation_status == "INCONCLUSIVE"
    assert result.real_model_cost_cny == "0.042344"
    report = render_demo_report(result)
    assert "AWAITING_PHASE_15_GATE" in report
    assert "REAL_MODEL_SMOKE_NOT_RUN" in report

    report_path = write_acceptance_report(tmp_path, result)
    assert report_path.exists()
    assert report_path.read_text(encoding="utf-8") == report
