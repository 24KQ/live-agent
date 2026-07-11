from __future__ import annotations

from uuid import uuid4

from src.config.settings import get_settings
from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplay, AgentReplayService, ReplayTimelineItem
from src.gateway.agent_evaluation_service import AgentEvaluationService, AgentEvaluationWorker
from src.gateway.agent_evaluation_store import (
    PostgresAgentEvaluationStore,
    initialize_agent_evaluation_schema,
)


class FakeReplayService(AgentReplayService):
    """集成测试只验证评估队列和落库，不依赖真实 checkpoint 历史。"""

    def __init__(self) -> None:
        pass

    def build_replay(self, trace_id: str) -> AgentReplay:
        return AgentReplay(
            trace_id=trace_id,
            graph_version="harness-v1",
            replay_fidelity="checkpoint",
            timeline=[
                ReplayTimelineItem(sequence=1, node_name="load_context", phase="on_live", status="completed"),
                ReplayTimelineItem(sequence=2, node_name="human_approval_interrupt", phase="on_live", status="pending"),
                ReplayTimelineItem(
                    sequence=3,
                    node_name="execute_tool",
                    phase="on_live",
                    status="completed",
                    tool_call={"tool_name": "handle_sold_out_event", "risk_level": "HIGH"},
                    approval={"decision": "approved"},
                    evidence_ids=["audit-integration", "decision-integration"],
                ),
                ReplayTimelineItem(sequence=4, node_name="write_audit", phase="on_live", status="completed"),
            ],
        )


def test_postgres_evaluation_worker_persists_replay_score_and_review() -> None:
    settings = get_settings()
    initialize_agent_evaluation_schema(settings)
    store = PostgresAgentEvaluationStore(settings)
    service = AgentEvaluationService(store=store)
    worker = AgentEvaluationWorker(
        store=store,
        replay_service=FakeReplayService(),
        evaluator=AgentRuleEvaluator(),
        worker_id="worker-integration",
    )
    trace_id = f"trace-eval-flow-{uuid4()}"

    queued = service.create_evaluation(trace_id=trace_id)
    processed = worker.run_once()
    completed = service.get_evaluation(queued["evaluation_id"])
    review = service.add_review(
        evaluation_id=queued["evaluation_id"],
        operator_id="ops-integration",
        conclusion="approved",
        reason="集成测试复核通过",
    )
    replay = service.get_latest_replay(trace_id)

    assert processed is True
    assert completed["status"] == "completed"
    assert completed["verdict"] == "PASS"
    assert completed["overall_score"] >= 80
    assert replay is not None
    assert replay["replay"]["trace_id"] == trace_id
    assert review["review"]["operator_id"] == "ops-integration"
