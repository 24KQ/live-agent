"""Phase 16 qualification PostgreSQL append-only 账本集成契约。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from src.config.settings import get_settings
from src.decision_support.phase16_qualification import (
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    build_phase16_qualification_policy,
    load_phase16_qualification_corpus,
)
from src.decision_support.phase16_qualification_ledger import (
    Phase16QualificationLedgerError,
    QualificationCampaign,
    QualificationCampaignKind,
    QualificationCorpusIdentity,
    QualificationReleaseEvent,
    QualificationRunStatus,
    PostgresPhase16QualificationLedger,
    build_qualification_candidate,
    build_qualification_metric,
    corpus_identity_from_manifest,
    initialize_phase16_qualification_schema,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("6e" * 32)
_WRONG_HMAC_KEY = bytes.fromhex("7f" * 32)


@pytest.fixture()
def qualification_ledger_factory():
    """每例使用真实独立 PostgreSQL schema，验证 append-only 约束不是 Python 惯例。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_qualification_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_qualification_schema(settings)

    def build(*, key: bytes = _TEST_HMAC_KEY) -> PostgresPhase16QualificationLedger:
        return PostgresPhase16QualificationLedger(settings, hmac_key=key)

    build.settings = settings
    try:
        yield build
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def _parents(ledger: PostgresPhase16QualificationLedger):
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    identity = corpus_identity_from_manifest(corpus.manifest)
    candidate = build_qualification_candidate(
        candidate_id="phase16-qualification-candidate-development-001",
        policy=policy,
        analyst_profile_digest="a" * 64,
        planner_profile_digest="b" * 64,
        adapter_digest="c" * 64,
    )
    ledger.ensure_policy(policy)
    ledger.ensure_corpus(identity)
    ledger.ensure_candidate(candidate)
    return policy, identity, candidate


def _campaign(*, policy, corpus, candidate, campaign_id: str, kind: QualificationCampaignKind):
    return QualificationCampaign(
        campaign_id=campaign_id,
        campaign_kind=kind,
        policy_digest=policy.policy_digest or "",
        corpus_digest=corpus.corpus_digest,
        candidate_digest=candidate.candidate_digest or "",
        manifest_digest=("d" if kind is QualificationCampaignKind.DEVELOPMENT else "e") * 64,
        reservation_cny="0.500000",
    )


def _append_complete_metrics(ledger, *, run_id: str, e2e_count: int, safety_count: int) -> None:
    ledger.append_metric(
        build_qualification_metric(
            run_id=run_id,
            metric_code="E2E_MULTI_AGENT_READY",
            numerator=e2e_count,
            denominator=e2e_count,
        )
    )
    ledger.append_metric(
        build_qualification_metric(
            run_id=run_id,
            metric_code="HARD_SAFETY_CONFORMANCE",
            numerator=safety_count,
            denominator=safety_count,
        )
    )


