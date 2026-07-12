"""AgentToolExecutor 到 Skill Runtime 的旧参数兼容层。

本模块只服务于旧 AgentToolExecutor 同步入口。它负责把历史上的 room_id、
trace_id、product_id 和 plan_item_ids 等隐式参数转换为 SkillCall 所要求的
显式领域快照；新的 Runtime Facade 和未来 PlanEngine 不应复用本兼容层。

信任边界：调用方 arguments 只被视为业务输入。尤其是 confirmed_setup 不能
转换成审批证据，setup_live_session 必须由 Runtime 在缺少可信 ApprovalContext
时返回 pending。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.core.agent_decision import AgentObservation
from src.skill_runtime.models import (
    SkillCall,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionRoute,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct
from src.state.models import LifecycleStage


CORE_SKILL_IDS: frozenset[str] = frozenset(
    {
        "query_products",
        "generate_live_plan",
        "generate_product_card",
        "setup_live_session",
    }
)

# 兼容入口只接受已经由历史调用点证实存在的旧字段。每个工具独立列出白名单，
# 避免新增某个工具的兼容参数时意外放宽其他工具；该检查必须早于货盘查询和计划生成。
CORE_COMPATIBILITY_ARGUMENT_KEYS: dict[str, frozenset[str]] = {
    "query_products": frozenset({"room_id", "trace_id"}),
    "generate_live_plan": frozenset({"room_id", "trace_id", "products"}),
    "generate_product_card": frozenset(
        {"room_id", "trace_id", "product", "product_id", "products"}
    ),
    "setup_live_session": frozenset(
        {
            "room_id",
            "trace_id",
            "plan",
            "plan_item_ids",
            "products",
            "idempotency_key",
            "confirmed_setup",
        }
    ),
}


class CompatibilityEnrichmentError(RuntimeError):
    """标记可信旧服务调用或返回数据校验失败，避免误判为调用方输入错误。

    该类型只在兼容层内部跨越到 AgentToolExecutor；异常文本固定且不包含原始
    服务数据。原异常仅通过异常链保留给受控调试环境，不进入 AgentObservation。
    """


def observation_from_skill_result(
    tool_name: str,
    result: SkillExecutionResult,
) -> AgentObservation:
    """把 Runtime 结果无损压缩到旧 AgentObservation 契约。

    AgentObservation 当前没有独立 error_code 字段，因此受控错误码作为摘要的
    稳定前缀保留。这样既不扩大旧模型和 checkpoint 的变更范围，也不会让 planner
    只能依赖易变的自然语言错误信息。
    """
    summary = result.summary
    if result.error_code is not None:
        summary = f"{result.error_code.value}: {summary}"
    return AgentObservation(
        tool_name=tool_name,
        status=result.status.value,
        summary=summary,
        audit_id=result.audit_id,
    )


class CompatibilityArgumentNormalizer:
    """把四个核心工具的历史参数规范化为冻结 SkillCall。

    规范化器可以调用旧播前服务读取货盘或生成确定性计划，但这些调用只用于补全
    调用快照，不会执行目标 Skill。最终目标 Skill 始终由 AgentToolExecutor 中的
    SyncSkillExecutorAdapter 调用且只调用一次。
    """

    def __init__(self, pre_live_service: Any) -> None:
        self._service = pre_live_service

    def normalize(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
        lifecycle: str | LifecycleStage,
    ) -> SkillCall:
        """根据核心工具名称构造版本固定、上下文显式且参数冻结的调用。

        参数容器及字段白名单必须在任何兼容补全前完成校验。异常信息只描述错误
        类别，不拼接字段名或字段值，防止旧入口把不可信输入带入日志或观察摘要。
        """
        if tool_name not in CORE_SKILL_IDS:
            raise ValueError(f"非核心工具不允许进入兼容规范化器: {tool_name}")
        if not isinstance(arguments, Mapping):
            raise TypeError("兼容参数必须是映射")

        allowed_keys = CORE_COMPATIBILITY_ARGUMENT_KEYS[tool_name]
        if any(key not in allowed_keys for key in arguments):
            raise ValueError("兼容参数包含未知字段")

        normalized_arguments = self._normalize_business_arguments(
            tool_name=tool_name,
            arguments=dict(arguments),
            room_id=room_id,
            trace_id=trace_id,
        )
        context = SkillExecutionContext(
            room_id=room_id,
            trace_id=trace_id,
            lifecycle=LifecycleStage(lifecycle),
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=(
                arguments.get("idempotency_key")
                if tool_name == "setup_live_session"
                else None
            ),
            # confirmed_setup 属于 LLM 可控业务参数，不能据此构造可信审批证据。
            approval=None,
            # 四个核心工具进入本兼容边界时都会发生旧参数搬移；商品和计划路径还会
            # 进行领域快照补全，因此统一记录为 True，满足 D-049 的审计要求。
            compatibility_enriched=True,
        )
        return SkillCall(
            skill_id=tool_name,
            version="1.0.0",
            context=context,
            arguments=normalized_arguments,
        )

    def _normalize_business_arguments(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """只返回 Manifest Schema 允许的业务字段，避免降低 Runtime 校验强度。"""
        if tool_name == "query_products":
            return {}
        if tool_name == "generate_live_plan":
            products = self._resolve_products(arguments, room_id, trace_id)
            return {"products": [self._product_snapshot(product) for product in products]}
        if tool_name == "generate_product_card":
            product = self._resolve_single_product(arguments, room_id, trace_id)
            return {"product": self._product_snapshot(product)}

        plan = self._resolve_plan(arguments, room_id, trace_id)
        return {"plan": plan.model_dump(mode="json")}

    def _resolve_products(
        self,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> list[CatalogProduct]:
        """分别验证调用方商品和服务补全商品，保留两种错误来源的稳定分类。"""
        raw_products = arguments.get("products")
        if raw_products is not None:
            # 调用方显式提供的快照仍属于业务输入；形状、字段或类型错误必须向上保留
            # Pydantic/ValueError/TypeError，由旧入口映射为 INVALID_ARGUMENTS。
            return [CatalogProduct.model_validate(product) for product in raw_products]

        try:
            service_products = self._service.query_products(room_id, trace_id)
            # 服务返回值验证必须与服务调用处于同一补全边界。缺字段、非法容器和模型
            # 校验失败均表示可信补全链路失败，而不是调用方传入了错误 products。
            return [
                CatalogProduct.model_validate(product) for product in service_products
            ]
        except Exception as exc:
            raise CompatibilityEnrichmentError(
                "compatibility enrichment failed"
            ) from exc

    def _resolve_single_product(
        self,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> CatalogProduct:
        """把完整 product 或旧 product_id 统一解析成单商品领域对象。"""
        raw_product = arguments.get("product")
        if raw_product is not None:
            return CatalogProduct.model_validate(raw_product)

        product_id = arguments.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            raise ValueError("generate_product_card 缺少有效 product 或 product_id")
        products = self._resolve_products(arguments, room_id, trace_id)
        for product in products:
            if product.product_id == product_id:
                return product
        raise ValueError(f"货盘中不存在商品: {product_id}")

    def _resolve_plan(
        self,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> LivePlanDraft:
        """验证完整计划，或用旧 plan_item_ids 从确定性计划中提取真实条目。"""
        raw_plan = arguments.get("plan")
        if raw_plan is not None:
            plan = LivePlanDraft.model_validate(raw_plan)
            # 完整计划虽然通过领域字段校验，仍属于调用方业务输入；其房间和追踪标识
            # 必须绑定可信函数参数。发现不一致时拒绝执行，且错误中不回显任何输入值。
            if plan.room_id != room_id or plan.trace_id != trace_id:
                raise ValueError("计划快照与可信执行上下文不一致")
            return plan

        plan_item_ids = arguments.get("plan_item_ids")
        if not isinstance(plan_item_ids, list) or not plan_item_ids:
            raise ValueError("setup_live_session 缺少有效 plan 或 plan_item_ids")
        if not all(isinstance(product_id, str) and product_id for product_id in plan_item_ids):
            raise ValueError("plan_item_ids 必须是非空商品 ID 列表")
        if len(set(plan_item_ids)) != len(plan_item_ids):
            raise ValueError("plan_item_ids 不允许包含重复商品 ID")

        products = self._resolve_products(arguments, room_id, trace_id)
        try:
            service_plan = self._service.generate_plan(room_id, products, trace_id)
            # 即使旧服务没有抛异常，也必须重新验证返回对象的完整领域形状；空条目、
            # 缺字段或错误类型都属于补全失败，不能随后被误判为 plan_item_ids 错误。
            generated_plan = LivePlanDraft.model_validate(service_plan)
        except Exception as exc:
            raise CompatibilityEnrichmentError(
                "compatibility enrichment failed"
            ) from exc
        generated_items = {item.product_id: item for item in generated_plan.items}
        missing_ids = [product_id for product_id in plan_item_ids if product_id not in generated_items]
        if missing_ids:
            raise ValueError(f"计划中不存在商品: {', '.join(missing_ids)}")

        # 旧 ID 列表表达了调用方顺序；重排时复用生成计划的名称、角色和理由，
        # 仅重新编号 rank，保证结果仍是可由 LivePlanDraft 验证的真实领域快照。
        selected_items = [
            LivePlanItem(
                rank=index,
                product_id=generated_items[product_id].product_id,
                product_name=generated_items[product_id].product_name,
                role=generated_items[product_id].role,
                reason=generated_items[product_id].reason,
            )
            for index, product_id in enumerate(plan_item_ids, start=1)
        ]
        return LivePlanDraft(room_id=room_id, trace_id=trace_id, items=selected_items)

    @staticmethod
    def _product_snapshot(product: CatalogProduct) -> dict[str, Any]:
        """输出 CatalogProduct 的全部 JSON 字段；SkillCall 随后递归冻结该字典。"""
        return product.model_dump(mode="json")
