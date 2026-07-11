from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import ToolCallAuditStore
from src.config.settings import get_settings
from src.core.agent_evaluation import AgentRuleEvaluator
from src.core.agent_replay import AgentReplayService
from src.gateway.agent_evaluation_service import AgentEvaluationWorker
from src.gateway.agent_evaluation_store import (
    PostgresAgentEvaluationStore,
    initialize_agent_evaluation_schema,
)
from src.gateway.harness_session_store import PostgresHarnessSessionStore, initialize_harness_session_schema


def build_worker(worker_id: str) -> AgentEvaluationWorker:
    """创建生产形态 Worker。

    Worker 使用 PostgreSQL 任务队列，并通过 Harness session + ToolCallAudit 做
    降级回放。真实 checkpoint 回放可以在后续版本注入 graph/checkpointer。
    """

    settings = get_settings()
    initialize_harness_session_schema(settings)
    initialize_agent_evaluation_schema(settings)
    store = PostgresAgentEvaluationStore(settings)
    replay_service = AgentReplayService(
        session_store=PostgresHarnessSessionStore(settings),
        audit_store=ToolCallAuditStore(settings),
    )
    return AgentEvaluationWorker(
        store=store,
        replay_service=replay_service,
        evaluator=AgentRuleEvaluator(),
        worker_id=worker_id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 7A Agent Evaluation worker")
    parser.add_argument("--once", action="store_true", help="只处理一个评估任务后退出")
    parser.add_argument("--forever", action="store_true", help="持续轮询评估任务")
    parser.add_argument("--worker-id", default="agent-evaluation-worker-local")
    parser.add_argument("--sleep-seconds", type=float, default=2.0)
    args = parser.parse_args()

    worker = build_worker(args.worker_id)
    if args.once or not args.forever:
        processed = worker.run_once()
        print({"processed": processed, "worker_id": args.worker_id})
        return

    print({"status": "running", "worker_id": args.worker_id})
    while True:
        processed = worker.run_once()
        if not processed:
            time.sleep(args.sleep_seconds)


if __name__ == "__main__":
    main()
