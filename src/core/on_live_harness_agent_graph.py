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
from langgraph.types import interrupt

from src.core.agent_decision import AgentObservation
from src.core.agent_harness_context import AgentContextResult, build_agent_context
from src.core.agent_lifecycle_hooks import AgentLifecycleHooks, HookResult
from src.core.human_approval import (
    HumanApprovalDecision,
    HumanApprovalRequest,
    HumanApprovalResponse,
    validate_human_approval_response,
)
from src.core.on_live_harness_audit import OnLiveHarnessAuditWriter
from src.plan_engine.preemption import PreemptionEvidenceRef, SoldOutExecutionRoute
from src.skill_runtime.policy_view import get_default_skill_policy_view
from src.skills.on_live_harness_planner import OnLiveHarnessDecision, OnLiveHarnessPlanner
from src.state.models import LifecycleStage


class OnLiveHarnessAgentState(TypedDict, total=False):
    """播中 Harness Agent 图状态。

    只保存 JSON 可序列化字段，方便后续接 LangGraph checkpoint。
    """

    room_id: str
    trace_id: str
    anchor_id: str | None
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
    audit_ids: list[str]
    decision_trace_ids: list[str]
    audit_status: str | None
    audit_payload: dict[str, Any] | None
    approval_request: dict[str, Any] | None
    approval_decision: str | None
    approval_resume_audit_id: str | None
    approval_operator_id: str | None
    approval_reason: str | None
    error: str | None
    hallucination_issues: list[str]
    completed_nodes: list[str]
    sold_out_execution_route: str
    available_tool_names: list[str]
    preemption_evidence_refs: list[dict[str, Any]]
    final_suggestion_fact: str | None


def create_initial_on_live_harness_state(
    room_id: str,
    trace_id: str,
    *,
    anchor_id: str | None = None,
    trust_score: float = 0.7,
    danmaku_summary: list[dict[str, Any]] | None = None,
    inventory_alerts: list[dict[str, Any]] | None = None,
    current_product: dict[str, Any] | None = None,
    memory_summary: str | None = None,
    max_iterations: int = 5,
    preemption_evidence_refs: list[PreemptionEvidenceRef | dict[str, Any]] | None = None,
    final_suggestion_fact: str | None = None,
) -> OnLiveHarnessAgentState:
    """创建播中 Harness Agent 初始状态。"""
    return {
        "room_id": room_id,
        "trace_id": trace_id,
        "anchor_id": anchor_id,
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
        "audit_ids": [],
        "decision_trace_ids": [],
        "audit_status": None,
        "audit_payload": None,
        "approval_request": None,
        "approval_decision": None,
        "approval_resume_audit_id": None,
        "approval_operator_id": None,
        "approval_reason": None,
        "error": None,
        "hallucination_issues": [],
        "completed_nodes": [],
        "sold_out_execution_route": SoldOutExecutionRoute.LEGACY.value,
        "available_tool_names": [],
        "preemption_evidence_refs": [
            PreemptionEvidenceRef.model_validate(item).model_dump(mode="json")
            for item in (preemption_evidence_refs or [])
        ],
        "final_suggestion_fact": final_suggestion_fact,
    }


