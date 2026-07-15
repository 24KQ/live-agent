"""Phase 13 Evaluation Store 的内存权威实现与唯一性门禁。"""

from __future__ import annotations

from types import MappingProxyType
from pathlib import Path
from datetime import datetime, timedelta, timezone
from functools import wraps
from threading import RLock
from typing import Any

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.specialist_evaluation.models import (
    CaseAttempt,
    EvaluationManifest,
    EvaluationRun,
    EvaluationRunClaim,
    EvaluationSplit,
    PairedMetric,
    RetentionDecisionRecord,
    EvaluationCandidate,
    EvaluationSubject,
    RetentionDecision,
    canonical_json_sha256,
    _plain_json,
)


class EvaluationInvariantError(RuntimeError):
    """评估事实、唯一性或跨对象身份链违反不变量。"""


def _assert_retained_evidence(
    *,
    validation_attempts: tuple[CaseAttempt, ...],
    holdout_attempts: tuple[CaseAttempt, ...],
    validation_metrics: tuple[PairedMetric, ...],
    holdout_metrics: tuple[PairedMetric, ...],
) -> None:
    """确认 RETAINED 绑定完整 40/20 对结果和覆盖全部 case 的正式指标。"""

    def paired_cases(attempts: tuple[CaseAttempt, ...], expected: int) -> set[str]:
        by_case: dict[str, set[EvaluationSubject]] = {}
        for attempt in attempts:
            if attempt.infrastructure_failure or attempt.severe_violation:
                raise EvaluationInvariantError("RETAINED contains invalid selected evidence")
            by_case.setdefault(attempt.case_id, set()).add(attempt.subject)
        complete = {
            case_id
            for case_id, subjects in by_case.items()
            if subjects == {EvaluationSubject.BASELINE, EvaluationSubject.AGENT}
        }
        if len(complete) != expected or len(by_case) != expected:
            raise EvaluationInvariantError("RETAINED lacks complete selected evidence")
        return complete

    validation_cases = paired_cases(validation_attempts, 40)
    holdout_cases = paired_cases(holdout_attempts, 20)
    if not validation_metrics or not holdout_metrics:
        raise EvaluationInvariantError("RETAINED lacks formal paired metrics")
    if any(set(metric.case_ids) != validation_cases for metric in validation_metrics):
        raise EvaluationInvariantError("RETAINED validation metrics do not cover all selected cases")
    if any(set(metric.case_ids) != holdout_cases for metric in holdout_metrics):
        raise EvaluationInvariantError("RETAINED holdout metrics do not cover all selected cases")


def _formal_agent_summary(
    attempts: tuple[CaseAttempt, ...],
) -> tuple[int, bool, int, int]:
    """从正式配对 Attempt 重算严重违规、共同硬门和两个 split 完成数。"""

    formal_agent = tuple(
        attempt
        for attempt in attempts
        if attempt.subject is EvaluationSubject.AGENT
        and attempt.split in {EvaluationSplit.VALIDATION, EvaluationSplit.HOLDOUT}
    )
    severe_count = sum(attempt.severe_violation for attempt in formal_agent)
    hard_gates_passed = bool(formal_agent) and all(
        all(_plain_json(attempt.gate_results).values()) for attempt in formal_agent
    )
    paired_cases: dict[EvaluationSplit, dict[str, set[EvaluationSubject]]] = {
        EvaluationSplit.VALIDATION: {},
        EvaluationSplit.HOLDOUT: {},
    }
    for attempt in attempts:
        if attempt.split not in paired_cases:
            continue
        paired_cases[attempt.split].setdefault(attempt.case_id, set()).add(attempt.subject)
    complete_counts = {
        split: sum(
            subjects == {EvaluationSubject.BASELINE, EvaluationSubject.AGENT}
            for subjects in cases.values()
        )
        for split, cases in paired_cases.items()
    }
    return (
        severe_count,
        hard_gates_passed,
        complete_counts[EvaluationSplit.VALIDATION],
        complete_counts[EvaluationSplit.HOLDOUT],
    )


def _metric_outcome(attempt: CaseAttempt, metric_id: str) -> bool:
    """读取已冻结的单指标事实；缺失指标必须 fail-closed。"""

    outcomes = _plain_json(attempt.metric_outcomes)
    if metric_id not in outcomes:
        raise EvaluationInvariantError("attempt lacks requested metric outcome")
    return bool(outcomes[metric_id])


