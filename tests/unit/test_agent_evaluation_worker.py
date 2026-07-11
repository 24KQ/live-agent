from __future__ import annotations

from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplay, AgentReplayService, ReplayTimelineItem
from src.gateway.agent_evaluation_service import AgentEvaluationWorker
from src.gateway.agent_evaluation_store import EvaluationRunCreate, InMemoryAgentEvaluationStore


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


def test_worker_processes_one_queued_run() -> None:
    store = InMemoryAgentEvaluationStore()
    run = store.create_or_reuse_run(
        EvaluationRunCreate(
            trace_id="trace-worker",
            evaluator_version="rules-v1",
            input_fingerprint="fp-worker",
            profile="production_hybrid",
        )
    )
    worker = AgentEvaluationWorker(
        store=store,
        replay_service=FakeReplayService(),
        evaluator=AgentRuleEvaluator(),
        worker_id="worker-unit",
    )

    processed = worker.run_once()
    saved = store.get(run.evaluation_id)

    assert processed is True
    assert saved.status in {"completed", "partial"}
    assert saved.replay_snapshot["trace_id"] == "trace-worker"
    assert saved.coverage_percent > 0


class FakeJudge:
    def judge(self, *, final_suggestion: str, context_summary: str):
        return type(
            "JudgeResult",
            (),
            {
                "status": "completed",
                "score": 88.0,
                "model_dump": lambda self, mode="python": {
                    "status": "completed",
                    "score": 88.0,
                    "reason": "建议质量良好",
                },
            },
        )()


class ReplayWithSuggestionService(AgentReplayService):
    def __init__(self) -> None:
        pass

    def build_replay(self, trace_id: str) -> AgentReplay:
        return AgentReplay(
            trace_id=trace_id,
            graph_version="harness-v1",
            replay_fidelity="checkpoint",
            timeline=[
                ReplayTimelineItem(
                    sequence=1,
                    node_name="write_audit",
                    phase="on_live",
                    status="completed",
                    state_delta={
                        "final_suggestion": "请主播强调优惠价",
                        "context_summary": "弹幕集中询问价格",
                    },
                    evidence_ids=["audit-judge"],
                )
            ],
        )


def test_worker_adds_llm_judge_dimension_when_available() -> None:
    store = InMemoryAgentEvaluationStore()
    run = store.create_or_reuse_run(
        EvaluationRunCreate(
            trace_id="trace-worker-judge",
            evaluator_version="rules-v1",
            input_fingerprint="fp-worker-judge",
            profile="production_hybrid",
        )
    )
    worker = AgentEvaluationWorker(
        store=store,
        replay_service=ReplayWithSuggestionService(),
        evaluator=AgentRuleEvaluator(),
        llm_judge=FakeJudge(),
        worker_id="worker-judge",
    )

    worker.run_once()
    saved = store.get(run.evaluation_id)
    semantic = [score for score in saved.dimension_scores if score["dimension"] == "建议语义质量"][0]

    assert semantic["score"] == 88.0
    assert semantic["evaluator_type"] == "llm_judge"
