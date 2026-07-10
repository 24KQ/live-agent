"""Phase 5G-B 播中 Harness Agent 集成测试。

这里不依赖真实平台 API，只验证 Harness Agent 图能把上下文、决策、工具、观察、
再规划串成完整闭环。
"""

from __future__ import annotations

from typing import Any

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class InventoryHarnessPlanner:
    """库存告警场景：先推荐备选，再生成最终建议。"""

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        if kwargs.get("observations"):
            return OnLiveHarnessDecision(
                thought="已获取备选商品",
                action="final_answer",
                final_suggestion="建议主播说明当前商品售罄，并切到备选商品 p002。",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="库存售罄，需要先推荐备选商品",
            action="call_tool",
            tool_name="recommend_backup_product",
            arguments={"sold_out_product_id": "p001"},
            risk_level="MEDIUM",
        )


class DanmakuHarnessPlanner:
    """弹幕高频场景：直接生成主播建议。"""

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="价格问题高频，直接给主播参考话术",
            action="final_answer",
            final_suggestion="建议主播解释券后价、保价周期和赠品权益。",
            risk_level="LOW",
        )


class HarnessExecutor:
    """集成测试用执行器，返回结构化工具结果。"""

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
            "summary": f"{tool_name} success for {room_id}",
            "backup_product_id": "p002",
        }


def test_inventory_alert_harness_flow() -> None:
    """库存告警：工具调用 -> observation 回灌 -> 最终建议。"""
    graph = build_on_live_harness_agent_graph(
        planner=InventoryHarnessPlanner(),
        executor=HarnessExecutor(),
    )
    state = create_initial_on_live_harness_state(
        room_id="room-integration-5g",
        trace_id="trace-integration-5g",
        inventory_alerts=[{"product_id": "p001", "product_name": "爆款鞋", "severity": "sold_out"}],
    )
    result = graph.invoke(state)
    assert result["agent_status"] == "final_answer"
    assert result["executed_tools"][0]["tool_name"] == "recommend_backup_product"
    assert result["observations"][0]["status"] == "success"
    assert "备选商品" in result["final_suggestion"]


def test_danmaku_harness_flow() -> None:
    """弹幕高频：直接生成主播建议，不调用工具。"""
    graph = build_on_live_harness_agent_graph(planner=DanmakuHarnessPlanner(), executor=HarnessExecutor())
    state = create_initial_on_live_harness_state(
        room_id="room-integration-5g",
        trace_id="trace-integration-5g",
        danmaku_summary=[{"category": "price", "summary": "价格问题", "count": 20}],
    )
    result = graph.invoke(state)
    assert result["agent_status"] == "final_answer"
    assert result["executed_tools"] == []
    assert "券后价" in result["final_suggestion"]
    assert "route_agent_decision" in result["completed_nodes"]
