"""Phase 11A 播前 Graph 兼容 Facade。

Facade 对外保持 PreLiveBusinessServiceProtocol 的领域模型接口，对内才把
对象转换成 Skill Runtime 的 JSON 快照。路由在装配时冻结，Runtime 失败会显式
抛错，不会隐式回退到 legacy 路径。
"""

from __future__ import annotations

from typing import Any

from src.audit.tool_call_audit import ToolCallAuditStore
from src.config.settings import Settings
from src.core.human_approval import HumanApprovalRequest, HumanApprovalResponse
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.security_hooks import GateDecision, GateResult
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.models import (
    ApprovalContext,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionRoute,
    SkillExecutionStatus,
)
from src.skill_runtime.routing import RouteConfig, RoutePolicy
from src.skills.live_plan_generator import LivePlanDraft
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct, ProductCatalogRepository


class SkillRuntimeCallError(RuntimeError):
    """Skill Runtime 返回非成功结果时的受控异常。"""

    def __init__(self, result: SkillExecutionResult) -> None:
        self.skill_id = result.skill_id
        self.status = result.status
        self.error_code = result.error_code
        super().__init__(
            f"{result.skill_id} 执行失败: "
            f"{result.error_code.value if result.error_code else result.status.value}; "
            f"{result.summary}"
        )


def _require_output(result: SkillExecutionResult, key: str) -> Any:
    """读取成功结果中的必需字段，缺失时按 Runtime 契约错误处理。"""
    if result.status != SkillExecutionStatus.SUCCESS:
        raise SkillRuntimeCallError(result)
    if result.output is None or key not in result.output:
        raise ValueError(f"{result.skill_id} 成功结果缺少字段: {key}")
    return result.output[key]


class RoutedPreLiveBusinessService:
    """按冻结批次路由实现现有播前业务服务协议。"""

    def __init__(
        self,
        policy: RoutePolicy,
        legacy_service: PreLiveBusinessFlowService,
        skill_executor: SyncSkillExecutorAdapter,
    ) -> None:
        self.policy = policy
        self._legacy = legacy_service
        self._executor = skill_executor

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RoutedPreLiveBusinessService":
        """从启动配置装配共享业务服务、Handler 和不可变路由。"""
        resolved_settings = settings or Settings()  # type: ignore[call-arg]
        policy = RoutePolicy.from_settings(resolved_settings)
        catalog_repository = ProductCatalogRepository(resolved_settings)
        audit_store = ToolCallAuditStore(resolved_settings)
        legacy_service = PreLiveBusinessFlowService(catalog_repository, audit_store)

        # 使用实例局部 Handler 映射，避免并发装配 Facade 时覆盖其他实例。
        from src.skill_runtime.pre_live_handlers import build_pre_live_handlers

        skill_executor = SyncSkillExecutorAdapter(
            SkillExecutor(handlers=build_pre_live_handlers(legacy_service))
        )
        return cls(policy, legacy_service, skill_executor)

    @staticmethod
    def _context(
        room_id: str,
        trace_id: str,
        *,
        idempotency_key: str | None = None,
        approval: ApprovalContext | None = None,
    ) -> SkillExecutionContext:
        """构造不暴露给业务 arguments 的可信执行上下文。"""
        return SkillExecutionContext(
            room_id=room_id,
            trace_id=trace_id,
            lifecycle="PRE_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=idempotency_key,
            approval=approval,
        )

    def query_products(self, room_id: str, trace_id: str) -> list[CatalogProduct]:
        """查询货盘，Runtime 快照在 Facade 边界恢复成领域对象。"""
        route = self.policy.generation
        if route == RouteConfig.LEGACY:
            return self._legacy.query_products(room_id, trace_id)

        result = self._executor.execute(
            SkillCall(
                skill_id="query_products",
                version="1.0.0",
                context=self._context(room_id, trace_id),
                arguments={},
            )
        )
        return [
            CatalogProduct.model_validate(snapshot)
            for snapshot in _require_output(result, "products")
        ]

    def generate_plan(
        self,
        room_id: str,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> LivePlanDraft:
        """使用调用方给出的冻结商品列表生成计划，不重新查询货盘。"""
        route = self.policy.generation
        if route == RouteConfig.LEGACY:
            return self._legacy.generate_plan(room_id, products, trace_id)

        result = self._executor.execute(
            SkillCall(
                skill_id="generate_live_plan",
                version="1.0.0",
                context=self._context(room_id, trace_id),
                arguments={
                    "products": [
                        product.model_dump(mode="json") for product in products
                    ]
                },
            )
        )
        return LivePlanDraft.model_validate(_require_output(result, "plan"))

    def generate_cards(
        self,
        room_id: str,
        plan: LivePlanDraft,
        products: list[CatalogProduct],
        trace_id: str,
    ) -> list[ProductCard]:
        """按计划前三项逐个调用原子手卡 Skill。"""
        route = self.policy.generation
        if route == RouteConfig.LEGACY:
            return self._legacy.generate_cards(room_id, plan, products, trace_id)

        product_map = {product.product_id: product for product in products}
        cards: list[ProductCard] = []
        for item in plan.items[:3]:
            product = product_map.get(item.product_id)
            if product is None:
                raise ValueError(f"计划商品缺少对应快照: {item.product_id}")
            result = self._executor.execute(
                SkillCall(
                    skill_id="generate_product_card",
                    version="1.0.0",
                    context=self._context(room_id, trace_id),
                    arguments={"product": product.model_dump(mode="json")},
                )
            )
            cards.append(ProductCard.model_validate(_require_output(result, "card")))
        return cards

    def setup_live_session(
        self,
        room_id: str,
        plan: LivePlanDraft,
        trace_id: str,
        confirmed_setup: bool,
        *,
        idempotency_key: str | None = None,
        approval_context: ApprovalContext | None = None,
    ) -> tuple[GateResult, str | None]:
        """执行建播路由；Runtime 只消费显式传入的真实人工审批证据。"""
        route = self.policy.setup
        if route == RouteConfig.LEGACY:
            return self._legacy.setup_live_session(
                room_id=room_id,
                plan=plan,
                trace_id=trace_id,
                confirmed_setup=confirmed_setup,
                idempotency_key=idempotency_key,
            )

        effective_idempotency_key = idempotency_key or f"{trace_id}:setup_live_session"
        result = self._executor.execute(
            SkillCall(
                skill_id="setup_live_session",
                version="1.0.0",
                context=self._context(
                    room_id,
                    trace_id,
                    idempotency_key=effective_idempotency_key,
                    # confirmed_setup 只为 Legacy Protocol 保留，绝不能在 Runtime
                    # 路径升级成权限；缺少 HUMAN_INTERRUPT 时 Executor 返回 pending。
                    approval=approval_context,
                ),
                arguments={"plan": plan.model_dump(mode="json")},
            )
        )
        if result.status == SkillExecutionStatus.PENDING:
            return (
                GateResult(
                    allowed=False,
                    decision=GateDecision.HARD_GATE,
                    requires_confirmation=True,
                    reason=result.summary,
                ),
                None,
            )
        allowed = bool(_require_output(result, "allowed"))
        return (
            GateResult(
                allowed=allowed,
                decision=GateDecision.HARD_GATE,
                requires_confirmation=not allowed,
                reason=result.summary,
            ),
            result.audit_id,
        )

    def record_setup_approval_event(
        self,
        request: HumanApprovalRequest,
        response: HumanApprovalResponse | None,
    ) -> str:
        """审批审计继续委托原业务服务，保持 checkpoint 重放幂等语义。"""
        return self._legacy.record_setup_approval_event(request, response)
