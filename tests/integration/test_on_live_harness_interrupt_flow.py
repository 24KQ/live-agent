"""Phase 14 旧播中 interrupt 权限退役的集成证据。"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.decision_support.routing import DecisionSupportRoute
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class HighRiskPlanner:
    """模拟旧 Planner 请求售罄经营写入。"""

    def plan_next_step(self, **_kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="请求售罄写入",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class Executor:
    """任何调用都意味着旧权限边界发生回归。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, tool_name: str, *_args, **_kwargs):
        self.calls.append(tool_name)
        return {"tool_name": tool_name, "status": "success"}


def test_explicit_decision_support_never_creates_legacy_write_interrupt() -> None:
    """完整 LangGraph/checkpointer 路径在 OperatorDecision 前必须闭合为权限等待。"""

    executor = Executor()
    graph = build_on_live_harness_agent_graph(
        planner=HighRiskPlanner(),
        executor=executor,
        checkpointer=InMemorySaver(),
        decision_support_route=DecisionSupportRoute.DECISION_SUPPORT,
    )
    config = {"configurable": {"thread_id": "trace-phase14-int"}}

    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-phase14-int",
            trace_id="trace-phase14-int",
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        ),
        config=config,
    )

    assert executor.calls == []
    assert "__interrupt__" not in result
    assert graph.get_state(config).interrupts == ()
    assert result["agent_status"] == "operator_decision_required"
    assert result["audit_status"] == "dry_run"
