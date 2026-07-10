"""Phase 5G-B LangGraph Harness Agent 图单元测试。

这些测试关注 LangGraph 的显式节点和条件边，而不是普通 while-loop：
- Agent 决策分支能走 no_action / final_answer / call_tool。
- 工具策略分支能区分 auto_execute / pending_human / blocked。
- 工具 observation 能回灌并触发下一轮 reasoning。
- max_iterations 能阻断循环。
"""

from __future__ import annotations

from typing import Any

from src.core.on_live_agent_graph import build_on_live_agent_graph, create_initial_on_live_state
from src.core.on_live_harness_agent_graph import (
    build_on_live_harness_agent_graph,
    create_initial_on_live_harness_state,
)
from src.skills.on_live_harness_planner import OnLiveHarnessDecision


class NoActionPlanner:
    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="无事件",
            action="no_action",
            final_suggestion=None,
            risk_level="LOW",
        )


class FinalAnswerPlanner:
    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="弹幕价格问题集中",
            action="final_answer",
            final_suggestion="建议主播解释券后价和保价规则",
            risk_level="LOW",
        )


class ToolThenFinalPlanner:
    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        observations = kwargs.get("observations", [])
        if observations:
            return OnLiveHarnessDecision(
                thought="已经拿到工具结果",
                action="final_answer",
                final_suggestion="建议主播切到备选商品并说明售罄原因",
                risk_level="LOW",
            )
        return OnLiveHarnessDecision(
            thought="库存告警，需要推荐备选",
            action="call_tool",
            tool_name="recommend_backup_product",
            arguments={"sold_out_product_id": "p001"},
            risk_level="MEDIUM",
        )


class HighRiskPlanner:
    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="尝试执行高风险售罄处理",
            action="call_tool",
            tool_name="handle_sold_out_event",
            arguments={"product_id": "p001"},
            risk_level="HIGH",
        )


class InfiniteToolPlanner:
    def plan_next_step(self, **kwargs) -> OnLiveHarnessDecision:
        return OnLiveHarnessDecision(
            thought="持续调用低风险提示工具",
            action="call_tool",
            tool_name="generate_on_live_prompt",
            arguments={"sold_out_product_id": "p001"},
            risk_level="LOW",
        )


class RecordingExecutor:
    """测试用执行器，记录被调用的标准工具名。"""

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
            "summary": f"executed {tool_name}",
            "backup_product_id": "p002",
        }


class RecordingAuditWriter:
    """测试用审计 writer，验证 Graph write_audit 节点会真正调用注入对象。"""

    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        self.states.append(dict(state))
        return {
            "audit_status": "recorded",
            "audit_ids": ["audit-graph-1"],
            "decision_trace_ids": ["decision-graph-1"],
        }


