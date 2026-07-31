"""Phase 16 qualification evaluator 与 candidate 输出契约测试。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from src.decision_support.controlled_e2e_v5 import (
    build_phase16_v5_analyst_profile,
    build_phase16_v5_planner_profile,
)
from src.decision_support.phase16_qualification import (
    HoldoutReleaseState,
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    build_phase16_qualification_policy,
    load_phase16_qualification_corpus,
)
from src.decision_support.phase16_qualification_evaluator import (
    QualificationAssessmentStatus,
    QualificationEvaluator,
    admit_qualification_campaign,
    build_phase16_qualification_candidate_bundle,
)
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaign,
    QualificationCampaignKind,
    QualificationCorpusIdentity,
    build_qualification_metric,
    corpus_identity_from_manifest,
)
from src.specialist_runtime.profiles import FinalEvidenceBindingMode


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _setup(kind: QualificationCampaignKind):
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    identity = corpus_identity_from_manifest(corpus.manifest)
    bundle = build_phase16_qualification_candidate_bundle(
        policy=policy,
        repository_root=_PROJECT_ROOT,
    )
    campaign = QualificationCampaign(
        campaign_id=f"phase16-qualification-{kind.value.lower()}-001",
        campaign_kind=kind,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=bundle.candidate.candidate_digest or "",
        manifest_digest="e" * 64,
        reservation_cny="0.900000",
    )
    return policy, identity, bundle, campaign


def _complete_metrics(*, run_id: str, count: int):
    codes = (
        "E2E_MULTI_AGENT_READY",
        "HARD_SAFETY_CONFORMANCE",
        "ANALYST_SCHEMA_AND_SEMANTIC_VALID",
        "PLANNER_RISK_COVERAGE",
        "EXPLANATION_BOUND",
        "CONTROLLED_EVIDENCE_BINDING",
        "OPTION_VALIDITY",
    )
    return tuple(
        build_qualification_metric(
            run_id=run_id,
            metric_code=code,
            numerator=count,
            denominator=count,
        )
        for code in codes
    )


def test_new_candidate_keeps_all_guardrails_and_makes_v8_failure_rules_executable() -> None:
    policy, _identity, bundle, campaign = _setup(QualificationCampaignKind.DEVELOPMENT)
    analyst = bundle.analyst_profile
    planner = bundle.planner_profile

    # V9 矩阵配置：模型/端点不再冻结进 policy，改由 campaign 声明运行时组合；
    # candidate profile 的冻结默认值必须与声明默认值一致（运行时切换走 env 覆写，
    # receipt 记录实际组合）。
    for profile in (analyst, planner):
        assert profile.model_id == campaign.declared_model_id
        assert profile.endpoint_host == campaign.declared_endpoint_hosts[0]
        assert profile.temperature == 0
        assert profile.allowed_skill_ids == ()
        assert profile.max_skill_calls == 0
        assert profile.max_model_calls == 1
        assert profile.final_evidence_binding_mode is FinalEvidenceBindingMode.SYSTEM_MANAGED_IDS
    assert "最多 360 个 Unicode 字符" in analyst.prompt_text
    assert '"maxLength":500' in analyst.prompt_text
    assert "逐字复制到该 option.risk_flags" in planner.prompt_text
    assert "HUMAN_CONFIRMATION_REQUIRED" in planner.prompt_text
    assert bundle.candidate.analyst_profile_digest == analyst.profile_digest
    assert bundle.candidate.planner_profile_digest == planner.profile_digest
    v8_analyst = build_phase16_v5_analyst_profile()
    v8_planner = build_phase16_v5_planner_profile()
    assert analyst.result_schema_hash == v8_analyst.result_schema_hash
    assert planner.result_schema_hash == v8_planner.result_schema_hash
    # Stage reservation 由 0.030000 调高到 0.100000，覆盖已验证的 reasoning 膨胀
    # （worst_case ×3），保留 V8 输出 schema 哈希与温度等所有其他不可放宽的冻结事实。
    assert analyst.max_case_cost_cny == Decimal("0.100000")
    assert planner.max_case_cost_cny == Decimal("0.100000")


def test_campaign_admission_requires_a_committed_holdout_but_allows_public_development() -> None:
    policy, pending, bundle, development = _setup(QualificationCampaignKind.DEVELOPMENT)
    development_admission = admit_qualification_campaign(
        policy=policy,
        corpus=pending,
        candidate_bundle=bundle,
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
    )
    assert development_admission.allowed is True

    holdout_admission = admit_qualification_campaign(
        policy=policy,
        corpus=pending,
        candidate_bundle=bundle,
        campaign_kind=QualificationCampaignKind.HOLDOUT,
    )
    assert holdout_admission.allowed is False
    assert holdout_admission.reason_codes == ("HOLDOUT_COMMITMENT_REQUIRED",)

    committed = QualificationCorpusIdentity(
        corpus_id="phase16-qualification-corpus-sealed-v1",
        corpus_version="1.0.0",
        policy_digest=policy.policy_digest or "",
        corpus_digest="9" * 64,
        holdout_release_state=HoldoutReleaseState.COMMITTED,
        holdout_commitment_digest="8" * 64,
        holdout_case_count=36,
        holdout_high_conflict_e2e_case_count=30,
    )
    committed_admission = admit_qualification_campaign(
        policy=policy,
        corpus=committed,
        candidate_bundle=bundle,
        campaign_kind=QualificationCampaignKind.HOLDOUT,
    )
    assert committed_admission.allowed is True


def test_evaluator_makes_development_validation_and_holdout_claims_non_substitutable() -> None:
    evaluator = QualificationEvaluator()
    policy, identity, bundle, development = _setup(QualificationCampaignKind.DEVELOPMENT)
    development_assessment = evaluator.assess(
        campaign=development,
        policy=policy,
        corpus=identity,
        candidate=bundle.candidate,
        metric_facts=_complete_metrics(run_id="development-run", count=12),
    )
    assert development_assessment.status is QualificationAssessmentStatus.DEVELOPMENT_DIAGNOSTIC
    assert development_assessment.run_status.value == "PASS"

    validation = development.model_copy(
        update={
            "campaign_id": "phase16-qualification-validation-001",
            "campaign_kind": QualificationCampaignKind.VALIDATION,
            "manifest_digest": "d" * 64,
        }
    )
    validation_assessment = evaluator.assess(
        campaign=validation,
        policy=policy,
        corpus=identity,
        candidate=bundle.candidate,
        metric_facts=_complete_metrics(run_id="validation-run", count=12),
    )
    assert validation_assessment.status is QualificationAssessmentStatus.VALIDATION_PERFORMANCE

    committed = QualificationCorpusIdentity(
        corpus_id="phase16-qualification-corpus-sealed-v2",
        corpus_version="2.0.0",
        policy_digest=policy.policy_digest or "",
        corpus_digest="9" * 64,
        holdout_release_state=HoldoutReleaseState.RELEASED,
        holdout_commitment_digest="8" * 64,
        holdout_case_count=36,
        holdout_high_conflict_e2e_case_count=30,
    )
    holdout_batch_one = validation.model_copy(
        update={
            "campaign_id": "phase16-qualification-holdout-batch-001",
            "campaign_kind": QualificationCampaignKind.HOLDOUT,
            "corpus_digest": committed.corpus_digest,
            "manifest_digest": "c" * 64,
            "batch_index": 1,
        }
    )
    holdout_batch_two = holdout_batch_one.model_copy(
        update={
            "campaign_id": "phase16-qualification-holdout-batch-002",
            "manifest_digest": "b" * 64,
            "batch_index": 2,
        }
    )
    first_batch_assessment = evaluator.assess(
        campaign=holdout_batch_one,
        policy=policy,
        corpus=committed,
        candidate=bundle.candidate,
        metric_facts=_complete_metrics(run_id="holdout-batch-one-run", count=15),
        release_verified=True,
    )
    second_batch_assessment = evaluator.assess(
        campaign=holdout_batch_two,
        policy=policy,
        corpus=committed,
        candidate=bundle.candidate,
        metric_facts=_complete_metrics(run_id="holdout-batch-two-run", count=15),
        release_verified=True,
    )
    assert first_batch_assessment.status is QualificationAssessmentStatus.HOLDOUT_BATCH_PASS
    final_holdout = evaluator.assess_holdout_batches(
        policy=policy,
        corpus=committed,
        candidate=bundle.candidate,
        batches=(first_batch_assessment, second_batch_assessment),
    )
    assert final_holdout.status is QualificationAssessmentStatus.HOLDOUT_QUALIFICATION
    assert (final_holdout.aggregate_e2e_numerator, final_holdout.aggregate_e2e_denominator) == (30, 30)
    assert final_holdout.assessment_digest


def test_evaluator_never_hides_a_semantic_failure_behind_high_aggregate_metrics() -> None:
    evaluator = QualificationEvaluator()
    policy, identity, bundle, validation = _setup(QualificationCampaignKind.VALIDATION)
    metrics = list(_complete_metrics(run_id="validation-run", count=12))
    metrics[3] = build_qualification_metric(
        run_id="validation-run",
        metric_code="PLANNER_RISK_COVERAGE",
        numerator=11,
        denominator=12,
    )
    assessment = evaluator.assess(
        campaign=validation,
        policy=policy,
        corpus=identity,
        candidate=bundle.candidate,
        metric_facts=metrics,
    )
    assert assessment.status is QualificationAssessmentStatus.NOT_QUALIFIED
    assert assessment.run_status.value == "FAILED"
    assert "PLANNER_RISK_COVERAGE_FAILED" in assessment.reason_codes


def test_engineering_safety_conformance_is_independent_from_model_e2e_quality() -> None:
    evaluator = QualificationEvaluator()
    policy, identity, _bundle, _campaign = _setup(QualificationCampaignKind.DEVELOPMENT)
    safety_metrics = tuple(
        build_qualification_metric(
            run_id="deterministic-safety-run",
            metric_code=code,
            numerator=48,
            denominator=48,
        )
        for code in policy.hard_safety_requirements
    )
    assessment = evaluator.assess_engineering_safety(
        policy=policy,
        corpus=identity,
        metric_facts=safety_metrics,
    )
    assert assessment.status is QualificationAssessmentStatus.ENGINEERING_SAFETY_CONFORMANCE
    assert assessment.reason_codes == ("ENGINEERING_SAFETY_CONFORMANCE_COMPLETE",)

    failed_metrics = list(safety_metrics)
    failed_metrics[0] = build_qualification_metric(
        run_id="deterministic-safety-run",
        metric_code=policy.hard_safety_requirements[0],
        numerator=47,
        denominator=48,
    )
    failed = evaluator.assess_engineering_safety(
        policy=policy,
        corpus=identity,
        metric_facts=failed_metrics,
    )
    assert failed.status is QualificationAssessmentStatus.NOT_QUALIFIED
    assert failed.reason_codes == (f"{policy.hard_safety_requirements[0]}_FAILED",)
