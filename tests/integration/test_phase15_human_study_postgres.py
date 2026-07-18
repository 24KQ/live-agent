"""Phase 15 Task 5 真人交叉对照 PostgreSQL 重启与身份恢复测试。"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from src.config.settings import get_settings
from src.release_gates.human_study import (
    HumanStudyConfig,
    PostgresHumanStudyStore,
    StudyDecisionAction,
    StudyResponse,
)


GROUP_CASES = {
    "SOLD_OUT_BACKUP_CONFLICT": ("study-case-1", "study-case-2"),
    "DANMAKU_NOISE": ("study-case-3", "study-case-4"),
    "PACE_SHIFT": ("study-case-5", "study-case-6"),
    "EVIDENCE_CONFLICT": ("study-case-7", "study-case-8"),
}


@pytest.fixture
def human_study_store():
    """使用唯一 study_id 隔离 PostgreSQL 真人事实，测试后按外键顺序清理。"""

    settings = get_settings()
    study_id = f"phase15-study-test-{uuid4()}"
    config = HumanStudyConfig(
        study_id=study_id,
        seed=20260718,
        dataset_manifest_digest="d" * 64,
        promotion_artifact_digest="e" * 64,
        group_case_ids=GROUP_CASES,
    )
    store = PostgresHumanStudyStore(settings, config, participant_salt="postgres-test-salt")
    try:
        yield settings, config, store
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM phase15_human_study_responses
                    WHERE session_id IN (
                        SELECT session_id FROM phase15_human_study_sessions WHERE study_id=%s
                    );
                    """,
                    (study_id,),
                )
                cursor.execute(
                    """
                    DELETE FROM phase15_human_study_assignments
                    WHERE session_id IN (
                        SELECT session_id FROM phase15_human_study_sessions WHERE study_id=%s
                    );
                    """,
                    (study_id,),
                )
                cursor.execute(
                    "DELETE FROM phase15_human_study_sessions WHERE study_id=%s;",
                    (study_id,),
                )
            conn.commit()


def test_postgres_study_reloads_identity_and_response_after_restart(human_study_store) -> None:
    """重建 Store 后 assignment/response 必须保留同一加盐 digest，不能返回占位身份。"""

    settings, config, store = human_study_store
    session = store.create_session("operator-code-101")
    assignments = store.list_assignments(session.session_id)
    assert len(assignments) == 8
    assert {item.participant_digest for item in assignments} == {session.participant_digest}

    assignment = store.next_trial(session.session_id)
    assert assignment is not None
    response = store.record_response(
        session.session_id,
        assignment.assignment_id,
        StudyResponse(
            action=StudyDecisionAction.WAIT_OPERATOR,
            conflict_detected=True,
            workload_score=3,
        ),
    )
    assert response.participant_digest == session.participant_digest

    restarted = PostgresHumanStudyStore(settings, config, participant_salt="postgres-test-salt")
    recovered_assignment = restarted.list_assignments(session.session_id)[0]
    assert recovered_assignment.participant_digest == session.participant_digest
    replay = restarted.record_response(
        session.session_id,
        assignment.assignment_id,
        StudyResponse(
            action=StudyDecisionAction.WAIT_OPERATOR,
            conflict_detected=True,
            workload_score=3,
        ),
    )
    assert replay == response


def test_postgres_study_rejects_cross_study_and_manifest_reuse(human_study_store) -> None:
    """已知其他 study 的 session 不能被读取，旧 Manifest 也不能静默复用事实。"""

    settings, config, store = human_study_store
    session = store.create_session("operator-code-102")
    other_config = HumanStudyConfig.model_validate(
        {**config.model_dump(mode="json"), "study_id": f"phase15-other-study-{uuid4()}"}
    )
    other = PostgresHumanStudyStore(settings, other_config, participant_salt="postgres-test-salt")
    other_session = other.create_session("operator-code-103")
    try:
        with pytest.raises(ValueError, match="study session does not exist"):
            store.list_assignments(other_session.session_id)

        mismatched_config = HumanStudyConfig.model_validate(
            {**config.model_dump(mode="json"), "dataset_manifest_digest": "f" * 64}
        )
        mismatched = PostgresHumanStudyStore(settings, mismatched_config, participant_salt="postgres-test-salt")
        with pytest.raises(ValueError, match="manifest digest"):
            mismatched.list_assignments(session.session_id)
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM phase15_human_study_assignments WHERE session_id=%s;",
                    (other_session.session_id,),
                )
                cursor.execute(
                    "DELETE FROM phase15_human_study_sessions WHERE session_id=%s;",
                    (other_session.session_id,),
                )
            conn.commit()
