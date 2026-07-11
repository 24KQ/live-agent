from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import get_settings
from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplay, AgentReplayService, ReplayTimelineItem
from src.gateway.agent_evaluation_service import AgentEvaluationService, AgentEvaluationWorker
from src.gateway.agent_evaluation_store import (
    PostgresAgentEvaluationStore,
    initialize_agent_evaluation_schema,
)


class DemoReplayService(AgentReplayService):
    """演示用回放服务，构造一条已通过人审的高风险工具链路。"""

    def __init__(self) -> None:
        pass

    def build_replay(self, trace_id: str) -> AgentReplay:
        return AgentReplay(
            trace_id=trace_id,
            graph_version="harness-v1",
            replay_fidelity="checkpoint",
            timeline=[
                ReplayTimelineItem(sequence=1, node_name="load_context", phase="on_live", status="completed"),
                ReplayTimelineItem(sequence=2, node_name="agent_reasoning", phase="on_live", status="completed"),
                ReplayTimelineItem(sequence=3, node_name="human_approval_interrupt", phase="on_live", status="pending"),
                ReplayTimelineItem(
                    sequence=4,
                    node_name="execute_tool",
                    phase="on_live",
                    status="completed",
                    tool_call={"tool_name": "handle_sold_out_event", "risk_level": "HIGH"},
                    approval={"decision": "approved", "operator_id": "operator-demo"},
                    observation={"summary": "售罄处理已执行"},
                    evidence_ids=["audit-demo-001", "decision-demo-001"],
                ),
                ReplayTimelineItem(sequence=5, node_name="write_audit", phase="on_live", status="completed"),
            ],
        )


def main() -> None:
    settings = get_settings()
    initialize_agent_evaluation_schema(settings)
    store = PostgresAgentEvaluationStore(settings)
    service = AgentEvaluationService(store=store)
    worker = AgentEvaluationWorker(
        store=store,
        replay_service=DemoReplayService(),
        evaluator=AgentRuleEvaluator(),
        worker_id="phase7a-demo-worker",
    )
    trace_id = f"trace-phase7a-demo-{uuid4()}"

    queued = service.create_evaluation(trace_id=trace_id)
    worker.run_once()
    completed = service.get_evaluation(queued["evaluation_id"])
    replay = service.get_latest_replay(trace_id)

    print("=== Phase 7A Agent Evaluation Demo ===")
    print({"queued": queued})
    print({"completed": completed})
    print({"timeline_nodes": [item["node_name"] for item in replay["replay"]["timeline"]] if replay else []})


if __name__ == "__main__":
    main()
