"""Phase 5A Agent 播前图 CLI 演示。

演示三种场景：
1. 正常 Agent 路由：planner 选择 memory_first -> 生成排品和手卡 -> 建播
2. LLM 失败 fallback：planner 返回 fallback 路由 -> 走确定性链路
3. 演示输出：trace_id、planner route、completed nodes、setup_status

用法：
    python scripts/run_phase5a_pre_live_agent_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timezone
from decimal import Decimal

from src.core.agent_decision import AgentPlannerDecision, AgentReplanRoute
from src.core.pre_live_agent_graph import build_pre_live_agent_graph, create_initial_agent_state
from src.skills.product_catalog import CatalogProduct
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.core.security_hooks import GateDecision, GateResult


def _run_scenario(name, route, trace_id_suffix):
    trace_id = "phase5a-" + trace_id_suffix

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
                CatalogProduct(product_id="p003", name="加绒打底裤", category="服饰",
                    price=Decimal("59.90"), inventory=800,
                    conversion_rate=Decimal("0.30"), commission_rate=Decimal("0.10"),
                    tags=["爆款", "冬季"], selling_points=["厚实保暖", "不起球"]),
            ]
        def generate_plan(self, room_id, products, trace_id):
            items = []
            for idx, p in enumerate(products, 1):
                role = "引流款" if idx == 1 else ("利润款" if idx == 2 else "福利款")
                items.append(LivePlanItem(rank=idx, product_id=p.product_id, product_name=p.name, role=role, reason="策略"))
            return LivePlanDraft(room_id=room_id, trace_id=trace_id, items=items)
        def generate_cards(self, room_id, plan, products, trace_id):
            return [ProductCard(product_id=it.product_id, title=it.product_name + "手卡", talking_points=["卖点一"], opening_script="开场", price_hint="价", risk_tips=["默认提示"]) for it in plan.items]
        def setup_live_session(self, room_id, plan, trace_id, confirmed_setup):
            if confirmed_setup:
                return GateResult(True, GateDecision.HARD_GATE, False, "已确认"), "audit-" + trace_id
            return GateResult(False, GateDecision.HARD_GATE, True, "待确认"), None
        def record_setup_approval_event(self, request, response):
            return "audit-approval-" + trace_id

    if route == "fallback":
        class FP:
            def plan(self, **kw):
                return AgentPlannerDecision(trace_id=trace_id, room_id="room-demo-001", goal="fallback", route=AgentReplanRoute.FALLBACK, reason="LLM unavailable", tool_calls=[], fallback_reason="Mock fallback for demo")
        planner = FP()
    else:
        class DP:
            def plan(self, **kw):
                return AgentPlannerDecision(trace_id=trace_id, room_id="room-demo-001", goal="播前排品", route=route, reason="根据记忆和历史表现", tool_calls=[])
        planner = DP()

    service = FakeService()
    graph = build_pre_live_agent_graph(planner=planner, executor=None, service=service)

    print()
    print("=" * 60)
    print("  场景: " + name)
    print("  trace_id: " + trace_id)
    print("=" * 60)

    result = graph.invoke(create_initial_agent_state(room_id="room-demo-001", trace_id=trace_id))

    print("  Planner 路由: " + str(result.get("planner_route", "N/A")))
    print("  Planner fallback: " + str(result.get("planner_fallback", False)))
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
    print("Phase 5A Agent 播前编排图 CLI 演示")
    print("=" * 60)
    print("时间: " + datetime.now(timezone.utc).isoformat())
    print("说明: 使用 fake planner 和 fake service。")
    print()
    _run_scenario("正常 Agent 路由 (memory_first)", "memory_first", "normal")
    _run_scenario("直接排品 (direct_plan)", "direct_plan", "direct")
    _run_scenario("LLM 失败 fallback", "fallback", "fallback")
    _run_scenario("完成路由 (finish)", "finish", "finish")
    print("演示完成。")


if __name__ == "__main__":
    main()