"""运行 Phase 2F LangGraph interrupt 人审恢复演示。

运行方式：
    python scripts/run_phase2f_pre_live_interrupt_demo.py

脚本会用官方 PostgresSaver 持久化 checkpoint，并演示两条人审路径：
1. approve：Graph 在建播 hard-gate 暂停，CLI 模拟批准后恢复并执行建播。
2. reject：Graph 在同一节点暂停，CLI 模拟拒绝后恢复为 rejected，不执行建播成功逻辑。
"""

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4

from langgraph.types import Command


# 直接执行脚本时，Python 只会把 scripts 目录加入 sys.path。这里显式加入仓库根目录，
# 保证无论从 IDE 还是命令行运行，都能稳定导入 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.langgraph_checkpoint import create_postgres_checkpointer, initialize_postgres_checkpointer
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.pre_live_graph import build_pre_live_graph, create_initial_pre_live_graph_state, create_pre_live_graph_config
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def _run_human_approval_scenario(decision: str) -> dict[str, object]:
    """运行单条 approve/reject 人审场景，并返回可打印的演示结果。

    每个场景使用独立 trace_id/thread_id，避免 checkpoint 和审计记录互相污染。
    Graph 首次运行会在建播节点触发 interrupt；随后脚本重新创建 checkpointer、service
    和 graph，模拟进程重启后的恢复执行。
    """

    settings = get_settings()
    audit_store = ToolCallAuditStore(settings)
    trace_id = f"trace-phase2f-demo-{decision}-{uuid4()}"
    config = create_pre_live_graph_config(trace_id)

    with create_postgres_checkpointer(settings) as first_checkpointer:
        first_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        first_graph = build_pre_live_graph(first_service, checkpointer=first_checkpointer)
        first_result = first_graph.invoke(
            create_initial_pre_live_graph_state(
                room_id=DEMO_ROOM_ID,
                trace_id=trace_id,
                confirmed_setup=False,
                enable_human_approval=True,
            ),
            config=config,
        )

    interrupt_payload = first_result["__interrupt__"][0].value
    audit_after_interrupt = audit_store.list_events_by_trace_id(trace_id)
    reason = "确认建播配置无误。" if decision == "approved" else "需要先调整排品节奏。"

    with create_postgres_checkpointer(settings) as resumed_checkpointer:
        resumed_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        resumed_graph = build_pre_live_graph(resumed_service, checkpointer=resumed_checkpointer)
        resumed_result = resumed_graph.invoke(
            Command(
                resume={
                    "trace_id": trace_id,
                    "room_id": DEMO_ROOM_ID,
                    "tool_name": "setup_live_session",
                    "decision": decision,
                    "operator_id": "operator-demo",
                    "reason": reason,
                }
            ),
            config=config,
        )

    final_audit_events = audit_store.list_events_by_trace_id(trace_id)
    return {
        "decision": decision,
        "trace_id": trace_id,
        "interrupt_payload": interrupt_payload,
        "audit_count_after_interrupt": len(audit_after_interrupt),
        "setup_status": resumed_result["setup_status"],
        "setup_audit_id": resumed_result.get("setup_audit_id"),
        "approval_pending_audit_id": resumed_result.get("approval_pending_audit_id"),
        "approval_resume_audit_id": resumed_result.get("approval_resume_audit_id"),
        "final_audit_count": len(final_audit_events),
        "final_audit_tools": [event["tool_name"] for event in final_audit_events],
        "approval_decisions": [
            event["operator_decision"]
            for event in final_audit_events
            if event["tool_name"] == "setup_live_session_approval"
        ],
    }


def _print_scenario(result: dict[str, object]) -> None:
    """把单条人审场景打印成稳定、可复制的验收输出。"""

    payload = result["interrupt_payload"]
    assert isinstance(payload, dict)
    print(f"\nscenario: {result['decision']}")
    print(f"trace_id/thread_id: {result['trace_id']}")
    print(f"interrupt_tool: {payload['tool_name']}")
    print(f"interrupt_risk_level: {payload['risk_level']}")
    print(f"interrupt_plan_item_count: {len(payload['plan_item_ids'])}")
    print(f"pending_audit_id: {result['approval_pending_audit_id']}")
    print(f"resume_decision: {result['decision']}")
    print(f"resume_audit_id: {result['approval_resume_audit_id']}")
    print(f"audit_count_after_interrupt: {result['audit_count_after_interrupt']}")
    print(f"setup_status: {result['setup_status']}")
    print(f"setup_audit_id: {result['setup_audit_id']}")
    print(f"final_audit_count: {result['final_audit_count']}")
    print(f"approval_decisions: {', '.join(result['approval_decisions'])}")
    print(f"final_audit_tools: {', '.join(result['final_audit_tools'])}")


def main() -> int:
    """初始化样例数据，运行 approve/reject 两条人审恢复演示。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    initialize_postgres_checkpointer(settings)
    seed_phase2_demo_data(settings)

    print("Phase 2F LangGraph interrupt human approval demo")
    print(f"room_id: {DEMO_ROOM_ID}")
    for decision in ("approved", "rejected"):
        _print_scenario(_run_human_approval_scenario(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
