"""Phase 15 Task 4 Release Store、双轨结论和预算隔离的 TDD 契约。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.release_gates.decisions import (
    DecisionSupportPromotionDecision,
    FinalReleaseStatus,
    PromotionStatus,
    ReleaseCaseResult,
    ReleaseMode,
    ReleaseRun,
    TechnicalReleaseStatus,
)
from src.release_gates.budget import Phase15BudgetLimitExceeded, Phase15BudgetStore
from src.release_gates.store import ReleaseInvariantError, InMemoryReleaseStore
from src.release_gates.models import EvaluationCaseStatus


MANIFEST_DIGEST = "b" * 64


def _run(run_id: str = "release-run-001", cases: tuple[str, ...] = ("case-1", "case-2")) -> ReleaseRun:
    """构造绑定 Manifest 和预期 case 集合的 ReleaseRun。"""

    return ReleaseRun(
        release_run_id=run_id,
        mode=ReleaseMode.PR,
        manifest_digest=MANIFEST_DIGEST,
        expected_case_ids=cases,
    )


def _result(
    run: ReleaseRun,
    case_id: str,
    *,
    status: EvaluationCaseStatus = EvaluationCaseStatus.PASS,
    severe_violation: bool = False,
) -> ReleaseCaseResult:
    """构造绑定 run/Manifest 的最小 case 结果。"""

    return ReleaseCaseResult(
        release_run_id=run.release_run_id,
        manifest_digest=run.manifest_digest,
        case_id=case_id,
        subject_id="subject-phase15",
        subject_version="1.0.0",
        status=status,
        severe_violation=severe_violation,
        rule_codes=(),
        summary="deterministic result",
    )


def _promote() -> DecisionSupportPromotionDecision:
    """构造满足严格 AND 门槛的晋升结论。"""

    return DecisionSupportPromotionDecision(
        status=PromotionStatus.PROMOTE,
        reason_codes=(),
        model_evidence_complete=True,
        human_evidence_complete=True,
        completed_smoke_cases=10,
        severe_violation_count=0,
        safety_correctness=Decimal("0.95"),
        key_conflict_miss_rate_reduction=Decimal("0.35"),
        decision_median_reduction=Decimal("0.25"),
    )


def test_release_run_and_case_result_are_idempotent_but_conflicts_fail_closed() -> None:
    """相同事实重放成功，身份相同但事实不同必须拒绝。"""

    store = InMemoryReleaseStore()
    run = _run()
    assert store.create_run(run) == run
    assert store.create_run(run) == run
    with pytest.raises(ReleaseInvariantError):
        store.create_run(run.model_validate({**run.model_dump(mode="json"), "mode": "RELEASE"}))

    result = _result(run, "case-1")
    assert store.append_case_result(result) == result
    assert store.append_case_result(result) == result
    with pytest.raises(ReleaseInvariantError):
        store.append_case_result(_result(run, "case-1", status=EvaluationCaseStatus.FAIL))
    with pytest.raises(ReleaseInvariantError):
        store.append_case_result(_result(run, "unknown"))


def test_missing_case_and_failed_case_cannot_be_reported_as_technical_pass() -> None:
    """缺 case 是 BLOCKED，确定性失败是 FAIL，均不能被平均分覆盖。"""

    missing = InMemoryReleaseStore()
    run = _run("release-missing")
    missing.create_run(run)
    missing.append_case_result(_result(run, "case-1"))
    technical = missing.finalize_technical(run.release_run_id)
    assert technical.status is TechnicalReleaseStatus.BLOCKED
    assert missing.get_run(run.release_run_id).status.value == "BLOCKED"

    failed = InMemoryReleaseStore()
    run = _run("release-failed")
    failed.create_run(run)
    failed.append_case_result(_result(run, "case-1", status=EvaluationCaseStatus.FAIL, severe_violation=True))
    failed.append_case_result(_result(run, "case-2"))
    technical = failed.finalize_technical(run.release_run_id)
    assert technical.status is TechnicalReleaseStatus.FAIL
    assert technical.severe_violation_count == 1


def test_complete_technical_release_and_promotion_form_final_status() -> None:
    """技术 PASS 与 Promotion 结论独立保存，再按固定映射生成最终状态。"""

    store = InMemoryReleaseStore()
    run = _run("release-complete")
    store.create_run(run)
    for case_id in run.expected_case_ids:
        store.append_case_result(_result(run, case_id))
    technical = store.finalize_technical(run.release_run_id)
    assert technical.status is TechnicalReleaseStatus.PASS

    final = store.save_promotion(
        run.release_run_id,
        DecisionSupportPromotionDecision.blocked("REAL_MODEL_EVIDENCE_MISSING"),
    )
    assert final.status is FinalReleaseStatus.RELEASED_DECISION_SUPPORT_DISABLED

    promoted = InMemoryReleaseStore()
    run = _run("release-promoted")
    promoted.create_run(run)
    for case_id in run.expected_case_ids:
        promoted.append_case_result(_result(run, case_id))
    promoted.finalize_technical(run.release_run_id)
    final = promoted.save_promotion(run.release_run_id, _promote())
    assert final.status is FinalReleaseStatus.RELEASED_DECISION_SUPPORT_ENABLED


def test_phase15_budget_isolated_at_sixty_cent_and_does_not_borrow_phase14() -> None:
    """Phase 15 只能消费自己的 0.60 元，Phase 14 额度保持独立。"""

    budget = Phase15BudgetStore(scope_id="phase15-budget-test")
    budget.reserve("phase15-reserve", Decimal("0.60"))
    with pytest.raises(Phase15BudgetLimitExceeded):
        budget.reserve("phase15-over", Decimal("0.01"))
    assert budget.snapshot().available_cny == Decimal("0.00")
