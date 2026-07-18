"""Phase 15 Task 7 PromotionDecision 与双轨报告的 TDD 契约。"""

from __future__ import annotations

from decimal import Decimal

from src.release_gates.copilot_smoke import CopilotSmokeReport, CopilotSmokeStatus
from src.release_gates.decisions import (
    DecisionSupportPromotionDecision,
    FinalReleaseStatus,
    PromotionStatus,
    TechnicalReleaseDecision,
    TechnicalReleaseStatus,
)
from src.release_gates.human_study import StudyEvidence, StudyEvidenceStatus
from src.release_gates.report import (
    build_promotion_decision,
    build_release_report,
    render_release_report_json,
    render_release_report_markdown,
)


def _model_report(*, status: CopilotSmokeStatus = CopilotSmokeStatus.PASS, eligible: bool = True) -> CopilotSmokeReport:
    """构造 10 例、无违规的模型证据快照。"""

    return CopilotSmokeReport(
        status=status,
        promotion_eligible=eligible,
        model_call_count=10,
        settled_cost_cny=Decimal("0.12"),
        unknown_usage_count=0,
        fallback_count=0,
        schema_error_count=0,
        severe_violation_count=0,
        duplicate_request_count=0,
    )


def _human_evidence(status: StudyEvidenceStatus = StudyEvidenceStatus.READY) -> StudyEvidence:
    """构造脱敏真人证据；status 由测试显式控制。"""

    return StudyEvidence(
        status=status,
        reason_codes=() if status is StudyEvidenceStatus.READY else ("MISSING_RESPONSES",),
        study_id="phase15-human-study-v1",
        dataset_manifest_digest="a" * 64,
        promotion_artifact_digest="b" * 64,
        participant_count=3,
        response_count=24,
        evidence_digest="c" * 64,
    )


def _technical(status: TechnicalReleaseStatus = TechnicalReleaseStatus.PASS) -> TechnicalReleaseDecision:
    """构造技术门禁快照，报告层不得自行改写计数。"""

    return TechnicalReleaseDecision(
        release_run_id="release-task7-001",
        status=status,
        expected_case_count=48,
        completed_case_count=48 if status is TechnicalReleaseStatus.PASS else 1,
        passed_case_count=48 if status is TechnicalReleaseStatus.PASS else 0,
        failed_case_count=0 if status is TechnicalReleaseStatus.PASS else 1,
        blocked_case_count=0,
        severe_violation_count=0,
        case_results_digest="d" * 64,
        reason_codes=() if status is TechnicalReleaseStatus.PASS else ("CASE_FAILED",),
    )


def test_missing_model_or_human_evidence_is_blocked() -> None:
    """缺任一外部证据时不得生成 KEEP_DISABLED 或 PROMOTE。"""

    decision = build_promotion_decision(model_report=None, human_evidence=None)

    assert decision.status is PromotionStatus.BLOCKED
    assert "MODEL_EVIDENCE_MISSING" in decision.reason_codes
    assert "HUMAN_EVIDENCE_MISSING" in decision.reason_codes


def test_complete_but_failed_quality_gate_keeps_decision_support_disabled() -> None:
    """证据完整但安全、冲突或耗时指标失败时只能 KEEP_DISABLED。"""

    decision = build_promotion_decision(
        model_report=_model_report(),
        human_evidence=_human_evidence(),
        safety_correctness=Decimal("0.90"),
        key_conflict_miss_rate_reduction=Decimal("0.10"),
        decision_median_reduction=Decimal("0.20"),
    )

    assert decision.status is PromotionStatus.KEEP_DISABLED
    assert decision.human_evidence_complete is True
    assert decision.model_evidence_complete is True


def test_strict_and_gate_promotes_only_when_all_metrics_pass() -> None:
    """10/10、严重违规 0 和三项严格指标同时满足才 PROMOTE。"""

    decision = build_promotion_decision(
        model_report=_model_report(),
        human_evidence=_human_evidence(),
        safety_correctness=Decimal("0.91"),
        key_conflict_miss_rate_reduction=Decimal("0.30"),
        decision_median_reduction=Decimal("0.20"),
    )

    assert decision.status is PromotionStatus.PROMOTE
    assert decision.completed_smoke_cases == 10


def test_report_json_markdown_are_stable_and_final_status_is_deterministic() -> None:
    """技术 PASS + Promotion BLOCKED 必须生成 disabled final，而非启用状态。"""

    report = build_release_report(
        technical=_technical(),
        promotion=DecisionSupportPromotionDecision.blocked("REAL_MODEL_EVIDENCE_MISSING"),
    )
    first_json = render_release_report_json(report)
    second_json = render_release_report_json(report)
    markdown = render_release_report_markdown(report)

    assert report.final.status is FinalReleaseStatus.RELEASED_DECISION_SUPPORT_DISABLED
    assert first_json == second_json
    assert '"promotion_status":"BLOCKED"' in first_json
    assert "RELEASED_DECISION_SUPPORT_DISABLED" in markdown
    assert report.report_digest


def test_technical_failure_always_wins_over_promotion() -> None:
    """即使输入 Promotion 为 PROMOTE，技术 FAIL 仍只能 NOT_RELEASED。"""

    report = build_release_report(
        technical=_technical(TechnicalReleaseStatus.FAIL),
        promotion=build_promotion_decision(
            model_report=_model_report(),
            human_evidence=_human_evidence(),
            safety_correctness=Decimal("0.95"),
            key_conflict_miss_rate_reduction=Decimal("0.35"),
            decision_median_reduction=Decimal("0.25"),
        ),
    )

    assert report.final.status is FinalReleaseStatus.NOT_RELEASED
