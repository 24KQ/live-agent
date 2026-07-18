"""Phase 15 Task 4 Release Store 的 PostgreSQL 并发事实与重启恢复测试。"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from src.config.settings import get_settings
from src.release_gates.decisions import (
    DecisionSupportPromotionDecision,
    FinalReleaseStatus,
    ReleaseCaseResult,
    ReleaseMode,
    ReleaseRun,
)
from src.release_gates.models import EvaluationCaseStatus
from src.release_gates.store import PostgresReleaseStore, initialize_release_gate_schema


@pytest.fixture
def release_store():
    """建立隔离 ReleaseRun，并按外键逆序删除本测试自己的事实。"""

    settings = get_settings()
    initialize_release_gate_schema(settings)
    run_id = f"phase15-test-{uuid4()}"
    store = PostgresReleaseStore(settings)
    run = ReleaseRun(
        release_run_id=run_id,
        mode=ReleaseMode.NIGHTLY,
        manifest_digest="c" * 64,
        expected_case_ids=("case-a", "case-b"),
    )
    store.create_run(run)
    try:
        yield settings, store, run
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM phase15_release_decisions WHERE release_run_id=%s;",
                    (run_id,),
                )
                cursor.execute(
                    "DELETE FROM phase15_release_technical_decisions WHERE release_run_id=%s;",
                    (run_id,),
                )
                cursor.execute(
                    "DELETE FROM phase15_release_case_results WHERE release_run_id=%s;",
                    (run_id,),
                )
                cursor.execute(
                    "DELETE FROM phase15_release_runs WHERE release_run_id=%s;",
                    (run_id,),
                )
            conn.commit()


def _result(run: ReleaseRun, case_id: str) -> ReleaseCaseResult:
    """构造可重放的最小 PASS case 结果。"""

    return ReleaseCaseResult(
        release_run_id=run.release_run_id,
        manifest_digest=run.manifest_digest,
        case_id=case_id,
        subject_id="subject-phase15",
        subject_version="1.0.0",
        status=EvaluationCaseStatus.PASS,
        severe_violation=False,
        summary="postgres deterministic result",
    )


def test_postgres_release_store_replays_case_and_final_decision(release_store) -> None:
    """新 Store 实例必须重放唯一 case、技术结论和最终禁用状态。"""

    _settings, store, run = release_store
    first = _result(run, "case-a")
    second = _result(run, "case-b")
    assert store.append_case_result(first) == first
    assert store.append_case_result(first) == first
    store.append_case_result(second)
    technical = store.finalize_technical(run.release_run_id)
    assert technical.status.value == "PASS"

    final = store.save_promotion(
        run.release_run_id,
        DecisionSupportPromotionDecision.blocked("REAL_MODEL_EVIDENCE_MISSING"),
    )
    assert final.status is FinalReleaseStatus.RELEASED_DECISION_SUPPORT_DISABLED

    recovered = PostgresReleaseStore(_settings)
    assert len(recovered.list_case_results(run.release_run_id)) == 2
    assert recovered.save_promotion(
        run.release_run_id,
        DecisionSupportPromotionDecision.blocked("REAL_MODEL_EVIDENCE_MISSING"),
    ) == final
