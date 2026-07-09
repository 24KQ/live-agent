"""Phase 5A Agent 播前图单元测试（精简版）。

测试 LangGraph 的基本路由、planner 决策和 fallback。
使用 InMemorySaver 验证 checkpoint 兼容性。
"""

import json
from decimal import Decimal

from langgraph.checkpoint.memory import InMemorySaver

from src.core.agent_decision import AgentPlannerDecision, AgentReplanRoute
from src.core.pre_live_agent_graph import (
    PreLiveAgentGraphState,
    build_pre_live_agent_graph,
    create_initial_agent_state,
)


def _make_service():
    """创建假 service，返回固定结果。"""
    from src.skills.product_catalog import CatalogProduct
    from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
    from src.skills.product_card_generator import ProductCard
    from src.core.security_hooks import GateDecision, GateResult

    class FakeService:
        def query_products(self, room_id, trace_id):
            return [
                CatalogProduct(
                    product_id="p001", name="测试商品A", category="日用",
                    price=Decimal("39.90"), inventory=100,
                    conversion_rate=Decimal("0.15"), commission_rate=Decimal("0.05"),
                    tags=["引流"], selling_points=["卖点A"],
                ),
            ]
        def generate_plan(self, room_id, products, trace_id):
            return LivePlanDraft(room_id=room_id, trace_id=trace_id,
                items=[LivePlanItem(rank=1, product_id=p.product_id, product_name=p.name, role="引流款", reason="test") for p in products])
        def generate_cards(self, room_id, plan, products, trace_id):
            return [ProductCard(product_id=it.product_id, title=it.product_name+"手卡", talking_points=["卖点1"], opening_script="开场", price_hint="价", risk_tips=[]) for it in plan.items]
        def setup_live_session(self, room_id, plan, trace_id, confirmed_setup):
            from src.core.security_hooks import GateDecision, GateResult
            if confirmed_setup:
                return GateResult(True, GateDecision.HARD_GATE, False, "ok"), "audit-setup-001"
            return GateResult(False, GateDecision.HARD_GATE, True, "待确认"), None
        def record_setup_approval_event(self, request, response):
            return "audit-approval-001"
    return FakeService()


def _make_planner(route="memory_first"):
    class FakePlanner:
        def plan(self, **kw):
            return AgentPlannerDecision(trace_id=kw.get("trace_id","t"), room_id=kw.get("room_id","r"), goal="测试", route=route, reason="测试", tool_calls=[])
    return FakePlanner()


class TestPreLiveAgentGraph:

    def test_initial_state_has_required_fields(self):
        state = create_initial_agent_state(room_id="room-001", trace_id="trace-001")
        assert state["room_id"] == "room-001"
        assert state["trace_id"] == "trace-001"
        assert state["replan_count"] == 0
        assert state["completed_nodes"] == []

    def test_graph_runs_all_planner_routes(self):
        for route in ["memory_first", "direct_plan", "cards_first", "risk_check", "finish", "fallback"]:
            graph = build_pre_live_agent_graph(planner=_make_planner(route), executor=None, service=_make_service())
            result = graph.invoke(create_initial_agent_state(room_id="room-001", trace_id=f"trace-{route}"))
            nodes = result.get("completed_nodes", [])
            assert "llm_planner" in nodes, f"{route}: no llm_planner in {nodes}"
            assert "setup_live_session" in nodes, f"{route}: no setup_live_session in {nodes}"

    def test_graph_finishes_after_setup_live_session(self):
        graph = build_pre_live_agent_graph(planner=_make_planner("finish"), executor=None, service=_make_service())
        result = graph.invoke(create_initial_agent_state(room_id="room-001", trace_id="trace-finish"))
        assert result.get("setup_status") in ("prepared", "error", "pending")

    def test_graph_supports_checkpointer(self):
        graph = build_pre_live_agent_graph(planner=_make_planner("finish"), executor=None, service=_make_service(), checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "trace-check"}}
        result = graph.invoke(create_initial_agent_state(room_id="room-001", trace_id="trace-check"), config=config)
        assert "completed_nodes" in result

    def test_graph_state_is_json_serializable(self):
        state = create_initial_agent_state(room_id="room-001", trace_id="trace-001")
        json.dumps(state, ensure_ascii=False)
        assert True
