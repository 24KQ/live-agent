"""Phase 5H Harness Agent 审计闭环集成测试。

本测试不连接 PostgreSQL，而是用 fake audit writer 验证 LangGraph 状态能在完整播中链路结束后
进入审计节点，且审计 payload 与工具执行顺序一致。
"""

from __future__ import annotations

from typing import Any

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class InventoryThenFinalPlanner:
    """库存售罄场景：第一轮调工具，第二轮基于 observation 给最终建议。"""

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        if kwargs.get("observations"):
            return OnLiveHarnessDecision(
                thought="备用商品已找到，生成最终建议",
                action="final_answer",
                final_suggestion="建议主播说明当前商品售罄，并自然切换到备用商品 p002。",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="库存售罄，先推荐备用商品",
            action="call_tool",
            tool_name="recommend_backup_product",
            arguments={"sold_out_product_id": "p001"},
            risk_level="MEDIUM",
        )


class IntegrationExecutor:
    """集成测试用本地执行器，返回稳定的工具结果。"""

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
            "summary": f"{room_id}:{tool_name}:ok",
            "backup_product_id": "p002",
            "arguments": arguments,
        }


class FakeAuditWriter:
    """记录 Graph 传入的最终 state，并返回稳定的审计 ID。"""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.append(dict(state))
        return {
            "audit_status": "recorded",
            "audit_ids": ["audit-integration-1"],
            "decision_trace_ids": ["decision-integration-1"],
            "audit_payload": {
                "executed_tool_names": [tool["tool_name"] for tool in state.get("executed_tools", [])],
                "agent_status": state.get("agent_status"),
            },
        }


def test_harness_inventory_flow_writes_audit_after_observation() -> None:
    """库存告警完整链路：工具执行、observation、最终建议、审计写入顺序一致。"""
    audit_writer = FakeAuditWriter()
    graph = build_on_live_harness_agent_graph(
        planner=InventoryThenFinalPlanner(),
        executor=IntegrationExecutor(),
        audit_writer=audit_writer,
    )
    state = create_initial_on_live_harness_state(
        room_id="room-5h-integration",
        trace_id="trace-5h-integration",
        anchor_id="anchor-5h",
        inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
    )

    result = graph.invoke(state)

    assert result["agent_status"] == "final_answer"
    assert result["audit_status"] == "recorded"
    assert result["audit_ids"] == ["audit-integration-1"]
    assert result["decision_trace_ids"] == ["decision-integration-1"]
    assert result["executed_tools"][0]["tool_name"] == "recommend_backup_product"
    assert result["observations"][0]["tool_name"] == "recommend_backup_product"
    assert "write_audit" == result["completed_nodes"][-1]
    assert audit_writer.states[0]["final_suggestion"] == result["final_suggestion"]
    assert audit_writer.states[0]["completed_nodes"][-1] == "write_audit"
