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

from typing import Any

# jsonschema is optional for parameter validation; skip when unavailable
_HAVE_JSONSCHEMA: bool = False
try:
    import jsonschema
    _HAVE_JSONSCHEMA = True
except ImportError:
    pass

from src.config.tool_registry import ToolNotFoundError, ToolRegistry
from src.core.agent_decision import AgentObservation
from src.core.security_hooks import evaluate_tool_gate
from src.skill_runtime.compatibility import (
    CORE_SKILL_IDS,
    CompatibilityArgumentNormalizer,
    observation_from_skill_result,
)
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.pre_live_handlers import build_pre_live_handlers
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
        skill_executor: SyncSkillExecutorAdapter | None = None,
    ) -> None:
        """保留原有两参数构造方式，并允许测试或装配层注入同步 Runtime 适配器。

        默认适配器使用与 legacy 入口相同的播前服务实例创建四个 Handler，确保货盘、
        审计和幂等存储保持一致；注入能力只用于隔离测试和上层显式装配。
        """
        self._registry = registry
        self._service = pre_live_service
        self._normalizer = CompatibilityArgumentNormalizer(pre_live_service)
        self._skill_executor = skill_executor or SyncSkillExecutorAdapter(
            SkillExecutor(handlers=build_pre_live_handlers(pre_live_service))
        )

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

        # 四个核心工具由统一 Runtime 完成 Schema、门禁、幂等和 Handler 校验。
        # 这里不再预先拦截 setup，否则 Runtime 无法返回统一的 pending/error 契约。
        if tool_name in CORE_SKILL_IDS:
            return self._dispatch_core_via_runtime(
                tool_name=tool_name,
                arguments=arguments,
                room_id=room_id,
                trace_id=trace_id,
                lifecycle=lifecycle_stage,
            )

        # Step 3: 未迁移工具继续沿用原安全门禁和 legacy 派发。
        gate = evaluate_tool_gate(tool, confirmed=False)
        if not gate.allowed and gate.requires_confirmation:
            return AgentObservation(
                tool_name=tool_name,
                status="pending",
                summary=f"{tool_name} requires human approval (hard-gate)",
                audit_id=None,
            )

        # Step 4: 派发到未迁移工具的 legacy 分支。
        return self._dispatch_legacy(tool_name, arguments, room_id, trace_id)

    def _dispatch_core_via_runtime(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        lifecycle: LifecycleStage,
    ) -> AgentObservation:
        """规范化并执行一个核心 Skill，异常时显式失败且绝不 legacy fallback。"""
        try:
            call = self._normalizer.normalize(
                tool_name=tool_name,
                arguments=arguments,
                room_id=room_id,
                trace_id=trace_id,
                lifecycle=lifecycle,
            )
            result = self._skill_executor.execute(call)
            return observation_from_skill_result(tool_name, result)
        except Exception:
            # 旧入口仍以 AgentObservation 表达失败，但不重试旧核心 service，避免一次
            # Agent 决策产生两次业务执行或 Runtime 失败后悄悄改变语义。异常文本可能
            # 包含凭据、商品标识或 Pydantic 输入，因此这里只返回受控错误码与固定摘要。
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary="HANDLER_FAILED: skill runtime execution failed",
                audit_id=None,
            )

    def _dispatch_legacy(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> AgentObservation:
        """派发九个尚未迁移的工具；四个核心工具不得出现在本方法中。"""
        # Step 4a: Parameter schema validation (optional dep, skip when no jsonschema)
        try:
            tool = self._registry.get(tool_name)
            if _HAVE_JSONSCHEMA and tool.parameter_schema:
                jsonschema.validate(instance=arguments, schema=tool.parameter_schema)
        except ToolNotFoundError:
            pass  # already checked in execute()
        except jsonschema.ValidationError as exc:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"参数校验失败: {exc.message}",
                audit_id=None,
            )
        try:
            # === ON_LIVE 工具 ===
            if tool_name == "on_live_context_collect":
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

            elif tool_name == "recommend_backup_product":
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
