"""播前最小可控闭环服务。

该服务串联工具注册表、安全 Hook、Reducer 和审计 Store。它仍然不接 LLM，
改价建议和确认状态都由调用方传入，便于 Phase 1 先验证工程控制边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.config.tool_registry import get_default_tool_registry
from src.core.security_hooks import GateResult, evaluate_tool_gate
from src.state.models import Action, ActionType, LiveRoomState
from src.state.reducer import apply_action


@dataclass(frozen=True)
class PreLiveFlowResult:
    """播前流程执行结果。"""

    updated_state: LiveRoomState
    gate_result: GateResult
    audit_id: str | None
    trace_id: str


class PreLiveFlowService:
    """Phase 1 播前流程应用服务。"""

    def __init__(self, audit_store: ToolCallAuditStore) -> None:
        self._audit_store = audit_store
        self._registry = get_default_tool_registry()

    def request_price_change(
        self,
        state: LiveRoomState,
        product_id: str,
        new_price: Decimal,
        confirmed: bool,
        trace_id: str,
    ) -> PreLiveFlowResult:
        """请求执行播前改价。

        未确认 hard-gate 时只写入 pending 审计，不调用 Reducer，因此商品价格
        保持不变；确认后才构造 SET_PRICE Action 并进入 Reducer。
        """

        tool = self._registry.get("set_product_price")
        if not self._registry.is_available(tool.name, state.lifecycle):
            raise ValueError(f"tool {tool.name} is not available in lifecycle {state.lifecycle}")

        gate_result = evaluate_tool_gate(tool, confirmed=confirmed)
        request_payload = {"product_id": product_id, "price": str(new_price)}

        if not gate_result.allowed:
            audit_id = self._audit_store.record_event(
                AuditEvent(
                    trace_id=trace_id,
                    room_id=state.room_id,
                    tool_name=tool.name,
                    action_type=ActionType.SET_PRICE,
                    risk_level=tool.risk_level,
                    gate_decision=gate_result.decision,
                    operator_decision="pending",
                    request_payload=request_payload,
                    result_payload={"status": "requires_confirmation", "reason": gate_result.reason},
                )
            )
            return PreLiveFlowResult(state, gate_result, audit_id, trace_id)

        old_price = state.get_product(product_id).price
        action = Action(
            action_type=ActionType.SET_PRICE,
            product_id=product_id,
            payload={"price": str(new_price)},
            trace_id=trace_id,
        )
        updated_state = apply_action(state, action)
        audit_id = self._audit_store.record_event(
            AuditEvent(
                trace_id=trace_id,
                room_id=state.room_id,
                tool_name=tool.name,
                action_type=ActionType.SET_PRICE,
                risk_level=tool.risk_level,
                gate_decision=gate_result.decision,
                operator_decision="approved",
                request_payload=request_payload,
                result_payload={
                    "status": "applied",
                    "product_id": product_id,
                    "old_price": str(old_price),
                    "new_price": str(new_price),
                },
            )
        )
        return PreLiveFlowResult(updated_state, gate_result, audit_id, trace_id)

    def query_products(self, state: LiveRoomState) -> list[dict[str, str]]:
        """查询播前模拟货盘。

        这里返回面向 CLI/测试的轻量字典，不访问数据库。真实货盘查询会在 Phase 2
        引入样例商品数据和持久化查询。
        """

        tool = self._registry.get("query_products")
        if not self._registry.is_available(tool.name, state.lifecycle):
            raise ValueError(f"query_products is only available in PRE_LIVE, current lifecycle is {state.lifecycle}")
        return [
            {
                "product_id": product.product_id,
                "name": product.name,
                "price": str(product.price),
                "inventory": str(product.inventory),
                "is_active": str(product.is_active),
            }
            for product in state.products
        ]
