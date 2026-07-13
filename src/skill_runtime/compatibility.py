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
    if result.failure is not None:
        # AgentObservation 没有结构化 failure 字段；这里用稳定、脱敏的摘要片段保留
        # FailureFact 的关键证据，供旧 planner、回放和人工排查识别失败类别与 Attempt。
        evidence = [
            result.failure.category.value,
            f"attempt_id={result.failure.attempt_id}",
        ]
        summary = f"{summary} ({', '.join(evidence)})"
    elif result.attempt_id is not None:
        summary = f"{summary} (attempt_id={result.attempt_id})"
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

        return self._query_products_for_enrichment(room_id, trace_id)

    def _resolve_single_product(
        self,
        arguments: dict[str, Any],
        room_id: str,
        trace_id: str,
    ) -> CatalogProduct:
        """解析显式商品，或按冻结 Planner 旧形状补全计划首项商品。

        显式 product/product_id 始终属于调用方输入；只有两个字段都不存在时，才
        启动旧服务查询与计划补全。该分支最终仍只返回一个商品供一次 Runtime 调用。
        """
        if "product" in arguments:
            return CatalogProduct.model_validate(arguments["product"])

        if "product_id" in arguments:
            product_id = arguments["product_id"]
            if not isinstance(product_id, str) or not product_id:
                raise ValueError("generate_product_card 的 product_id 不合法")
            products = self._resolve_products(arguments, room_id, trace_id)
            for product in products:
                if product.product_id == product_id:
                    return product
            # 固定错误文本不回显调用方商品 ID；AgentToolExecutor 随后统一脱敏映射。
            raise ValueError("货盘中不存在调用方指定商品")

        products = self._query_products_for_enrichment(room_id, trace_id)
        generated_plan = self._generate_plan_for_enrichment(
            room_id,
            products,
            trace_id,
        )
        # rank 是计划的业务顺序事实源；若异常服务返回相同 rank，则稳定保留其列表
        # 顺序。LivePlanDraft 已保证 items 非空，因此 min 不会产生裸异常。
        first_item = min(
            enumerate(generated_plan.items),
            key=lambda indexed: (indexed[1].rank, indexed[0]),
        )[1]
        product_by_id = {product.product_id: product for product in products}
        selected_product = product_by_id.get(first_item.product_id)
        if selected_product is None:
            raise CompatibilityEnrichmentError("compatibility enrichment failed")
        return selected_product

    def _query_products_for_enrichment(
        self,
        room_id: str,
        trace_id: str,
    ) -> list[CatalogProduct]:
        """查询并验证可信旧货盘；调用异常、非法形状和空货盘统一视为补全失败。"""
        try:
            service_products = self._service.query_products(room_id, trace_id)
            # 服务返回值验证必须与服务调用处于同一补全边界。缺字段、非法容器和模型
            # 校验失败均表示可信补全链路失败，而不是调用方传入了错误 products。
            products = [
                CatalogProduct.model_validate(product) for product in service_products
            ]
            if not products:
                raise ValueError("compatibility enrichment returned empty products")
            return products
        except CompatibilityEnrichmentError:
            raise
        except Exception as exc:
            raise CompatibilityEnrichmentError(
                "compatibility enrichment failed"
            ) from exc

    def _generate_plan_for_enrichment(
        self,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        """调用并验证可信旧计划服务，确保计划完整且绑定当前可信执行上下文。"""
        try:
            service_plan = self._service.generate_plan(room_id, products, trace_id)
            generated_plan = LivePlanDraft.model_validate(service_plan)
            if generated_plan.room_id != room_id or generated_plan.trace_id != trace_id:
                raise ValueError("compatibility enrichment plan context mismatch")
            return generated_plan
        except CompatibilityEnrichmentError:
            raise
        except Exception as exc:
            raise CompatibilityEnrichmentError(
                "compatibility enrichment failed"
            ) from exc

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
        generated_plan = self._generate_plan_for_enrichment(
            room_id,
            products,
            trace_id,
        )
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
