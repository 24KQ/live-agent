"""Phase 5I LangGraph Harness interrupt 人审恢复演示。

脚本演示播中 Agent 遇到高风险工具时不会直接执行，而是通过 LangGraph interrupt 暂停；
随后用同一个 thread_id 和 Command(resume=...) 分别恢复 approve / reject 两条路径。
"""

from __future__ import annotations

import os
import sys
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.on_live_harness_agent_graph import (  # noqa: E402
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision  # noqa: E402


class DemoHighRiskPlanner:
    """演示用确定性 planner：先请求高风险售罄工具，观察后输出最终建议。"""

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        observations = kwargs.get("observations", [])
        if observations:
            return OnLiveHarnessDecision(
                thought="售罄工具已在人审批准后执行，生成最终主播建议",
                action="final_answer",
                final_suggestion="建议主播说明当前商品已售罄，并切换到备用讲解节奏。",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="检测到商品售罄，需要调用高风险售罄处理工具",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class DemoExecutor:
    """演示用执行器，记录高风险工具是否真的在 approve 后才执行。"""

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
            "summary": f"{tool_name} approved and executed for {room_id}",
            "arguments": arguments,
        }


def _config(trace_id: str) -> dict[str, Any]:
    """使用 trace_id 作为 LangGraph thread_id，保证恢复命中同一条执行链路。"""

    return {"configurable": {"thread_id": trace_id}}


def _run_scenario(decision: str) -> dict[str, Any]:
    """运行单个 approve / reject 场景。"""

    trace_id = f"trace-phase5i-{decision}"
    executor = DemoExecutor()
    graph = build_on_live_harness_agent_graph(
        planner=DemoHighRiskPlanner(),
        executor=executor,
        checkpointer=InMemorySaver(),
    )
    config = _config(trace_id)
    first_result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-demo-5i",
            trace_id=trace_id,
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        ),
        config=config,
    )
    interrupt_payload = first_result["__interrupt__"][0].value
    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": trace_id,
                "room_id": "room-demo-5i",
                "tool_name": interrupt_payload["tool_name"],
                "decision": decision,
                "operator_id": "operator-demo",
                "reason": "CLI 演示审批结果。",
            }
        ),
        config=config,
    )
    return {
        "decision": decision,
        "thread_id": trace_id,
        "interrupt_payload": interrupt_payload,
        "resumed": resumed,
        "executor_calls": executor.calls,
    }


def _print_scenario(result: dict[str, Any]) -> None:
    """打印单个场景结果，突出 interrupt payload 和恢复后的最终状态。"""

    payload = result["interrupt_payload"]
    resumed = result["resumed"]
    print("=" * 72)
    print(f"Phase 5I scenario: {result['decision']}")
    print("=" * 72)
    print("thread_id:", result["thread_id"])
    print("interrupt_tool:", payload["tool_name"])
    print("interrupt_risk_level:", payload["risk_level"])
    print("interrupt_arguments:", payload["tool_arguments"])
    print("resume_decision:", resumed.get("approval_decision"))
    print("agent_status:", resumed.get("agent_status"))
    print("completed_nodes:", " -> ".join(resumed.get("completed_nodes", [])))
    print("executed_tools:", resumed.get("executed_tools"))
    print("observations:", resumed.get("observations"))
    print("audit_status:", resumed.get("audit_status"))
    print("audit_ids:", resumed.get("audit_ids"))
    print("executor_calls:", result["executor_calls"])
    print()


def main() -> None:
    """运行 approve / reject 两条人审恢复演示。"""

    print("Phase 5I LangGraph Harness interrupt human approval demo")
    print()
    _print_scenario(_run_scenario("approved"))
    _print_scenario(_run_scenario("rejected"))


if __name__ == "__main__":
    main()
