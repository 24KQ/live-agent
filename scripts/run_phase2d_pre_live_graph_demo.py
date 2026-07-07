"""运行 Phase 2D LangGraph 播前 Harness 骨架演示。

运行方式：
    python scripts/run_phase2d_pre_live_graph_demo.py

脚本会初始化本地脱敏样例数据，然后用 LangGraph 编排现有播前业务服务。
本阶段不接 LLM、不接真实淘宝 API、不启用持久 checkpoint，也不使用 interrupt。
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
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.pre_live_graph import build_pre_live_graph, create_initial_pre_live_graph_state
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def main() -> int:
    """执行 LangGraph 播前演示并返回进程退出码。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)

    # 演示脚本每次生成新的 trace_id，避免重复运行时把历史审计记录混入本次输出。
    trace_id = f"trace-phase2d-demo-{uuid4()}"
    audit_store = ToolCallAuditStore(settings)
    service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
    graph = build_pre_live_graph(service)
    result = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id=DEMO_ROOM_ID,
            trace_id=trace_id,
            confirmed_setup=True,
        )
    )

    audit_events = audit_store.list_events_by_trace_id(trace_id)
    print("Phase 2D LangGraph pre-live demo")
    print(f"room_id: {DEMO_ROOM_ID}")
    print(f"trace_id: {trace_id}")
    print(f"completed_nodes: {', '.join(result['completed_nodes'])}")
    print(f"product_count: {result['product_count']}")
    print(f"plan_item_count: {result['plan_item_count']}")
    print(f"card_count: {result['card_count']}")
    print(f"setup_gate_decision: {result['setup_gate_decision']}")
    print(f"setup_gate_allowed: {result['setup_gate_allowed']}")
    print(f"setup_status: {result['setup_status']}")
    print(f"setup_audit_id: {result['setup_audit_id']}")
    print(f"compliance_summary: {result['compliance_summary']}")
    print(f"audit_event_count: {len(audit_events)}")
    print(f"audit_tools: {', '.join(event['tool_name'] for event in audit_events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
