"""
Phase 5F 播中 LLM Planner 演示脚本。

演示三种场景：
1. 弹幕集中（价格问题高频）-> LLM 建议主播强调优惠
2. 库存告警（商品售罄）-> LLM 建议主播切换备用
3. 无事件 -> LLM 不干预

对比 LLM 建议 vs 确定性规则建议，输出审计记录。
"""

from __future__ import annotations

from typing import Any

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.skills.on_live_llm_planner import OnLiveLLMPlanner, build_on_live_prompt


def scenario_price_focus() -> None:
    """场景 1：弹幕集中（价格问题高频），LLM 应建议主播强调优惠。"""
    print("=" * 60)
    print("场景 1：弹幕集中（价格问题高频）")
    print("=" * 60)

    danmaku_summary = [
        {"category": "价格问题", "summary": "价格太贵了", "count": 25, "sample_contents": ["多少钱", "太贵了", "有没有优惠"]},
        {"category": "质量问题", "summary": "质量怎么样", "count": 7, "sample_contents": ["耐穿吗", "会不会掉色"]},
    ]
    inventory_alerts = []
    trust_score = 0.8
    memory_hints = [("偏好中高端商品", 0.6)]

    prompt = build_on_live_prompt(danmaku_summary, inventory_alerts, trust_score, memory_hints)
    print()
    print("[Prompt 预览]（前 200 字符）")
    print("-" * 40)
    print(prompt[:200])
    print("...")
    print()

    planner = OnLiveLLMPlanner()
    decision = planner.plan(
        danmaku_summary=danmaku_summary,
        inventory_alerts=inventory_alerts,
        trust_score=trust_score,
        memory_hints=memory_hints,
    )
    print("[LLM 决策]")
    print("  route:      " + str(decision.get("route")))
    print("  goal:       " + str(decision.get("goal")))
    print("  suggestion: " + str(decision.get("suggestion")))
    print()

    rule_decision = planner._rule_fallback(danmaku_summary, inventory_alerts)
    print("[规则降级对比]")
    print("  route:      " + str(rule_decision.get("route")))
    print("  goal:       " + str(rule_decision.get("goal")))
    print("  suggestion: " + str(rule_decision.get("suggestion")))
    print()


def scenario_inventory_alert() -> None:
    """场景 2：库存告警，LLM 应建议主播关注库存并准备切换商品。"""
    print("=" * 60)
    print("场景 2：库存告警（商品售罄）")
    print("=" * 60)

    danmaku_summary = [
        {"category": "尺码问题", "summary": "还有大码吗", "count": 4, "sample_contents": ["大码", "XL"]},
    ]
    inventory_alerts = [
        {"product_id": "P001", "product_name": "爆款运动鞋", "severity": "critical", "remaining": 0},
        {"product_id": "P002", "product_name": "经典卫衣", "severity": "warning", "remaining": 5},
    ]
    trust_score = 0.7

    planner = OnLiveLLMPlanner()
    decision = planner.plan(
        danmaku_summary=danmaku_summary,
        inventory_alerts=inventory_alerts,
        trust_score=trust_score,
    )
    print("[LLM 决策]")
    print("  route:      " + str(decision.get("route")))
    print("  goal:       " + str(decision.get("goal")))
    print("  suggestion: " + str(decision.get("suggestion")))
    print()

    rule_decision = planner._rule_fallback(danmaku_summary, inventory_alerts)
    print("[规则降级对比]")
    print("  route:      " + str(rule_decision.get("route")))
    print("  goal:       " + str(rule_decision.get("goal")))
    print("  suggestion: " + str(rule_decision.get("suggestion")))
    print()


def scenario_no_events() -> None:
    """场景 3：无事件，LLM 应返回 finish 不干预。"""
    print("=" * 60)
    print("场景 3：无事件（不干预）")
    print("=" * 60)

    planner = OnLiveLLMPlanner()
    decision = planner.plan(danmaku_summary=[], inventory_alerts=[], trust_score=0.7)
    print("[LLM 决策]")
    print("  route:      " + str(decision.get("route")))
    print("  goal:       " + str(decision.get("goal")))
    print("  suggestion: " + str(decision.get("suggestion")))
    print()


def scenario_graph_integration() -> None:
    """场景 4：LLM Planner 集成到 LangGraph 运行。"""
    print("=" * 60)
    print("场景 4：LLM Planner + LangGraph 集成")
    print("=" * 60)

    llm_planner = OnLiveLLMPlanner()
    from src.core.on_live_agent_graph import build_on_live_agent_graph, create_initial_on_live_state
    graph = build_on_live_agent_graph(planner=llm_planner)

    state = create_initial_on_live_state(
        room_id="room_llm_demo",
        trace_id="trace_llm_001",
        trust_score=0.75,
        danmaku_summary=[
            {"category": "价格问题", "summary": "什么时候打折", "count": 15, "sample_contents": ["打折吗", "有活动吗"]},
        ],
        inventory_alerts=[],
    )

    result = graph.invoke(state)
    print("[Graph 结果]")
    print("  planner_route: " + str(result.get("planner_route")))
    print("  goal:          " + str(result.get("goal")))
    print("  suggestion:    " + str(result.get("suggestion")))
    print("  completed:     " + str(result.get("completed_nodes")))
    print()


def main() -> None:
    """运行所有演示场景。"""
    print()
    print("=" * 60)
    print("Phase 5F -- 播中 LLM Planner 演示")
    print("=" * 60)
    print()

    scenario_no_events()
    scenario_price_focus()
    scenario_inventory_alert()
    scenario_graph_integration()

    print("=" * 60)
    print("演示完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
