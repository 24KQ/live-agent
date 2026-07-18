"""Phase 15 Task 12 三场景 Demo、双次 Release 和最终 Acceptance 的契约。

测试只使用固定的内存业务闭环与本地确定性 Release Runner，不连接真实模型、
GitHub Actions、淘宝 API 或生产数据库；外部证据不足必须保留为 INCONCLUSIVE。
"""

from __future__ import annotations

from pathlib import Path

from scripts.run_phase15_release_demo import (
    Phase15AcceptanceStatus,
    render_acceptance_report,
    run_demo,
    write_acceptance_reports,
)


def test_phase15_demo_contains_three_views_business_loop_and_two_release_profiles() -> None:
    """一个 Phase 15 Demo 必须同时携带三场景闭环和两次路由 Release 摘要。"""

    result = run_demo(Path("evaluation"))

    assert result.status is Phase15AcceptanceStatus.INCONCLUSIVE
    assert result.frozen_case_count == 48
    assert result.business_loop.views == ("PREPARE", "LIVE", "REVIEW")
    assert result.business_loop.automatic_protection_status == "APPLIED"
    assert result.business_loop.operator_decision_kind == "MODIFY"
    assert result.business_loop.execution_command_submitted is False
    assert result.business_loop.memory_promotion_status == "APPLIED"
    assert result.business_loop.memory_replay_status == "APPLIED"
    assert len(result.release_runs) == 2
    assert result.release_runs[0]["route_profile"]["mode"] == "EXPLICIT_RELEASE"
    assert result.release_runs[1]["route_profile"]["mode"] == "VERIFIED_DEFAULTS"
    assert result.release_runs[1]["route_profile"]["decision_support_policy"]["route"] == "DETERMINISTIC_ONLY"


def test_phase15_demo_keeps_external_evidence_blocked_and_reports_no_new_model_cost() -> None:
    """没有真实模型、真人和托管 CI 时，最终报告不得冒充可发布或已晋升。"""

    result = run_demo(Path("evaluation"))

    assert result.external_evidence_status == "BLOCKED"
    assert result.promotion_status == "BLOCKED"
    assert result.final_route == "DETERMINISTIC_ONLY"
    assert result.phase15_model_cost_cny == "0.000000"
    assert "REAL_MODEL_SMOKE_NOT_RUN" in result.blocker_codes
    assert "GITHUB_ACTIONS_EVIDENCE_MISSING" in result.blocker_codes
    report = render_acceptance_report(result)
    assert "PHASE_15_COMPLETE_INCONCLUSIVE" in report
    assert "Frozen Golden cases: `48`" in report
    assert "INCONCLUSIVE" in report
    assert "no automatic next phase" in report


def test_phase15_demo_writes_phase_and_final_acceptance_reports(tmp_path: Path) -> None:
    """阶段报告和总验收报告使用同一稳定事实源并可重复生成。"""

    result = run_demo(Path("evaluation"))
    phase_report, final_report = write_acceptance_reports(tmp_path, result)

    assert phase_report.exists()
    assert final_report.exists()
    assert phase_report.read_text(encoding="utf-8") == render_acceptance_report(result)
    assert "Agent Runtime Final Acceptance" in final_report.read_text(encoding="utf-8")
