"""运行 Phase 2E LangGraph PostgreSQL checkpoint 恢复演示。

运行方式：
    python scripts/run_phase2e_pre_live_checkpoint_demo.py

脚本会用官方 PostgresSaver 把播前 Graph 中断在 `generate_product_cards` 之后，
再模拟进程重启：重新创建 checkpointer、service 和 graph，并用同一个 trace_id
作为 thread_id 恢复执行到 END。
"""

from __future__ import annotations

from pathlib import Path
import sys
from uuid import uuid4


# 直接执行脚本时，Python 默认只把 scripts 目录加入 sys.path。这里显式加入
# 仓库根目录，保证可以稳定导入 src 包。
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


def main() -> int:
    """执行 checkpoint 中断与恢复演示并返回进程退出码。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    initialize_postgres_checkpointer(settings)
    seed_phase2_demo_data(settings)

    # 每次演示都使用新的 trace_id，避免重复运行时复用旧 checkpoint 或混入历史审计。
    trace_id = f"trace-phase2e-demo-{uuid4()}"
    config = create_pre_live_graph_config(trace_id)
    audit_store = ToolCallAuditStore(settings)

    with create_postgres_checkpointer(settings) as first_checkpointer:
        first_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        first_graph = build_pre_live_graph(
            first_service,
            checkpointer=first_checkpointer,
            interrupt_after=["generate_product_cards"],
        )
        first_result = first_graph.invoke(
            create_initial_pre_live_graph_state(
                room_id=DEMO_ROOM_ID,
                trace_id=trace_id,
                confirmed_setup=True,
            ),
            config=config,
        )
        interrupted_next_nodes = first_graph.get_state(config).next

    audit_after_interrupt = audit_store.list_events_by_trace_id(trace_id)

    # 这里重新创建 checkpointer 和 graph，模拟真实进程重启后的恢复路径。
    with create_postgres_checkpointer(settings) as resumed_checkpointer:
        resumed_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        resumed_graph = build_pre_live_graph(resumed_service, checkpointer=resumed_checkpointer)
        resumed_result = resumed_graph.invoke(None, config=config)

    final_audit_events = audit_store.list_events_by_trace_id(trace_id)

    print("Phase 2E LangGraph PostgreSQL checkpoint demo")
    print(f"room_id: {DEMO_ROOM_ID}")
    print(f"trace_id/thread_id: {trace_id}")
    print(f"first_completed_nodes: {', '.join(first_result['completed_nodes'])}")
    print(f"interrupted_next_nodes: {', '.join(interrupted_next_nodes)}")
    print(f"audit_count_after_interrupt: {len(audit_after_interrupt)}")
    print(f"resumed_completed_nodes: {', '.join(resumed_result['completed_nodes'])}")
    print(f"product_count: {resumed_result['product_count']}")
    print(f"plan_item_count: {resumed_result['plan_item_count']}")
    print(f"card_count: {resumed_result['card_count']}")
    print(f"setup_gate_allowed: {resumed_result['setup_gate_allowed']}")
    print(f"setup_status: {resumed_result['setup_status']}")
    print(f"setup_audit_id: {resumed_result['setup_audit_id']}")
    print(f"final_audit_event_count: {len(final_audit_events)}")
    print(f"final_audit_tools: {', '.join(event['tool_name'] for event in final_audit_events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

