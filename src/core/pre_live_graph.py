"""Phase 2D LangGraph 播前 Harness 骨架。

本模块只把已经稳定的播前业务服务包装成 LangGraph 编排层，不接 LLM、不接
真实平台 API。Phase 2E 开始支持 PostgreSQL checkpoint，因此 Graph state 只
保存 JSON 可序列化快照；业务逻辑仍在 service、状态变更仍走 Reducer、安全门禁
仍走 SecurityHook、审计仍写 PostgreSQL。
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from src.core.human_approval import (
    HumanApprovalDecision,
    HumanApprovalRequest,
    HumanApprovalResponse,
    validate_human_approval_response,
)
from src.core.security_hooks import GateResult
from src.skills.live_plan_generator import LivePlanDraft
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct
from src.skill_runtime.models import ApprovalContext, ApprovalSource
from src.state.models import RiskLevel


ProductSnapshot = dict[str, Any]
PlanSnapshot = dict[str, Any]
CardSnapshot = dict[str, Any]


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
        *,
        approval_context: ApprovalContext | None = None,
    ) -> tuple[GateResult, str | None]:
        """执行模拟建播 hard-gate，并在确认后写入审计。"""
        ...

    def record_setup_approval_event(
        self,
        request: HumanApprovalRequest,
        response: HumanApprovalResponse | None,
    ) -> str:
        """写入建播人工审批审计，并返回审批审计 ID。"""
        ...


class PreLiveGraphState(TypedDict, total=False):
    """LangGraph 播前流程状态。

    Phase 2E 需要把 state 写入 PostgreSQL checkpoint，因此这里不再保存
    CatalogProduct、LivePlanDraft、ProductCard 等 Pydantic 对象，而是保存
    `model_dump(mode="json")` 生成的普通 dict/list。节点内部如需调用现有
    service，会从快照恢复领域对象，调用后再转回快照。
    """

    room_id: str
    trace_id: str
    confirmed_setup: bool
    enable_human_approval: bool
    completed_nodes: list[str]
    products_snapshot: list[ProductSnapshot]
    product_count: int
    plan_snapshot: PlanSnapshot
    plan_item_ids: list[str]
    plan_item_count: int
    cards_snapshot: list[CardSnapshot]
    card_count: int
    compliance_summary: str
    setup_gate_decision: str
    setup_gate_allowed: bool
    setup_requires_confirmation: bool
    setup_status: str
    setup_audit_id: str | None
    approval_request: dict[str, Any]
    approval_pending_audit_id: str | None
    approval_resume_audit_id: str | None
    approval_decision: str | None
    approval_operator_id: str | None
    approval_reason: str | None
    error: str | None


def create_initial_pre_live_graph_state(
    room_id: str,
    trace_id: str,
    confirmed_setup: bool,
    enable_human_approval: bool = False,
) -> PreLiveGraphState:
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
        "enable_human_approval": enable_human_approval,
        "completed_nodes": [],
        "error": None,
    }


def create_pre_live_graph_config(trace_id: str) -> dict[str, dict[str, str]]:
    """创建 LangGraph checkpoint 配置。

    LangGraph 使用 `thread_id` 定位一条可恢复执行链。这里直接复用 trace_id，
    让 checkpoint、工具审计和 CLI 输出可以用同一个 ID 串起来排查。
    """

    if not trace_id.strip():
        raise ValueError("trace_id must not be blank")
    return {"configurable": {"thread_id": trace_id.strip()}}


def build_pre_live_graph(
    service: PreLiveBusinessServiceProtocol,
    *,
    checkpointer: Any | None = None,
    interrupt_after: list[str] | None = None,
):
    """构建 LangGraph 播前编排应用。

    图节点保持固定顺序。Phase 2E 允许传入官方 PostgresSaver 或 InMemorySaver
    作为 checkpointer，并通过 `interrupt_after` 模拟中断恢复；业务节点本身仍不
    直接操作 checkpoint 表。
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
    return graph.compile(checkpointer=checkpointer, interrupt_after=interrupt_after)


def _query_products_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """查询货盘节点。

    节点只调用服务层公开方法，服务层负责工具注册、安全 Hook 和审计；LangGraph
    本身不直接访问数据库，也不绕过审计链路。
    """

    products = service.query_products(room_id=state["room_id"], trace_id=state["trace_id"])
    return {
        "products_snapshot": [product_to_snapshot(product) for product in products],
        "product_count": len(products),
        "completed_nodes": _append_node(state, "query_products"),
    }


def _generate_plan_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """生成排品节点。"""

    products = [product_from_snapshot(snapshot) for snapshot in _require_state_value(state, "products_snapshot")]
    plan = service.generate_plan(room_id=state["room_id"], products=products, trace_id=state["trace_id"])
    return {
        "plan_snapshot": plan_to_snapshot(plan),
        "plan_item_ids": [item.product_id for item in plan.items],
        "plan_item_count": len(plan.items),
        "completed_nodes": _append_node(state, "generate_live_plan"),
    }


