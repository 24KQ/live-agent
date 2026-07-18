"""Phase 15 Task 5 真人 Study API 的受控内存闭环测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.gateway import api_server
from src.release_gates.human_study import HumanStudyConfig, HumanStudyStore


GROUP_CASES = {
    "SOLD_OUT_BACKUP_CONFLICT": ("api-case-1", "api-case-2"),
    "DANMAKU_NOISE": ("api-case-3", "api-case-4"),
    "PACE_SHIFT": ("api-case-5", "api-case-6"),
    "EVIDENCE_CONFLICT": ("api-case-7", "api-case-8"),
}


def _store() -> HumanStudyStore:
    """构造绑定冻结 Manifest 的内存 Store，避免 API 测试访问外部事实源。"""

    return HumanStudyStore(
        HumanStudyConfig(
            study_id="phase15-api-study-v1",
            seed=20260718,
            dataset_manifest_digest="d" * 64,
            promotion_artifact_digest="e" * 64,
            group_case_ids=GROUP_CASES,
        ),
        participant_salt="api-test-salt",
    )


def test_phase15_study_api_runs_closed_trial_and_evidence_flow() -> None:
    """四个端点只暴露封闭 trial 事实，并由服务端补充响应耗时。"""

    api_server.set_phase15_human_study_store(_store())
    client = TestClient(api_server.app)
    try:
        created = client.post(
            "/api/phase15/study/sessions",
            json={"participant_code": "operator-api-001"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        trial = client.get(f"/api/phase15/study/sessions/{session_id}/next")
        assert trial.status_code == 200
        assignment = trial.json()["assignment"]
        assert "label" not in assignment

        response = client.post(
            f"/api/phase15/study/sessions/{session_id}/responses",
            json={
                "assignment_id": assignment["assignment_id"],
                "action": "WAIT_OPERATOR",
                "conflict_detected": True,
                "workload_score": 3,
            },
        )
        assert response.status_code == 200
        assert response.json()["server_latency_ms"] is not None

        evidence = client.get("/api/phase15/study/evidence")
        assert evidence.status_code == 200
        assert evidence.json()["status"] == "BLOCKED"
    finally:
        api_server.set_phase15_human_study_store(None)


def test_phase15_study_api_stays_blocked_without_explicit_store() -> None:
    """未装配真人 Store 时，API 不能伪造可用 study 或 Promotion 证据。"""

    api_server.set_phase15_human_study_store(None)
    response = TestClient(api_server.app).post(
        "/api/phase15/study/sessions",
        json={"participant_code": "operator-api-002"},
    )
    assert response.status_code == 503
    assert response.json()["status"] == "BLOCKED"
