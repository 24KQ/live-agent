"""Phase 2D LangGraph 播前骨架集成测试。"""

from uuid import uuid4

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.pre_live_graph import build_pre_live_graph, create_initial_pre_live_graph_state
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def test_pre_live_graph_flow_runs_full_pre_live_chain_and_writes_audit() -> None:
    """真实 PostgreSQL 样例数据下，LangGraph 应能跑通完整播前闭环。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)

    trace_id = f"trace-phase2d-integration-{uuid4()}"
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

    assert result["room_id"] == DEMO_ROOM_ID
    assert result["trace_id"] == trace_id
    assert result["product_count"] == 10
    assert result["plan_item_count"] == 10
    assert result["card_count"] == 3
    assert result["setup_gate_allowed"] is True
    assert result["setup_status"] == "prepared"
    assert result["setup_audit_id"]

    events = audit_store.list_events_by_trace_id(trace_id)
    assert {event["tool_name"] for event in events} >= {
        "query_products",
        "generate_live_plan",
        "generate_product_card",
        "setup_live_session",
    }