def _locked(method):
    """让内存替身与 PostgreSQL 唯一约束具有等价的进程内原子边界。"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class InMemorySpecialistEvaluationStore:
    """用锁内唯一索引模拟生产 Store 的 selected/attempt 关系约束。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._manifests: dict[str, EvaluationManifest] = {}
        self._runs: dict[str, EvaluationRun] = {}
        self._attempts: dict[str, CaseAttempt] = {}
        self._attempt_identity: dict[tuple[str, str, str, int], str] = {}
        self._selected: dict[tuple[str, str, str, str], str] = {}
        self._metrics: dict[tuple[str, EvaluationSplit, str], PairedMetric] = {}
        self._decisions: dict[str, RetentionDecisionRecord] = {}
        self._decision_identity: dict[tuple[str, str], str] = {}
        self._claims: dict[str, EvaluationRunClaim] = {}

    @_locked
    def register_manifest(self, manifest: EvaluationManifest) -> EvaluationManifest:
        existing = self._manifests.get(manifest.manifest_id)
        if existing is not None and existing != manifest:
            raise EvaluationInvariantError("manifest identity conflict")
        self._manifests[manifest.manifest_id] = manifest
        return manifest

    @_locked
    def create_run(self, run: EvaluationRun) -> EvaluationRun:
        if run.status != "RUNNING":
            raise EvaluationInvariantError("new evaluation run must start RUNNING")
        manifest = self._manifests.get(run.manifest_id)
        if manifest is None or manifest.manifest_digest != run.manifest_digest:
            raise EvaluationInvariantError("run manifest does not match registered manifest")
        if run.candidate.value not in manifest.candidate_ids:
            raise EvaluationInvariantError("run candidate is not declared by manifest")
        if (run.manifest_id, run.candidate.value) in self._decision_identity:
            raise EvaluationInvariantError("candidate retention decision already exists")
        existing = self._runs.get(run.run_id)
        if existing is not None and existing != run:
            raise EvaluationInvariantError("run identity conflict")
        self._runs[run.run_id] = run
        return run

    @_locked
    def append_attempt(
        self, attempt: CaseAttempt, claim: EvaluationRunClaim | None = None
    ) -> CaseAttempt:
        run = self._runs.get(attempt.run_id)
        if run is None:
            raise EvaluationInvariantError("attempt run does not exist")
        if run.manifest_id != attempt.manifest_id or run.candidate is not attempt.candidate:
            raise EvaluationInvariantError("attempt manifest or candidate mismatch")
        manifest = self._manifests[run.manifest_id]
        split_cases = {
            EvaluationSplit.DEVELOPMENT: manifest.development_case_ids,
            EvaluationSplit.VALIDATION: manifest.validation_case_ids,
            EvaluationSplit.HOLDOUT: manifest.holdout_case_ids,
        }
        if attempt.case_id not in split_cases[attempt.split]:
            raise EvaluationInvariantError("attempt case does not belong to declared split")
        if _plain_json(manifest.case_candidate_map)[attempt.case_id] != run.candidate.value:
            raise EvaluationInvariantError("attempt case does not belong to run candidate")
        self._assert_memory_claim(attempt.run_id, claim)
        identity = (attempt.run_id, attempt.case_id, attempt.subject.value, attempt.attempt_number)
        existing_id = self._attempt_identity.get(identity)
        if existing_id is not None and existing_id != attempt.attempt_id:
            raise EvaluationInvariantError("attempt identity conflict")
        existing = self._attempts.get(attempt.attempt_id)
        if existing is not None and existing != attempt:
            raise EvaluationInvariantError("attempt ID conflict")
        self._attempt_identity[identity] = attempt.attempt_id
        self._attempts[attempt.attempt_id] = attempt
        return attempt

    @_locked
    def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int = 60,
        manifest_id: str | None = None,
    ) -> EvaluationRunClaim | None:
        if not worker_id or lease_seconds <= 0 or lease_seconds > 3600:
            raise EvaluationInvariantError("claim requires worker and lease within 1..3600 seconds")
        now = datetime.now(timezone.utc)
        for run in sorted(self._runs.values(), key=lambda item: item.run_id):
            if manifest_id is not None and run.manifest_id != manifest_id:
                continue
            existing = self._claims.get(run.run_id)
            if run.status != "RUNNING" or (existing is not None and existing.lease_until > now):
                continue
            claim = EvaluationRunClaim(
                run_id=run.run_id,
                worker_id=worker_id,
                lease_until=now + timedelta(seconds=lease_seconds),
                claim_version=1 if existing is None else existing.claim_version + 1,
            )
            self._claims[run.run_id] = claim
            return claim
        return None

    @_locked
    def list_attempts(self, run_id: str) -> tuple[CaseAttempt, ...]:
        return tuple(
            sorted(
                (item for item in self._attempts.values() if item.run_id == run_id),
                key=lambda item: (item.attempt_number, item.attempt_id),
            )
        )

    @_locked
    def select_attempt(
        self, attempt_id: str, claim: EvaluationRunClaim | None = None
    ) -> CaseAttempt:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise EvaluationInvariantError("attempt does not exist")
        if attempt.infrastructure_failure:
            raise EvaluationInvariantError("infrastructure failure cannot be selected")
        self._assert_memory_claim(attempt.run_id, claim)
        key = (
            attempt.manifest_id,
            attempt.candidate.value,
            attempt.case_id,
            attempt.subject.value,
        )
        existing = self._selected.get(key)
        if existing is not None and existing != attempt_id:
            raise EvaluationInvariantError("selected result already exists")
        self._selected[key] = attempt_id
        return attempt

    @_locked
    def get_selected_attempt(self, run_id: str, case_id: str, subject: str) -> CaseAttempt:
        run = self._runs.get(run_id)
        if run is None:
            raise EvaluationInvariantError("run does not exist")
        attempt_id = self._selected.get(
            (run.manifest_id, run.candidate.value, case_id, subject)
        )
        if attempt_id is None:
            raise EvaluationInvariantError("selected result does not exist")
        return self._attempts[attempt_id]

    @_locked
    def save_paired_metric(
        self,
        run_id: str,
        split: EvaluationSplit,
        metric: PairedMetric,
        claim: EvaluationRunClaim | None = None,
    ) -> PairedMetric:
        if run_id not in self._runs:
            raise EvaluationInvariantError("metric run does not exist")
        run = self._runs[run_id]
        self._assert_memory_claim(run_id, claim)
        selected = [
            self._attempts[attempt_id]
            for (manifest_id, candidate, _case_id, _subject), attempt_id in self._selected.items()
            if manifest_id == run.manifest_id and candidate == run.candidate.value
        ]
        selected_by_case = {(item.case_id, item.subject): item for item in selected if item.split is split}
        expected_cases = {
            case_id
            for case_id in metric.case_ids
            if (case_id, EvaluationSubject.BASELINE) in selected_by_case
            and (case_id, EvaluationSubject.AGENT) in selected_by_case
        }
        if expected_cases != set(metric.case_ids):
            raise EvaluationInvariantError("metric case set does not match selected paired results")
        if sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
            )
            for case_id in metric.case_ids
        ) != metric.baseline_success_count:
            raise EvaluationInvariantError("metric baseline count does not match selected results")
        if sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
            )
            for case_id in metric.case_ids
        ) != metric.agent_success_count:
            raise EvaluationInvariantError("metric agent count does not match selected results")
        wins = sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
            )
            and not _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
            )
            for case_id in metric.case_ids
        )
        losses = sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
            )
            and not _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
            )
            for case_id in metric.case_ids
        )
        severe = sum(
            selected_by_case[(case_id, EvaluationSubject.AGENT)].severe_violation
            for case_id in metric.case_ids
        )
        if (wins, losses, metric.sample_count - wins - losses, severe) != (
            metric.paired_wins, metric.paired_losses, metric.tied, metric.severe_violation_count
        ):
            raise EvaluationInvariantError("metric paired facts do not match selected results")
        facts = [
            {
                "case_id": case_id,
                "baseline_success": _metric_outcome(
                    selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
                ),
                "agent_success": _metric_outcome(
                    selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
                ),
                "agent_severe_violation": selected_by_case[(case_id, EvaluationSubject.AGENT)].severe_violation,
            }
            for case_id in sorted(metric.case_ids)
        ]
        if metric.metric_facts_digest != canonical_json_sha256(facts):
            raise EvaluationInvariantError("metric facts digest does not match selected results")
        from src.specialist_evaluation.comparison import BinaryPair, aggregate_binary_pairs

        expected_metric = aggregate_binary_pairs(
            metric_id=metric.metric_id,
            pairs=tuple(
                BinaryPair(
                    case_id=case_id,
                    baseline_success=_metric_outcome(
                        selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
                    ),
                    agent_success=_metric_outcome(
                        selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
                    ),
                    agent_severe_violation=selected_by_case[(case_id, EvaluationSubject.AGENT)].severe_violation,
                )
                for case_id in sorted(metric.case_ids)
            ),
        )
        if expected_metric != metric:
            raise EvaluationInvariantError("metric rates or Wilson intervals do not match selected results")
        key = (run_id, split, metric.metric_id)
        if key in self._metrics:
            raise EvaluationInvariantError("metric already exists")
        self._metrics[key] = metric
        return metric

    @_locked
    def save_retention_decision(
        self,
        decision: RetentionDecisionRecord,
        claim: EvaluationRunClaim | None = None,
    ) -> RetentionDecisionRecord:
        if decision.run_id not in self._runs:
            raise EvaluationInvariantError("decision run does not exist")
        self._assert_memory_claim(decision.run_id, claim)
        if self._runs[decision.run_id].candidate is not decision.candidate:
            raise EvaluationInvariantError("decision candidate mismatch")
        if decision.metrics_digest != self.metrics_digest(decision.run_id):
            raise EvaluationInvariantError("decision metrics digest mismatch")
        run = self._runs[decision.run_id]
        selected = tuple(
            self._attempts[attempt_id]
            for (manifest_id, candidate, _case_id, _subject), attempt_id in self._selected.items()
            if manifest_id == run.manifest_id and candidate == run.candidate.value
        )
        if decision.decision is RetentionDecision.RETAINED:
            _assert_retained_evidence(
                validation_attempts=tuple(
                    item for item in selected if item.split is EvaluationSplit.VALIDATION
                ),
                holdout_attempts=tuple(
                    item for item in selected if item.split is EvaluationSplit.HOLDOUT
                ),
                validation_metrics=tuple(
                    metric for (metric_run, split, _metric_id), metric in self._metrics.items()
                    if metric_run == decision.run_id and split is EvaluationSplit.VALIDATION
                ),
                holdout_metrics=tuple(
                    metric for (metric_run, split, _metric_id), metric in self._metrics.items()
                    if metric_run == decision.run_id and split is EvaluationSplit.HOLDOUT
                ),
            )
        severe_count, hard_gates_passed, validation_count, holdout_count = (
            _formal_agent_summary(selected)
        )
        if decision.severe_violation_count != severe_count:
            raise EvaluationInvariantError("decision severe violation count mismatch")
        if decision.hard_gates_passed != hard_gates_passed:
            raise EvaluationInvariantError("decision hard gate summary mismatch")
        if (
            decision.completed_validation_cases != validation_count
            or decision.completed_holdout_cases != holdout_count
        ):
            raise EvaluationInvariantError("decision completed case counts mismatch")
        if decision.run_id in self._decisions:
            raise EvaluationInvariantError("retention decision already exists")
        identity = (run.manifest_id, run.candidate.value)
        if identity in self._decision_identity:
            raise EvaluationInvariantError("candidate retention decision already exists")
        self._decisions[decision.run_id] = decision
        self._decision_identity[identity] = decision.run_id
        for sibling_id, sibling in tuple(self._runs.items()):
            if (
                sibling.manifest_id == run.manifest_id
                and sibling.candidate is run.candidate
                and sibling.status == "RUNNING"
            ):
                terminal_status = "COMPLETED" if sibling_id == decision.run_id else "CANCELLED"
                self._runs[sibling_id] = EvaluationRun.model_validate(
                    {**sibling.model_dump(mode="json"), "status": terminal_status}
                )
                self._claims.pop(sibling_id, None)
        return decision

    @_locked
    def get_retention_decision(self, run_id: str) -> RetentionDecisionRecord:
        return self._decisions[run_id]

    @_locked
    def metrics_digest(self, run_id: str) -> str:
        metrics = [
            metric.model_dump(mode="json")
            for (metric_run_id, split, metric_id), metric in sorted(
                self._metrics.items(), key=lambda item: (item[0][0], item[0][1].value, item[0][2])
            )
            if metric_run_id == run_id
        ]
        return canonical_json_sha256(metrics)

    @_locked
    def snapshot(self) -> MappingProxyType:
        """返回只读事实快照，测试和报告不得直接修改 Store 内部索引。"""

        return MappingProxyType(
            {
                "manifests": tuple(self._manifests.values()),
                "runs": tuple(self._runs.values()),
                "attempts": tuple(self._attempts.values()),
                "selected": MappingProxyType(dict(self._selected)),
                "metrics": MappingProxyType(dict(self._metrics)),
                "decisions": tuple(self._decisions.values()),
            }
        )

    def _assert_memory_claim(
        self, run_id: str, claim: EvaluationRunClaim | None
    ) -> None:
        run = self._runs.get(run_id)
        if run is None or run.status != "RUNNING":
            raise EvaluationInvariantError("formal facts require a RUNNING run")
        current = self._claims.get(run_id)
        if current is None or claim is None:
            raise EvaluationInvariantError("formal fact write requires an active claim")
        if claim != current or current.lease_until <= datetime.now(timezone.utc):
            raise EvaluationInvariantError("claim is stale or owned by another worker")

