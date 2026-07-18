"""Phase 15 Task 5 真人交叉对照采集器的 TDD 契约。"""

from __future__ import annotations

import pytest

from src.release_gates.human_study import (
    HumanStudyConfig,
    HumanStudyStore,
    StudyCondition,
    StudyDecisionAction,
    StudyEvidenceStatus,
    StudyResponse,
)


DATASET_DIGEST = "d" * 64
PROMOTION_DIGEST = "e" * 64
GROUP_CASES = {
    "SOLD_OUT_BACKUP_CONFLICT": ("live-case-1", "live-case-2"),
    "DANMAKU_NOISE": ("live-case-3", "live-case-4"),
    "PACE_SHIFT": ("live-case-5", "live-case-6"),
    "EVIDENCE_CONFLICT": ("live-case-7", "live-case-8"),
}


def _config() -> HumanStudyConfig:
    """构造绑定 Golden Manifest 和真实 smoke artifact 的 study 配置。"""

    return HumanStudyConfig(
        study_id="phase15-human-study-v1",
        seed=20260718,
        dataset_manifest_digest=DATASET_DIGEST,
        promotion_artifact_digest=PROMOTION_DIGEST,
        group_case_ids=GROUP_CASES,
    )


def _complete(store: HumanStudyStore, session_id: str) -> None:
    """完成一个参与者的 8 个封闭试验。"""

    while (assignment := store.next_trial(session_id)) is not None:
        store.record_response(
            session_id,
            assignment.assignment_id,
            StudyResponse(
                action=StudyDecisionAction.WAIT_OPERATOR,
                conflict_detected=True,
                workload_score=3,
            ),
        )


def test_assignment_is_balanced_and_each_real_participant_gets_eight_trials() -> None:
    """每人必须得到四组场景各一 baseline/decision-support 配对。"""

    store = HumanStudyStore(_config(), participant_salt="test-salt")
    session = store.create_session("operator-code-001")
    assignments = store.list_assignments(session.session_id)
    assert len(assignments) == 8
    assert {item.scenario_group for item in assignments} == set(GROUP_CASES)
    assert {(item.scenario_group, item.condition) for item in assignments} == {
        (group, condition) for group in GROUP_CASES for condition in StudyCondition
    }


def test_response_uses_server_latency_and_replay_is_idempotent() -> None:
    """客户端不能提交耗时；同一结构化响应重放返回同一事实。"""

    store = HumanStudyStore(_config(), participant_salt="test-salt")
    session = store.create_session("operator-code-002")
    assignment = store.next_trial(session.session_id)
    assert assignment is not None
    response = StudyResponse(
        action=StudyDecisionAction.WAIT_OPERATOR,
        conflict_detected=False,
        workload_score=4,
    )
    first = store.record_response(session.session_id, assignment.assignment_id, response)
    second = store.record_response(session.session_id, assignment.assignment_id, response)
    assert first == second
    assert first.server_latency_ms >= 0
    with pytest.raises(ValueError, match="conflicting"):
        store.record_response(
            session.session_id,
            assignment.assignment_id,
            response.model_validate({**response.model_dump(mode="json"), "workload_score": 5}),
        )


def test_free_text_pii_and_client_latency_are_rejected() -> None:
    """Study 只接受封闭动作和服务端计时，额外文本/PII/客户端耗时全部拒绝。"""

    with pytest.raises(ValueError):
        StudyResponse.model_validate(
            {
                "action": "WAIT_OPERATOR",
                "conflict_detected": False,
                "workload_score": 4,
                "free_text": "真实姓名",
            }
        )
    with pytest.raises(ValueError):
        StudyResponse.model_validate(
            {
                "action": "WAIT_OPERATOR",
                "conflict_detected": False,
                "workload_score": 4,
                "latency_ms": 1,
            }
        )


def test_promotion_evidence_is_blocked_until_three_complete_real_sessions() -> None:
    """不足 3 人、缺行或缺 smoke digest 时不能伪造 Promotion 证据。"""

    store = HumanStudyStore(_config(), participant_salt="test-salt")
    first = store.create_session("operator-code-003")
    _complete(store, first.session_id)
    assert store.promotion_evidence().status is StudyEvidenceStatus.BLOCKED

    second = store.create_session("operator-code-004")
    third = store.create_session("operator-code-005")
    _complete(store, second.session_id)
    _complete(store, third.session_id)
    evidence = store.promotion_evidence()
    assert evidence.status is StudyEvidenceStatus.READY
    assert evidence.participant_count == 3
    assert evidence.response_count == 24
    assert evidence.promotion_artifact_digest == PROMOTION_DIGEST


def test_participant_identity_is_salted_and_duplicate_session_is_replayed() -> None:
    """Store 只保留加盐摘要，同一参与者 code 重试不能制造第二个 session。"""

    store = HumanStudyStore(_config(), participant_salt="test-salt")
    first = store.create_session("operator-code-006")
    replay = store.create_session("operator-code-006")
    assert first == replay
    assert "operator-code-006" not in first.participant_digest
