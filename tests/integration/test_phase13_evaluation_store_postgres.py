"""Phase 13 Task 5 PostgreSQL Evaluation Store 集成测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from src.config.settings import get_settings
from src.specialist_evaluation.comparison import BinaryPair, aggregate_binary_pairs
from src.specialist_evaluation.models import (
    CaseAttempt,
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationRun,
    EvaluationSplit,
    EvaluationSubject,
    RetentionDecision,
    RetentionDecisionRecord,
    canonical_json_sha256,
)
from src.specialist_evaluation.store import (
    EvaluationInvariantError,
    PostgresSpecialistEvaluationStore,
    initialize_specialist_evaluation_schema,
)


HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def evaluation_store():
    """使用唯一 Manifest/Run 隔离测试，并按外键逆序精确清理。"""

    settings = get_settings()
    initialize_specialist_evaluation_schema(settings)
    suffix = str(uuid4())
    development = tuple(f"development-{index:03d}" for index in range(60))
    validation = ("live-validation-001",) + tuple(
        f"validation-{index:03d}" for index in range(119)
    )
    holdout = tuple(f"holdout-{index:03d}" for index in range(60))
    case_candidate_map = {}
    candidates = tuple(EvaluationCandidate)
    for split_ids, per_candidate in ((development, 20), (validation, 40), (holdout, 20)):
        for candidate_index, candidate in enumerate(candidates):
            start = candidate_index * per_candidate
            for case_id in split_ids[start : start + per_candidate]:
                case_candidate_map[case_id] = candidate.value
    manifest = EvaluationManifest(
        manifest_id=f"phase13-test-{suffix}",
        manifest_version="2.0.0",
        dataset_digest=HASH_A,
        schema_digest=HASH_B,
        generator_digest=HASH_A,
        seed=20260715,
        development_case_ids=development,
        validation_case_ids=validation,
        holdout_case_ids=holdout,
        case_candidate_map=case_candidate_map,
        profile_bundle_digest=HASH_A,
        prompt_bundle_digest=HASH_B,
        result_schema_bundle_digest=HASH_A,
        pricing_source_digest=HASH_B,
        temperature=Decimal("0"),
        code_digest=HASH_A,
        price_policy_digest=HASH_B,
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-flash",
        candidate_ids=tuple(item.value for item in EvaluationCandidate),
    )
    run = EvaluationRun(
        run_id=f"run-{suffix}",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    store = PostgresSpecialistEvaluationStore(settings)
    store.register_manifest(manifest)
    store.create_run(run)
    try:
        yield settings, store, manifest, run
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                for table in (
                    "specialist_retention_decisions",
                    "specialist_paired_metrics",
                    "specialist_selected_case_results",
                    "specialist_case_attempts",
                    "specialist_evaluation_runs",
                    "specialist_evaluation_manifests",
                ):
                    cursor.execute(
                        f"DELETE FROM {table} WHERE manifest_id=%s"
                        if table != "specialist_retention_decisions"
                        else """
                            DELETE FROM specialist_retention_decisions
                            WHERE run_id IN (
                                SELECT run_id FROM specialist_evaluation_runs
                                WHERE manifest_id=%s
                            )
                        """,
                        (manifest.manifest_id,),
                    )
            conn.commit()


def _attempt(
    run: EvaluationRun,
    *,
    attempt_id: str,
    number: int,
    subject: EvaluationSubject = EvaluationSubject.AGENT,
    success: bool | None = None,
) -> CaseAttempt:
    return CaseAttempt(
        attempt_id=attempt_id,
        run_id=run.run_id,
        manifest_id=run.manifest_id,
        candidate=run.candidate,
        case_id="live-validation-001",
        split=EvaluationSplit.VALIDATION,
        subject=subject,
        attempt_number=number,
        success=(number == 2) if success is None else success,
        severe_violation=False,
        infrastructure_failure=False,
        latency_ms=Decimal("10"),
        input_tokens=10,
        output_tokens=5,
        cost_cny=Decimal("0.001"),
        result_digest=canonical_json_sha256(None),
        metric_outcomes={
            "action_success_rate": (number == 2) if success is None else success,
            "incident_recovery_rate": (number == 2) if success is None else success,
        },
        gate_results=(
            {
                "schema_valid": True,
                "permission_valid": True,
                "evidence_valid": True,
                "fallback_absent": True,
            }
            if subject is EvaluationSubject.AGENT
            else {}
        ),
    )


def test_postgres_restart_preserves_attempt_history_and_selected_result(evaluation_store) -> None:
    """Store 重建后仍能读取全部历史 Attempt 和唯一正式结果。"""

    settings, store, _manifest, run = evaluation_store
    first = _attempt(run, attempt_id="attempt-1", number=1)
    output = {"decision": "NO_ACTION"}
    second_payload = _attempt(run, attempt_id="attempt-2", number=2).model_dump(mode="json")
    second_payload["output"] = output
    second_payload["result_digest"] = canonical_json_sha256(output)
    second = CaseAttempt.model_validate(second_payload)
    claim = store.claim_next_run("restart-worker", manifest_id=run.manifest_id)
    assert claim is not None
    store.append_attempt(first, claim=claim)
    store.append_attempt(second, claim=claim)
    store.select_attempt(second.attempt_id, claim=claim)

    recovered = PostgresSpecialistEvaluationStore(settings)
    assert [item.attempt_id for item in recovered.list_attempts(run.run_id)] == [
        first.attempt_id,
        second.attempt_id,
    ]
    assert recovered.get_selected_attempt(
        run.run_id, second.case_id, second.subject.value
    ).attempt_id == second.attempt_id
    assert recovered.list_attempts(run.run_id)[1].output["decision"] == "NO_ACTION"


def test_postgres_selection_is_unique_across_runs_for_manifest_candidate(evaluation_store) -> None:
    """第二个 Run 不能为同一 Manifest/Candidate/case/subject 再选正式结果。"""

    _settings, store, manifest, first_run = evaluation_store
    second_run = EvaluationRun(
        run_id=f"{first_run.run_id}-retry",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=first_run.candidate,
    )
    store.create_run(second_run)
    first_claim = store.claim_next_run("first-run-worker", manifest_id=manifest.manifest_id)
    second_claim = store.claim_next_run("second-run-worker", manifest_id=manifest.manifest_id)
    assert first_claim is not None and second_claim is not None
    first = _attempt(first_run, attempt_id="cross-run-1", number=1)
    second = _attempt(second_run, attempt_id="cross-run-2", number=1)
    store.append_attempt(first, claim=first_claim)
    store.append_attempt(second, claim=second_claim)
    store.select_attempt(first.attempt_id, claim=first_claim)
    with pytest.raises(EvaluationInvariantError, match="selected"):
        store.select_attempt(second.attempt_id, claim=second_claim)


def test_postgres_concurrent_selection_allows_only_one_attempt(evaluation_store) -> None:
    """两个连接并发选择同一 case/subject 时，数据库唯一约束只能接受一个。"""

    settings, store, _manifest, run = evaluation_store
    attempts = (
        _attempt(run, attempt_id="attempt-a", number=1),
        _attempt(run, attempt_id="attempt-b", number=2),
    )
    claim = store.claim_next_run("selection-worker", manifest_id=run.manifest_id)
    assert claim is not None
    for attempt in attempts:
        store.append_attempt(attempt, claim=claim)

    def select(attempt_id: str) -> bool:
        try:
            PostgresSpecialistEvaluationStore(settings).select_attempt(
                attempt_id, claim=claim
            )
            return True
        except EvaluationInvariantError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(select, (item.attempt_id for item in attempts)))
    assert sorted(results) == [False, True]


def test_postgres_concurrent_claim_and_attempt_identity_are_single_winner(evaluation_store) -> None:
    """Run 租约和同一 Attempt identity 在两个连接间都只能有一个胜者。"""

    settings, store, _manifest, run = evaluation_store

    with pytest.raises(EvaluationInvariantError, match="claim"):
        store.append_attempt(
            _attempt(run, attempt_id="unclaimed-attempt", number=1)
        )

    def claim(worker_id: str):
        return PostgresSpecialistEvaluationStore(settings).claim_next_run(
            worker_id, manifest_id=run.manifest_id
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(claim, ("worker-a", "worker-b")))
    assert sum(item is not None for item in claims) == 1
    winning_claim = next(item for item in claims if item is not None)

    attempts = (
        _attempt(run, attempt_id="concurrent-attempt-a", number=1),
        _attempt(run, attempt_id="concurrent-attempt-b", number=1),
    )

    def append(attempt: CaseAttempt) -> bool:
        try:
            PostgresSpecialistEvaluationStore(settings).append_attempt(
                attempt, claim=winning_claim
            )
            return True
        except EvaluationInvariantError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(append, attempts)) == [False, True]
    assert len(store.list_attempts(run.run_id)) == 1

    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE specialist_evaluation_runs SET lease_until=now()-interval '1 second' WHERE run_id=%s;",
                (run.run_id,),
            )
        conn.commit()
    with pytest.raises(EvaluationInvariantError, match="claim"):
        store.append_attempt(
            _attempt(run, attempt_id="late-attempt", number=2), claim=winning_claim
        )

    renewed_claim = store.claim_next_run(
        "worker-renewed", manifest_id=run.manifest_id
    )
    assert renewed_claim is not None
    assert renewed_claim.claim_version > winning_claim.claim_version
    stored_agent = store.list_attempts(run.run_id)[0]
    baseline = _attempt(
        run,
        attempt_id="claim-baseline",
        number=1,
        subject=EvaluationSubject.BASELINE,
        success=False,
    )
    store.append_attempt(baseline, claim=renewed_claim)
    store.select_attempt(baseline.attempt_id, claim=renewed_claim)
    store.select_attempt(stored_agent.attempt_id, claim=renewed_claim)
    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(
            BinaryPair(
                case_id=baseline.case_id,
                baseline_success=baseline.success,
                agent_success=stored_agent.success,
            ),
        ),
    )
    with pytest.raises(EvaluationInvariantError, match="claim"):
        store.save_paired_metric(
            run.run_id, EvaluationSplit.VALIDATION, metric, claim=winning_claim
        )
    store.save_paired_metric(
        run.run_id, EvaluationSplit.VALIDATION, metric, claim=renewed_claim
    )
    decision = RetentionDecisionRecord(
        decision_id="claim-decision",
        run_id=run.run_id,
        candidate=run.candidate,
        decision=RetentionDecision.REJECTED,
        reason_code="METRIC_THRESHOLD_FAILED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(run.run_id),
        completed_validation_cases=1,
        hard_gates_passed=True,
    )
    with pytest.raises(EvaluationInvariantError, match="claim"):
        store.save_retention_decision(decision, claim=winning_claim)
    store.save_retention_decision(decision, claim=renewed_claim)


def test_postgres_metric_and_retention_decision_are_unique(evaluation_store) -> None:
    """正式聚合和去留决策都不能被后写覆盖。"""

    _settings, store, _manifest, run = evaluation_store
    baseline = _attempt(
        run,
        attempt_id="metric-baseline",
        number=1,
        subject=EvaluationSubject.BASELINE,
        success=False,
    )
    agent = _attempt(run, attempt_id="metric-agent", number=1, success=True)
    claim = store.claim_next_run("metric-worker", manifest_id=run.manifest_id)
    assert claim is not None
    store.append_attempt(baseline, claim=claim)
    store.append_attempt(agent, claim=claim)
    store.select_attempt(baseline.attempt_id, claim=claim)
    store.select_attempt(agent.attempt_id, claim=claim)
    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(
            BinaryPair(
                case_id=baseline.case_id,
                baseline_success=baseline.success,
                agent_success=agent.success,
            ),
        ),
    )
    def save_metric(_index: int) -> bool:
        try:
            PostgresSpecialistEvaluationStore(_settings).save_paired_metric(
                run.run_id, EvaluationSplit.VALIDATION, metric, claim=claim
            )
            return True
        except EvaluationInvariantError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save_metric, (1, 2))) == [False, True]

    decision = RetentionDecisionRecord(
        decision_id="decision-1",
        run_id=run.run_id,
        candidate=run.candidate,
        decision=RetentionDecision.REJECTED,
        reason_code="METRIC_THRESHOLD_FAILED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(run.run_id),
        completed_validation_cases=1,
        hard_gates_passed=True,
    )
    def save_decision(_index: int) -> bool:
        try:
            PostgresSpecialistEvaluationStore(_settings).save_retention_decision(
                decision, claim=claim
            )
            return True
        except EvaluationInvariantError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save_decision, (1, 2))) == [False, True]
    assert store.get_retention_decision(run.run_id) == decision
    with psycopg.connect(**_settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, lease_owner, lease_until FROM specialist_evaluation_runs WHERE run_id=%s;",
                (run.run_id,),
            )
            status, lease_owner, lease_until = cursor.fetchone()
    assert (status, lease_owner, lease_until) == ("COMPLETED", None, None)

    recovery_metric = aggregate_binary_pairs(
        metric_id="incident_recovery_rate",
        pairs=(
            BinaryPair(
                case_id=baseline.case_id,
                baseline_success=baseline.metric_outcomes["incident_recovery_rate"],
                agent_success=agent.metric_outcomes["incident_recovery_rate"],
            ),
        ),
    )
    with pytest.raises(EvaluationInvariantError, match="RUNNING"):
        store.save_paired_metric(
            run.run_id,
            EvaluationSplit.VALIDATION,
            recovery_metric,
            claim=claim,
        )


def test_postgres_manifest_row_is_immutable(evaluation_store) -> None:
    """正式 Manifest 一旦注册，数据库层也不得在摘要不变时漂移模型身份。"""

    settings, _store, manifest, _run = evaluation_store
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE specialist_evaluation_manifests SET model_id='tampered' WHERE manifest_id=%s;",
                    (manifest.manifest_id,),
                )
            conn.commit()


def test_postgres_candidate_decision_cancels_sibling_run(evaluation_store) -> None:
    """同一 Manifest/Candidate 的首个结论必须原子取消其他 Run。"""

    _settings, store, manifest, first_run = evaluation_store
    sibling = EvaluationRun(
        run_id=f"{first_run.run_id}-sibling",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=first_run.candidate,
    )
    store.create_run(sibling)
    first_claim = store.claim_next_run(
        "candidate-first-worker", manifest_id=manifest.manifest_id
    )
    sibling_claim = store.claim_next_run(
        "candidate-sibling-worker", manifest_id=manifest.manifest_id
    )
    assert first_claim is not None and sibling_claim is not None
    decision = RetentionDecisionRecord(
        decision_id=f"{first_run.run_id}-candidate-decision",
        run_id=first_run.run_id,
        candidate=first_run.candidate,
        decision=RetentionDecision.INCONCLUSIVE,
        reason_code="INFRASTRUCTURE_UNAVAILABLE",
        external_evidence_sufficient=False,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(first_run.run_id),
    )
    store.save_retention_decision(decision, claim=first_claim)

    with psycopg.connect(**_settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM specialist_evaluation_runs WHERE run_id=%s;",
                (sibling.run_id,),
            )
            assert cursor.fetchone()[0] == "CANCELLED"
    with pytest.raises(EvaluationInvariantError, match="RUNNING"):
        store.append_attempt(
            CaseAttempt.model_validate(
                {
                    **_attempt(
                        first_run,
                        attempt_id="candidate-sibling-late-attempt",
                        number=1,
                    ).model_dump(mode="json"),
                    "run_id": sibling.run_id,
                }
            ),
            claim=sibling_claim,
        )
    with pytest.raises(EvaluationInvariantError, match="candidate.*decision"):
        store.create_run(
            EvaluationRun(
                run_id=f"{first_run.run_id}-after-decision",
                manifest_id=manifest.manifest_id,
                manifest_digest=manifest.manifest_digest,
                candidate=first_run.candidate,
            )
        )


def test_postgres_rejects_attempt_identity_inconsistent_with_run(evaluation_store) -> None:
    """绕过 Python Store 直接写错 candidate 时，复合外键仍必须拒绝矛盾事实。"""

    settings, _store, manifest, run = evaluation_store
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO specialist_case_attempts (
                        attempt_id, run_id, manifest_id, candidate_id, case_id, split,
                        subject, attempt_number, success, severe_violation,
                        infrastructure_failure, latency_ms, input_tokens, output_tokens,
                        cost_cny, result_digest, metric_outcomes, gate_results, result_snapshot
                    ) VALUES (%s,%s,%s,'PLANNER','forged-case','VALIDATION','AGENT',1,
                              FALSE,FALSE,FALSE,1,0,0,0,%s,
                              '{"action_success_rate": false}'::jsonb,
                              '{"schema_valid": true, "permission_valid": true,
                                "evidence_valid": true, "fallback_absent": true}'::jsonb,
                              NULL);
                    """,
                    ("forged-attempt", run.run_id, manifest.manifest_id, HASH_A),
                )
            conn.commit()
