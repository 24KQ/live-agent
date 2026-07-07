"""Phase 2E 播前 Graph PostgreSQL checkpoint 集成测试。"""

from __future__ import annotations

from uuid import uuid4

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.langgraph_checkpoint import create_postgres_checkpointer, initialize_postgres_checkpointer
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.pre_live_graph import build_pre_live_graph, create_initial_pre_live_graph_state, create_pre_live_graph_config
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def test_pre_live_graph_recovers_from_postgres_checkpoint_without_duplicate_audit() -> None:
    """真实 PostgreSQL checkpoint 应支持中断后恢复，且不重复写前半段审计。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    initialize_postgres_checkpointer(settings)
    seed_phase2_demo_data(settings)

    trace_id = f"trace-phase2e-integration-{uuid4()}"
    audit_store = ToolCallAuditStore(settings)
    config = create_pre_live_graph_config(trace_id)

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

    assert first_result["completed_nodes"] == [
        "query_products",
        "generate_live_plan",
        "generate_product_cards",
    ]
    first_events = audit_store.list_events_by_trace_id(trace_id)
    assert [event["tool_name"] for event in first_events].count("query_products") == 1
    assert [event["tool_name"] for event in first_events].count("generate_live_plan") == 1
    assert [event["tool_name"] for event in first_events].count("generate_product_card") == 3
    assert "setup_live_session" not in [event["tool_name"] for event in first_events]

    with create_postgres_checkpointer(settings) as resumed_checkpointer:
        resumed_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        resumed_graph = build_pre_live_graph(resumed_service, checkpointer=resumed_checkpointer)
        resumed_result = resumed_graph.invoke(None, config=config)

    assert resumed_result["setup_status"] == "prepared"
    assert resumed_result["setup_audit_id"]
    final_events = audit_store.list_events_by_trace_id(trace_id)
    assert [event["tool_name"] for event in final_events] == [
        "query_products",
        "generate_live_plan",
        "generate_product_card",
        "generate_product_card",
        "generate_product_card",
        "setup_live_session",
    ]

