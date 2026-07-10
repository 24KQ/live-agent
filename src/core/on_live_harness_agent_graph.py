"""Phase 5G-B LangGraph Harness Agent Loop。

这个图刻意不写成普通 ReAct while-loop，而是把 Harness 的控制点显式拆成
LangGraph 节点和条件边：

load_context -> pre_reasoning_hook -> agent_reasoning -> route_agent_decision
-> pre_tool_call_hook -> route_tool_policy -> execute_tool -> post_tool_call_hook
-> observe_result -> route_replan -> pre_reasoning_hook / write_audit -> END

这样每个关键控制点都可 checkpoint、可观测、可测试，也便于后续把
pending_human 接到 LangGraph interrupt。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.core.agent_decision import AgentObservation
from src.core.agent_harness_context import AgentContextResult, build_agent_context
from src.core.agent_lifecycle_hooks import AgentLifecycleHooks, HookResult
from src.skills.on_live_harness_planner import OnLiveHarnessDecision, OnLiveHarnessPlanner


class OnLiveHarnessAgentState(TypedDict, total=False):
    """播中 Harness Agent 图状态。

    只保存 JSON 可序列化字段，方便后续接 LangGraph checkpoint。
    """

    room_id: str
    trace_id: str
    danmaku_summary: list[dict[str, Any]]
    inventory_alerts: list[dict[str, Any]]
    current_product: dict[str, Any] | None
    trust_score: float
    memory_summary: str | None
    iteration: int
    max_iterations: int
    context_summary: str | None
    system_context: str | None
    messages: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    pending_tool_call: dict[str, Any] | None
    tool_policy: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    executed_tools: list[dict[str, Any]]
    final_suggestion: str | None
    agent_status: str | None
    error: str | None
    completed_nodes: list[str]


def create_initial_on_live_harness_state(
    room_id: str,
    trace_id: str,
    *,
    trust_score: float = 0.7,
    danmaku_summary: list[dict[str, Any]] | None = None,
    inventory_alerts: list[dict[str, Any]] | None = None,
    current_product: dict[str, Any] | None = None,
    memory_summary: str | None = None,
    max_iterations: int = 5,
) -> OnLiveHarnessAgentState:
    """创建播中 Harness Agent 初始状态。"""
    return {
        "room_id": room_id,
        "trace_id": trace_id,
        "danmaku_summary": danmaku_summary or [],
        "inventory_alerts": inventory_alerts or [],
        "current_product": current_product,
        "trust_score": trust_score,
        "memory_summary": memory_summary,
        "iteration": 0,
        "max_iterations": max_iterations,
        "context_summary": None,
        "system_context": None,
        "messages": [],
        "observations": [],
        "pending_tool_call": None,
        "tool_policy": None,
        "tool_result": None,
        "executed_tools": [],
        "final_suggestion": None,
        "agent_status": None,
        "error": None,
        "completed_nodes": [],
    }


def build_on_live_harness_agent_graph(
    planner: Any | None = None,
    executor: Any | None = None,
    hooks: AgentLifecycleHooks | None = None,
    *,
    checkpointer: Any | None = None,
):
    """构建播中 LangGraph Harness Agent Loop。"""
    _planner = planner or OnLiveHarnessPlanner()
    _executor = executor or _HarnessDefaultExecutor()
    _hooks = hooks or AgentLifecycleHooks()

    graph = StateGraph(OnLiveHarnessAgentState)
    graph.add_node("load_context", _load_context_node)
    graph.add_node("pre_reasoning_hook", _pre_reasoning_hook_node)
    graph.add_node("agent_reasoning", lambda state: _agent_reasoning_node(state, _planner))
    graph.add_node("route_agent_decision", _route_agent_decision_node)
    graph.add_node("pre_tool_call_hook", lambda state: _pre_tool_call_hook_node(state, _hooks))
    graph.add_node("route_tool_policy", _route_tool_policy_node)
    graph.add_node("execute_tool", lambda state: _execute_tool_node(state, _executor))
    graph.add_node("post_tool_call_hook", lambda state: _post_tool_call_hook_node(state, _hooks))
    graph.add_node("observe_result", _observe_result_node)
    graph.add_node("route_replan", _route_replan_node)
    graph.add_node("write_audit", _write_audit_node)

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "pre_reasoning_hook")
    graph.add_edge("pre_reasoning_hook", "agent_reasoning")
    graph.add_edge("agent_reasoning", "route_agent_decision")
    graph.add_conditional_edges(
        "route_agent_decision",
        _agent_decider,
        {
            "pre_tool_call_hook": "pre_tool_call_hook",
            "write_audit": "write_audit",
        },
    )
    graph.add_edge("pre_tool_call_hook", "route_tool_policy")
    graph.add_conditional_edges(
        "route_tool_policy",
        _tool_policy_decider,
        {
            "execute_tool": "execute_tool",
            "write_audit": "write_audit",
        },
    )
    graph.add_edge("execute_tool", "post_tool_call_hook")
    graph.add_edge("post_tool_call_hook", "observe_result")
    graph.add_edge("observe_result", "route_replan")
    graph.add_conditional_edges(
        "route_replan",
        _replan_decider,
        {
            "pre_reasoning_hook": "pre_reasoning_hook",
            "write_audit": "write_audit",
        },
    )
    graph.add_edge("write_audit", END)
    return graph.compile(checkpointer=checkpointer)


def _append_node(state: OnLiveHarnessAgentState, node_name: str) -> list[str]:
    """向 completed_nodes 追加节点名，用于 CLI 和测试观察路径。"""
    return [*state.get("completed_nodes", []), node_name]


def _load_context_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """构造压缩后的 Agent 可见上下文。"""
    context = build_agent_context(
        danmaku_summary=state.get("danmaku_summary", []),
        inventory_alerts=state.get("inventory_alerts", []),
        current_product=state.get("current_product"),
        trust_score=state.get("trust_score", 0.7),
        memory_summary=state.get("memory_summary"),
    )
    return {
        "system_context": context.system_context,
        "context_summary": context.summary,
        "agent_status": "context_degraded" if context.should_degrade else state.get("agent_status"),
        "completed_nodes": _append_node(state, "load_context"),
    }


def _pre_reasoning_hook_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """推理前 Hook：把最新状态快照压入 messages。"""
    messages = list(state.get("messages", []))
    messages.append(
        {
            "role": "system",
            "content": state.get("system_context") or "",
            "iteration": state.get("iteration", 0),
        }
    )
    return {
        "messages": messages,
        "completed_nodes": _append_node(state, "pre_reasoning_hook"),
    }


def _context_from_state(state: OnLiveHarnessAgentState) -> AgentContextResult:
    """从 graph state 还原 planner 需要的上下文对象。"""
    return AgentContextResult(
        system_context=state.get("system_context") or "",
        should_degrade=state.get("agent_status") == "context_degraded",
        summary=state.get("context_summary") or "",
    )


def _agent_reasoning_node(state: OnLiveHarnessAgentState, planner: Any) -> dict[str, Any]:
    """LLM/Harness planner 决策节点。"""
    try:
        decision: OnLiveHarnessDecision = planner.plan_next_step(
            context=_context_from_state(state),
            danmaku_summary=state.get("danmaku_summary", []),
            inventory_alerts=state.get("inventory_alerts", []),
            observations=state.get("observations", []),
        )
        messages = list(state.get("messages", []))
        messages.append({"role": "assistant", "content": decision.model_dump(mode="json")})

        pending_tool_call = None
        if decision.action == "call_tool":
            pending_tool_call = {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "risk_level": decision.risk_level,
                "thought": decision.thought,
            }

        return {
            "messages": messages,
            "pending_tool_call": pending_tool_call,
            "final_suggestion": decision.final_suggestion,
            "agent_status": decision.action,
            "error": decision.fallback_reason,
            "completed_nodes": _append_node(state, "agent_reasoning"),
        }
    except Exception as exc:
        return {
            "agent_status": "fallback",
            "error": f"agent_reasoning failed: {exc}",
            "completed_nodes": _append_node(state, "agent_reasoning"),
        }


def _route_agent_decision_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """Agent 决策路由锚点。"""
    return {"completed_nodes": _append_node(state, "route_agent_decision")}


def _agent_decider(state: OnLiveHarnessAgentState) -> str:
    """根据 Agent action 选择后续节点。"""
    if state.get("agent_status") == "call_tool" and state.get("pending_tool_call"):
        return "pre_tool_call_hook"
    return "write_audit"


def _pre_tool_call_hook_node(state: OnLiveHarnessAgentState, hooks: AgentLifecycleHooks) -> dict[str, Any]:
    """工具调用前 Hook：校验白名单、生命周期、风险和重复调用。"""
    call = state.get("pending_tool_call") or {}
    tool_name = call.get("tool_name") or ""
    arguments = call.get("arguments") or {}
    policy: HookResult = hooks.pre_tool_call(
        tool_name=tool_name,
        arguments=arguments,
        iteration=state.get("iteration", 0),
        lifecycle="ON_LIVE",
    )
    status = "auto_execute"
    error = state.get("error")
    if not policy.allowed:
        status = "blocked"
        error = policy.reason
    elif not policy.auto_execute:
        status = "pending_human"
        error = policy.reason
    return {
        "tool_policy": {
            "status": status,
            "reason": policy.reason,
            "tool_name": tool_name,
        },
        "agent_status": status if status != "auto_execute" else state.get("agent_status"),
        "error": error,
        "completed_nodes": _append_node(state, "pre_tool_call_hook"),
    }


def _route_tool_policy_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """工具策略路由锚点。"""
    return {"completed_nodes": _append_node(state, "route_tool_policy")}


def _tool_policy_decider(state: OnLiveHarnessAgentState) -> str:
    """根据 Hook 策略决定是否执行工具。"""
    policy = state.get("tool_policy") or {}
    if policy.get("status") == "auto_execute":
        return "execute_tool"
    return "write_audit"


def _execute_tool_node(state: OnLiveHarnessAgentState, executor: Any) -> dict[str, Any]:
    """执行工具节点。"""
    call = state.get("pending_tool_call") or {}
    tool_name = call.get("tool_name") or ""
    arguments = call.get("arguments") or {}
    try:
        result = executor.execute(
            tool_name=tool_name,
            arguments=arguments,
            room_id=state.get("room_id", ""),
            trace_id=state.get("trace_id", ""),
            state=state,
        )
    except TypeError:
        result = executor.execute(tool_name, arguments, state.get("room_id", ""), state.get("trace_id", ""))
    if not isinstance(result, dict):
        result = {"tool_name": tool_name, "status": "error", "summary": "executor returned non-dict"}
    result.setdefault("tool_name", tool_name)
    executed = [*state.get("executed_tools", []), result]
    return {
        "tool_result": result,
        "executed_tools": executed,
        "completed_nodes": _append_node(state, "execute_tool"),
    }


def _post_tool_call_hook_node(state: OnLiveHarnessAgentState, hooks: AgentLifecycleHooks) -> dict[str, Any]:
    """工具调用后 Hook：把工具结果转为结构化 observation。"""
    call = state.get("pending_tool_call") or {}
    result = state.get("tool_result") or {}
    observation: AgentObservation = hooks.post_tool_call(
        tool_name=call.get("tool_name") or result.get("tool_name", "unknown"),
        arguments=call.get("arguments") or {},
        result=result,
    )
    observations = [*state.get("observations", []), observation.model_dump(mode="json")]
    return {
        "observations": observations,
        "completed_nodes": _append_node(state, "post_tool_call_hook"),
    }


def _observe_result_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """观察工具结果并递增 iteration。"""
    return {
        "iteration": state.get("iteration", 0) + 1,
        "pending_tool_call": None,
        "agent_status": "tool_observed",
        "completed_nodes": _append_node(state, "observe_result"),
    }


def _route_replan_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """决定继续 replan 还是结束。"""
    if state.get("iteration", 0) >= state.get("max_iterations", 5):
        return {
            "agent_status": "max_iterations",
            "error": "max_iterations reached",
            "completed_nodes": _append_node(state, "route_replan"),
        }
    return {"completed_nodes": _append_node(state, "route_replan")}


def _replan_decider(state: OnLiveHarnessAgentState) -> str:
    """工具 observation 回灌后是否继续推理。"""
    if state.get("agent_status") == "max_iterations":
        return "write_audit"
    return "pre_reasoning_hook"


def _write_audit_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """写审计占位节点。

    当前阶段先记录图状态，后续可接 ToolCallAuditStore / DecisionTraceStore。
    """
    status = state.get("agent_status") or "finished"
    return {
        "agent_status": status,
        "completed_nodes": _append_node(state, "write_audit"),
    }


class _HarnessDefaultExecutor:
    """测试和演示用默认执行器。"""

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
            "summary": f"simulated execution for {tool_name}",
            "arguments": arguments,
        }
