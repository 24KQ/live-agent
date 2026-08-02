"""Phase 16 qualification PostgreSQL append-only 账本集成契约。"""

from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from src.config.settings import get_settings
from src.decision_support.phase16_qualification import (
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    PHASE16_QUALIFICATION_POLICY_PATH,
    Phase16QualificationManifest,
    Phase16QualificationPolicy,
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
    qualification_campaign_id,
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
    # 账本行为测试固定使用冻结的 v2 资产（直接 model_validate，跳过
    # load_* 的源码闭包 rebuild 校验）：闭包一致性由 CI release gate 与真实 run
    # 的 load 路径负责，本测试不依赖实时闭包哈希。
    policy = Phase16QualificationPolicy.model_validate(
        json.loads((_PROJECT_ROOT / PHASE16_QUALIFICATION_POLICY_PATH).read_bytes())
    )
    manifest = Phase16QualificationManifest.model_validate(
        json.loads(
            (_PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY / "manifest.json").read_bytes()
        )
    )
    identity = corpus_identity_from_manifest(manifest)
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


def _campaign(*, policy, corpus, candidate, kind: QualificationCampaignKind):
    # fixture 使用模型默认声明组合（gpt-5.6-luna / 无 effort / synapse 单渠道）；
    # campaign_id 由闭包 canonical 函数按声明字段派生。
    return QualificationCampaign(
        campaign_id=qualification_campaign_id(
            kind=kind,
            candidate_digest=candidate.candidate_digest or "",
            declared_model_id="gpt-5.6-luna",
            declared_reasoning_effort=None,
            declared_endpoint_hosts=("synapse-ai.uk",),
        ),
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


def test_qualification_same_digest_distinct_declared_combos_are_separate_campaigns(
    qualification_ledger_factory,
) -> None:
    """同一 digest 下不同声明组合必须并存为两个 campaign（组合是身份的一部分）。

    回归：遗留 UNIQUE (policy_digest, campaign_kind, candidate_digest, batch_index)
    不含组合，会把本测试第二个 campaign 误判为重复并抛 UniqueViolation；身份修复
    后 campaign_id PRIMARY KEY 才是唯一后盾（alter_phase16_qualification_campaign_identity.sql）。
    """
    ledger = qualification_ledger_factory()
    policy, corpus, candidate = _parents(ledger)
    first = _campaign(
        policy=policy,
        corpus=corpus,
        candidate=candidate,
        kind=QualificationCampaignKind.DEVELOPMENT,
    )
    second = QualificationCampaign(
        campaign_id=qualification_campaign_id(
            kind=QualificationCampaignKind.DEVELOPMENT,
            candidate_digest=candidate.candidate_digest or "",
            declared_model_id="gpt-5.6-luna",
            declared_reasoning_effort=None,
            declared_endpoint_hosts=("synapse-ai.uk", "api.imagebridge.top"),
        ),
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
        policy_digest=policy.policy_digest or "",
        corpus_digest=corpus.corpus_digest,
        candidate_digest=candidate.candidate_digest or "",
        manifest_digest="d" * 64,
        reservation_cny="0.500000",
        declared_model_id="gpt-5.6-luna",
        declared_reasoning_effort=None,
        declared_endpoint_hosts=("synapse-ai.uk", "api.imagebridge.top"),
    )
    assert second.campaign_id != first.campaign_id
    ledger.ensure_campaign(first)
    ledger.ensure_campaign(second)
    with psycopg.connect(**qualification_ledger_factory.settings.postgres_connection_kwargs) as connection:
        for campaign in (first, second):
            row = connection.execute(
                "SELECT 1 FROM phase16_qualification_campaigns WHERE campaign_id=%s",
                (campaign.campaign_id,),
            ).fetchone()
            assert row is not None, campaign.campaign_id


def test_qualification_same_digest_same_declared_combo_is_idempotent_single_row(
    qualification_ledger_factory,
) -> None:
    """同一 digest 同一组合重复 ensure_campaign 幂等返回既有行，不产生第二行。"""
    ledger = qualification_ledger_factory()
    policy, corpus, candidate = _parents(ledger)
    campaign = _campaign(
        policy=policy,
        corpus=corpus,
        candidate=candidate,
        kind=QualificationCampaignKind.DEVELOPMENT,
    )
    ledger.ensure_campaign(campaign)
    ledger.ensure_campaign(campaign)
    with psycopg.connect(**qualification_ledger_factory.settings.postgres_connection_kwargs) as connection:
        count = connection.execute(
            "SELECT count(*) FROM phase16_qualification_campaigns WHERE campaign_id=%s",
            (campaign.campaign_id,),
        ).fetchone()[0]
    assert count == 1


def test_qualification_development_campaign_limit_is_enforced_per_policy(
    qualification_ledger_factory,
) -> None:
    """V9：maximum_future_development_candidates=2 运行时强制。

    同一 policy digest 下最多 2 个 DEVELOPMENT campaign；第 3 个全新组合被拒绝；
    幂等重复 ensure 不受计数影响；VALIDATION 不受 DEVELOPMENT 上限约束。
    """
    ledger = qualification_ledger_factory()
    policy, corpus, candidate = _parents(ledger)

    def dev_campaign(*, hosts):
        return QualificationCampaign(
            campaign_id=qualification_campaign_id(
                kind=QualificationCampaignKind.DEVELOPMENT,
                candidate_digest=candidate.candidate_digest or "",
                declared_model_id="gpt-5.6-luna",
                declared_reasoning_effort=None,
                declared_endpoint_hosts=hosts,
            ),
            campaign_kind=QualificationCampaignKind.DEVELOPMENT,
            policy_digest=policy.policy_digest or "",
            corpus_digest=corpus.corpus_digest,
            candidate_digest=candidate.candidate_digest or "",
            manifest_digest="d" * 64,
            reservation_cny="0.500000",
            declared_model_id="gpt-5.6-luna",
            declared_reasoning_effort=None,
            declared_endpoint_hosts=hosts,
        )

    first = dev_campaign(hosts=("synapse-ai.uk",))
    second = dev_campaign(hosts=("synapse-ai.uk", "api.imagebridge.top"))
    third = dev_campaign(hosts=("synapse-ai.uk", "ai.vote520.com"))
    ledger.ensure_campaign(first)
    ledger.ensure_campaign(second)
    # 幂等：同一组合重复 ensure 命中既有行，不触发上限。
    ledger.ensure_campaign(second)
    with pytest.raises(Phase16QualificationLedgerError, match="development campaign limit"):
        ledger.ensure_campaign(third)
    # VALIDATION 不受 DEVELOPMENT 上限约束。
    ledger.ensure_campaign(
        _campaign(
            policy=policy,
            corpus=corpus,
            candidate=candidate,
            kind=QualificationCampaignKind.VALIDATION,
        )
    )
    with psycopg.connect(**qualification_ledger_factory.settings.postgres_connection_kwargs) as connection:
        count = connection.execute(
            "SELECT count(*) FROM phase16_qualification_campaigns WHERE policy_digest=%s",
            (policy.policy_digest or "",),
        ).fetchone()[0]
    assert count == 3  # 2 dev + 1 val


def test_qualification_development_campaign_limit_is_concurrency_safe(
    qualification_ledger_factory,
) -> None:
    """V9：dev 候选上限在并发 ensure 下仍成立（计数在 policy 行锁内）。

    回归：此前 dev COUNT 检查在 policy 行锁（FOR UPDATE）之前执行，两个并发
    事务可同时读到 dev_count=1 并双双插入第 2 个 campaign 突破上限 2；修复后
    「查重 → 计数 → INSERT」与并发事务在 policy 行上串行，终态 dev 数 ≤2。
    """
    ledger = qualification_ledger_factory()
    policy, corpus, candidate = _parents(ledger)

    def dev_campaign(*, hosts):
        return QualificationCampaign(
            campaign_id=qualification_campaign_id(
                kind=QualificationCampaignKind.DEVELOPMENT,
                candidate_digest=candidate.candidate_digest or "",
                declared_model_id="gpt-5.6-luna",
                declared_reasoning_effort=None,
                declared_endpoint_hosts=hosts,
            ),
            campaign_kind=QualificationCampaignKind.DEVELOPMENT,
            policy_digest=policy.policy_digest or "",
            corpus_digest=corpus.corpus_digest,
            candidate_digest=candidate.candidate_digest or "",
            manifest_digest="d" * 64,
            reservation_cny="0.500000",
            declared_model_id="gpt-5.6-luna",
            declared_reasoning_effort=None,
            declared_endpoint_hosts=hosts,
        )

    ledger.ensure_campaign(dev_campaign(hosts=("synapse-ai.uk",)))
    # 两个独立连接并发竞争第 2 个名额（不同组合），至多一个成功。
    contenders = [
        ("synapse-ai.uk", "api.imagebridge.top"),
        ("synapse-ai.uk", "ai.vote520.com"),
    ]
    outcomes = []
    barrier = threading.Barrier(3)

    def worker(hosts):
        local = qualification_ledger_factory()
        barrier.wait()
        try:
            local.ensure_campaign(dev_campaign(hosts=hosts))
            outcomes.append(("ok", hosts))
        except Phase16QualificationLedgerError as error:
            outcomes.append(("limit", str(error)))

    threads = [threading.Thread(target=worker, args=(hosts,)) for hosts in contenders]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=30)

    assert sum(1 for kind, _ in outcomes if kind == "ok") == 1
    assert sum(1 for kind, _ in outcomes if kind == "limit") == 1
    with psycopg.connect(**qualification_ledger_factory.settings.postgres_connection_kwargs) as connection:
        dev_count = connection.execute(
            """SELECT count(*) FROM phase16_qualification_campaigns
                WHERE policy_digest=%s AND campaign_kind='DEVELOPMENT'""",
            (policy.policy_digest or "",),
        ).fetchone()[0]
    assert dev_count == 2  # 1 既有 + 1 并发胜出