class FailingAuditWriter:
    """测试用异常 writer，确保审计失败不会让 Graph 崩溃。"""

    def write(self, state: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("audit store unavailable")


def test_harness_graph_runs_start_to_end() -> None:
    """Graph 能从 START 到 END。"""
    graph = build_on_live_harness_agent_graph(planner=NoActionPlanner())
    state = create_initial_on_live_harness_state(room_id="room-5g", trace_id="trace-5g")
    result = graph.invoke(state)
    assert result["agent_status"] == "no_action"
    assert "load_context" in result["completed_nodes"]
    assert "write_audit" in result["completed_nodes"]


def test_no_event_path_skips_tool_nodes() -> None:
    """无事件路径不进入工具节点。"""
    graph = build_on_live_harness_agent_graph(planner=NoActionPlanner())
    state = create_initial_on_live_harness_state(room_id="room-5g", trace_id="trace-5g")
    result = graph.invoke(state)
    assert "execute_tool" not in result["completed_nodes"]
    assert result["executed_tools"] == []


def test_final_answer_writes_suggestion() -> None:
    """final_answer 分支应写入 final_suggestion。"""
    graph = build_on_live_harness_agent_graph(planner=FinalAnswerPlanner())
    state = create_initial_on_live_harness_state(
        room_id="room-5g",
        trace_id="trace-5g",
        danmaku_summary=[{"category": "price", "count": 15, "summary": "价格"}],
    )
    result = graph.invoke(state)
    assert result["agent_status"] == "final_answer"
    assert "券后价" in result["final_suggestion"]


def test_call_tool_path_executes_and_replans() -> None:
    """call_tool 路径应执行工具、回灌 observation，并触发下一轮 reasoning。"""
    executor = RecordingExecutor()
    graph = build_on_live_harness_agent_graph(planner=ToolThenFinalPlanner(), executor=executor)
    state = create_initial_on_live_harness_state(
        room_id="room-5g",
        trace_id="trace-5g",
        inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
    )
    result = graph.invoke(state)
    assert executor.calls == ["recommend_backup_product"]
    assert result["iteration"] == 1
    assert result["observations"]
    assert result["agent_status"] == "final_answer"
    assert "备选商品" in result["final_suggestion"]
    assert "pre_tool_call_hook" in result["completed_nodes"]
    assert "post_tool_call_hook" in result["completed_nodes"]


def test_high_risk_tool_pending_not_executed() -> None:
    """高风险工具只能 pending，不自动执行。"""
    executor = RecordingExecutor()
    graph = build_on_live_harness_agent_graph(planner=HighRiskPlanner(), executor=executor)
    state = create_initial_on_live_harness_state(
        room_id="room-5g",
        trace_id="trace-5g",
        inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
    )
    result = graph.invoke(state)
    assert executor.calls == []
    assert result["agent_status"] == "pending_human"
    assert "high risk" in result["error"] or "pending" in result["error"]


def test_max_iterations_forces_finish() -> None:
    """超过 max_iterations 时强制结束，避免死循环。"""
    graph = build_on_live_harness_agent_graph(planner=InfiniteToolPlanner(), executor=RecordingExecutor())
    state = create_initial_on_live_harness_state(
        room_id="room-5g",
        trace_id="trace-5g",
        danmaku_summary=[{"category": "price", "count": 15}],
        max_iterations=2,
    )
    result = graph.invoke(state)
    assert result["iteration"] == 2
    assert result["agent_status"] in {"max_iterations", "blocked"}
    assert "write_audit" in result["completed_nodes"]


def test_old_on_live_graph_still_works() -> None:
    """旧 build_on_live_agent_graph 保持可用。"""
    graph = build_on_live_agent_graph()
    state = create_initial_on_live_state(room_id="old-room", trace_id="old-trace")
    result = graph.invoke(state)
    assert result["room_id"] == "old-room"


def test_write_audit_node_calls_injected_audit_writer() -> None:
    """Phase 5H：write_audit 节点应调用注入的 audit writer 并回填审计 ID。"""
    audit_writer = RecordingAuditWriter()
    graph = build_on_live_harness_agent_graph(planner=FinalAnswerPlanner(), audit_writer=audit_writer)
    state = create_initial_on_live_harness_state(room_id="room-5h", trace_id="trace-5h")

    result = graph.invoke(state)

    assert len(audit_writer.states) == 1
    assert audit_writer.states[0]["trace_id"] == "trace-5h"
    assert result["audit_status"] == "recorded"
    assert result["audit_ids"] == ["audit-graph-1"]
    assert result["decision_trace_ids"] == ["decision-graph-1"]


def test_audit_writer_error_does_not_crash_graph() -> None:
    """Phase 5H：审计写入失败时 Graph 返回 audit_status=error，而不是直接崩溃。"""
    graph = build_on_live_harness_agent_graph(planner=FinalAnswerPlanner(), audit_writer=FailingAuditWriter())
    state = create_initial_on_live_harness_state(room_id="room-5h", trace_id="trace-5h")

    result = graph.invoke(state)

    assert result["agent_status"] == "final_answer"
    assert result["audit_status"] == "error"
    assert "audit store unavailable" in result["error"]
