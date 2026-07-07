"""Phase 2F 播前 Graph 人审 interrupt PostgreSQL 集成测试。"""

from __future__ import annotations

from uuid import uuid4

from langgraph.types import Command

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.langgraph_checkpoint import create_postgres_checkpointer, initialize_postgres_checkpointer
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.pre_live_graph import build_pre_live_graph, create_initial_pre_live_graph_state, create_pre_live_graph_config
from src.skills.demo_data_seed import DEMO_ROOM_ID, initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def _prepare_integration_dependencies() -> tuple[ToolCallAuditStore, str]:
    """初始化 Phase 2F 集成测试依赖，并返回审计 Store 和唯一 trace_id。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    initialize_postgres_checkpointer(settings)
    seed_phase2_demo_data(settings)
    return ToolCallAuditStore(settings), f"trace-phase2f-integration-{uuid4()}"


def test_pre_live_graph_interrupt_approve_flow_recovers_and_writes_non_duplicate_audit() -> None:
    """批准场景应从 PostgresSaver 恢复，并只写一条 pending、一条 approved 和一条建播成功审计。"""

    audit_store, trace_id = _prepare_integration_dependencies()
    settings = get_settings()
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
    assert interrupt_payload["tool_name"] == "setup_live_session"
    assert interrupt_payload["risk_level"] == "HIGH"
    first_events = audit_store.list_events_by_trace_id(trace_id)
    assert [event["tool_name"] for event in first_events].count("query_products") == 1
    assert [event["tool_name"] for event in first_events].count("generate_live_plan") == 1
    assert [event["tool_name"] for event in first_events].count("generate_product_card") == 3
    assert [event["tool_name"] for event in first_events].count("setup_live_session_approval") == 1
    assert "setup_live_session" not in [event["tool_name"] for event in first_events]

    with create_postgres_checkpointer(settings) as resumed_checkpointer:
        resumed_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        resumed_graph = build_pre_live_graph(resumed_service, checkpointer=resumed_checkpointer)
        resumed_result = resumed_graph.invoke(
            Command(
                resume={
                    "trace_id": trace_id,
                    "room_id": DEMO_ROOM_ID,
                    "tool_name": "setup_live_session",
                    "decision": "approved",
                    "operator_id": "operator-demo",
                    "reason": "确认建播配置无误。",
                }
            ),
            config=config,
        )

    assert resumed_result["setup_status"] == "prepared"
    assert resumed_result["approval_decision"] == "approved"
    final_events = audit_store.list_events_by_trace_id(trace_id)
    assert [event["tool_name"] for event in final_events].count("query_products") == 1
    assert [event["tool_name"] for event in final_events].count("generate_live_plan") == 1
    assert [event["tool_name"] for event in final_events].count("generate_product_card") == 3
    assert [event["tool_name"] for event in final_events].count("setup_live_session_approval") == 2
    assert [event["tool_name"] for event in final_events].count("setup_live_session") == 1
    approval_decisions = [
        event["operator_decision"]
        for event in final_events
        if event["tool_name"] == "setup_live_session_approval"
    ]
    assert approval_decisions == ["pending", "approved"]


def test_pre_live_graph_interrupt_reject_flow_recovers_without_setup_success_audit() -> None:
    """拒绝场景应记录拒绝原因，但不得写入建播成功审计。"""

    audit_store, trace_id = _prepare_integration_dependencies()
    settings = get_settings()
    config = create_pre_live_graph_config(trace_id)

    with create_postgres_checkpointer(settings) as first_checkpointer:
        first_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        first_graph = build_pre_live_graph(first_service, checkpointer=first_checkpointer)
        first_graph.invoke(
            create_initial_pre_live_graph_state(
                room_id=DEMO_ROOM_ID,
                trace_id=trace_id,
                confirmed_setup=False,
                enable_human_approval=True,
            ),
            config=config,
        )

    with create_postgres_checkpointer(settings) as resumed_checkpointer:
        resumed_service = PreLiveBusinessFlowService(ProductCatalogRepository(settings), audit_store)
        resumed_graph = build_pre_live_graph(resumed_service, checkpointer=resumed_checkpointer)
        resumed_result = resumed_graph.invoke(
            Command(
                resume={
                    "trace_id": trace_id,
                    "room_id": DEMO_ROOM_ID,
                    "tool_name": "setup_live_session",
                    "decision": "rejected",
                    "operator_id": "operator-demo",
                    "reason": "排品顺序需要先调整。",
                }
            ),
            config=config,
        )

    assert resumed_result["setup_status"] == "rejected"
    assert resumed_result["setup_audit_id"] is None
    final_events = audit_store.list_events_by_trace_id(trace_id)
    assert [event["tool_name"] for event in final_events].count("query_products") == 1
    assert [event["tool_name"] for event in final_events].count("generate_live_plan") == 1
    assert [event["tool_name"] for event in final_events].count("generate_product_card") == 3
    assert [event["tool_name"] for event in final_events].count("setup_live_session_approval") == 2
    assert "setup_live_session" not in [event["tool_name"] for event in final_events]
    rejection_events = [
        event for event in final_events if event["tool_name"] == "setup_live_session_approval"
    ]
    assert rejection_events[-1]["operator_decision"] == "rejected"
    assert rejection_events[-1]["result_payload"]["reason"] == "排品顺序需要先调整。"
