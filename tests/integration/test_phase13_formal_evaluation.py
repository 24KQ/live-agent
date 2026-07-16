"""Phase 13 Task 11 正式协调器的无网络端到端门禁。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from src.specialist_evaluation.models import (
    CaseAttempt,
    EvaluationCandidate,
    EvaluationRun,
    EvaluationSplit,
    EvaluationSubject,
    RetentionDecision,
    _build_formal_manifest_authorization,
    canonical_json_sha256,
)
from src.specialist_evaluation.runner import (
    CandidateEvaluationSlice,
    FormalEvaluationCoordinator,
    build_formal_manifest_from_dataset,
)
from src.specialist_evaluation.store import InMemorySpecialistEvaluationStore


ROOT = Path(__file__).parents[2]


@dataclass(frozen=True)
class _Gate:
    """测试替身只暴露正式协调器消费的冻结状态值。"""

    status: str


class _AlwaysPassingLiveOpsSlice:
    """用真实 Evaluation Store 模拟一个通过 validation 后才可消费 holdout 的候选。"""

    candidate = EvaluationCandidate.LIVE_OPS
    metric_ids = ("action_success_rate", "incident_recovery_rate")

    def __init__(self, *, store, manifest) -> None:
        self.store = store
        self.manifest = manifest
        self.executed_splits: list[EvaluationSplit] = []

    def cases_for(self, split: EvaluationSplit) -> tuple[dict, ...]:
        identifiers = {
            EvaluationSplit.VALIDATION: self.manifest.validation_case_ids,
            EvaluationSplit.HOLDOUT: self.manifest.holdout_case_ids,
        }[split]
        return tuple(
            {"case_id": case_id, "split": split.value.lower()}
            for case_id in identifiers
            if self.manifest.case_candidate_map[case_id] == self.candidate.value
        )

    async def run_agent_case(self, case: dict) -> object:
        self.executed_splits.append(EvaluationSplit(case["split"].upper()))
        return object()

    def baseline_for_case(self, _case: dict) -> object:
        return object()

    def record_pair(self, *, run, claim, case, baseline, agent_result) -> None:
        del baseline, agent_result
        split = EvaluationSplit(case["split"].upper())
        for subject, success in (
            (EvaluationSubject.BASELINE, False),
            (EvaluationSubject.AGENT, True),
        ):
            output = {"subject": subject.value, "case_id": case["case_id"]}
            attempt = CaseAttempt(
                attempt_id=f"{run.run_id}:{subject.value}:{case['case_id']}",
                run_id=run.run_id,
                manifest_id=run.manifest_id,
                candidate=run.candidate,
                case_id=case["case_id"],
                split=split,
                subject=subject,
                attempt_number=1,
                success=success,
                severe_violation=False,
                infrastructure_failure=False,
                latency_ms=Decimal("0"),
                input_tokens=0,
                output_tokens=0,
                cost_cny=Decimal("0"),
                result_digest=canonical_json_sha256(output),
                metric_outcomes={
                    "action_success_rate": success,
                    "incident_recovery_rate": success,
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
                output=output,
            )
            stored = self.store.append_attempt(attempt, claim=claim)
            self.store.select_attempt(stored.attempt_id, claim=claim)

    def rebuild_validation_gate(self, *, run) -> _Gate:
        selected = self.store.list_attempts(run.run_id)
        completed = {
            attempt.case_id
            for attempt in selected
            if attempt.split is EvaluationSplit.VALIDATION
            and attempt.subject is EvaluationSubject.AGENT
        }
        return _Gate("HOLDOUT_UNLOCKED" if len(completed) == 40 else "CONTINUE")

    def extra_gate_metrics(self, *, run, split: EvaluationSplit) -> dict[str, tuple[Decimal, Decimal]]:
        del run, split
        return {}


class _EarlyRejectedLiveOpsSlice(_AlwaysPassingLiveOpsSlice):
    """第一批十例后提供数学早停事实，验证协调器不会窥视 holdout。"""

    def rebuild_validation_gate(self, *, run) -> _Gate:
        selected = self.store.list_attempts(run.run_id)
        completed = {
            attempt.case_id
            for attempt in selected
            if attempt.split is EvaluationSplit.VALIDATION
            and attempt.subject is EvaluationSubject.AGENT
        }
        return _Gate("REJECTED" if len(completed) == 10 else "CONTINUE")


def test_formal_coordinator_runs_holdout_once_after_validation_and_persists_retained_decision() -> None:
    """协调器只从 selected Attempt 重算指标，且 Holdout 不能在 validation 前提前消费。"""

    manifest = build_formal_manifest_from_dataset(ROOT / "evaluation", ROOT)
    authorization = _build_formal_manifest_authorization(manifest)
    store = InMemorySpecialistEvaluationStore()
    store.register_manifest(manifest, authorization=authorization)
    run = EvaluationRun(
        run_id="formal-live-ops-scripted",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    store.create_run(run, authorization=authorization)
    claim = store.claim_next_run("formal-worker", manifest_id=manifest.manifest_id)
    assert claim is not None
    slice_ = _AlwaysPassingLiveOpsSlice(store=store, manifest=manifest)

    report = asyncio.run(
        FormalEvaluationCoordinator(store=store).evaluate_candidate(
            run=run,
            claim=claim,
            slice_=CandidateEvaluationSlice.from_object(slice_),
        )
    )

    assert report.decision.decision is RetentionDecision.RETAINED
    assert slice_.executed_splits[:40] == [EvaluationSplit.VALIDATION] * 40
    assert slice_.executed_splits[40:] == [EvaluationSplit.HOLDOUT] * 20
    assert len(store.list_paired_metrics(run.run_id, EvaluationSplit.VALIDATION)) == 2
    assert len(store.list_paired_metrics(run.run_id, EvaluationSplit.HOLDOUT)) == 2


def test_formal_coordinator_stops_after_rejected_validation_shard_without_holdout() -> None:
    """数学早停是规则失败，必须写 REJECTED 且完全不读取 evaluator-only holdout。"""

    manifest = build_formal_manifest_from_dataset(ROOT / "evaluation", ROOT)
    authorization = _build_formal_manifest_authorization(manifest)
    store = InMemorySpecialistEvaluationStore()
    store.register_manifest(manifest, authorization=authorization)
    run = EvaluationRun(
        run_id="formal-live-ops-early-stop",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    store.create_run(run, authorization=authorization)
    claim = store.claim_next_run("early-stop-worker", manifest_id=manifest.manifest_id)
    assert claim is not None
    slice_ = _EarlyRejectedLiveOpsSlice(store=store, manifest=manifest)

    report = asyncio.run(
        FormalEvaluationCoordinator(store=store).evaluate_candidate(
            run=run,
            claim=claim,
            slice_=CandidateEvaluationSlice.from_object(slice_),
        )
    )

    assert report.decision.decision is RetentionDecision.REJECTED
    assert len(slice_.executed_splits) == 10
    assert set(slice_.executed_splits) == {EvaluationSplit.VALIDATION}
