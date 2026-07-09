"""Phase 5A 播前 Agent 编排图 CLI 演示（修正版）。

播前使用 rules_planner（确定性规则），不走 LLM 决策。

演示内容：
1. rules_planner 播前路由
2. 生成排品和手卡
3. 建播确认
4. 低信任分状态

用法：python scripts/run_phase5a_pre_live_agent_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timezone
from decimal import Decimal

from src.core.agent_rules_planner import AgentRulesPlanner
from src.core.pre_live_agent_graph import build_pre_live_agent_graph, create_initial_agent_state
from src.skills.product_catalog import CatalogProduct
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.core.security_hooks import GateDecision, GateResult


def _make_service():
    """创建假 service。"""
    class FakeService:
        def query_products(self, room_id, trace_id):
            return [
                CatalogProduct(product_id="p001", name="轻薄羽绒服", category="服饰",
                    price=Decimal("199.00"), inventory=500,
                    conversion_rate=Decimal("0.25"), commission_rate=Decimal("0.15"),
                    tags=["爆款", "冬季"], selling_points=["轻便", "保暖", "显瘦"]),
                CatalogProduct(product_id="p002", name="暖宝宝贴", category="日用",
                    price=Decimal("19.90"), inventory=2000,
                    conversion_rate=Decimal("0.40"), commission_rate=Decimal("0.05"),
                    tags=["引流", "冬季"], selling_points=["发热持久", "随身携带"]),
            ]
        def generate_plan(self, room_id, products, trace_id):
            items = []
            for idx, p in enumerate(products, 1):
                role = "引流款" if idx == 1 else "利润款"
                items.append(LivePlanItem(rank=idx, product_id=p.product_id, product_name=p.name, role=role, reason="策略"))
            return LivePlanDraft(room_id=room_id, trace_id=trace_id, items=items)
        def generate_cards(self, room_id, plan, products, trace_id):
            return [ProductCard(product_id=it.product_id, title=it.product_name+"手卡", talking_points=["卖点一"], opening_script="开场", price_hint="价", risk_tips=["默认提示"]) for it in plan.items]
        def setup_live_session(self, room_id, plan, trace_id, confirmed_setup):
            if confirmed_setup:
                return GateResult(True, GateDecision.HARD_GATE, False, "已确认"), "audit-" + trace_id
            return GateResult(False, GateDecision.HARD_GATE, True, "待确认"), None
        def record_setup_approval_event(self, request, response):
            return "audit-approval-" + trace_id
    return FakeService()


def _run_scenario(name, trust_score, trace_id_suffix):
    """运行单个场景。"""
    trace_id = "phase5a-" + trace_id_suffix
    service = _make_service()
    graph = build_pre_live_agent_graph(planner=AgentRulesPlanner(), executor=None, service=service)

    print()
    print("=" * 60)
    print("  场景: " + name)
    print("  trace_id: " + trace_id)
    print("  trust_score: " + str(trust_score))
    print("=" * 60)

    result = graph.invoke(create_initial_agent_state(room_id="room-demo-001", trace_id=trace_id, trust_score=trust_score))

    print("  Planner 路由: " + str(result.get("planner_route", "N/A")))
    print("  Planner reason: " + str(result.get("planner_reason", "N/A")))
    print("  已完成节点: " + str(result.get("completed_nodes", [])))
    print("  商品数: " + str(result.get("product_count", 0)))
    print("  手卡数: " + str(result.get("card_count", 0)))
    print("  建播状态: " + str(result.get("setup_status", "N/A")))
    print("  建播审计: " + str(result.get("setup_audit_id", "N/A")))
    if result.get("error"):
        print("  错误: " + result["error"])
    print()


def main():
    print("Phase 5A 播前 Agent 编排图 CLI 演示（修正版）")
    print("=" * 60)
    print("说明：使用 AgentRulesPlanner（确定性规则），不走 LLM 决策。")
    print("播前图作为实验/预研路径保留，默认播前仍走 pre_live_graph.py。")
    print()

    _run_scenario("正常播前路由 (trust_score=0.7)", 0.7, "normal")
    _run_scenario("低信任分播前 (trust_score=0.3)", 0.3, "low-trust")

    print("演示完成。播前固定走 deterministic_prelive 路径。")


if __name__ == "__main__":
    main()
