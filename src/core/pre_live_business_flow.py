"""Phase 2A 播前业务闭环服务。

该服务串联数据库货盘、确定性排品、确定性手卡、安全 Hook 和 PostgreSQL 审计。
它仍然不调用 LLM、不接真实淘宝 API，也不处理播中 Kafka 事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.core.human_approval import HumanApprovalDecision, HumanApprovalRequest, HumanApprovalResponse
from src.core.security_hooks import (
    GateResult,
    evaluate_tool_gate,
    require_allowed_tool_gate,
)
from src.skill_runtime.policy_view import SkillPolicyView, get_default_skill_policy_view
from src.skills.live_plan_generator import LivePlanDraft, generate_live_plan
from src.skills.product_card_generator import ProductCard, generate_product_card
from src.skills.product_catalog import CatalogProduct, ProductCatalogRepository
from src.state.models import ActionType, LifecycleStage

if TYPE_CHECKING:
    from src.skill_runtime.models import ApprovalContext


@dataclass(frozen=True)
class PreLiveBusinessFlowResult:
    """Phase 2A 播前业务流结果。"""

    products: list[CatalogProduct]
    plan: LivePlanDraft
    cards: list[ProductCard]
    setup_gate: GateResult
    setup_audit_id: str | None
    trace_id: str


class PreLiveBusinessFlowService:
    """播前业务流应用服务。"""

    def __init__(
        self,
        catalog_repository: ProductCatalogRepository,
        audit_store: ToolCallAuditStore,
        *,
        policy_view: SkillPolicyView | None = None,
    ) -> None:
        self._catalog_repository = catalog_repository
        self._audit_store = audit_store
        self._policy_view = policy_view or get_default_skill_policy_view()

    def prepare_room(self, room_id: str, trace_id: str, confirmed_setup: bool) -> PreLiveBusinessFlowResult:
        """执行播前准备闭环。

        流程固定为查询货盘、生成排品、生成前三个商品手卡、模拟建播确认。每一步都
        经过 Skill 治理视图和安全 Hook，且生成审计记录，方便后续按 trace_id 回放。
        """

        products = self.query_products(room_id=room_id, trace_id=trace_id)
        plan = self.generate_plan(room_id=room_id, products=products, trace_id=trace_id)
        cards = self.generate_cards(room_id=room_id, plan=plan, products=products, trace_id=trace_id)
        setup_gate, setup_audit_id = self.setup_live_session(
            room_id=room_id,
            plan=plan,
            trace_id=trace_id,
            confirmed_setup=confirmed_setup,
        )
        return PreLiveBusinessFlowResult(
            products=products,
            plan=plan,
            cards=cards,
            setup_gate=setup_gate,
            setup_audit_id=setup_audit_id,
            trace_id=trace_id,
        )

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """查询数据库货盘并写入审计。"""

        tool = self._require_pre_live_tool("query_products")
        gate = require_allowed_tool_gate(tool)
        products = self._catalog_repository.list_room_products(room_id)
        self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.skill_id,
                action_type=ActionType.QUERY_PRODUCTS,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                request_payload={"room_id": room_id},
                result_payload={"product_count": len(products)},
            )
        )
        return products

    def generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
        """生成排品草案并写入审计。"""

        tool = self._require_pre_live_tool("generate_live_plan")
        gate = require_allowed_tool_gate(tool)
        plan = generate_live_plan(room_id=room_id, products=products, trace_id=trace_id)
        self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.skill_id,
                action_type=ActionType.GENERATE_LIVE_PLAN,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                request_payload={"room_id": room_id, "product_count": len(products)},
                result_payload={"plan_item_ids": [item.product_id for item in plan.items]},
            )
        )
        return plan

    def generate_cards(
        self,
        room_id: str,
        plan: LivePlanDraft,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> list[ProductCard]:
        """为排品前三个商品生成手卡并写入审计。"""

        product_map = {product.product_id: product for product in products}
        cards: list[ProductCard] = []
        for item in plan.items[:3]:
            cards.append(
                self.generate_card(
                    room_id=room_id,
                    product=product_map[item.product_id],
                    trace_id=trace_id,
                )
            )
        return cards

    def generate_card(
        self,
        room_id: str,
        product: CatalogProduct,
        trace_id: str,
    ) -> ProductCard:
        """为单个商品生成手卡并写入审计。

        这是 Phase 11A Skill Runtime 新增的显式单商品入口；
        generate_cards 继续保留供旧 Workflow 使用。
        """
        tool = self._require_pre_live_tool("generate_product_card")
        gate = require_allowed_tool_gate(tool)
        card = generate_product_card(product)
        self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.skill_id,
                action_type=ActionType.GENERATE_PRODUCT_CARD,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                request_payload={"product_id": product.product_id},
                result_payload={"title": card.title, "talking_point_count": len(card.talking_points)},
            )
        )
        return card



    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
        *,
        idempotency_key: str | None = None,
        approval_context: "ApprovalContext | None" = None,
    ) -> tuple[GateResult, str | None]:
        """模拟建播确认，并在确认后写入审计。

        approval_context 由 Runtime Facade 消费；legacy 服务保留该关键字仅为
        Protocol 兼容，不据此绕过现有 confirmed_setup 安全门禁。
        """

        tool = self._require_pre_live_tool("setup_live_session")
        gate = evaluate_tool_gate(tool, confirmed=confirmed_setup)
        if not gate.allowed:
            return gate, None

        if idempotency_key is None:
            idempotency_key = f"{trace_id}:setup_live_session"

        # 幂等判定统一交给 Audit Store 的数据库唯一约束和完整事实比较。这里不再做
        # 仅按 trace/key 的预查，否则同一调用键携带不同排品方案时会错误返回旧 ID。
        audit_id = self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.skill_id,
                action_type=ActionType.SETUP_LIVE_SESSION,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                idempotency_key=idempotency_key,
                request_payload={
                    "room_id": room_id,
                    "idempotency_key": idempotency_key,
                },
                result_payload={
                    "status": "prepared",
                    "plan_item_ids": [item.product_id for item in plan.items],
                },
            )
        )
        return gate, audit_id

    def record_setup_approval_event(
        self,
        request: HumanApprovalRequest,
        response: HumanApprovalResponse | None,
    ) -> str:
        """记录建播人工审批事件，并对 LangGraph 节点重放做幂等保护。

        `interrupt()` 恢复时会从当前节点开头重新执行，pending 审计写在 interrupt 前
        才能让人工看到“正在等待确认”的留痕。为避免恢复时重复插入 pending 记录，
        这里使用 trace_id、工具名和审批状态组成 idempotency_key，并交由 Audit Store
        的全局唯一约束及完整事实比较处理重放，防止同键异审批内容被误判为成功。
        """

        tool = self._require_pre_live_tool(request.tool_name)
        status = "pending" if response is None else response.decision.value
        idempotency_key = f"{request.trace_id}:{request.tool_name}:approval:{status}"

        is_approved = response is not None and response.decision == HumanApprovalDecision.APPROVED
        gate = evaluate_tool_gate(tool, confirmed=is_approved)
        request_payload = request.model_dump(mode="json")
        request_payload["idempotency_key"] = idempotency_key

        if response is None:
            action_type = ActionType.HUMAN_APPROVAL_PENDING
            operator_decision = "pending"
            result_payload = {
                "status": "pending",
                "requires_confirmation": True,
                "decision": None,
            }
        else:
            action_type = ActionType.HUMAN_APPROVAL_RESUMED
            operator_decision = response.decision.value
            result_payload = {
                "status": "resumed" if response.decision == HumanApprovalDecision.APPROVED else "rejected",
                "decision": response.decision.value,
                "operator_id": response.operator_id,
                "reason": response.reason,
            }

        return self._audit_store.record_event(
            AuditEvent(
                trace_id=request.trace_id,
                room_id=request.room_id,
                tool_name=f"{request.tool_name}_approval",
                action_type=action_type,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision=operator_decision,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                result_payload=result_payload,
            )
        )

    def _require_pre_live_tool(self, tool_name: str):
        """读取工具元数据，并确保该工具只在播前阶段开放。"""

        tool = self._policy_view.get(tool_name)
        if not self._policy_view.is_available(tool.skill_id, LifecycleStage.PRE_LIVE):
            raise ValueError(f"tool {tool.skill_id} is not available in PRE_LIVE")
        return tool
