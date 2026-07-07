"""Phase 2B 播中售罄事件闭环服务。

该服务串联播中事件、安全 Hook、Reducer、备选商品推荐、主播提示和审计写入。
它不启动 Kafka consumer，也不接真实淘宝 API；当前阶段只处理本地模拟的售罄事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.audit.tool_call_audit import AuditEvent, ToolCallAuditStore
from src.config.tool_registry import get_default_tool_registry
from src.core.security_hooks import evaluate_tool_gate
from src.skills.backup_product_recommender import BackupProductNotFoundError, recommend_backup_product
from src.skills.on_live_events import InventoryEvent, OnLiveEventType
from src.skills.on_live_prompt import OnLivePrompt, generate_sold_out_prompt
from src.state.models import Action, ActionType, LifecycleStage, LiveRoomState, Product
from src.state.reducer import apply_action


@dataclass(frozen=True)
class OnLiveFlowResult:
    """播中售罄闭环处理结果。"""

    updated_state: LiveRoomState
    backup_product: Product | None
    prompt: OnLivePrompt
    audit_ids: list[str] = field(default_factory=list)
    trace_id: str = ""


class OnLiveFlowService:
    """播中事件应用服务。"""

    def __init__(self, audit_store: ToolCallAuditStore) -> None:
        self._audit_store = audit_store
        self._registry = get_default_tool_registry()

    def handle_sold_out_event(self, state: LiveRoomState, event: InventoryEvent) -> OnLiveFlowResult:
        """处理售罄事件。

        处理顺序固定：校验生命周期 -> Reducer 下架售罄商品 -> 推荐备选商品 ->
        如有备选则切换当前商品 -> 生成主播提示 -> 写入审计链路。
        """

        if state.lifecycle != LifecycleStage.ON_LIVE:
            raise ValueError(f"sold out event can only be handled in ON_LIVE, current lifecycle is {state.lifecycle}")
        if state.room_id != event.room_id:
            raise ValueError(f"event room_id {event.room_id} does not match state room_id {state.room_id}")
        if event.event_type != OnLiveEventType.SOLD_OUT:
            raise ValueError(f"unsupported on-live event type: {event.event_type}")

        audit_ids: list[str] = []
        updated_state = self._mark_sold_out(state, event, audit_ids)
        backup_product = self._recommend_backup(updated_state, event, audit_ids)

        if backup_product is not None:
            switch_action = Action(
                action_type=ActionType.SWITCH_PRODUCT,
                product_id=backup_product.product_id,
                trace_id=event.trace_id,
            )
            updated_state = apply_action(updated_state, switch_action)

        sold_out_product = updated_state.get_product(event.product_id)
        prompt = self._generate_prompt(updated_state, event, sold_out_product, backup_product, audit_ids)
        return OnLiveFlowResult(
            updated_state=updated_state,
            backup_product=backup_product,
            prompt=prompt,
            audit_ids=audit_ids,
            trace_id=event.trace_id,
        )

    def _mark_sold_out(self, state: LiveRoomState, event: InventoryEvent, audit_ids: list[str]) -> LiveRoomState:
        """调用 Reducer 下架售罄商品并记录审计。"""

        tool = self._require_on_live_tool("handle_sold_out_event")
        gate = evaluate_tool_gate(tool, confirmed=True)
        action = Action(
            action_type=ActionType.MARK_SOLD_OUT,
            product_id=event.product_id,
            trace_id=event.trace_id,
        )
        updated_state = apply_action(state, action)
        audit_ids.append(
            self._audit_store.record_event(
                AuditEvent(
                    trace_id=event.trace_id,
                    room_id=event.room_id,
                    tool_name=tool.name,
                    action_type=ActionType.HANDLE_SOLD_OUT_EVENT,
                    risk_level=tool.risk_level,
                    gate_decision=gate.decision,
                    operator_decision="approved",
                    request_payload={
                        "product_id": event.product_id,
                        "event_type": event.event_type.value,
                        "idempotency_key": f"{event.trace_id}:handle_sold_out_event",
                    },
                    result_payload={"status": "sold_out_marked", "product_id": event.product_id},
                )
            )
        )
        return updated_state

    def _recommend_backup(
        self,
        state: LiveRoomState,
        event: InventoryEvent,
        audit_ids: list[str],
    ) -> Product | None:
        """推荐备选商品；没有备选时写入人工接管审计。"""

        tool = self._require_on_live_tool("recommend_backup_product")
        gate = evaluate_tool_gate(tool, confirmed=True)
        try:
            backup_product = recommend_backup_product(state, sold_out_product_id=event.product_id)
            result_payload = {"status": "backup_found", "backup_product_id": backup_product.product_id}
        except BackupProductNotFoundError as exc:
            backup_product = None
            result_payload = {"status": "manual_takeover_required", "reason": str(exc)}

        audit_ids.append(
            self._audit_store.record_event(
                AuditEvent(
                    trace_id=event.trace_id,
                    room_id=event.room_id,
                    tool_name=tool.name,
                    action_type=ActionType.RECOMMEND_BACKUP_PRODUCT,
                    risk_level=tool.risk_level,
                    gate_decision=gate.decision,
                    operator_decision="approved",
                    request_payload={"sold_out_product_id": event.product_id},
                    result_payload=result_payload,
                )
            )
        )
        return backup_product

    def _generate_prompt(
        self,
        state: LiveRoomState,
        event: InventoryEvent,
        sold_out_product: Product,
        backup_product: Product | None,
        audit_ids: list[str],
    ) -> OnLivePrompt:
        """生成主播提示并写入审计。"""

        tool = self._require_on_live_tool("generate_on_live_prompt")
        gate = evaluate_tool_gate(tool, confirmed=True)
        prompt = generate_sold_out_prompt(sold_out_product=sold_out_product, backup_product=backup_product)
        audit_ids.append(
            self._audit_store.record_event(
                AuditEvent(
                    trace_id=event.trace_id,
                    room_id=state.room_id,
                    tool_name=tool.name,
                    action_type=ActionType.GENERATE_ON_LIVE_PROMPT,
                    risk_level=tool.risk_level,
                    gate_decision=gate.decision,
                    operator_decision="approved",
                    request_payload={
                        "sold_out_product_id": sold_out_product.product_id,
                        "backup_product_id": backup_product.product_id if backup_product else None,
                    },
                    result_payload={"severity": prompt.severity, "message": prompt.message},
                )
            )
        )
        return prompt

    def _require_on_live_tool(self, tool_name: str):
        """读取播中工具元数据，并确保该工具只在 ON_LIVE 阶段开放。"""

        tool = self._registry.get(tool_name)
        if not self._registry.is_available(tool.name, LifecycleStage.ON_LIVE):
            raise ValueError(f"tool {tool.name} is not available in ON_LIVE")
        return tool
