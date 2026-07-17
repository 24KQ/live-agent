"""Phase 14 对旧 Harness interrupt 权限的退役回归。"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.decision_support.routing import DecisionSupportRoute
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class HighRiskPlanner:
    """请求需要可信授权的售罄写 Skill。"""

    def plan_next_step(self, **_kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="商品售罄，请求执行写入",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class RecordingExecutor:
    """记录是否有旧审批路径越权调用执行器。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, tool_name: str, *_args, **_kwargs) -> dict[str, Any]:
        self.calls.append(tool_name)
        return {"tool_name": tool_name, "status": "success"}


class RecordingAuditWriter:
    """保存 Graph 终态，证明阻断事实进入既有审计节点。"""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.append(dict(state))
        return {
            "audit_status": "recorded",
            "audit_ids": ["audit-phase14-authority"],
            "decision_trace_ids": [],
        }


def _run_governed_write(trace_id: str):
    """运行显式 Decision Support，并返回权限阻断后的全部证据。"""

    executor = RecordingExecutor()
    audit_writer = RecordingAuditWriter()
    checkpointer = InMemorySaver()
    graph = build_on_live_harness_agent_graph(
        planner=HighRiskPlanner(),
        executor=executor,
        audit_writer=audit_writer,
        checkpointer=checkpointer,
        decision_support_route=DecisionSupportRoute.DECISION_SUPPORT,
    )
    config = {"configurable": {"thread_id": trace_id}}
    result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-phase14-authority",
            trace_id=trace_id,
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        ),
        config=config,
    )
    return graph, config, result, executor, audit_writer


def test_governed_write_is_blocked_before_legacy_interrupt() -> None:
    """授权型 Skill 不再生成旧 HumanApproval interrupt，更不会执行。"""

    graph, config, result, executor, _ = _run_governed_write(
        "trace-phase14-no-legacy-interrupt"
    )

    assert "__interrupt__" not in result
    assert graph.get_state(config).interrupts == ()
    assert executor.calls == []
    assert result["agent_status"] == "operator_decision_required"
    assert result["approval_decision"] is None
    assert "OperatorDecision" in result["error"]


def test_authority_block_is_written_to_audit() -> None:
    """权限拒绝必须进入审计，而不是以异常或 fallback 隐藏。"""

    _, _, result, executor, audit_writer = _run_governed_write(
        "trace-phase14-authority-audit"
    )

    assert executor.calls == []
    assert result["audit_status"] == "recorded"
    assert result["audit_ids"] == ["audit-phase14-authority"]
    assert audit_writer.states[-1]["agent_status"] == "operator_decision_required"
    assert audit_writer.states[-1]["pending_tool_call"] is None


def test_deterministic_route_blocks_real_legacy_checkpoint_queued_for_tool() -> None:
    """真实 checkpoint 即使已把只读工具排到执行节点，也不能绕过默认关闭路由。"""

    executor = RecordingExecutor()
    checkpointer = InMemorySaver()
    graph = build_on_live_harness_agent_graph(
        executor=executor,
        checkpointer=checkpointer,
        decision_support_route=DecisionSupportRoute.DETERMINISTIC_ONLY,
    )
    config = {"configurable": {"thread_id": "trace-phase14-legacy-checkpoint"}}
    legacy_state = create_initial_on_live_harness_state(
        room_id="room-phase14-legacy-checkpoint",
        trace_id="trace-phase14-legacy-checkpoint",
    )
    legacy_state.update(
        {
            "pending_tool_call": {
                "tool_name": "generate_on_live_prompt",
                "arguments": {"product_id": "p001"},
                "risk_level": "LOW",
            },
            "tool_policy": {
                "status": "auto_execute",
                "tool_name": "generate_on_live_prompt",
            },
            "agent_status": "call_tool",
        }
    )
    graph.update_state(config, legacy_state, as_node="route_tool_policy")

    assert graph.get_state(config).next == ("execute_tool",)
    result = graph.invoke(None, config=config)

    assert executor.calls == []
    assert result["agent_status"] == "decision_support_disabled"
    assert result["pending_tool_call"] is None
    assert "route" in result["error"]