class PostgresSpecialistEvaluationStore:
    """以 PostgreSQL 唯一约束保存不可变 Attempt、正式选择和去留事实。"""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def register_manifest(self, manifest: EvaluationManifest) -> EvaluationManifest:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO specialist_evaluation_manifests (
                        manifest_id, manifest_version, manifest_digest, dataset_digest,
                        schema_digest, generator_digest, seed, development_case_ids,
                        validation_case_ids, holdout_case_ids, case_candidate_map, profile_bundle_digest,
                        prompt_bundle_digest, result_schema_bundle_digest,
                        pricing_source_digest, temperature, code_digest,
                        price_policy_digest, endpoint_host, model_id, candidate_ids
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (manifest_id) DO NOTHING;
                    """,
                    (
                        manifest.manifest_id, manifest.manifest_version, manifest.manifest_digest,
                        manifest.dataset_digest, manifest.schema_digest, manifest.generator_digest,
                        manifest.seed, Jsonb(list(manifest.development_case_ids)),
                        Jsonb(list(manifest.validation_case_ids)), Jsonb(list(manifest.holdout_case_ids)),
                        Jsonb(_plain_json(manifest.case_candidate_map)),
                        manifest.profile_bundle_digest, manifest.prompt_bundle_digest,
                        manifest.result_schema_bundle_digest, manifest.pricing_source_digest,
                        manifest.temperature, manifest.code_digest,
                        manifest.price_policy_digest, manifest.endpoint_host, manifest.model_id,
                        Jsonb(list(manifest.candidate_ids)),
                    ),
                )
                cursor.execute(
                    "SELECT * FROM specialist_evaluation_manifests WHERE manifest_id=%s;",
                    (manifest.manifest_id,),
                )
                row = cursor.fetchone()
            conn.commit()
        loaded = self._manifest_from_row(row)
        if loaded != manifest:
            raise EvaluationInvariantError("manifest identity conflict")
        return loaded

    def create_run(self, run: EvaluationRun) -> EvaluationRun:
        if run.status != "RUNNING":
            raise EvaluationInvariantError("new evaluation run must start RUNNING")
        manifest = self._load_manifest(run.manifest_id)
        if manifest.manifest_digest != run.manifest_digest:
            raise EvaluationInvariantError("run manifest does not match registered manifest")
        if run.candidate.value not in manifest.candidate_ids:
            raise EvaluationInvariantError("run candidate is not declared by manifest")
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                # Manifest 行是候选生命周期的稳定锁对象。Run 尚不存在时无法锁 Run，
                # 因此 create 与 decision 都先锁 Manifest，避免结论提交后重新开 Run。
                cursor.execute(
                    "SELECT manifest_id FROM specialist_evaluation_manifests WHERE manifest_id=%s FOR UPDATE;",
                    (run.manifest_id,),
                )
                if cursor.fetchone() is None:
                    raise EvaluationInvariantError("manifest does not exist")
                cursor.execute(
                    """
                    SELECT decision_id FROM specialist_retention_decisions
                    WHERE manifest_id=%s AND candidate_id=%s;
                    """,
                    (run.manifest_id, run.candidate.value),
                )
                if cursor.fetchone() is not None:
                    raise EvaluationInvariantError("candidate retention decision already exists")
                cursor.execute(
                    """
                    INSERT INTO specialist_evaluation_runs (
                        run_id, manifest_id, manifest_digest, candidate_id, status
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id) DO NOTHING;
                    """,
                    (run.run_id, run.manifest_id, run.manifest_digest, run.candidate.value, run.status),
                )
                cursor.execute("SELECT * FROM specialist_evaluation_runs WHERE run_id=%s;", (run.run_id,))
                row = cursor.fetchone()
            conn.commit()
        loaded = self._run_from_row(row)
        if loaded != run:
            raise EvaluationInvariantError("run identity conflict")
        return loaded

    def append_attempt(
        self, attempt: CaseAttempt, claim: EvaluationRunClaim | None = None
    ) -> CaseAttempt:
        run = self._load_run(attempt.run_id)
        if run.manifest_id != attempt.manifest_id or run.candidate is not attempt.candidate:
            raise EvaluationInvariantError("attempt manifest or candidate mismatch")
        manifest = self._load_manifest(run.manifest_id)
        split_cases = {
            EvaluationSplit.DEVELOPMENT: manifest.development_case_ids,
            EvaluationSplit.VALIDATION: manifest.validation_case_ids,
            EvaluationSplit.HOLDOUT: manifest.holdout_case_ids,
        }
        if attempt.case_id not in split_cases[attempt.split]:
            raise EvaluationInvariantError("attempt case does not belong to declared split")
        if _plain_json(manifest.case_candidate_map)[attempt.case_id] != run.candidate.value:
            raise EvaluationInvariantError("attempt case does not belong to run candidate")
        try:
            with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
                with conn.cursor() as cursor:
                    self._assert_claim_cursor(cursor, attempt.run_id, claim)
                    cursor.execute(
                    """
                    INSERT INTO specialist_case_attempts (
                        attempt_id, run_id, manifest_id, candidate_id, case_id, split,
                        subject, attempt_number, success, severe_violation,
                        infrastructure_failure, latency_ms, input_tokens, output_tokens,
                        cost_cny, result_digest, metric_outcomes, gate_results, result_snapshot
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (run_id, case_id, subject, attempt_number) DO NOTHING;
                    """,
                    (
                        attempt.attempt_id, attempt.run_id, attempt.manifest_id,
                        attempt.candidate.value, attempt.case_id, attempt.split.value,
                        attempt.subject.value, attempt.attempt_number, attempt.success,
                        attempt.severe_violation, attempt.infrastructure_failure,
                        attempt.latency_ms, attempt.input_tokens, attempt.output_tokens,
                        attempt.cost_cny, attempt.result_digest,
                        Jsonb(_plain_json(attempt.metric_outcomes)),
                        Jsonb(_plain_json(attempt.gate_results)),
                        None if attempt.output is None else Jsonb(_plain_json(attempt.output)),
                    ),
                    )
                    cursor.execute(
                        """
                        SELECT * FROM specialist_case_attempts
                        WHERE run_id=%s AND case_id=%s AND subject=%s AND attempt_number=%s;
                        """,
                        (attempt.run_id, attempt.case_id, attempt.subject.value, attempt.attempt_number),
                    )
                    row = cursor.fetchone()
                conn.commit()
        except (pg_errors.IntegrityError, pg_errors.DataError) as error:
            raise EvaluationInvariantError("attempt identity conflict") from error
        loaded = self._attempt_from_row(row)
        if loaded != attempt:
            raise EvaluationInvariantError("attempt identity conflict")
        return loaded

    def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int = 60,
        manifest_id: str | None = None,
    ) -> EvaluationRunClaim | None:
        """按可选冻结 Manifest 范围，用 SKIP LOCKED 原子领取 EvaluationRun。"""

        if not worker_id or lease_seconds <= 0 or lease_seconds > 3600:
            raise EvaluationInvariantError("claim requires worker and lease within 1..3600 seconds")
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT run_id FROM specialist_evaluation_runs
                        WHERE status='RUNNING'
                          AND (lease_owner IS NULL OR lease_until <= now())
                          AND (%s::text IS NULL OR manifest_id=%s::text)
                        ORDER BY created_at, run_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE specialist_evaluation_runs r
                    SET lease_owner=%s,
                        lease_until=now() + (%s || ' seconds')::interval,
                        claim_version=claim_version+1
                    FROM candidate
                    WHERE r.run_id=candidate.run_id
                    RETURNING r.run_id, r.lease_owner, r.lease_until, r.claim_version;
                    """,
                    (manifest_id, manifest_id, worker_id, lease_seconds),
                )
                row = cursor.fetchone()
            conn.commit()
        if row is None:
            return None
        return EvaluationRunClaim(
            run_id=row["run_id"], worker_id=row["lease_owner"],
            lease_until=row["lease_until"], claim_version=int(row["claim_version"]),
        )

    def list_attempts(self, run_id: str) -> tuple[CaseAttempt, ...]:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM specialist_case_attempts WHERE run_id=%s ORDER BY attempt_number, attempt_id;",
                    (run_id,),
                )
                return tuple(self._attempt_from_row(row) for row in cursor.fetchall())

    def select_attempt(
        self, attempt_id: str, claim: EvaluationRunClaim | None = None
    ) -> CaseAttempt:
        attempt = self._load_attempt(attempt_id)
        if attempt.infrastructure_failure:
            raise EvaluationInvariantError("infrastructure failure cannot be selected")
        try:
            with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
                with conn.cursor() as cursor:
                    self._assert_claim_cursor(cursor, attempt.run_id, claim)
                    cursor.execute(
                        """
                        INSERT INTO specialist_selected_case_results (
                            manifest_id, candidate_id, run_id, case_id, subject, attempt_id,
                            infrastructure_failure
                        ) VALUES (%s,%s,%s,%s,%s,%s,FALSE)
                        ON CONFLICT (manifest_id, candidate_id, case_id, subject) DO NOTHING
                        RETURNING attempt_id;
                        """,
                        (
                            attempt.manifest_id, attempt.candidate.value, attempt.run_id,
                            attempt.case_id, attempt.subject.value, attempt.attempt_id,
                        ),
                    )
                    inserted = cursor.fetchone()
                    if inserted is None:
                        cursor.execute(
                            """
                            SELECT attempt_id FROM specialist_selected_case_results
                            WHERE manifest_id=%s AND candidate_id=%s AND case_id=%s AND subject=%s;
                            """,
                            (
                                attempt.manifest_id, attempt.candidate.value,
                                attempt.case_id, attempt.subject.value,
                            ),
                        )
                        existing = cursor.fetchone()
                conn.commit()
        except pg_errors.IntegrityError as error:
            raise EvaluationInvariantError("selected result violates attempt identity") from error
        if inserted is None and existing["attempt_id"] != attempt_id:
            raise EvaluationInvariantError("selected result already exists")
        return attempt

    def get_selected_attempt(self, run_id: str, case_id: str, subject: str) -> CaseAttempt:
        run = self._load_run(run_id)
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT a.* FROM specialist_selected_case_results s
                    JOIN specialist_case_attempts a ON a.attempt_id=s.attempt_id
                    WHERE s.manifest_id=%s AND s.candidate_id=%s
                      AND s.case_id=%s AND s.subject=%s;
                    """,
                    (run.manifest_id, run.candidate.value, case_id, subject),
                )
                row = cursor.fetchone()
        if row is None:
            raise EvaluationInvariantError("selected result does not exist")
        return self._attempt_from_row(row)

    def save_paired_metric(
        self,
        run_id: str,
        split: EvaluationSplit,
        metric: PairedMetric,
        claim: EvaluationRunClaim | None = None,
    ) -> PairedMetric:
        run = self._load_run(run_id)
        try:
            with psycopg.connect(
                **self._settings.postgres_connection_kwargs, row_factory=dict_row
            ) as conn:
                with conn.cursor() as cursor:
                    self._assert_claim_cursor(cursor, run_id, claim)
                    # selected 读取、指标重算和 INSERT 共用 Run 行锁；其他正式写入也先
                    # 锁同一 Run，因此指标不会绑定到读取后已经变化的证据集合。
                    cursor.execute(
                        """
                        SELECT a.* FROM specialist_selected_case_results s
                        JOIN specialist_case_attempts a ON a.attempt_id=s.attempt_id
                        WHERE s.manifest_id=%s AND s.candidate_id=%s AND a.split=%s
                        ORDER BY a.case_id, a.subject;
                        """,
                        (run.manifest_id, run.candidate.value, split.value),
                    )
                    selected = tuple(
                        self._attempt_from_row(row) for row in cursor.fetchall()
                    )
                    self._validate_metric(metric, selected)
                    cursor.execute(
                        """
                        INSERT INTO specialist_paired_metrics (
                            manifest_id, run_id, candidate_id, split, metric_id, case_ids, sample_count,
                            baseline_success_count, agent_success_count,
                            baseline_rate, agent_rate, delta_percentage_points,
                            paired_wins, paired_losses, tied, severe_violation_count,
                            baseline_wilson_low, baseline_wilson_high,
                            agent_wilson_low, agent_wilson_high, metric_facts_digest
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                        """,
                        (
                            run.manifest_id, run_id, run.candidate.value, split.value, metric.metric_id,
                            Jsonb(list(metric.case_ids)), metric.sample_count,
                            metric.baseline_success_count, metric.agent_success_count,
                            metric.baseline_rate, metric.agent_rate,
                            metric.delta_percentage_points, metric.paired_wins,
                            metric.paired_losses, metric.tied, metric.severe_violation_count,
                            metric.baseline_wilson_low, metric.baseline_wilson_high,
                            metric.agent_wilson_low, metric.agent_wilson_high,
                            metric.metric_facts_digest,
                        ),
                    )
                conn.commit()
        except pg_errors.IntegrityError as error:
            raise EvaluationInvariantError("metric violates evaluation invariants") from error
        return metric

    def save_retention_decision(
        self,
        decision: RetentionDecisionRecord,
        claim: EvaluationRunClaim | None = None,
    ) -> RetentionDecisionRecord:
        run = self._load_run(decision.run_id)
        if run.candidate is not decision.candidate:
            raise EvaluationInvariantError("decision candidate mismatch")
        try:
            with psycopg.connect(
                **self._settings.postgres_connection_kwargs, row_factory=dict_row
            ) as conn:
                with conn.cursor() as cursor:
                    # 结论作用域是 Manifest/Candidate，而 selected 结果也跨 Run 唯一。
                    # 先锁 Manifest 阻止新 Run，再按 run_id 稳定顺序锁住全部兄弟 Run，
                    # 避免两个 Run 同时形成冲突结论或在结论后继续改变正式证据。
                    cursor.execute(
                        "SELECT manifest_id FROM specialist_evaluation_manifests WHERE manifest_id=%s FOR UPDATE;",
                        (run.manifest_id,),
                    )
                    if cursor.fetchone() is None:
                        raise EvaluationInvariantError("manifest does not exist")
                    cursor.execute(
                        """
                        SELECT run_id, status, lease_owner, lease_until, claim_version,
                               lease_until > now() AS lease_valid
                        FROM specialist_evaluation_runs
                        WHERE manifest_id=%s AND candidate_id=%s
                        ORDER BY run_id
                        FOR UPDATE;
                        """,
                        (run.manifest_id, run.candidate.value),
                    )
                    candidate_runs = tuple(cursor.fetchall())
                    current_row = next(
                        (row for row in candidate_runs if row["run_id"] == decision.run_id),
                        None,
                    )
                    self._assert_claim_row(current_row, decision.run_id, claim)
                    cursor.execute(
                        "SELECT * FROM specialist_paired_metrics WHERE run_id=%s ORDER BY split, metric_id;",
                        (decision.run_id,),
                    )
                    metric_rows = tuple(cursor.fetchall())
                    calculated_digest = canonical_json_sha256(
                        [self._metric_from_row(row).model_dump(mode="json") for row in metric_rows]
                    )
                    if decision.metrics_digest != calculated_digest:
                        raise EvaluationInvariantError("decision metrics digest mismatch")
                    cursor.execute(
                        """
                        SELECT a.* FROM specialist_selected_case_results s
                        JOIN specialist_case_attempts a ON a.attempt_id=s.attempt_id
                        WHERE s.manifest_id=%s AND s.candidate_id=%s
                          AND a.split IN ('VALIDATION', 'HOLDOUT')
                        ORDER BY a.split, a.case_id, a.subject;
                        """,
                        (run.manifest_id, run.candidate.value),
                    )
                    selected = tuple(self._attempt_from_row(row) for row in cursor.fetchall())
                    severe_count, hard_gates_passed, validation_count, holdout_count = (
                        _formal_agent_summary(selected)
                    )
                    if decision.severe_violation_count != severe_count:
                        raise EvaluationInvariantError("decision severe violation count mismatch")
                    if decision.hard_gates_passed != hard_gates_passed:
                        raise EvaluationInvariantError("decision hard gate summary mismatch")
                    if (
                        decision.completed_validation_cases != validation_count
                        or decision.completed_holdout_cases != holdout_count
                    ):
                        raise EvaluationInvariantError("decision completed case counts mismatch")
                    if decision.decision is RetentionDecision.RETAINED:
                        _assert_retained_evidence(
                            validation_attempts=tuple(
                                item for item in selected
                                if item.split is EvaluationSplit.VALIDATION
                            ),
                            holdout_attempts=tuple(
                                item for item in selected
                                if item.split is EvaluationSplit.HOLDOUT
                            ),
                            validation_metrics=tuple(
                                self._metric_from_row(row)
                                for row in metric_rows
                                if EvaluationSplit(row["split"]) is EvaluationSplit.VALIDATION
                            ),
                            holdout_metrics=tuple(
                                self._metric_from_row(row)
                                for row in metric_rows
                                if EvaluationSplit(row["split"]) is EvaluationSplit.HOLDOUT
                            ),
                        )
                    cursor.execute(
                        """
                        INSERT INTO specialist_retention_decisions (
                            decision_id, run_id, manifest_id, candidate_id, decision, reason_code,
                            external_evidence_sufficient, severe_violation_count, metrics_digest,
                            completed_validation_cases, completed_holdout_cases, hard_gates_passed
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
                        """,
                        (
                            decision.decision_id, decision.run_id, run.manifest_id,
                            decision.candidate.value,
                            decision.decision.value, decision.reason_code,
                            decision.external_evidence_sufficient,
                            decision.severe_violation_count, decision.metrics_digest,
                            decision.completed_validation_cases,
                            decision.completed_holdout_cases, decision.hard_gates_passed,
                        ),
                    )
                    cursor.execute(
                        """
                        UPDATE specialist_evaluation_runs
                        SET status=CASE WHEN run_id=%s THEN 'COMPLETED' ELSE 'CANCELLED' END,
                            lease_owner=NULL, lease_until=NULL
                        WHERE manifest_id=%s AND candidate_id=%s AND status='RUNNING';
                        """,
                        (decision.run_id, run.manifest_id, run.candidate.value),
                    )
                conn.commit()
        except pg_errors.IntegrityError as error:
            raise EvaluationInvariantError("retention decision violates evaluation invariants") from error
        return decision

    def get_retention_decision(self, run_id: str) -> RetentionDecisionRecord:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM specialist_retention_decisions WHERE run_id=%s;", (run_id,))
                row = cursor.fetchone()
        if row is None:
            raise EvaluationInvariantError("retention decision does not exist")
        return RetentionDecisionRecord(
            decision_id=row["decision_id"], run_id=row["run_id"],
            candidate=EvaluationCandidate(row["candidate_id"]),
            decision=RetentionDecision(row["decision"]), reason_code=row["reason_code"],
            external_evidence_sufficient=bool(row["external_evidence_sufficient"]),
            severe_violation_count=int(row["severe_violation_count"]),
            metrics_digest=row["metrics_digest"],
            completed_validation_cases=int(row["completed_validation_cases"]),
            completed_holdout_cases=int(row["completed_holdout_cases"]),
            hard_gates_passed=bool(row["hard_gates_passed"]),
        )

    def metrics_digest(self, run_id: str) -> str:
        """按稳定 split/metric 顺序重算已持久化正式指标摘要。"""

        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT * FROM specialist_paired_metrics
                    WHERE run_id=%s ORDER BY split, metric_id;
                    """,
                    (run_id,),
                )
                rows = cursor.fetchall()
        metrics = [self._metric_from_row(row).model_dump(mode="json") for row in rows]
        return canonical_json_sha256(metrics)

    def _selected_attempts(
        self, run: EvaluationRun, split: EvaluationSplit
    ) -> tuple[CaseAttempt, ...]:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT a.* FROM specialist_selected_case_results s
                    JOIN specialist_case_attempts a ON a.attempt_id=s.attempt_id
                    WHERE s.manifest_id=%s AND s.candidate_id=%s AND a.split=%s
                    ORDER BY a.case_id, a.subject;
                    """,
                    (run.manifest_id, run.candidate.value, split.value),
                )
                return tuple(self._attempt_from_row(row) for row in cursor.fetchall())

    @staticmethod
    def _validate_metric(metric: PairedMetric, selected: tuple[CaseAttempt, ...]) -> None:
        selected_by_case = {(item.case_id, item.subject): item for item in selected}
        expected_cases = {
            case_id
            for case_id in metric.case_ids
            if (case_id, EvaluationSubject.BASELINE) in selected_by_case
            and (case_id, EvaluationSubject.AGENT) in selected_by_case
        }
        if expected_cases != set(metric.case_ids):
            raise EvaluationInvariantError("metric case set does not match selected paired results")
        baseline_count = sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
            )
            for case_id in metric.case_ids
        )
        agent_count = sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
            )
            for case_id in metric.case_ids
        )
        wins = sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
            )
            and not _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
            )
            for case_id in metric.case_ids
        )
        losses = sum(
            _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
            )
            and not _metric_outcome(
                selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
            )
            for case_id in metric.case_ids
        )
        severe = sum(
            selected_by_case[(case_id, EvaluationSubject.AGENT)].severe_violation
            for case_id in metric.case_ids
        )
        if (baseline_count, agent_count, wins, losses, metric.sample_count - wins - losses, severe) != (
            metric.baseline_success_count, metric.agent_success_count, metric.paired_wins,
            metric.paired_losses, metric.tied, metric.severe_violation_count,
        ):
            raise EvaluationInvariantError("metric facts do not match selected results")
        facts = [
            {
                "case_id": case_id,
                "baseline_success": _metric_outcome(
                    selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
                ),
                "agent_success": _metric_outcome(
                    selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
                ),
                "agent_severe_violation": selected_by_case[(case_id, EvaluationSubject.AGENT)].severe_violation,
            }
            for case_id in sorted(metric.case_ids)
        ]
        if metric.metric_facts_digest != canonical_json_sha256(facts):
            raise EvaluationInvariantError("metric facts digest does not match selected results")
        from src.specialist_evaluation.comparison import BinaryPair, aggregate_binary_pairs

        expected_metric = aggregate_binary_pairs(
            metric_id=metric.metric_id,
            pairs=tuple(
                BinaryPair(
                    case_id=case_id,
                    baseline_success=_metric_outcome(
                        selected_by_case[(case_id, EvaluationSubject.BASELINE)], metric.metric_id
                    ),
                    agent_success=_metric_outcome(
                        selected_by_case[(case_id, EvaluationSubject.AGENT)], metric.metric_id
                    ),
                    agent_severe_violation=selected_by_case[(case_id, EvaluationSubject.AGENT)].severe_violation,
                )
                for case_id in sorted(metric.case_ids)
            ),
        )
        if expected_metric != metric:
            raise EvaluationInvariantError("metric rates or Wilson intervals do not match selected results")

    def _load_manifest(self, manifest_id: str) -> EvaluationManifest:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM specialist_evaluation_manifests WHERE manifest_id=%s;", (manifest_id,))
                row = cursor.fetchone()
        if row is None:
            raise EvaluationInvariantError("manifest does not exist")
        return self._manifest_from_row(row)

    @staticmethod
    def _assert_claim_cursor(
        cursor: Any, run_id: str, claim: EvaluationRunClaim | None
    ) -> None:
        cursor.execute(
            """
            SELECT status, lease_owner, lease_until, claim_version,
                   lease_until > now() AS lease_valid
            FROM specialist_evaluation_runs WHERE run_id=%s FOR UPDATE;
            """,
            (run_id,),
        )
        row = cursor.fetchone()
        PostgresSpecialistEvaluationStore._assert_claim_row(row, run_id, claim)

    @staticmethod
    def _assert_claim_row(
        row: Any, run_id: str, claim: EvaluationRunClaim | None
    ) -> None:
        """校验已锁定 Run 行的 lease/fencing；时间事实只采用数据库 now()。"""

        if row is None:
            raise EvaluationInvariantError("run does not exist")
        if row["status"] != "RUNNING":
            raise EvaluationInvariantError("formal facts require a RUNNING run")
        if row["lease_owner"] is None or claim is None:
            raise EvaluationInvariantError("formal fact write requires an active claim")
        if (
            claim.run_id != run_id
            or claim.worker_id != row["lease_owner"]
            or claim.claim_version != int(row["claim_version"])
            or claim.lease_until != row["lease_until"]
            or not row["lease_valid"]
        ):
            raise EvaluationInvariantError("claim is stale or owned by another worker")

    def _load_run(self, run_id: str) -> EvaluationRun:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM specialist_evaluation_runs WHERE run_id=%s;", (run_id,))
                row = cursor.fetchone()
        if row is None:
            raise EvaluationInvariantError("run does not exist")
        return self._run_from_row(row)

    def _load_attempt(self, attempt_id: str) -> CaseAttempt:
        with psycopg.connect(**self._settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM specialist_case_attempts WHERE attempt_id=%s;", (attempt_id,))
                row = cursor.fetchone()
        if row is None:
            raise EvaluationInvariantError("attempt does not exist")
        return self._attempt_from_row(row)

    @staticmethod
    def _manifest_from_row(row: Any) -> EvaluationManifest:
        return EvaluationManifest(
            manifest_id=row["manifest_id"], manifest_version=row["manifest_version"],
            manifest_digest=row["manifest_digest"], dataset_digest=row["dataset_digest"],
            schema_digest=row["schema_digest"], generator_digest=row["generator_digest"],
            seed=int(row["seed"]),
            development_case_ids=tuple(row["development_case_ids"]),
            validation_case_ids=tuple(row["validation_case_ids"]),
            holdout_case_ids=tuple(row["holdout_case_ids"]), code_digest=row["code_digest"],
            case_candidate_map=row["case_candidate_map"],
            profile_bundle_digest=row["profile_bundle_digest"],
            prompt_bundle_digest=row["prompt_bundle_digest"],
            result_schema_bundle_digest=row["result_schema_bundle_digest"],
            pricing_source_digest=row["pricing_source_digest"],
            temperature=row["temperature"],
            price_policy_digest=row["price_policy_digest"], endpoint_host=row["endpoint_host"],
            model_id=row["model_id"], candidate_ids=tuple(row["candidate_ids"]),
        )

    @staticmethod
    def _run_from_row(row: Any) -> EvaluationRun:
        return EvaluationRun(
            run_id=row["run_id"], manifest_id=row["manifest_id"],
            manifest_digest=row["manifest_digest"],
            candidate=EvaluationCandidate(row["candidate_id"]), status=row["status"],
        )

    @staticmethod
    def _attempt_from_row(row: Any) -> CaseAttempt:
        return CaseAttempt(
            attempt_id=row["attempt_id"], run_id=row["run_id"],
            manifest_id=row["manifest_id"], candidate=EvaluationCandidate(row["candidate_id"]),
            case_id=row["case_id"], split=EvaluationSplit(row["split"]),
            subject=EvaluationSubject(row["subject"]), attempt_number=int(row["attempt_number"]),
            success=bool(row["success"]), severe_violation=bool(row["severe_violation"]),
            infrastructure_failure=bool(row["infrastructure_failure"]),
            latency_ms=row["latency_ms"], input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]), cost_cny=row["cost_cny"],
            result_digest=row["result_digest"],
            metric_outcomes=row["metric_outcomes"], gate_results=row["gate_results"],
            output=row["result_snapshot"],
        )

    @staticmethod
    def _metric_from_row(row: Any) -> PairedMetric:
        return PairedMetric(
            metric_id=row["metric_id"], case_ids=tuple(row["case_ids"]),
            sample_count=int(row["sample_count"]),
            baseline_success_count=int(row["baseline_success_count"]),
            agent_success_count=int(row["agent_success_count"]),
            baseline_rate=row["baseline_rate"], agent_rate=row["agent_rate"],
            delta_percentage_points=row["delta_percentage_points"],
            paired_wins=int(row["paired_wins"]), paired_losses=int(row["paired_losses"]),
            tied=int(row["tied"]), severe_violation_count=int(row["severe_violation_count"]),
            baseline_wilson_low=row["baseline_wilson_low"],
            baseline_wilson_high=row["baseline_wilson_high"],
            agent_wilson_low=row["agent_wilson_low"], agent_wilson_high=row["agent_wilson_high"],
            metric_facts_digest=row["metric_facts_digest"],
        )


def initialize_specialist_evaluation_schema(settings: Any) -> None:
    """幂等执行 Phase 13 DDL；Task 3 预算与 Task 5 评估表共享迁移入口。"""

    sql = (Path(__file__).parents[2] / "docker" / "init_phase13_specialist_evaluations.sql").read_text(encoding="utf-8")
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
        conn.commit()
