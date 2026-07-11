# -*- coding: utf-8 -*-
"""Phase 6C Harness Dashboard PostgreSQL 人审闭环演示。

该脚本不启动 Web 服务，只直接调用后端 service，验证：
1. start_session 会生成 pending_human 会话并写入 PostgreSQL；
2. submit_approval 会用同一个 trace_id/thread_id 恢复 LangGraph；
3. approve 会执行工具，reject 不执行工具。
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import get_settings  # noqa: E402
from src.gateway.harness_dashboard_service import HarnessDashboardService  # noqa: E402
from src.gateway.harness_session_store import (  # noqa: E402
    PostgresHarnessSessionStore,
    initialize_harness_session_schema,
)


def _service() -> HarnessDashboardService:
    """创建使用 PostgreSQL store 和 PostgresSaver 的 Phase 6C service。"""

    settings = get_settings()
    initialize_harness_session_schema(settings)
    return HarnessDashboardService(
        store=PostgresHarnessSessionStore(settings),
        settings=settings,
        use_postgres_checkpointer=True,
    )


def _print_status(title: str, status: dict) -> None:
    """打印副屏关心的核心字段，便于人工验收。"""

    print("=" * 72)
    print(title)
    print("=" * 72)
    print("trace_id:", status.get("trace_id"))
    print("status:", status.get("status"))
    print("agent_status:", status.get("agent_status"))
    print("pending_approval:", status.get("pending_approval"))
    print("approval_decision:", status.get("approval_decision"))
    print("completed_nodes:", " -> ".join(status.get("completed_nodes", [])))
    print("interrupt_tool:", (status.get("interrupt_payload") or {}).get("tool_name"))
    print("executed_tools:", status.get("executed_tools"))
    print("observations:", status.get("observations"))
    print("final_suggestion:", status.get("final_suggestion"))
    print("audit_status:", status.get("audit_status"))
    print()


def _run_approve_demo() -> None:
    trace_id = f"trace-phase6c-demo-approve-{uuid4()}"
    first_service = _service()
    pending = first_service.start_session(room_id="room-dashboard-001", trace_id=trace_id)
    _print_status("Phase 6C approve 场景：pending", pending)

    resumed_service = _service()
    completed = resumed_service.submit_approval(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="approved",
        operator_id="operator-dashboard",
        reason="CLI 演示确认执行售罄处理。",
    )
    _print_status("Phase 6C approve 场景：completed", completed)


def _run_reject_demo() -> None:
    trace_id = f"trace-phase6c-demo-reject-{uuid4()}"
    service = _service()
    pending = service.start_session(room_id="room-dashboard-001", trace_id=trace_id)
    _print_status("Phase 6C reject 场景：pending", pending)

    rejected = service.submit_approval(
        trace_id=trace_id,
        room_id="room-dashboard-001",
        tool_name="handle_sold_out_event",
        decision="rejected",
        operator_id="operator-dashboard",
        reason="CLI 演示拒绝自动处理，主播手动接管。",
    )
    _print_status("Phase 6C reject 场景：rejected", rejected)


def main() -> None:
    print("Phase 6C Harness Dashboard PostgreSQL approval demo")
    print()
    _run_approve_demo()
    _run_reject_demo()


if __name__ == "__main__":
    main()