def _generate_cards_node(state: PreLiveGraphState, service: PreLiveBusinessServiceProtocol) -> dict[str, Any]:
    """生成商品手卡节点。"""

    products = [product_from_snapshot(snapshot) for snapshot in _require_state_value(state, "products_snapshot")]
    plan = plan_from_snapshot(_require_state_value(state, "plan_snapshot"))
    cards = service.generate_cards(
        room_id=state["room_id"],
        plan=plan,
        products=products,
        trace_id=state["trace_id"],
    )
    return {
        "cards_snapshot": [card_to_snapshot(card) for card in cards],
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

    plan = plan_from_snapshot(_require_state_value(state, "plan_snapshot"))
    if state.get("enable_human_approval", False):
        return _setup_live_session_with_human_approval(state, service, plan)

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


def _setup_live_session_with_human_approval(
    state: PreLiveGraphState,
    service: PreLiveBusinessServiceProtocol,
    plan: LivePlanDraft,
) -> dict[str, Any]:
    """通过 LangGraph interrupt 执行建播人审。

    LangGraph 的 `interrupt()` 在恢复时会从当前节点开头重新执行，因此 pending 审计
    必须通过服务层幂等写入；否则 `Command(resume=...)` 会导致同一条 pending 审计
    被重复插入。真正的建播动作只放在 interrupt 返回之后，并且只有 approved 才执行。
    """

    approval_request = HumanApprovalRequest(
        trace_id=state["trace_id"],
        room_id=state["room_id"],
        tool_name="setup_live_session",
        risk_level=RiskLevel.HIGH,
        action="confirm_setup_live_session",
        plan_item_ids=[item.product_id for item in plan.items],
        message="请确认是否按当前排品方案模拟建播。",
    )
    pending_audit_id = service.record_setup_approval_event(approval_request, None)
    interrupt_payload = approval_request.model_dump(mode="json")
    interrupt_payload["pending_audit_id"] = pending_audit_id

    resume_payload = interrupt(interrupt_payload)
    approval_response = validate_human_approval_response(
        approval_request,
        HumanApprovalResponse.model_validate(resume_payload),
    )
    resume_audit_id = service.record_setup_approval_event(approval_request, approval_response)

    if approval_response.decision == HumanApprovalDecision.REJECTED:
        return {
            "approval_request": interrupt_payload,
            "approval_pending_audit_id": pending_audit_id,
            "approval_resume_audit_id": resume_audit_id,
            "approval_decision": approval_response.decision.value,
            "approval_operator_id": approval_response.operator_id,
            "approval_reason": approval_response.reason,
            "setup_gate_decision": "hard-gate",
            "setup_gate_allowed": False,
            "setup_requires_confirmation": False,
            "setup_status": "rejected",
            "setup_audit_id": None,
            "completed_nodes": _append_node(state, "setup_live_session"),
        }

    gate, audit_id = service.setup_live_session(
        room_id=state["room_id"],
        plan=plan,
        trace_id=state["trace_id"],
        confirmed_setup=True,
        approval_context=ApprovalContext(
            source=ApprovalSource.HUMAN_INTERRUPT,
            decision="APPROVED",
            operator_id=approval_response.operator_id,
            approval_audit_id=resume_audit_id,
        ),
    )
    return {
        "approval_request": interrupt_payload,
        "approval_pending_audit_id": pending_audit_id,
        "approval_resume_audit_id": resume_audit_id,
        "approval_decision": approval_response.decision.value,
        "approval_operator_id": approval_response.operator_id,
        "approval_reason": approval_response.reason,
        "setup_gate_decision": gate.decision.value,
        "setup_gate_allowed": gate.allowed,
        "setup_requires_confirmation": gate.requires_confirmation,
        "setup_status": "prepared" if gate.allowed else "pending_confirmation",
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


def product_to_snapshot(product: CatalogProduct) -> ProductSnapshot:
    """把商品模型转换成 JSON 安全快照。

    `mode="json"` 会把 Decimal 等类型转换成可序列化字符串，避免 PostgresSaver
    的 msgpack 序列化器收到业务模型对象或非 JSON 类型。
    """

    return product.model_dump(mode="json")


def product_from_snapshot(snapshot: ProductSnapshot) -> CatalogProduct:
    """从 checkpoint 快照恢复商品模型。"""

    return CatalogProduct.model_validate(snapshot)


def plan_to_snapshot(plan: LivePlanDraft) -> PlanSnapshot:
    """把排品草案转换成 JSON 安全快照。"""

    return plan.model_dump(mode="json")


def plan_from_snapshot(snapshot: PlanSnapshot) -> LivePlanDraft:
    """从 checkpoint 快照恢复排品草案。"""

    return LivePlanDraft.model_validate(snapshot)


def card_to_snapshot(card: ProductCard) -> CardSnapshot:
    """把商品手卡转换成 JSON 安全快照。"""

    return card.model_dump(mode="json")


def card_from_snapshot(snapshot: CardSnapshot) -> ProductCard:
    """从 checkpoint 快照恢复商品手卡。"""

    return ProductCard.model_validate(snapshot)
