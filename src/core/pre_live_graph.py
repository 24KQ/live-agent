"""Phase 2D LangGraph 播前 Harness 骨架。

本模块只把已经稳定的播前业务服务包装成 LangGraph 编排层，不接 LLM、不接
真实平台 API、不启用持久 checkpoint，也不使用 interrupt。这样可以先验证
LangGraph 与现有 Harness 边界的配合方式：业务逻辑仍在 service、状态变更仍走
Reducer、安全门禁仍走 SecurityHook、审计仍写 PostgreSQL。
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from src.core.security_hooks import GateResult
from src.skills.live_plan_generator import LivePlanDraft
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


class PreLiveBusinessServiceProtocol(Protocol):
    """LangGraph 节点需要的播前业务服务接口。

    使用 Protocol 是为了让图编排层只依赖服务能力，而不是强绑定某个具体类。
    单元测试可以传入轻量替身，集成测试则传入真实 `PreLiveBusinessFlowService`。
    """

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """查询播前货盘并写入审计。"""
        ...

    def generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
        """生成播前排品并写入审计。"""
        ...

    def generate_cards(
        self,
        room_id: str,
        plan: LivePlanDraft,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> list[ProductCard]:
        """生成商品手卡并写入审计。"""
        ...

    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
    ) -> tuple[GateResult, str | None]:
        """执行模拟建播 hard-gate，并在确认后写入审计。"""
        ...


class PreLiveGraphState(TypedDict, total=False):
    """LangGraph 播前流程状态。

    该 state 同时包含对外展示字段和节点间传递的内部对象。Phase 2D 不启用持久
    checkpoint，因此可以临时携带 Pydantic 对象；后续做 PostgreSQL checkpoint
    时，再把内部对象改成可序列化快照。
    """

    room_id: str
    trace_id: str
    confirmed_setup: bool
    completed_nodes: list[str]
    products: list[CatalogProduct]
    product_count: int
    plan: LivePlanDraft
    plan_item_ids: list[str]
    plan_item_count: int
    cards: list[ProductCard]
    card_count: int
    compliance_summary: str
    setup_gate_decision: str
    setup_gate_allowed: bool
    setup_requires_confirmation: bool
    setup_status: str
    setup_audit_id: str | None
    error: str | None


def create_initial_pre_live_graph_state(room_id: str, trace_id: str, confirmed_setup: bool) -> PreLiveGraphState:
    """创建播前 graph 的初始 state。

    这里做最基础的空字符串校验，避免 graph 运行到中间节点后才因为缺少 room_id
    或 trace_id 产生难以定位的审计错误。
    """

    if not room_id.strip():
        raise ValueError("room_id must not be blank")
    if not trace_id.strip():
        raise ValueError("trace_id must not be blank")
    return {
        "room_id": room_id.strip(),
        "trace_id": trace_id.strip(),
        "confirmed_setup": confirmed_setup,
        "completed_nodes": [],
        "error": None,
    }


def build_pre_live_graph(service: PreLiveBusinessServiceProtocol):
    """构建 LangGraph 播前编排应用。

    图节点保持固定顺序，先验证 LangGraph 作为轻量 workflow 的接入方式。后续接入
    LLM、interrupt 或 checkpoint 时，可以在这个图上继续增加条件边和持久化配置。
    """

    graph = StateGraph(PreLiveGraphState)
    graph.add_node("query_products", lambda state: _query_products_node(state, service))
    graph.add_node("generate_live_plan", lambda state: _generate_plan_node(state, service))
    graph.add_node("generate_product_cards", lambda state: _generate_cards_node(state, service))
    graph.add_node("compliance_check", _compliance_check_node)
    graph.add_node("setup_live_session", lambda state: _setup_live_session_node(state, service))

    graph.add_edge(START, "query_products")
    graph.add_edge("query_products", "generate_live_plan")
    graph.add_edge("generate_live_plan", "generate_product_cards")
    graph.add_edge("generate_product_cards", "compliance_check")
    graph.add_edge("compliance_check", "setup_live_session")
    graph.add_edge("setup_live_session", END)
    return graph.compile()


def _query_products_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """查询货盘节点。

    节点只调用服务层公开方法，服务层负责工具注册、安全 Hook 和审计；LangGraph
    本身不直接访问数据库，也不绕过审计链路。
    """

    products = service.query_products(room_id=state["room_id"], trace_id=state["trace_id"])
    return {
        "products": products,
        "product_count": len(products),
        "completed_nodes": _append_node(state, "query_products"),
    }


def _generate_plan_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """生成排品节点。"""

    products = _require_state_value(state, "products")
    plan = service.generate_plan(room_id=state["room_id"], products=products, trace_id=state["trace_id"])
    return {
        "plan": plan,
        "plan_item_ids": [item.product_id for item in plan.items],
        "plan_item_count": len(plan.items),
        "completed_nodes": _append_node(state, "generate_live_plan"),
    }


def _generate_cards_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """生成商品手卡节点。"""

    products = _require_state_value(state, "products")
    plan = _require_state_value(state, "plan")
    cards = service.generate_cards(
        room_id=state["room_id"],
        plan=plan,
        products=products,
        trace_id=state["trace_id"],
    )
    return {
        "cards": cards,
        "card_count": len(cards),
        "completed_nodes": _append_node(state, "generate_product_cards"),
    }


def _compliance_check_node(state: PreLiveGraphState) -> dict[str, Any]:
    """生成轻量合规/风险摘要。

    Phase 2D 不新增合规引擎，只把当前播前 graph 的关键边界写入 state：样例数据、
    确定性手卡、建播前 hard-gate。后续接入 LLM 后，这个节点可以扩展为 Schema
    校验、禁用词检查和人工复核。
    """

    summary = (
        "Phase 2D 播前 Graph 使用本地脱敏样例数据和确定性规则；"
        "建播写入仍由 setup_live_session 执行 hard-gate，未确认不得伪装成功。"
    )
    return {
        "compliance_summary": summary,
        "completed_nodes": _append_node(state, "compliance_check"),
    }


def _setup_live_session_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """模拟建播节点。

    是否允许建播完全取决于服务层返回的 GateResult。LangGraph 只保存结果，不直接
    修改业务状态，也不在 hard-gate 未确认时创建成功审计。
    """

    plan = _require_state_value(state, "plan")
    gate, audit_id = service.setup_live_session(
        room_id=state["room_id"],
        plan=plan,
        trace_id=state["trace_id"],
        confirmed_setup=state["confirmed_setup"],
    )
    setup_status = "prepared" if gate.allowed else "pending_confirmation"
    return {
        "setup_gate_decision": gate.decision.value,
        "setup_gate_allowed": gate.allowed,
        "setup_requires_confirmation": gate.requires_confirmation,
        "setup_status": setup_status,
        "setup_audit_id": audit_id,
        "completed_nodes": _append_node(state, "setup_live_session"),
    }


def _append_node(state: PreLiveGraphState, node_name: str) -> list[str]:
    """返回追加节点名后的执行历史。"""

    return [*state.get("completed_nodes", []), node_name]


def _require_state_value(state: PreLiveGraphState, key: str):
    """读取节点依赖字段，缺失时返回明确错误。"""

    if key not in state:
        raise ValueError(f"pre-live graph state missing required key: {key}")
    return state[key]
