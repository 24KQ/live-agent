"""Phase 5G-B LangGraph Harness Agent Loop CLI 演示。

演示重点不是“LLM 调一次工具”，而是 LangGraph 显式编排：
load_context / pre_reasoning_hook / agent_reasoning / route_agent_decision /
pre_tool_call_hook / route_tool_policy / execute_tool / post_tool_call_hook /
observe_result / route_replan / write_audit。
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.on_live_harness_agent_graph import (  # noqa: E402
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision  # noqa: E402


class DemoPlanner:
    """演示用 deterministic planner，避免 CLI 依赖真实 LLM。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        observations = kwargs.get("observations", [])
        if self.mode == "no_action":
            return OnLiveHarnessDecision(
                thought="无事件，不干预",
                action="no_action",
                risk_level="LOW",
            )
        if self.mode == "danmaku":
            return OnLiveHarnessDecision(
                thought="弹幕价格问题集中，直接给主播建议",
                action="final_answer",
                final_suggestion="建议主播强调券后价、保价周期和赠品权益。",
                risk_level="LOW",
            )
        if self.mode == "inventory" and not observations:
            return OnLiveHarnessDecision(
                thought="库存售罄，需要先找备选商品",
                action="call_tool",
                tool_name="recommend_backup_product",
                arguments={"sold_out_product_id": "p001"},
                risk_level="MEDIUM",
            )
        return OnLiveHarnessDecision(
            thought="工具结果已返回，生成最终建议",
            action="final_answer",
            final_suggestion="建议主播说明当前商品售罄，并自然切到备选商品 p002。",
            risk_level="LOW",
        )


class DemoExecutor:
    """演示用工具执行器。"""

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        **kwargs,
    ) -> dict[str, Any]:
        return {
            "tool_name": tool_name,
            "status": "success",
            "summary": f"{tool_name} executed",
            "backup_product_id": "p002",
        }


def _print_result(title: str, result: dict[str, Any]) -> None:
    """打印单个场景结果。"""
    print("=" * 72)
    print(title)
    print("=" * 72)
    print("节点路径:")
    print(" -> ".join(result.get("completed_nodes", [])))
    print()
    print("iteration:", result.get("iteration"))
    print("agent_status:", result.get("agent_status"))
    print("tool_policy:", result.get("tool_policy"))
    print("executed_tools:", result.get("executed_tools"))
    print("observations:", result.get("observations"))
    print("final_suggestion:", result.get("final_suggestion"))
    print("audit_status:", result.get("audit_status"))
    print("audit_ids:", result.get("audit_ids"))
    print("decision_trace_ids:", result.get("decision_trace_ids"))
    audit_payload = result.get("audit_payload") or {}
    if audit_payload:
        print("audit_payload keys:", sorted(audit_payload.keys()))
        print("dry_run decision trace:", audit_payload.get("decision_trace_dry_run"))
    print("error:", result.get("error"))
    print()


def run_no_action() -> None:
    """场景 1：无事件 -> no_action -> END。"""
    graph = build_on_live_harness_agent_graph(planner=DemoPlanner("no_action"), executor=DemoExecutor())
    state = create_initial_on_live_harness_state(room_id="room-demo-5g", trace_id="trace-demo-no-action")
    _print_result("场景 1：无事件 -> no_action", graph.invoke(state))


def run_danmaku() -> None:
    """场景 2：价格弹幕高频 -> final_answer。"""
    graph = build_on_live_harness_agent_graph(planner=DemoPlanner("danmaku"), executor=DemoExecutor())
    state = create_initial_on_live_harness_state(
        room_id="room-demo-5g",
        trace_id="trace-demo-danmaku",
        danmaku_summary=[{"category": "price", "summary": "价格问题", "count": 20}],
    )
    _print_result("场景 2：弹幕价格高频 -> final_answer", graph.invoke(state))


def run_inventory() -> None:
    """场景 3：库存售罄 -> call_tool -> observation -> final_answer。"""
    graph = build_on_live_harness_agent_graph(planner=DemoPlanner("inventory"), executor=DemoExecutor())
    state = create_initial_on_live_harness_state(
        room_id="room-demo-5g",
        trace_id="trace-demo-inventory",
        inventory_alerts=[{"product_id": "p001", "product_name": "爆款鞋", "severity": "sold_out"}],
    )
    _print_result("场景 3：库存售罄 -> 工具调用 -> 最终建议", graph.invoke(state))


def main() -> None:
    """运行所有 Phase 5G-B 演示场景。"""
    print("Phase 5G-B LangGraph Harness Agent Loop Demo")
    print()
    run_no_action()
    run_danmaku()
    run_inventory()


if __name__ == "__main__":
    main()
