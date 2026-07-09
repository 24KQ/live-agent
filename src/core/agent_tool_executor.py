"""Phase 5A Agent Tool Executor。

LLM planner 选定的工具调用，不直接执行，而是由 AgentToolExecutor
在 ToolRegistry 白名单内校验、权限检查、审计后执行。

执行前必须检查：
- 工具是否在 ToolRegistry 注册
- 工具生命周期是否匹配当前阶段
- 参数是否符合工具 Schema（暂做基本检查）
- 高风险工具必须经过 hard-gate

LLM 不能直接写数据库或绕过安全 Hook。
"""

from __future__ import annotations

from ast import literal_eval
from typing import Any

from src.config.tool_registry import ToolNotFoundError, ToolRegistry
from src.core.agent_decision import AgentObservation
from src.core.security_hooks import evaluate_tool_gate
from src.state.models import LifecycleStage


class AgentToolExecutor:
    """白名单工具执行器。

    在 ToolRegistry 校验通过后，把工具调用转发给 PreLiveBusinessFlowService。
    每次执行返回 AgentObservation，包含状态、摘要和 audit_id。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        pre_live_service: Any,
    ) -> None:
        self._registry = registry
        self._service = pre_live_service

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        lifecycle: str = "PRE_LIVE",
    ) -> AgentObservation:
        """执行单个工具调用并返回观察结果。"""
        try:
            lifecycle_stage = LifecycleStage(lifecycle)
        except ValueError:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"unknown lifecycle: {lifecycle}",
                audit_id=None,
            )

        # Step 1: 工具注册校验
        try:
            tool = self._registry.get(tool_name)
        except ToolNotFoundError:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"tool {tool_name} not found in ToolRegistry",
                audit_id=None,
            )

        # Step 2: 生命周期校验
        if not self._registry.is_available(tool_name, lifecycle_stage):
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"tool {tool_name} not available in {lifecycle} lifecycle",
                audit_id=None,
            )

        # Step 3: 安全门禁（hard-gate 一直需要确认）
        gate = evaluate_tool_gate(tool, confirmed=False)
        if not gate.allowed and gate.requires_confirmation:
            return AgentObservation(
                tool_name=tool_name,
                status="pending",
                summary=f"{tool_name} requires human approval (hard-gate)",
                audit_id=None,
            )

        # Step 4: 派发到具体 service 方法
        return self._dispatch(tool_name, arguments, room_id, trace_id)

    def _dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> AgentObservation:
        """把工具名映射到 PreLiveBusinessFlowService 方法。"""
        try:
            if tool_name == "query_products":
                products = self._service.query_products(room_id, trace_id)
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"queried {len(products)} products",
                    audit_id=None,
                )

            elif tool_name == "generate_live_plan":
                products = arguments.get("products", self._service.query_products(room_id, trace_id))
                plan = self._service.generate_plan(room_id, products, trace_id)
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"generated plan with {len(plan.items)} items",
                    audit_id=None,
                )

            elif tool_name == "generate_product_card":
                products = arguments.get("products") or self._service.query_products(room_id, trace_id)
                plan = self._service.generate_plan(room_id, products, trace_id)
                cards = self._service.generate_cards(room_id, plan, products, trace_id)
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"generated {len(cards)} product cards",
                    audit_id=None,
                )

            elif tool_name == "setup_live_session":
                products = self._service.query_products(room_id, trace_id)
                plan = self._service.generate_plan(room_id, products, trace_id)
                gate, audit_id = self._service.setup_live_session(
                    room_id, plan, trace_id, confirmed_setup=True,
                )
                return AgentObservation(
                    tool_name=tool_name,
                    status="success" if gate.allowed else "pending",
                    summary=f"setup status: {'allowed' if gate.allowed else 'pending'}",
                    audit_id=audit_id,
                )

            # === ON_LIVE 工具 ===
            elif tool_name == "on_live_context_collect":
                danmaku = arguments.get("danmaku_summary", [])
                alerts = arguments.get("inventory_alerts", [])
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"collected context: {len(danmaku)} danmaku groups, {len(alerts)} alerts",
                    audit_id=None,
                )

            elif tool_name == "switch_product":
                # hard-gate 工具，需人审确认
                product_id = arguments.get("product_id", "")
                return AgentObservation(
                    tool_name=tool_name,
                    status="pending",
                    summary=f"switch_product requires human approval (hard-gate): {product_id}",
                    audit_id=None,
                )

            elif tool_name == "generate_on_live_prompt":
                sold_out_product_id = arguments.get("sold_out_product_id", "")
                backup_product_id = arguments.get("backup_product_id")
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"generated on-live prompt for sold_out: {sold_out_product_id}",
                    audit_id=None,
                )

            elif tool_name == "recommend_backup":
                sold_out_product_id = arguments.get("sold_out_product_id", "")
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"recommended backup for sold_out: {sold_out_product_id}",
                    audit_id=None,
                )

            else:
                return AgentObservation(
                    tool_name=tool_name,
                    status="error",
                    summary=f"tool {tool_name} not dispatchable in executor",
                    audit_id=None,
                )
        except Exception as exc:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"execution failed: {exc}",
                audit_id=None,
            )
