"""Phase 11A Skill Runtime 播前 Graph 集成测试。

测试使用真实 PostgreSQL 货盘与审计、LangGraph InMemorySaver 以及两批
SKILL_RUNTIME 路由，覆盖完整 generation 流程和 HUMAN_INTERRUPT 恢复。
"""

from __future__ import annotations

from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.pre_live_graph import (
    build_pre_live_graph,
    create_initial_pre_live_graph_state,
    create_pre_live_graph_config,
)
from src.skill_runtime.pre_live_facade import RoutedPreLiveBusinessService
from src.skills.demo_data_seed import (
    DEMO_ROOM_ID,
    initialize_phase2_schema,
    seed_phase2_demo_data,
)


def _build_runtime_graph():
    """装配两批均走 Runtime 的真实播前 Graph。"""
    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    runtime_settings = settings.model_copy(
        update={
            "skill_route_prelive_generation": "SKILL_RUNTIME",
            "skill_route_prelive_setup": "SKILL_RUNTIME",
        }
    )
    service = RoutedPreLiveBusinessService.from_settings(runtime_settings)
    return build_pre_live_graph(service, checkpointer=InMemorySaver()), ToolCallAuditStore(settings)


def test_runtime_graph_approved_resume_runs_all_core_skills() -> None:
    """人工批准后，Graph 应携带 HUMAN_INTERRUPT 证据完成四个核心 Skill。"""
    graph, audit_store = _build_runtime_graph()
    trace_id = f"trace-phase11a-runtime-approved-{uuid4()}"
    config = create_pre_live_graph_config(trace_id)

    first = graph.invoke(
        create_initial_pre_live_graph_state(
            room_id=DEMO_ROOM_ID,
            trace_id=trace_id,
            confirmed_setup=False,
            enable_human_approval=True,
        ),
        config=config,
    )
    assert first["__interrupt__"][0].value["tool_name"] == "setup_live_session"

    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": trace_id,
                "room_id": DEMO_ROOM_ID,
                "tool_name": "setup_live_session",
                "decision": "approved",
                "operator_id": "operator-phase11a",
                "reason": "确认 Runtime 建播。",
            }
        ),
        config=config,
    )

    assert resumed["product_count"] == 10
    assert resumed["plan_item_count"] == 10
    assert resumed["card_count"] == 3
    assert resumed["setup_status"] == "prepared"
    assert resumed["setup_gate_allowed"] is True
    assert resumed["setup_audit_id"]

    events = audit_store.list_events_by_trace_id(trace_id)
    tool_names = [event["tool_name"] for event in events]
    assert tool_names.count("query_products") == 1
    assert tool_names.count("generate_live_plan") == 1
    assert tool_names.count("generate_product_card") == 3
    assert tool_names.count("setup_live_session_approval") == 2
    assert tool_names.count("setup_live_session") == 1


def test_runtime_graph_rejected_resume_never_calls_setup_handler() -> None:
    """人工拒绝后只记录拒绝审计，不得产生建播成功副作用。"""
    graph, audit_store = _build_runtime_graph()
    trace_id = f"trace-phase11a-runtime-rejected-{uuid4()}"
    config = create_pre_live_graph_config(trace_id)

    graph.invoke(
        create_initial_pre_live_graph_state(
            room_id=DEMO_ROOM_ID,
            trace_id=trace_id,
            confirmed_setup=False,
            enable_human_approval=True,
        ),
        config=config,
    )
    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": trace_id,
                "room_id": DEMO_ROOM_ID,
                "tool_name": "setup_live_session",
                "decision": "rejected",
                "operator_id": "operator-phase11a",
                "reason": "拒绝 Runtime 建播。",
            }
        ),
        config=config,
    )

    assert resumed["setup_status"] == "rejected"
    assert resumed["setup_audit_id"] is None
    events = audit_store.list_events_by_trace_id(trace_id)
    assert "setup_live_session" not in [event["tool_name"] for event in events]
