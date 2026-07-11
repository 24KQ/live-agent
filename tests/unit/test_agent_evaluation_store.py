from __future__ import annotations

from src.gateway.agent_evaluation_store import (
    EvaluationRunCreate,
    InMemoryAgentEvaluationStore,
)


def test_in_memory_store_reuses_idempotent_evaluation_run() -> None:
    store = InMemoryAgentEvaluationStore()
    request = EvaluationRunCreate(
        trace_id="trace-store-eval",
        evaluator_version="rules-v1",
        input_fingerprint="fingerprint-001",
        profile="production_hybrid",
    )

    first = store.create_or_reuse_run(request)
    second = store.create_or_reuse_run(request)

    assert first.evaluation_id == second.evaluation_id
    assert second.status == "queued"


def test_worker_claim_uses_lease_and_marks_terminal_once() -> None:
    store = InMemoryAgentEvaluationStore()
    run = store.create_or_reuse_run(
        EvaluationRunCreate(
            trace_id="trace-claim",
            evaluator_version="rules-v1",
            input_fingerprint="fp",
            profile="production_hybrid",
        )
    )

    claimed = store.claim_next_run(worker_id="worker-001")
    store.complete_run(
        evaluation_id=claimed.evaluation_id,
        replay_snapshot={"trace_id": "trace-claim"},
        overall_score=91.0,
        coverage_percent=90.0,
        verdict="PASS",
        violations=[],
        dimension_scores=[],
    )
    repeated = store.complete_run(
        evaluation_id=run.evaluation_id,
        replay_snapshot={"ignored": True},
        overall_score=1.0,
        coverage_percent=1.0,
        verdict="FAIL",
        violations=["should not overwrite"],
        dimension_scores=[],
    )

    assert claimed.status == "running"
    assert store.claim_next_run(worker_id="worker-002") is None
    assert repeated.overall_score == 91.0
    assert repeated.verdict == "PASS"


def test_failed_run_retries_three_times_then_fails() -> None:
    store = InMemoryAgentEvaluationStore()
    run = store.create_or_reuse_run(
        EvaluationRunCreate(
            trace_id="trace-fail",
            evaluator_version="rules-v1",
            input_fingerprint="fp",
            profile="production_hybrid",
        )
    )

    for _ in range(3):
        claimed = store.claim_next_run(worker_id="worker-error")
        store.fail_run(claimed.evaluation_id, "boom")

    failed = store.get(run.evaluation_id)

    assert failed.status == "failed"
    assert failed.retry_count == 3
    assert failed.error == "boom"
