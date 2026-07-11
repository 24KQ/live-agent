from __future__ import annotations

from fastapi.testclient import TestClient

from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplay, AgentReplayService, ReplayTimelineItem
from src.gateway import api_server
from src.gateway.agent_evaluation_service import AgentEvaluationService, AgentEvaluationWorker
from src.gateway.agent_evaluation_store import InMemoryAgentEvaluationStore


class FakeReplayService(AgentReplayService):
    def __init__(self) -> None:
        pass

    def build_replay(self, trace_id: str) -> AgentReplay:
        return AgentReplay(
            trace_id=trace_id,
            graph_version="harness-v1",
            replay_fidelity="checkpoint",
            timeline=[
                ReplayTimelineItem(sequence=1, node_name="load_context", phase="on_live", status="completed"),
                ReplayTimelineItem(sequence=2, node_name="write_audit", phase="on_live", status="completed"),
            ],
        )


store = InMemoryAgentEvaluationStore()
service = AgentEvaluationService(store=store)
worker = AgentEvaluationWorker(
    store=store,
    replay_service=FakeReplayService(),
    evaluator=AgentRuleEvaluator(),
    worker_id="api-test-worker",
)
api_server.set_agent_evaluation_service(service)
api_server.set_agent_evaluation_worker(worker)

client = TestClient(api_server.app)


def test_create_evaluation_returns_accepted_and_worker_can_complete() -> None:
    resp = client.post("/api/agent/evaluations", json={"trace_id": "trace-api-eval"})

    assert resp.status_code == 202
    evaluation_id = resp.json()["evaluation_id"]

    worker.run_once()
    status = client.get(f"/api/agent/evaluations/{evaluation_id}")

    assert status.status_code == 200
    assert status.json()["status"] in {"completed", "partial"}
    assert status.json()["replay_fidelity"] == "checkpoint"


def test_replay_endpoint_returns_latest_persisted_replay() -> None:
    resp = client.post("/api/agent/evaluations", json={"trace_id": "trace-api-replay"})
    evaluation_id = resp.json()["evaluation_id"]
    worker.run_once()

    replay = client.get("/api/agent/replays/trace-api-replay")

    assert replay.status_code == 200
    assert replay.json()["evaluation_id"] == evaluation_id
    assert replay.json()["replay"]["trace_id"] == "trace-api-replay"


def test_review_endpoint_records_human_overlay() -> None:
    resp = client.post("/api/agent/evaluations", json={"trace_id": "trace-api-review"})
    evaluation_id = resp.json()["evaluation_id"]

    review = client.post(
        f"/api/agent/evaluations/{evaluation_id}/reviews",
        json={"operator_id": "ops-001", "conclusion": "approved", "reason": "样例复核通过"},
    )

    assert review.status_code == 200
    assert review.json()["review"]["operator_id"] == "ops-001"


def test_evaluation_page_is_served() -> None:
    resp = client.get("/evaluation")

    assert resp.status_code == 200
    assert "Agent Evaluation" in resp.text
