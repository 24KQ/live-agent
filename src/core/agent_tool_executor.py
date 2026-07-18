"""Phase 5A Agent Tool Executor。

LLM planner 选定的工具调用，不直接执行，而是由 AgentToolExecutor
在 SkillPolicyView 白名单内校验、权限检查、审计后执行。

执行前必须检查：
- Skill 是否在 Catalog 治理视图注册
- 工具生命周期是否匹配当前阶段
- 参数是否符合工具 Schema（暂做基本检查）
- 高风险工具必须经过 hard-gate

LLM 不能直接写数据库或绕过安全 Hook。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import jsonschema
from pydantic import ValidationError

from src.core.agent_decision import AgentObservation
from src.core.security_hooks import evaluate_tool_gate
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.compatibility import (
    CORE_SKILL_IDS,
    CompatibilityArgumentNormalizer,
    CompatibilityEnrichmentError,
    observation_from_skill_result,
)
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.models import SkillCall, SkillExecutionContext, SkillExecutionRoute
from src.skill_runtime.pre_live_handlers import build_pre_live_handlers
from src.skill_runtime.policy_view import (
    SkillPolicyView,
    assert_policy_view_matches_catalog,
    get_default_skill_policy_view,
)
from src.skill_runtime.routing import RouteConfig, RoutePolicy
from src.state.models import LifecycleStage


class AgentToolExecutor:
    """白名单工具执行器。

    在 SkillPolicyView 校验通过后，把工具调用转发给 PreLiveBusinessFlowService。
    每次执行返回 AgentObservation，包含状态、摘要和 audit_id。
    """

    def __init__(
        self,
        pre_live_service: Any | None = None,
        skill_executor: SyncSkillExecutorAdapter | None = None,
        route_policy: RoutePolicy | None = None,
        *,
        policy_view: SkillPolicyView | None = None,
    ) -> None:
        """保留原有两参数构造方式，并允许测试或装配层注入同步 Runtime 适配器。

        默认适配器使用与 legacy 入口相同的播前服务实例创建四个 Handler，确保货盘、
        审计和幂等存储保持一致；注入能力只用于隔离测试和上层显式装配。
        """
        # 治理入口只接受启动冻结的 SkillPolicyView；旧 Facade 不再参与装配或执行。
        self._policy_view = policy_view or get_default_skill_policy_view()
        catalog = tuple(get_default_skill_catalog())
        assert_policy_view_matches_catalog(catalog, self._policy_view)
        if pre_live_service is None:
            raise TypeError("pre_live_service is required")
        self._service = pre_live_service
        # RoutePolicy 是启动装配快照；执行期间不重新读取 Settings 或环境变量。
        # 默认全部 LEGACY，避免 Phase 11B 未显式灰度时自动进入新执行链。
        self._route_policy = route_policy or RoutePolicy.default()
        self._normalizer = CompatibilityArgumentNormalizer(pre_live_service)
        # Catalog 是唯一版本事实源。装配时复制成只读快照，确保同一 Executor 生命周期
        # 内的调用不会因外部配置或 Catalog 重装配而悄然改变 Skill 版本钉住结果。
        self._skill_versions = MappingProxyType(
            {
                skill_id: self._policy_view.get(skill_id).version
                for skill_id in self._policy_view.skill_ids()
            }
        )
        self._skill_executor = skill_executor or SyncSkillExecutorAdapter(
            SkillExecutor(
                handlers=build_pre_live_handlers(pre_live_service),
                policy_view=self._policy_view,
            )
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
            tool = self._policy_view.get(tool_name)
        except KeyError:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"skill {tool_name} not found in SkillPolicyView",
                audit_id=None,
            )

        # Step 2: 生命周期校验
        if not self._policy_view.is_available(tool_name, lifecycle_stage):
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"tool {tool_name} not available in {lifecycle} lifecycle",
                audit_id=None,
            )

        route = self._route_policy.route_for_skill(tool_name)
        if route == RouteConfig.SKILL_RUNTIME:
            # Runtime 路径负责 Schema、门禁、幂等和 Handler 校验。该分支返回后不会
            # fallback 到 legacy，避免同一个 Agent 决策在失败时产生第二次外部动作。
            return self._dispatch_via_runtime(
                tool_name=tool_name,
                arguments=arguments,
                room_id=room_id,
                trace_id=trace_id,
                lifecycle=lifecycle_stage,
            )

        # Step 3: LEGACY 路径继续沿用原安全门禁和旧服务派发。
        gate = evaluate_tool_gate(tool, confirmed=False)
        if not gate.allowed:
            if gate.requires_confirmation:
                return AgentObservation(
                    tool_name=tool_name,
                    status="pending",
                    summary=f"{tool_name} requires human approval (hard-gate)",
                    audit_id=None,
                )
            # BLOCK 没有审批恢复语义，必须在 legacy dispatch 和业务副作用前终止。
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"{tool_name} is blocked by security policy",
                audit_id=None,
            )

        # Step 4: 派发到 legacy 分支。
        return self._dispatch_legacy(tool_name, arguments, room_id, trace_id)

    def _dispatch_via_runtime(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        lifecycle: LifecycleStage,
    ) -> AgentObservation:
        """按 Runtime 契约执行一个 Skill，异常时显式失败且绝不 legacy fallback。

        四个 Phase 11A 核心工具需要兼容旧参数形状，因此先进入规范化器；其余批次
        一能力已经有显式 Runtime Schema，直接把业务参数交给 SkillCall。两类路径
        都只调用一次 SyncSkillExecutorAdapter。
        """
        try:
            call = self._runtime_call(tool_name, arguments, room_id, trace_id, lifecycle)
        except CompatibilityEnrichmentError:
            # 可信旧服务的调用异常、返回形状错误和模型校验失败都属于补全链路失败。
            # 固定摘要不得包含服务返回数据，也不得回退 legacy 或调用 Runtime。
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary="HANDLER_FAILED: skill runtime execution failed",
                audit_id=None,
            )
        except (ValidationError, ValueError, TypeError):
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary="INVALID_ARGUMENTS: invalid compatibility arguments",
                audit_id=None,
            )
        except Exception:
            # 规范化阶段可能调用旧服务补全货盘或计划；这类非输入异常属于服务失败，
            # 必须保持 HANDLER_FAILED，且禁止退回 legacy 再执行一次业务逻辑。
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary="HANDLER_FAILED: skill runtime execution failed",
                audit_id=None,
            )

        try:
            result = self._skill_executor.execute(call)
            return observation_from_skill_result(tool_name, result)
        except Exception:
            # Runtime 调用及结果映射中的异常都属于执行失败。固定摘要既避免泄露
            # Handler、Pydantic 输出或业务参数，也保证一次 Agent 决策只执行一次。
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary="HANDLER_FAILED: skill runtime execution failed",
                audit_id=None,
            )

    def _runtime_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        lifecycle: LifecycleStage,
    ) -> SkillCall:
        """构造 Runtime SkillCall，并隔离四个旧核心工具的兼容规范化边界。"""
        version = self._skill_versions.get(tool_name)
        if version is None:
            # execute() 已完成 SkillPolicyView 校验；此分支防御 Catalog 装配漂移。
            # 不猜测版本或回退 legacy，避免一个未治理调用绕开精确版本约束。
            raise ValueError("runtime skill version is not registered")
        if tool_name in CORE_SKILL_IDS:
            return self._normalizer.normalize(
                tool_name=tool_name,
                arguments=arguments,
                room_id=room_id,
                trace_id=trace_id,
                lifecycle=lifecycle,
                version=version,
            )
        runtime_arguments = dict(arguments)
        idempotency_key = (
            runtime_arguments.get("idempotency_key")
            if isinstance(runtime_arguments.get("idempotency_key"), str)
            else None
        )
        if self._policy_view.get(tool_name).requires_idempotency_key:
            # 幂等键属于执行控制证据而非业务字段。所有显式 Runtime Skill 都必须
            # 通过冻结 Context 传递它，避免 Catalog Schema 因兼容参数而被放宽。
            runtime_arguments.pop("idempotency_key", None)
        return SkillCall(
            skill_id=tool_name,
            version=version,
            context=SkillExecutionContext(
                room_id=room_id,
                trace_id=trace_id,
                lifecycle=lifecycle,
                execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                # 由 Manifest 声明要求幂等键的 Skill 已从业务 arguments 搬入可信 Context；
                # 没有该声明的 Skill 保持 None，不能由调用方任意伪造控制字段。
                idempotency_key=idempotency_key,
            ),
            arguments=runtime_arguments,
        )

    def _legacy_product_from_arguments(
        self,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> Any:
        """为 LEGACY 单商品手卡路径解析商品对象。

        该 helper 只维护旧 AgentToolExecutor 的兼容外观；Runtime 路径仍由
        CompatibilityArgumentNormalizer 生成冻结快照并接受严格 Schema 校验。
        """
        from src.skills.product_catalog import CatalogProduct

        if "product" in arguments:
            return CatalogProduct.model_validate(arguments["product"])
        products = arguments.get("products")
        if products is None:
            products = self._service.query_products(room_id, trace_id)
        catalog_products = [CatalogProduct.model_validate(product) for product in products]
        product_id = arguments.get("product_id")
        if product_id is None and catalog_products:
            return catalog_products[0]
        for product in catalog_products:
            if product.product_id == product_id:
                return product
        raise ValueError("legacy product not found")

    def _legacy_plan_from_arguments(
        self,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> Any:
        """为 LEGACY setup 路径恢复旧计划对象。"""
        from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
        from src.skills.product_catalog import CatalogProduct

        if "plan" in arguments:
            return LivePlanDraft.model_validate(arguments["plan"])
        products = arguments.get("products")
        if products is None:
            products = self._service.query_products(room_id, trace_id)
        catalog_products = [CatalogProduct.model_validate(product) for product in products]
        generated_plan = self._service.generate_plan(room_id, catalog_products, trace_id)
        plan_item_ids = arguments.get("plan_item_ids")
        if not plan_item_ids:
            return generated_plan
        generated_items = {item.product_id: item for item in generated_plan.items}
        selected_items = [
            LivePlanItem(
                rank=index,
                product_id=generated_items[product_id].product_id,
                product_name=generated_items[product_id].product_name,
                role=generated_items[product_id].role,
                reason=generated_items[product_id].reason,
            )
            for index, product_id in enumerate(plan_item_ids, start=1)
            if product_id in generated_items
        ]
        if len(selected_items) != len(plan_item_ids):
            raise ValueError("legacy plan item not found")
        return LivePlanDraft(room_id=room_id, trace_id=trace_id, items=selected_items)

    def _dispatch_legacy(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> AgentObservation:
        """派发 LEGACY 路径工具；Runtime 路由失败不会进入本方法。"""
        # Step 4a: jsonschema 是 Phase 11B 声明依赖，legacy 路径也必须强制校验。
        try:
            tool = self._policy_view.get(tool_name)
            if tool.parameter_schema and tool_name not in CORE_SKILL_IDS:
                jsonschema.validate(instance=arguments, schema=tool.parameter_schema)
        except KeyError:
            pass  # already checked in execute()
        except jsonschema.ValidationError as exc:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                summary=f"参数校验失败: {exc.message}",
                audit_id=None,
            )
        try:
            # === PRE_LIVE 核心 legacy 工具 ===
            if tool_name == "query_products":
                products = self._service.query_products(room_id, trace_id)
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"queried products: {len(products)}",
                    audit_id=None,
                )

            elif tool_name == "generate_live_plan":
                products = arguments.get("products")
                if products is None:
                    products = self._service.query_products(room_id, trace_id)
                plan = self._service.generate_plan(room_id, products, trace_id)
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"generated live plan: {len(plan.items)} items",
                    audit_id=None,
                )

            elif tool_name == "generate_product_card":
                product = self._legacy_product_from_arguments(arguments, room_id, trace_id)
                if hasattr(self._service, "generate_card"):
                    self._service.generate_card(room_id, product, trace_id)
                else:
                    from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem

                    plan = LivePlanDraft(
                        room_id=room_id,
                        trace_id=trace_id,
                        items=[
                            LivePlanItem(
                                rank=1,
                                product_id=product.product_id,
                                product_name=product.name,
                                role="引流款",
                                reason="legacy single card",
                            )
                        ],
                    )
                    self._service.generate_cards(room_id, plan, [product], trace_id)
                return AgentObservation(
                    tool_name=tool_name,
                    status="success",
                    summary=f"generated product card: {product.product_id}",
                    audit_id=None,
                )

            elif tool_name == "setup_live_session":
                plan = self._legacy_plan_from_arguments(arguments, room_id, trace_id)
                gate, audit_id = self._service.setup_live_session(
                    room_id=room_id,
                    plan=plan,
                    trace_id=trace_id,
                    confirmed_setup=bool(arguments.get("confirmed_setup", False)),
                    idempotency_key=arguments.get("idempotency_key"),
                )
                return AgentObservation(
                    tool_name=tool_name,
                    status="success" if gate.allowed else "pending",
                    summary=gate.reason,
                    audit_id=audit_id,
                )

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
        except Exception:
            return AgentObservation(
                tool_name=tool_name,
                status="error",
                # Legacy 入口仍可能触碰数据库或平台适配器，不能把供应商异常、
                # SQL 片段或内部路径直接回显给 Agent；详细异常只应进入内部日志。
                summary="HANDLER_FAILED: legacy execution failed",
                audit_id=None,
            )