def test_qualification_ledger_authenticates_terminal_result_and_rejects_mutation(
    qualification_ledger_factory,
) -> None:
    ledger = qualification_ledger_factory()
    policy, corpus, candidate = _parents(ledger)
    campaign = _campaign(
        policy=policy,
        corpus=corpus,
        candidate=candidate,
        campaign_id="phase16-qualification-development-001",
        kind=QualificationCampaignKind.DEVELOPMENT,
    )
    ledger.ensure_campaign(campaign)
    ledger.begin_run(run_id="phase16-qualification-development-run-001", campaign_id=campaign.campaign_id)
    _append_complete_metrics(
        ledger, run_id="phase16-qualification-development-run-001", e2e_count=12, safety_count=18
    )
    report = ledger.close_run(
        run_id="phase16-qualification-development-run-001",
        status=QualificationRunStatus.PASS,
        reason_code="DEVELOPMENT_DIAGNOSTIC_COMPLETE",
        evaluation_digest="f" * 64,
    )

    assert report.status is QualificationRunStatus.PASS
    assert report.authenticated is True
    assert {item.metric_code for item in report.metrics} == {
        "E2E_MULTI_AGENT_READY",
        "HARD_SAFETY_CONFORMANCE",
    }
    with pytest.raises(Phase16QualificationLedgerError, match="already terminal"):
        ledger.append_metric(
            build_qualification_metric(
                run_id=report.run_id,
                metric_code="OPTION_VALIDITY",
                numerator=12,
                denominator=12,
            )
        )
    with psycopg.connect(**qualification_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                "UPDATE phase16_qualification_results SET status='FAILED' WHERE run_id=%s",
                (report.run_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.Error, match="cannot be truncated"):
            connection.execute("TRUNCATE phase16_qualification_metric_facts")
        connection.rollback()
        with pytest.raises(psycopg.Error, match="cannot follow terminal"):
            connection.execute(
                """INSERT INTO phase16_qualification_metric_facts
                   (run_id, metric_code, numerator, denominator, metric_digest)
                   VALUES (%s,'OPTION_VALIDITY',12,12,%s)""",
                (report.run_id, "1" * 64),
            )


def test_qualification_holdout_batch_requires_committed_corpus_release_and_fifteen_e2e_cases(
    qualification_ledger_factory,
) -> None:
    ledger = qualification_ledger_factory()
    policy, pending_corpus, candidate = _parents(ledger)
    pending_campaign = _campaign(
        policy=policy,
        corpus=pending_corpus,
        candidate=candidate,
        campaign_id="phase16-qualification-holdout-pending-001",
        kind=QualificationCampaignKind.HOLDOUT,
    )
    with pytest.raises(Phase16QualificationLedgerError, match="not committed"):
        ledger.ensure_campaign(pending_campaign)

    committed_corpus = QualificationCorpusIdentity(
        corpus_id="phase16-qualification-corpus-sealed-v2",
        corpus_version="2.0.0",
        policy_digest=policy.policy_digest or "",
        corpus_digest="9" * 64,
        holdout_release_state="COMMITTED",
        holdout_commitment_digest="8" * 64,
        holdout_case_count=36,
        holdout_high_conflict_e2e_case_count=30,
    )
    ledger.ensure_corpus(committed_corpus)
    campaign = _campaign(
        policy=policy,
        corpus=committed_corpus,
        candidate=candidate,
        campaign_id="phase16-qualification-holdout-001",
        kind=QualificationCampaignKind.HOLDOUT,
    )
    ledger.ensure_campaign(campaign)
    ledger.record_release_event(
        QualificationReleaseEvent(
            corpus_digest=committed_corpus.corpus_digest,
            commitment_digest="8" * 64,
            plaintext_digest="7" * 64,
            release_owner_id_digest="6" * 64,
        )
    )
    ledger.begin_run(run_id="phase16-qualification-holdout-run-001", campaign_id=campaign.campaign_id)
    _append_complete_metrics(
        ledger, run_id="phase16-qualification-holdout-run-001", e2e_count=15, safety_count=18
    )
    report = ledger.close_run(
        run_id="phase16-qualification-holdout-run-001",
        status=QualificationRunStatus.PASS,
        reason_code="HOLDOUT_QUALIFICATION_COMPLETE",
        evaluation_digest="5" * 64,
    )
    assert report.status is QualificationRunStatus.PASS
    assert report.authenticated is True


def test_qualification_result_hmac_fails_closed_for_wrong_process_key(qualification_ledger_factory) -> None:
    writer = qualification_ledger_factory()
    policy, corpus, candidate = _parents(writer)
    campaign = _campaign(
        policy=policy,
        corpus=corpus,
        candidate=candidate,
        campaign_id="phase16-qualification-validation-001",
        kind=QualificationCampaignKind.VALIDATION,
    )
    writer.ensure_campaign(campaign)
    writer.begin_run(run_id="phase16-qualification-validation-run-001", campaign_id=campaign.campaign_id)
    _append_complete_metrics(
        writer, run_id="phase16-qualification-validation-run-001", e2e_count=12, safety_count=18
    )
    writer.close_run(
        run_id="phase16-qualification-validation-run-001",
        status=QualificationRunStatus.PASS,
        reason_code="VALIDATION_PERFORMANCE_COMPLETE",
        evaluation_digest="4" * 64,
    )

    wrong_key_report = qualification_ledger_factory(key=_WRONG_HMAC_KEY).report(
        run_id="phase16-qualification-validation-run-001"
    )
    assert wrong_key_report.status is QualificationRunStatus.BLOCKED
    assert wrong_key_report.reason_code == "RESULT_AUTHENTICATION_FAILED"
    assert wrong_key_report.authenticated is False
