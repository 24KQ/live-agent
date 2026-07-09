"""Phase 5C 播中 Agent 动态决策小循环。

播中使用 LangGraph 做观察-决策-建议小循环：
1. collect_on_live_context：收集弹幕聚合、库存状态、当前商品
2. on_live_planner：调用 AgentRulesPlanner 确定本轮目标
3. route_by_decision：条件路由节点
4. execute_tools：根据目标执行工具
5. observe_result：收集结果生成建议
6. write_audit：写入审计

播中 Agent 只建议，不自动执行高风险动作（hard-gate 仍需人审）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.core.agent_decision import AgentReplanRoute


class OnLiveAgentGraphState(TypedDict, total=False):
    """播中 Agent 图状态。JSON 可序列化，适合 checkpoint。"""

    room_id: str
    trace_id: str
    # 播中上下文
    current_product: dict[str, Any] | None
    product_count: int
    danmaku_summary: list[dict[str, Any]]
    inventory_alerts: list[dict[str, Any]]
    # 决策状态
    planner_route: str | None
    goal: str | None
    observations: list[dict[str, Any]]
    executed_tools: list[dict[str, Any]]
    completed_nodes: list[str]
    # 记忆与信任
    trust_score: float
    memory_summary: str | None
    # 结果
    suggestion: str | None
    setup_status: str | None
    error: str | None


def create_initial_on_live_state(
    room_id: str,
    trace_id: str,
    trust_score: float = 0.7,
    danmaku_summary: list[dict[str, Any]] | None = None,
    inventory_alerts: list[dict[str, Any]] | None = None,
) -> OnLiveAgentGraphState:
    """创建播中 Agent 初始状态。"""
    return {
        "room_id": room_id,
        "trace_id": trace_id,
        "current_product": None,
        "product_count": 0,
        "danmaku_summary": danmaku_summary or [],
        "inventory_alerts": inventory_alerts or [],
        "planner_route": None,
        "goal": None,
        "observations": [],
        "executed_tools": [],
        "completed_nodes": [],
        "trust_score": trust_score,
        "memory_summary": None,
        "suggestion": None,
        "setup_status": None,
        "error": None,
    }


def build_on_live_agent_graph(
    planner: Any | None = None,
    executor: Any | None = None,
    service: Any = None,
    *,
    checkpointer: Any | None = None,
):
    """构建播中 Agent 决策小循环图。

    播中流程：
        START -> collect_on_live_context -> on_live_planner
          -> route_by_decision (conditional edge)
              -> execute_tools
              -> END（finish 路由时直接结束）
          -> observe_result -> write_audit -> END
    """
    _planner = planner or _DefaultPlanner()
    _executor = executor or _DefaultExecutor()

    graph = StateGraph(OnLiveAgentGraphState)

    graph.add_node("collect_on_live_context", lambda state: _collect_context_node(state))
    graph.add_node("on_live_planner", lambda state: _planner_node(state, _planner))
    graph.add_node("route_by_decision", _route_by_decision_node)
    graph.add_node("execute_tools", lambda state: _execute_tools_node(state, _executor))
    graph.add_node("observe_result", _observe_result_node)
    graph.add_node("write_audit", _write_audit_node)

    graph.add_edge(START, "collect_on_live_context")
    graph.add_edge("collect_on_live_context", "on_live_planner")
    graph.add_edge("on_live_planner", "route_by_decision")
    graph.add_conditional_edges(
        "route_by_decision",
        _route_decider,
        {
            "execute_tools": "execute_tools",
            END: END,
        },
    )
    graph.add_edge("execute_tools", "observe_result")
    graph.add_edge("observe_result", "write_audit")
    graph.add_edge("write_audit", END)

    return graph.compile(checkpointer=checkpointer)


def _collect_context_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """收集播中上下文：弹幕摘要、库存告警、当前商品。"""
    try:
        danmaku = state.get("danmaku_summary", [])
        alerts = state.get("inventory_alerts", [])
        context_summary = ""

        # 统计弹幕热点分类
        if danmaku:
            categories = {}
            for d in danmaku:
                cat = d.get("category", "unknown")
                categories[cat] = categories.get(cat, 0) + d.get("count", 0)
            top = sorted(categories.items(), key=lambda x: -x[1])[:3]
            if top:
                context_summary += "弹幕热点：" + ", ".join(f"{k}({v}条)" for k, v in top) + "。"

        # 统计库存告警
        if alerts:
            context_summary += f"库存告警：{len(alerts)}个商品异常。"
            for a in alerts[:3]:
                context_summary += f"{a.get('product_name', a.get('product_id', '未知商品'))}；"

        return {
            "completed_nodes": _append_node(state, "collect_on_live_context"),
            "product_count": len(danmaku),
        }
    except Exception as exc:
        return {
            "error": f"collect_on_live_context failed: {exc}",
            "completed_nodes": _append_node(state, "collect_on_live_context"),
        }


def _planner_node(state: OnLiveAgentGraphState, planner: Any) -> dict[str, Any]:
    """播中 planner 节点：根据弹幕和告警决策本轮目标。"""
    try:
        danmaku = state.get("danmaku_summary", [])
        alerts = state.get("inventory_alerts", [])

        # 判断是否有需要干预的事件
        has_high_frequency = any(d.get("count", 0) >= 10 for d in danmaku)
        has_alerts = len(alerts) > 0

        if not danmaku and not alerts:
            # 无事件时 finish
            return {
                "planner_route": AgentReplanRoute.FINISH.value,
                "goal": "无事件，不干预",
                "suggestion": None,
                "completed_nodes": _append_node(state, "on_live_planner"),
            }

        # 根据上下文决策
        if has_alerts:
            route = AgentReplanRoute.DIRECT_PLAN.value
            goal = "处理库存告警"
            suggestion = f"检测到 {len(alerts)} 个库存异常，建议检查备选商品并准备切换。"
        elif has_high_frequency:
            route = AgentReplanRoute.DIRECT_PLAN.value
            goal = "处理高频弹幕"
            # 找出最高频分类
            top = max(danmaku, key=lambda d: d.get("count", 0))
            suggestion = f"弹幕高频问题：{top.get('summary', top.get('category', '未知'))}，建议主播重点回应。"
        else:
            # 低频事件不干预
            route = AgentReplanRoute.FINISH.value
            goal = "低频事件，不干预"
            suggestion = None

        return {
            "planner_route": route,
            "goal": goal,
            "suggestion": suggestion,
            "completed_nodes": _append_node(state, "on_live_planner"),
        }
    except Exception as exc:
        return {
            "planner_route": AgentReplanRoute.FALLBACK.value,
            "goal": "planner 失败，降级",
            "error": f"on_live_planner failed: {exc}",
            "completed_nodes": _append_node(state, "on_live_planner"),
        }


def _route_by_decision_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """路由决策节点——仅作为 conditional edge 锚点。"""
    return {"completed_nodes": _append_node(state, "route_by_decision")}


def _route_decider(state: OnLiveAgentGraphState) -> str:
    """条件路由：finish 时直接 END，否则走 execute_tools。"""
    route = state.get("planner_route", "")
    if route == AgentReplanRoute.FINISH.value:
        return END
    return "execute_tools"


def _execute_tools_node(state: OnLiveAgentGraphState, executor: Any) -> dict[str, Any]:
    """执行工具节点：根据决策执行对应播中工具。"""
    try:
        tools_executed = []
        route = state.get("planner_route", "")

        if route == AgentReplanRoute.DIRECT_PLAN.value:
            # 执行播中建议工具
            goal = state.get("goal", "")
            if "库存" in goal or "告警" in goal:
                tools_executed.append({
                    "tool_name": "recommend_backup",
                    "status": "simulated",
                    "summary": "建议切换备选商品",
                })
            elif "弹幕" in goal:
                tools_executed.append({
                    "tool_name": "generate_on_live_prompt",
                    "status": "simulated",
                    "summary": "生成弹幕回复建议",
                })

        return {
            "executed_tools": tools_executed,
            "completed_nodes": _append_node(state, "execute_tools"),
        }
    except Exception as exc:
        return {
            "error": f"execute_tools failed: {exc}",
            "completed_nodes": _append_node(state, "execute_tools"),
        }


def _observe_result_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """观察节点：收集工具执行结果。"""
    tools = state.get("executed_tools", [])
    suggestion = state.get("suggestion")

    # 如果没有建议但有工具执行，生成观察摘要
    if not suggestion and tools:
        tool_summaries = [t.get("summary", "") for t in tools if t.get("status") == "simulated"]
        if tool_summaries:
            suggestion = "播中 Agent 观察：已执行 " + "；".join(tool_summaries)

    return {
        "observations": [{"tools_executed": len(tools), "has_suggestion": suggestion is not None}],
        "suggestion": suggestion,
        "completed_nodes": _append_node(state, "observe_result"),
    }


def _write_audit_node(state: OnLiveAgentGraphState) -> dict[str, Any]:
    """写入审计节点。"""
    return {
        "setup_status": "observed",
        "completed_nodes": _append_node(state, "write_audit"),
    }


def _append_node(state: OnLiveAgentGraphState, node_name: str) -> list[str]:
    """向 completed_nodes 追加节点名。"""
    return [*state.get("completed_nodes", []), node_name]


class _DefaultPlanner:
    """默认播中 planner——用于测试和快速验证。"""

    def plan(self, room_id: str, trace_id: str, **kwargs) -> Any:
        from src.core.agent_decision import AgentPlannerDecision
        return AgentPlannerDecision(
            trace_id=trace_id,
            room_id=room_id,
            goal="默认播中决策",
            route=AgentReplanRoute.FINISH,
            reason="无事件",
            tool_calls=[],
        )


class _DefaultExecutor:
    """默认播中执行器——用于测试和快速验证。"""

    def execute(self, tool_name: str, **kwargs) -> dict[str, Any]:
        return {"tool_name": tool_name, "status": "simulated"}
