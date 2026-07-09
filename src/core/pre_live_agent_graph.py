"""Phase 5A 播前 Agent 编排图（修正版）。

播前流程默认使用确定性规则路由（AgentRulesPlanner），
不走 LLM 决策。AgentPlanner（LLM 驱动）保留供后续播中 Agent。

播前执行路径：

    START -> collect_context -> rules_planner
      -> route_by_decision (conditional edge)
         -> deterministic_prelive（排品 + 手卡）
      -> observe_result
      -> replan_or_finish（conditional edge，出错时 replan，最多 1 次）
      -> setup_live_session
      -> END

本图是实验/预研路径，默认播前仍走 pre_live_graph.py。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from src.core.agent_decision import (
    AgentPlannerDecision,
    AgentReplanRoute,
)
from src.core.security_hooks import GateResult
from src.skills.live_plan_generator import LivePlanDraft
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


class PreLiveAgentGraphState(TypedDict, total=False):
    """Agent 播前编排图的状态。JSON 可序列化，适合 checkpoint。"""

    room_id: str
    trace_id: str
    planner_decision: dict[str, Any] | None
    planner_route: str | None
    planner_reason: str | None
    goal: str | None
    observations: list[dict[str, Any]]
    replan_count: int
    max_replan: int
    completed_nodes: list[str]
    products: list[dict[str, Any]]
    product_count: int
    memory_summary: str | None
    trust_score: float
    plan_info: dict[str, Any] | None
    cards_info: list[dict[str, Any]] | None
    card_count: int
    risk_summary: str | None
    setup_status: str | None
    setup_audit_id: str | None
    error: str | None


def create_initial_agent_state(
    room_id: str,
    trace_id: str,
    trust_score: float = 0.7,
) -> PreLiveAgentGraphState:
    """创建初始 Agent 图状态。"""
    return {
        "room_id": room_id,
        "trace_id": trace_id,
        "planner_decision": None,
        "planner_route": None,
        "planner_reason": None,
        "goal": None,
        "observations": [],
        "replan_count": 0,
        "max_replan": 1,
        "completed_nodes": [],
        "products": [],
        "product_count": 0,
        "memory_summary": None,
        "trust_score": trust_score,
        "plan_info": None,
        "cards_info": None,
        "card_count": 0,
        "risk_summary": None,
        "setup_status": None,
        "setup_audit_id": None,
        "error": None,
    }


def build_pre_live_agent_graph(
    planner: Any = None,
    executor: Any = None,
    service: Any = None,
    *,
    checkpointer: Any | None = None,
):
    """构建播前 Agent 编排图。

    播前默认使用 rules_planner（确定性规则），不走 LLM 决策。
    planner 参数接受 AgentRulesPlanner 或后续播中使用的 AgentPlanner。
    """
    graph = StateGraph(PreLiveAgentGraphState)

    graph.add_node("collect_context", lambda state: _collect_context_node(state, service))
    graph.add_node("rules_planner", lambda state: _rules_planner_node(state, planner))
    graph.add_node("route_by_decision", _route_by_decision_node)
    graph.add_node("deterministic_prelive", lambda state: _deterministic_prelive_node(state, service))
    graph.add_node("observe_result", _observe_result_node)
    graph.add_node("replan_or_finish", lambda state: _replan_or_finish_node(state))
    graph.add_node("setup_live_session", lambda state: _setup_live_session_node(state, service))

    graph.add_edge(START, "collect_context")
    graph.add_edge("collect_context", "rules_planner")
    graph.add_edge("rules_planner", "route_by_decision")
    graph.add_conditional_edges(
        "route_by_decision",
        _route_decider,
        {
            "deterministic_prelive": "deterministic_prelive",
        },
    )
    graph.add_edge("deterministic_prelive", "observe_result")
    graph.add_edge("observe_result", "replan_or_finish")
    graph.add_conditional_edges(
        "replan_or_finish",
        _replan_decider,
        {
            "deterministic_prelive": "deterministic_prelive",
            "setup_live_session": "setup_live_session",
        },
    )
    graph.add_edge("setup_live_session", END)

    return graph.compile(checkpointer=checkpointer)


def _collect_context_node(state: PreLiveAgentGraphState, service: Any) -> dict[str, Any]:
    """收集上下文：查货盘。"""
    try:
        products = service.query_products(state["room_id"], state["trace_id"])
        snapshots = [p.model_dump(mode="json") if hasattr(p, "model_dump") else {"product_id": p.product_id} for p in products]
        return {
            "products": snapshots,
            "product_count": len(products),
            "completed_nodes": _append_node(state, "collect_context"),
        }
    except Exception as exc:
        return {
            "error": "collect_context failed: " + str(exc),
            "completed_nodes": _append_node(state, "collect_context"),
        }


def _rules_planner_node(state: PreLiveAgentGraphState, planner: Any) -> dict[str, Any]:
    """规则 planner 节点。播前不用 LLM，用确定性规则生成路由决策。"""
    try:
        products = [CatalogProduct.model_validate(p) for p in state.get("products", [])]
        decision = planner.plan(
            room_id=state["room_id"],
            trace_id=state["trace_id"],
            products=products,
            trust_score=state.get("trust_score", 0.7),
        )
        return {
            "planner_decision": decision.model_dump(mode="json"),
            "planner_route": decision.route.value,
            "planner_reason": decision.reason,
            "goal": decision.goal,
            "completed_nodes": _append_node(state, "rules_planner"),
        }
    except Exception as exc:
        return {
            "planner_route": AgentReplanRoute.DIRECT_PLAN.value,
            "planner_reason": "planner failed, default to direct_plan: " + str(exc),
            "error": "rules_planner failed: " + str(exc),
            "completed_nodes": _append_node(state, "rules_planner"),
        }


def _route_by_decision_node(state: PreLiveAgentGraphState) -> dict[str, Any]:
    """路由决策节点——仅作为 conditional edge 的锚点。"""
    return {"completed_nodes": _append_node(state, "route_by_decision")}


def _route_decider(state: PreLiveAgentGraphState) -> str:
    """播前固定走 deterministic_prelive。"""
    return "deterministic_prelive"


def _deterministic_prelive_node(state: PreLiveAgentGraphState, service: Any) -> dict[str, Any]:
    """确定性播前链路：排品 + 手卡。"""
    try:
        products_raw = state.get("products", [])
        products = [CatalogProduct.model_validate(p) for p in products_raw] if products_raw else []
        plan = service.generate_plan(state["room_id"], products, state["trace_id"])
        cards = service.generate_cards(state["room_id"], plan, products, state["trace_id"])
        return {
            "plan_info": plan.model_dump(mode="json"),
            "cards_info": [c.model_dump(mode="json") for c in cards],
            "card_count": len(cards),
            "completed_nodes": _append_node(state, "deterministic_prelive"),
        }
    except Exception as exc:
        return {
            "error": "deterministic_prelive failed: " + str(exc),
            "completed_nodes": _append_node(state, "deterministic_prelive"),
        }


def _observe_result_node(state: PreLiveAgentGraphState) -> dict[str, Any]:
    """观察节点。"""
    return {
        "observations": state.get("observations", []),
        "completed_nodes": _append_node(state, "observe_result"),
    }


def _replan_or_finish_node(state: PreLiveAgentGraphState) -> dict[str, Any]:
    """决定是 re-plan 还是进入建播。"""
    return {
        "replan_count": state.get("replan_count", 0) + 1,
        "completed_nodes": _append_node(state, "replan_or_finish"),
    }


def _replan_decider(state: PreLiveAgentGraphState) -> str:
    """决定是否 replan。仅当有错误且未超过限制时回调 deterministic_prelive。"""
    replan_count = state.get("replan_count", 0)
    max_replan = state.get("max_replan", 1)
    has_error = state.get("error") is not None

    if has_error and replan_count <= max_replan:
        return "deterministic_prelive"
    return "setup_live_session"


def _setup_live_session_node(state: PreLiveAgentGraphState, service: Any) -> dict[str, Any]:
    """建播节点。复用现有 service setup_live_session。"""
    try:
        plan = LivePlanDraft.model_validate(state.get("plan_info", {})) if state.get("plan_info") else None
        if plan is None:
            products = [CatalogProduct.model_validate(p) for p in state.get("products", [])]
            plan = service.generate_plan(state["room_id"], products, state["trace_id"])

        gate_result, audit_id = service.setup_live_session(
            room_id=state["room_id"],
            plan=plan,
            trace_id=state["trace_id"],
            confirmed_setup=True,
        )
        return {
            "setup_status": "prepared" if gate_result.allowed else "pending",
            "setup_audit_id": audit_id,
            "completed_nodes": _append_node(state, "setup_live_session"),
        }
    except Exception as exc:
        return {
            "error": "setup_live_session failed: " + str(exc),
            "setup_status": "error",
            "completed_nodes": _append_node(state, "setup_live_session"),
        }


def _append_node(state: PreLiveAgentGraphState, node_name: str) -> list[str]:
    return [*state.get("completed_nodes", []), node_name]
