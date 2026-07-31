"""Phase 16 qualification ledger 的纯模型与 DDL 结构契约。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.run_db_migrations import MIGRATIONS
from src.decision_support.phase16_qualification import build_phase16_qualification_policy
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaign,
    QualificationCampaignKind,
    QualificationCorpusIdentity,
    QualificationMetricFact,
    QualificationRunStatus,
    build_qualification_candidate,
    build_qualification_metric,
    corpus_identity_from_manifest,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DDL_PATH = _PROJECT_ROOT / "docker" / "init_phase16_qualification_ledger.sql"


def _sql() -> str:
    return " ".join(_DDL_PATH.read_text(encoding="utf-8").lower().split())


def test_qualification_migration_is_required_and_additive_after_v5() -> None:
    steps = [item.phase for item in MIGRATIONS]
    assert steps.index("phase16_qualification") == steps.index("phase16_v5_controlled_e2e") + 1
    step = next(item for item in MIGRATIONS if item.phase == "phase16_qualification")
    assert step.sql_file == "init_phase16_qualification_ledger.sql"
    assert step.required is True
    budget_step = next(
        item for item in MIGRATIONS if item.phase == "phase16_qualification_budget_ranges"
    )
    assert (
        steps.index("phase16_qualification_budget_ranges")
        == steps.index("phase16_qualification") + 1
    )
    assert budget_step.sql_file == "alter_phase16_qualification_budget_ranges.sql"
    assert budget_step.required is True
    identity_step = next(
        item for item in MIGRATIONS if item.phase == "phase16_qualification_identity_uniques"
    )
    assert (
        steps.index("phase16_qualification_identity_uniques")
        == steps.index("phase16_qualification_budget_ranges") + 1
    )
    assert identity_step.sql_file == "alter_phase16_qualification_identity_uniques.sql"
    assert identity_step.required is True

    sql = _sql()
    for table in (
        "phase16_qualification_policies",
        "phase16_qualification_corpora",
        "phase16_qualification_candidates",
        "phase16_qualification_campaigns",
        "phase16_qualification_source_evidence",
        "phase16_qualification_release_events",
        "phase16_qualification_runs",
        "phase16_qualification_metric_facts",
        "phase16_qualification_results",
    ):
        assert f"create table if not exists {table}" in sql
    assert "foreach table_name in array array[" in sql
    assert "trg_%s_append_only" in sql
    assert "trg_%s_no_truncate" in sql
    assert "phase16_qualification_reject_mutation" in sql
    assert "phase16_qualification_reject_truncate" in sql
    assert "alter table phase16_v5" not in sql
    assert "insert into phase16_v5" not in sql
    assert "phase16_v5_" not in sql


def test_qualification_candidate_and_metric_are_self_authenticating() -> None:
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    candidate = build_qualification_candidate(
        candidate_id="phase16-qualification-candidate-development-001",
        policy=policy,
        analyst_profile_digest="a" * 64,
        planner_profile_digest="b" * 64,
        adapter_digest="c" * 64,
    )
    metric = build_qualification_metric(
        run_id="phase16-qualification-development-001",
        metric_code="E2E_MULTI_AGENT_READY",
        numerator=12,
        denominator=12,
    )

    assert candidate.candidate_digest is not None
    assert metric.metric_digest is not None
    assert QualificationMetricFact.model_validate(metric.model_dump(mode="json")) == metric

    tampered = candidate.model_dump(mode="json")
    tampered["candidate_id"] = "phase16-qualification-candidate-development-002"
    with pytest.raises(ValidationError, match="candidate digest"):
        type(candidate).model_validate(tampered)


def test_pending_corpus_cannot_be_used_as_holdout_campaign() -> None:
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    identity = QualificationCorpusIdentity(
        corpus_id="phase16-qualification-corpus-v1",
        corpus_version="1.0.0",
        policy_digest=policy.policy_digest or "",
        corpus_digest="d" * 64,
        holdout_release_state="PENDING_INDEPENDENT_COMMITMENT",
        holdout_case_count=30,
        holdout_high_conflict_e2e_case_count=30,
    )
    candidate = build_qualification_candidate(
        candidate_id="phase16-qualification-candidate-development-001",
        policy=policy,
        analyst_profile_digest="a" * 64,
        planner_profile_digest="b" * 64,
        adapter_digest="c" * 64,
    )
    campaign = QualificationCampaign(
        campaign_id="phase16-qualification-development-001",
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=candidate.candidate_digest or "",
        manifest_digest="e" * 64,
        reservation_cny="0.500000",
    )
    assert campaign.campaign_kind is QualificationCampaignKind.DEVELOPMENT
    assert identity.holdout_commitment_digest is None
    assert QualificationRunStatus.PASS.value == "PASS"


def test_metric_rejects_inconsistent_count_or_digest() -> None:
    with pytest.raises(ValidationError, match="numerator"):
        QualificationMetricFact(
            run_id="phase16-qualification-run-001",
            metric_code="E2E_MULTI_AGENT_READY",
            numerator=13,
            denominator=12,
        )
    metric = build_qualification_metric(
        run_id="phase16-qualification-run-001",
        metric_code="HARD_SAFETY_CONFORMANCE",
        numerator=18,
        denominator=18,
    )
    payload = metric.model_dump(mode="json")
    payload["numerator"] = 17
    with pytest.raises(ValidationError, match="metric digest"):
        QualificationMetricFact.model_validate(payload)
