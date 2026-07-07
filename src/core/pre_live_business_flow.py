"""Phase 2A 播前业务闭环服务。

该服务串联数据库货盘、确定性排品、确定性手卡、安全 Hook 和 PostgreSQL 审计。
它仍然不调用 LLM、不接真实淘宝 API，也不处理播中 Kafka 事件。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.config.tool_registry import get_default_tool_registry
from src.core.security_hooks import GateResult, evaluate_tool_gate
from src.skills.live_plan_generator import LivePlanDraft, generate_live_plan
from src.skills.product_card_generator import ProductCard, generate_product_card
from src.skills.product_catalog import CatalogProduct, ProductCatalogRepository
from src.state.models import ActionType, LifecycleStage


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

    def __init__(self, catalog_repository: ProductCatalogRepository, audit_store: ToolCallAuditStore) -> None:
        self._catalog_repository = catalog_repository
        self._audit_store = audit_store
        self._registry = get_default_tool_registry()

    def prepare_room(self, room_id: str, trace_id: str, confirmed_setup: bool) -> PreLiveBusinessFlowResult:
        """执行播前准备闭环。

        流程固定为查询货盘、生成排品、生成前三个商品手卡、模拟建播确认。每一步都
        经过工具注册表和安全 Hook，且生成审计记录，方便后续按 trace_id 回放。
        """

        products = self._query_products(room_id=room_id, trace_id=trace_id)
        plan = self._generate_plan(room_id=room_id, products=products, trace_id=trace_id)
        cards = self._generate_cards(room_id=room_id, plan=plan, products=products, trace_id=trace_id)
        setup_gate, setup_audit_id = self._setup_live_session(
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

    def _query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """查询数据库货盘并写入审计。"""

        tool = self._require_pre_live_tool("query_products")
        gate = evaluate_tool_gate(tool, confirmed=True)
        products = self._catalog_repository.list_room_products(room_id)
        self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.name,
                action_type=ActionType.QUERY_PRODUCTS,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                request_payload={"room_id": room_id},
                result_payload={"product_count": len(products)},
            )
        )
        return products

    def _generate_plan(self, room_id: str, products: list[CatalogProduct], trace_id: str) -> LivePlanDraft:
        """生成排品草案并写入审计。"""

        tool = self._require_pre_live_tool("generate_live_plan")
        gate = evaluate_tool_gate(tool, confirmed=True)
        plan = generate_live_plan(room_id=room_id, products=products, trace_id=trace_id)
        self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.name,
                action_type=ActionType.GENERATE_LIVE_PLAN,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                request_payload={"room_id": room_id, "product_count": len(products)},
                result_payload={"plan_item_ids": [item.product_id for item in plan.items]},
            )
        )
        return plan

    def _generate_cards(
        self,
        room_id: str,
        plan: LivePlanDraft,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> list[ProductCard]:
        """为排品前三个商品生成手卡并写入审计。"""

        tool = self._require_pre_live_tool("generate_product_card")
        gate = evaluate_tool_gate(tool, confirmed=True)
        product_map = {product.product_id: product for product in products}
        cards: list[ProductCard] = []
        for item in plan.items[:3]:
            card = generate_product_card(product_map[item.product_id])
            cards.append(card)
            self._audit_store.record_event(
                AuditEvent(
                    trace_id=trace_id,
                    room_id=room_id,
                    tool_name=tool.name,
                    action_type=ActionType.GENERATE_PRODUCT_CARD,
                    risk_level=tool.risk_level,
                    gate_decision=gate.decision,
                    operator_decision="approved",
                    request_payload={"product_id": item.product_id},
                    result_payload={"title": card.title, "talking_point_count": len(card.talking_points)},
                )
            )
        return cards

    def _setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
    ) -> tuple[GateResult, str | None]:
        """模拟建播确认，并在确认后写入审计。"""

        tool = self._require_pre_live_tool("setup_live_session")
        gate = evaluate_tool_gate(tool, confirmed=confirmed_setup)
        if not gate.allowed:
            return gate, None

        audit_id = self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=room_id,
                tool_name=tool.name,
                action_type=ActionType.SETUP_LIVE_SESSION,
                risk_level=tool.risk_level,
                gate_decision=gate.decision,
                operator_decision="approved",
                request_payload={
                    "room_id": room_id,
                    "idempotency_key": f"{trace_id}:setup_live_session",
                },
                result_payload={
                    "status": "prepared",
                    "plan_item_ids": [item.product_id for item in plan.items],
                },
            )
        )
        return gate, audit_id

    def _require_pre_live_tool(self, tool_name: str):
        """读取工具元数据，并确保该工具只在播前阶段开放。"""

        tool = self._registry.get(tool_name)
        if not self._registry.is_available(tool.name, LifecycleStage.PRE_LIVE):
            raise ValueError(f"tool {tool.name} is not available in PRE_LIVE")
        return tool
