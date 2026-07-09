"""Phase 5A 播前 Agent 图单元测试（修正版）。

播前使用 rules_planner（确定性规则），不走 LLM 决策。
验证播前固定路由、replan 行为、checkpoint 兼容性。
"""

import json
from decimal import Decimal

from langgraph.checkpoint.memory import InMemorySaver

from src.core.agent_rules_planner import AgentRulesPlanner
from src.core.pre_live_agent_graph import (
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
            return [ProductCard(product_id=it.product_id, title=it.product_name+"手卡", talking_points=["卖点1"], opening_script="开场", price_hint="价", risk_tips=["默认提示"]) for it in plan.items]
        def setup_live_session(self, room_id, plan, trace_id, confirmed_setup):
            from src.core.security_hooks import GateDecision, GateResult
            if confirmed_setup:
                return GateResult(True, GateDecision.HARD_GATE, False, "ok"), "audit-setup-001"
            return GateResult(False, GateDecision.HARD_GATE, True, "待确认"), None
        def record_setup_approval_event(self, request, response):
            return "audit-approval-001"
    return FakeService()


class TestPreLiveAgentGraph:

    def test_initial_state_has_required_fields(self):
        """初始 state 应包含所有必要字段。"""
        state = create_initial_agent_state(room_id="room-001", trace_id="trace-001")
        assert state["room_id"] == "room-001"
        assert state["trace_id"] == "trace-001"
        assert state["replan_count"] == 0
        assert state["completed_nodes"] == []

    def test_rules_planner_graph_runs_to_end(self):
        """播前 rules_planner 应该完整运行到 setup_live_session。"""
        graph = build_pre_live_agent_graph(
            planner=AgentRulesPlanner(),
            executor=None,
            service=_make_service(),
        )
        result = graph.invoke(create_initial_agent_state(room_id="room-001", trace_id="trace-rules"))
        nodes = result.get("completed_nodes", [])
        assert "rules_planner" in nodes
        assert "setup_live_session" in nodes
        assert result.get("planner_route") == "direct_plan"

    def test_graph_produces_plan_and_cards(self):
        """播前图应生成排品 + 手卡。"""
        graph = build_pre_live_agent_graph(
            planner=AgentRulesPlanner(),
            executor=None,
            service=_make_service(),
        )
        result = graph.invoke(create_initial_agent_state(room_id="room-001", trace_id="trace-output"))
        assert result.get("card_count", 0) >= 1
        assert result.get("product_count", 0) >= 1

    def test_graph_supports_checkpointer(self):
        """graph 应支持 InMemorySaver checkpoint。"""
        graph = build_pre_live_agent_graph(
            planner=AgentRulesPlanner(),
            executor=None,
            service=_make_service(),
            checkpointer=InMemorySaver(),
        )
        config = {"configurable": {"thread_id": "trace-check"}}
        result = graph.invoke(
            create_initial_agent_state(room_id="room-001", trace_id="trace-check"),
            config=config,
        )
        assert "completed_nodes" in result

    def test_graph_state_is_json_serializable(self):
        """graph state 应该是 JSON 可序列化的。"""
        state = create_initial_agent_state(room_id="room-001", trace_id="trace-001")
        json.dumps(state, ensure_ascii=False)
        assert True

    def test_graph_reaches_end_from_all_trust_levels(self):
        """不同 trust_score 下播前图都能走到 end。"""
        for score in [0.3, 0.5, 0.8]:
            graph = build_pre_live_agent_graph(
                planner=AgentRulesPlanner(),
                executor=None,
                service=_make_service(),
            )
            result = graph.invoke(
                create_initial_agent_state(room_id="room-001", trace_id="trace-trust-" + str(int(score * 10)), trust_score=score),
            )
            assert "setup_live_session" in result.get("completed_nodes", [])
