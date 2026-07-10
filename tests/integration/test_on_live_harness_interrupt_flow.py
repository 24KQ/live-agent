"""Phase 5I 播中 Harness interrupt 集成测试。

使用 InMemorySaver 跑完整 approve / reject 链路，验证同一 thread_id 下恢复执行。
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class HighRiskPlanner:
    """集成测试 planner：先请求高风险工具，观察后给最终建议。"""

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        if kwargs.get("observations"):
            return OnLiveHarnessDecision(
                thought="人审批准后工具已执行",
                action="final_answer",
                final_suggestion="售罄处理已完成，请主播切换讲解节奏。",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="库存售罄，需要高风险工具处理",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class Executor:
    """稳定返回高风险工具执行结果。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        **kwargs,
    ) -> dict[str, Any]:
        self.calls.append(tool_name)
        return {
            "tool_name": tool_name,
            "status": "success",
            "summary": f"{tool_name} approved and executed",
        }


def _run_first(trace_id: str, executor: Executor):
    """启动 graph 并运行到 interrupt。"""

    graph = build_on_live_harness_agent_graph(
        planner=HighRiskPlanner(),
        executor=executor,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": trace_id}}
    first = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-5i-integration",
            trace_id=trace_id,
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        ),
        config=config,
    )
    return graph, config, first


def test_on_live_harness_interrupt_approve_flow() -> None:
    """approve 链路：interrupt 后恢复，工具执行，最终建议和审计状态可见。"""

    executor = Executor()
    graph, config, first = _run_first("trace-5i-int-approved", executor)

    assert first["__interrupt__"][0].value["tool_name"] == "handle_sold_out_event"
    assert executor.calls == []

    result = graph.invoke(
        Command(
            resume={
                "trace_id": "trace-5i-int-approved",
                "room_id": "room-5i-integration",
                "tool_name": "handle_sold_out_event",
                "decision": "approved",
                "operator_id": "operator-demo",
                "reason": "允许执行售罄处理。",
            }
        ),
        config=config,
    )

    assert executor.calls == ["handle_sold_out_event"]
    assert result["approval_decision"] == "approved"
    assert result["agent_status"] == "final_answer"
    assert result["observations"][0]["status"] == "success"
    assert result["audit_status"] == "dry_run"


def test_on_live_harness_interrupt_reject_flow() -> None:
    """reject 链路：interrupt 后恢复，不执行工具，直接审计结束。"""

    executor = Executor()
    graph, config, first = _run_first("trace-5i-int-rejected", executor)

    assert first["__interrupt__"][0].value["tool_name"] == "handle_sold_out_event"

    result = graph.invoke(
        Command(
            resume={
                "trace_id": "trace-5i-int-rejected",
                "room_id": "room-5i-integration",
                "tool_name": "handle_sold_out_event",
                "decision": "rejected",
                "operator_id": "operator-demo",
                "reason": "主播选择手动处理。",
            }
        ),
        config=config,
    )

    assert executor.calls == []
    assert result["approval_decision"] == "rejected"
    assert result["agent_status"] == "rejected_by_human"
    assert result["audit_status"] == "dry_run"
