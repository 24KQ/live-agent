"""Phase 5I 播中 Harness Agent LangGraph interrupt 单元测试。

这些测试使用 InMemorySaver 验证高风险工具的人审暂停与恢复语义，不依赖 PostgreSQL。
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class HighRiskThenFinalPlanner:
    """第一轮请求高风险工具；工具 observation 回灌后给出最终建议。"""

    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        if kwargs.get("observations"):
            return OnLiveHarnessDecision(
                thought="高风险工具已执行，给主播最终建议",
                action="final_answer",
                final_suggestion="建议主播说明商品售罄，并切换到已确认的备用方案。",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="商品售罄，需要执行高风险售罄处理工具",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class RecordingExecutor:
    """记录工具调用次数，确保 interrupt 前不会执行高风险工具。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        **kwargs,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "room_id": room_id,
                "trace_id": trace_id,
            }
        )
        return {
            "tool_name": tool_name,
            "status": "success",
            "summary": "sold out event handled after approval",
        }


class RecordingAuditWriter:
    """记录 write_audit 收到的 state，验证审批结果进入审计上下文。"""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.append(dict(state))
        return {
            "audit_status": "recorded",
            "audit_ids": [f"audit-{len(self.states)}"],
            "decision_trace_ids": [],
            "audit_payload": {
                "agent_status": state.get("agent_status"),
                "approval_decision": state.get("approval_decision"),
            },
        }


def _config(trace_id: str) -> dict[str, Any]:
    """LangGraph 使用 trace_id 作为 thread_id，便于恢复同一条播中 Agent 链路。"""

    return {"configurable": {"thread_id": trace_id}}


def _start_high_risk_graph(trace_id: str = "trace-5i-interrupt"):
    """运行到高风险工具人审 interrupt，并返回 graph/config/result/executor/audit_writer。"""

    executor = RecordingExecutor()
    audit_writer = RecordingAuditWriter()
    graph = build_on_live_harness_agent_graph(
        planner=HighRiskThenFinalPlanner(),
        executor=executor,
        audit_writer=audit_writer,
        checkpointer=InMemorySaver(),
    )
    config = _config(trace_id)
    first_result = graph.invoke(
        create_initial_on_live_harness_state(
            room_id="room-5i",
            trace_id=trace_id,
            inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
        ),
        config=config,
    )
    return graph, config, first_result, executor, audit_writer


def test_high_risk_tool_triggers_interrupt_before_execution() -> None:
    """高风险工具应触发 LangGraph interrupt，并且暂停前不得执行工具。"""

    graph, config, first_result, executor, _ = _start_high_risk_graph()

    interrupt_payload = first_result["__interrupt__"][0].value

    assert executor.calls == []
    assert interrupt_payload["trace_id"] == "trace-5i-interrupt"
    assert interrupt_payload["room_id"] == "room-5i"
    assert interrupt_payload["tool_name"] == "handle_sold_out_event"
    assert interrupt_payload["risk_level"] == "HIGH"
    assert interrupt_payload["tool_arguments"] == {"product_id": "p001"}
    assert "alerts=1" in interrupt_payload["context_summary"]
    assert graph.get_state(config).interrupts[0].value == interrupt_payload


def test_approved_resume_executes_tool_and_replans() -> None:
    """人工批准后，Graph 应恢复执行原 pending tool，并把 observation 回灌给下一轮推理。"""

    graph, config, _, executor, audit_writer = _start_high_risk_graph("trace-5i-approved")

    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": "trace-5i-approved",
                "room_id": "room-5i",
                "tool_name": "handle_sold_out_event",
                "decision": "approved",
                "operator_id": "operator-demo",
                "reason": "确认售罄处理可以执行。",
            }
        ),
        config=config,
    )

    assert executor.calls == [
        {
            "tool_name": "handle_sold_out_event",
            "arguments": {"product_id": "p001"},
            "room_id": "room-5i",
            "trace_id": "trace-5i-approved",
        }
    ]
    assert resumed["approval_decision"] == "approved"
    assert resumed["approval_operator_id"] == "operator-demo"
    assert resumed["observations"][0]["tool_name"] == "handle_sold_out_event"
    assert resumed["agent_status"] == "final_answer"
    assert audit_writer.states[-1]["approval_decision"] == "approved"


def test_rejected_resume_skips_tool_and_writes_audit() -> None:
    """人工拒绝后不得执行工具，Graph 应写审计后结束。"""

    graph, config, _, executor, audit_writer = _start_high_risk_graph("trace-5i-rejected")

    resumed = graph.invoke(
        Command(
            resume={
                "trace_id": "trace-5i-rejected",
                "room_id": "room-5i",
                "tool_name": "handle_sold_out_event",
                "decision": "rejected",
                "operator_id": "operator-demo",
                "reason": "主播决定手动处理售罄。",
            }
        ),
        config=config,
    )

    assert executor.calls == []
    assert resumed["agent_status"] == "rejected_by_human"
    assert resumed["approval_decision"] == "rejected"
    assert resumed["approval_reason"] == "主播决定手动处理售罄。"
    assert resumed["audit_status"] == "recorded"
    assert audit_writer.states[-1]["approval_decision"] == "rejected"


def test_resume_payload_mismatch_fails_closed_without_tool_execution() -> None:
    """恢复 payload 与 pending 请求不匹配时必须 fail-closed，不能执行高风险工具。"""

    graph, config, _, executor, _ = _start_high_risk_graph("trace-5i-mismatch")

    with pytest.raises(ValueError, match="trace_id"):
        graph.invoke(
            Command(
                resume={
                    "trace_id": "trace-other",
                    "room_id": "room-5i",
                    "tool_name": "handle_sold_out_event",
                    "decision": "approved",
                    "operator_id": "operator-demo",
                    "reason": "错误 trace 不应恢复。",
                }
            ),
            config=config,
        )

    assert executor.calls == []