def build_on_live_harness_agent_graph(
    planner: Any | None = None,
    executor: Any | None = None,
    hooks: AgentLifecycleHooks | None = None,
    audit_writer: OnLiveHarnessAuditWriter | None = None,
    *,
    checkpointer: Any | None = None,
    sold_out_execution_route: SoldOutExecutionRoute | str = SoldOutExecutionRoute.LEGACY,
):
    """构建播中 LangGraph Harness Agent Loop。"""
    frozen_route = SoldOutExecutionRoute(sold_out_execution_route)
    policy_view = get_default_skill_policy_view()
    all_on_live_tools = [
        skill_id
        for skill_id in policy_view.skill_ids()
        if policy_view.is_available(skill_id, LifecycleStage.ON_LIVE)
    ]
    available_on_live_tools = [
        skill_id
        for skill_id in all_on_live_tools
        if skill_id != "handle_sold_out_event"
        or frozen_route is SoldOutExecutionRoute.LEGACY
    ]
    _planner = planner or OnLiveHarnessPlanner()
    _executor = executor or _HarnessDefaultExecutor()
    _hooks = hooks or AgentLifecycleHooks()
    _audit_writer = audit_writer or OnLiveHarnessAuditWriter()

    graph = StateGraph(OnLiveHarnessAgentState)
    graph.add_node(
        "load_context",
        lambda state: _load_context_node(state, frozen_route, available_on_live_tools),
    )
    graph.add_node("pre_reasoning_hook", _pre_reasoning_hook_node)
    graph.add_node(
        "agent_reasoning",
        lambda state: _agent_reasoning_node(state, _planner, frozen_route),
    )
    graph.add_node(
        "post_reasoning_hook",
        lambda state: _post_reasoning_hook_node(state, _hooks, frozen_route),
    )
    graph.add_node("route_agent_decision", _route_agent_decision_node)
    graph.add_node("pre_tool_call_hook", lambda state: _pre_tool_call_hook_node(state, _hooks))
    graph.add_node("route_tool_policy", _route_tool_policy_node)
    graph.add_node("execute_tool", lambda state: _execute_tool_node(state, _executor))
    graph.add_node("human_approval_interrupt", _human_approval_interrupt_node)
    graph.add_node("post_tool_call_hook", lambda state: _post_tool_call_hook_node(state, _hooks))
    graph.add_node("observe_result", _observe_result_node)
    graph.add_node("route_replan", _route_replan_node)
    graph.add_node("write_audit", lambda state: _write_audit_node(state, _audit_writer))

    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "pre_reasoning_hook")
    graph.add_edge("pre_reasoning_hook", "agent_reasoning")
    graph.add_edge("agent_reasoning", "post_reasoning_hook")
    graph.add_edge("post_reasoning_hook", "route_agent_decision")
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
            "human_approval_interrupt": "human_approval_interrupt",
            "write_audit": "write_audit",
        },
    )
    graph.add_conditional_edges(
        "human_approval_interrupt",
        _human_approval_decider,
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


def _load_context_node(
    state: OnLiveHarnessAgentState,
    route: SoldOutExecutionRoute = SoldOutExecutionRoute.LEGACY,
    available_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    """构造压缩后的 Agent 可见上下文。"""
    context = build_agent_context(
        danmaku_summary=state.get("danmaku_summary", []),
        inventory_alerts=state.get("inventory_alerts", []),
        current_product=state.get("current_product"),
        trust_score=state.get("trust_score", 0.7),
        memory_summary=state.get("memory_summary"),
    )
    evidence_refs = list(state.get("preemption_evidence_refs", []))
    return {
        "system_context": context.system_context,
        "context_summary": context.summary,
        "agent_status": "context_degraded" if context.should_degrade else state.get("agent_status"),
        "completed_nodes": _append_node(state, "load_context"),
        "sold_out_execution_route": route.value,
        "available_tool_names": list(available_tool_names or []),
        "preemption_evidence_refs": evidence_refs,
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


def _agent_reasoning_node(
    state: OnLiveHarnessAgentState,
    planner: Any,
    route: SoldOutExecutionRoute = SoldOutExecutionRoute.LEGACY,
) -> dict[str, Any]:
    """LLM/Harness planner 决策节点。"""
    if route is SoldOutExecutionRoute.PLAN_ENGINE:
        # 售罄执行已由确定性 Coordinator 完成。此路由不调用 Planner，也不开放任何
        # 新工具决策；只接受摘要闭合的 EvidenceRef 与其中已有的最终建议事实。
        try:
            evidence_refs = [
                PreemptionEvidenceRef.model_validate(item)
                for item in state.get("preemption_evidence_refs", [])
            ]
            suggestion = state.get("final_suggestion_fact")
            if not evidence_refs or not suggestion:
                raise ValueError("PlanEngine Harness 缺少售罄 EvidenceRef 或建议事实")
            if suggestion not in {
                evidence.final_suggestion_fact for evidence in evidence_refs
            }:
                raise ValueError("最终建议与 EvidenceRef 不一致")
            return {
                "messages": [
                    *state.get("messages", []),
                    {
                        "role": "assistant",
                        "content": {
                            "action": "evidence_only",
                            "evidence_digests": [
                                evidence.evidence_digest for evidence in evidence_refs
                            ],
                        },
                    },
                ],
                "pending_tool_call": None,
                "final_suggestion": suggestion,
                "agent_status": "evidence_only",
                "error": None,
                "completed_nodes": _append_node(state, "agent_reasoning"),
            }
        except (TypeError, ValueError) as exc:
            return {
                "pending_tool_call": None,
                "final_suggestion": None,
                "agent_status": "blocked",
                "error": f"preemption evidence invalid: {exc}",
                "completed_nodes": _append_node(state, "agent_reasoning"),
            }
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



def _post_reasoning_hook_node(
    state: OnLiveHarnessAgentState,
    hooks: AgentLifecycleHooks,
    route: SoldOutExecutionRoute = SoldOutExecutionRoute.LEGACY,
) -> dict[str, Any]:
    """PostReasoning 幻觉检测节点。

    在 LLM 决策之后、工具执行之前，对决策结果做交叉验证。
    发现幻觉时阻断工具执行，记录问题到 hallucination_issues。
    """
    call = state.get("pending_tool_call") or {}
    tool_name = call.get("tool_name")
    # 没有 pending tool call（如 LLM 返回 final_answer），跳过幻觉检查
    if not tool_name:
        return {
            "hallucination_issues": [],
            "completed_nodes": _append_node(state, "post_reasoning_hook"),
        }
    arguments = call.get("arguments") or {}
    if (
        route is SoldOutExecutionRoute.PLAN_ENGINE
        and tool_name == "handle_sold_out_event"
    ):
        # PlanEngine 已经负责唯一售罄写入。Harness 只把持久化 EvidenceRef 转换为
        # 建议事实，不能将模型重新选择的写 Skill 送入 pre_tool_call_hook。
        suggestion = state.get("final_suggestion_fact")
        if not suggestion and state.get("preemption_evidence_refs"):
            suggestion = state["preemption_evidence_refs"][0].get(
                "final_suggestion_fact"
            )
        return {
            "pending_tool_call": None,
            "agent_status": "evidence_only",
            "final_suggestion": suggestion,
            "hallucination_issues": [
                "PlanEngine 路由下售罄写由 PreemptionCoordinator 唯一执行"
            ],
            "completed_nodes": _append_node(state, "post_reasoning_hook"),
        }
    result = hooks.post_reasoning(
        tool_name=tool_name,
        arguments=arguments,
        current_product=state.get("current_product"),
        inventory_alerts=state.get("inventory_alerts", []),
    )
    updates: dict = {
        "hallucination_issues": result.issues,
        "completed_nodes": _append_node(state, "post_reasoning_hook"),
    }
    if not result.passed and result.corrected_decision:
        # 发现幻觉，阻断工具执行
        updates["pending_tool_call"] = None
        updates["agent_status"] = "corrected"
        updates["error"] = "; ".join(result.issues)
        messages = list(state.get("messages", []))
        messages.append({"role": "system", "content": f"幻觉检测修正: {result.issues}"})
        updates["messages"] = messages
    return updates

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
    if policy.get("status") == "pending_human":
        return "human_approval_interrupt"
    return "write_audit"


def _human_approval_interrupt_node(state: OnLiveHarnessAgentState) -> dict[str, Any]:
    """对高风险播中工具触发 LangGraph interrupt 人审。

    恢复时会从当前节点重新进入，`interrupt()` 返回 `Command(resume=...)` 的审批结果。因此这里必须
    重新校验 trace、room 和 tool，避免错误审批结果驱动高风险工具执行。
    """

    request = _build_on_live_approval_request(state)
    request_payload = request.model_dump(mode="json")
    resume_payload = interrupt(request_payload)
    response = validate_human_approval_response(
        request,
        HumanApprovalResponse.model_validate(resume_payload),
    )
    completed_nodes = _append_node(state, "human_approval_interrupt")
    base_result: dict[str, Any] = {
        "approval_request": request_payload,
        "approval_decision": response.decision.value,
        "approval_resume_audit_id": f"dry-run:{request.trace_id}:{request.tool_name}:approval:{response.decision.value}",
        "approval_operator_id": response.operator_id,
        "approval_reason": response.reason,
        "completed_nodes": completed_nodes,
    }
    if response.decision == HumanApprovalDecision.APPROVED:
        return {
            **base_result,
            "agent_status": "call_tool",
            "error": None,
            "tool_policy": {
                **(state.get("tool_policy") or {}),
                "status": "human_approved",
                "reason": response.reason,
            },
        }
    return {
        **base_result,
        "agent_status": "rejected_by_human",
        "error": response.reason,
        "tool_policy": {
            **(state.get("tool_policy") or {}),
            "status": "human_rejected",
            "reason": response.reason,
        },
    }


def _build_on_live_approval_request(state: OnLiveHarnessAgentState) -> HumanApprovalRequest:
    """从 pending_tool_call 构造给人工审批看的播中请求。"""

    call = state.get("pending_tool_call") or {}
    tool_name = str(call.get("tool_name") or "")
    arguments = dict(call.get("arguments") or {})
    return HumanApprovalRequest(
        trace_id=str(state.get("trace_id") or ""),
        room_id=str(state.get("room_id") or ""),
        tool_name=tool_name,
        risk_level=str(call.get("risk_level") or "HIGH"),
        action="approve_on_live_tool_call",
        message=f"是否允许 Agent 执行播中高风险工具 {tool_name}？",
        tool_arguments=arguments,
        context_summary=state.get("context_summary"),
    )


def _human_approval_decider(state: OnLiveHarnessAgentState) -> str:
    """根据人工审批结果决定执行工具还是写审计结束。"""

    if state.get("approval_decision") == HumanApprovalDecision.APPROVED.value:
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


def _write_audit_node(state: OnLiveHarnessAgentState, audit_writer: OnLiveHarnessAuditWriter) -> dict[str, Any]:
    """写审计占位节点。

    当前阶段先记录图状态，后续可接 ToolCallAuditStore / DecisionTraceStore。
    """
    status = state.get("agent_status") or "finished"
    completed_nodes = _append_node(state, "write_audit")
    audit_state = dict(state)
    audit_state["agent_status"] = status
    audit_state["completed_nodes"] = completed_nodes
    try:
        audit_result = audit_writer.write(audit_state)
        return {
            "agent_status": status,
            "audit_status": audit_result.get("audit_status"),
            "audit_ids": audit_result.get("audit_ids", []),
            "decision_trace_ids": audit_result.get("decision_trace_ids", []),
            "audit_payload": audit_result.get("audit_payload"),
            "completed_nodes": completed_nodes,
        }
    except Exception as exc:  # noqa: BLE001 - 审计失败必须被 Graph 捕获并留痕，不能让播中建议链路崩溃。
        return {
            "agent_status": status,
            "audit_status": "error",
            "audit_ids": state.get("audit_ids", []),
            "decision_trace_ids": state.get("decision_trace_ids", []),
            "error": f"audit write failed: {exc}",
            "completed_nodes": completed_nodes,
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
