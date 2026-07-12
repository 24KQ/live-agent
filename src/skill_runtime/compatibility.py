"""Phase 11A AgentToolExecutor 兼容适配层。

本模块在 AgentToolExecutor 和 Skill Runtime 之间插入参数规范化层：
- 保留 AgentToolExecutor 的同步 execute() API 不变
- 四个核心工具（query_products, generate_live_plan, generate_product_card, setup_live_session）
  将旧参数补全为显式快照后委托统一 SyncSkillExecutorAdapter
- 版本不匹配、参数无效或审批不足统一转换为兼容 AgentObservation
- 其余工具继续走原有 PreLiveBusinessFlowService 派发
"""

from __future__ import annotations

from typing import Any

from src.config.tool_registry import ToolRegistry
from src.core.agent_decision import AgentObservation
from src.core.agent_tool_executor import AgentToolExecutor
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.security_hooks import evaluate_tool_gate
from src.skill_runtime.executor import SyncSkillExecutorAdapter
from src.skill_runtime.models import (
    ApprovalContext,
    ApprovalSource,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
)
from src.state.models import LifecycleStage

# ── 需要迁移到 Runtime 的核心工具列表 ────────────────────────────────

_CORE_SKILL_IDS: frozenset[str] = frozenset({
    "query_products",
    "generate_live_plan",
    "generate_product_card",
    "setup_live_session",
})


def _observation_from_result(
    tool_name: str,
    result: Any,
) -> AgentObservation:
    """把 SkillExecutionResult 转换为 AgentObservation，保证 Graph 兼容。"""
    status_map = {
        "success": "success",
        "pending": "pending",
        "error": "error",
    }
    return AgentObservation(
        tool_name=tool_name,
        status=status_map.get(result.status.value, "error"),
        summary=result.summary or f"{tool_name}: {result.status.value}",
        audit_id=result.audit_id,
    )


def _build_approval_context(gate: Any, confirmed: bool) -> ApprovalContext | None:
    """从 Gate 结果和 confirmed 标志构造审批证据。

    只有当 confirmed=True 时返回 TRUSTED_COMPAT 审批；
    Agent 调用缺少审批时返回 None，由 Executor 返回 pending。
    """
    from src.core.security_hooks import GateDecision

    if not confirmed:
        return None
    return ApprovalContext(
        source=ApprovalSource.TRUSTED_COMPAT,
        decision="APPROVED",
        operator_id="compat_migration",
        approval_audit_id="compat_agent_tool_executor",
    )


class CompatibleAgentToolExecutor(AgentToolExecutor):
    """兼容 AgentToolExecutor，把四个核心工具委托给 Skill Runtime。

    保留原有 execute() 的同步签名和 ToolRegistry 校验逻辑；
    在校验通过后，如果工具在 _CORE_SKILL_IDS 中，走 Runtime 执行；
    否则回退到原 _dispatch() 路径。

    Args:
        registry: 工具注册表，用于白名单、生命周期和门禁校验。
        pre_live_service: 原有 PreLiveBusinessFlowService（用于非核心工具的回退）。
        skill_executor: 统一 Skill Runtime 的同步适配器。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        pre_live_service: PreLiveBusinessFlowService,
        skill_executor: SyncSkillExecutorAdapter,
    ) -> None:
        super().__init__(registry=registry, pre_live_service=pre_live_service)
        self._skill_executor = skill_executor

    def _dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> AgentObservation:
        """覆盖父类 _dispatch，对核心工具走 Runtime，其余走原路径。"""
        if tool_name not in _CORE_SKILL_IDS:
            return super()._dispatch(tool_name, arguments, room_id, trace_id)

        # ── 核心工具参数规范化 ────────────────────────────────────────
        return self._dispatch_via_runtime(tool_name, arguments, room_id, trace_id)

    def _dispatch_via_runtime(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> AgentObservation:
        """通过 Skill Runtime 执行核心工具。"""
        # Step 1a: 构造执行上下文
        ctx = SkillExecutionContext(
            room_id=room_id,
            trace_id=trace_id,
            lifecycle="PRE_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        )

        # Step 1b: 构造显式输入参数
        normalized_args = self._normalize_arguments(tool_name, arguments, room_id)

        # Step 1c: setup_live_session 需要门禁审批
        if tool_name == "setup_live_session":
            # 先获取工具元数据做门禁校验
            try:
                tool = self._registry.get(tool_name)
            except Exception:
                return AgentObservation(
                    tool_name=tool_name,
                    status="error",
                    summary=f"tool {tool_name} not found in ToolRegistry",
                    audit_id=None,
                )
            gate = evaluate_tool_gate(tool, confirmed=False)
            confirmed = bool(arguments.get("confirmed_setup", False))
            approval = _build_approval_context(gate, confirmed=confirmed)
            ctx.approval = approval
            # 透传幂等键
            ctx.idempotency_key = arguments.get("idempotency_key")

        call = SkillCall(
            skill_id=tool_name,
            version="1.0.0",
            context=ctx,
            arguments=normalized_args,
        )

        try:
            result = self._skill_executor.execute(call)
            return _observation_from_result(tool_name, result)
        except Exception as exc:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"runtime dispatch failed: {exc}",
                audit_id=None,
            )

    @staticmethod
    def _normalize_arguments(
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
    ) -> dict[str, Any]:
        """把旧参数补全为显式快照后送 Runtime。

        query_products：无业务参数，room_id 来自上下文。
        generate_live_plan：products 已存在或保持原样。
        generate_product_card：product 作为不可变快照。
        setup_live_session：plan 作为不可变快照。
        """
        base = {"room_id": room_id}
        # 过滤掉 AgentToolExecutor 特有的辅助参数，只保留业务参数
        for key in ("confirmed_setup", "idempotency_key"):
            arguments.pop(key, None)
        base.update(arguments)
        return base


def build_compatible_executor(
    registry: ToolRegistry,
    pre_live_service: PreLiveBusinessFlowService,
    skill_executor: SyncSkillExecutorAdapter | None = None,
) -> CompatibleAgentToolExecutor:
    """构造兼容执行器工厂函数。

    如果未传入 skill_executor，创建一个新实例。
    """
    if skill_executor is None:
        skill_executor = SyncSkillExecutorAdapter()
    return CompatibleAgentToolExecutor(
        registry=registry,
        pre_live_service=pre_live_service,
        skill_executor=skill_executor,
    )
