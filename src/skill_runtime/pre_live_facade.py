"""Phase 11A 播前 Graph 兼容 Facade。

RoutedPreLiveBusinessService 实现现有 Service Protocol，
从 Settings 读取启动配置并形成不可变 RoutePolicy。

Facade 负责：
- 按批次路由选择 LEGACY 或 SKILL_RUNTIME
- 为 confirmed_setup 构造 TRUSTED_COMPAT ApprovalContext
- 不触发隐式 fallback（回滚由重启或重新装配实现）
"""

from __future__ import annotations

from typing import Any

from src.config.settings import Settings
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skills.product_catalog import ProductCatalogRepository
from src.audit.tool_call_audit import ToolCallAuditStore
from src.skill_runtime.executor import SyncSkillExecutorAdapter
from src.skill_runtime.models import (
    ApprovalContext,
    ApprovalSource,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
)
from src.skill_runtime.routing import RouteConfig, RoutePolicy


# ── 审批证据工厂 ──────────────────────────────────────────────────────


def create_compat_approval(confirmed: bool = True) -> ApprovalContext | None:
    """构造兼容适配用的 ApprovalContext。

    当 confirmed=True 时，返回标记为 TRUSTED_COMPAT 的批准证据；
    当 confirmed=False 时返回 None，Executor 会返回 pending。
    """
    if not confirmed:
        return None
    return ApprovalContext(
        source=ApprovalSource.TRUSTED_COMPAT,
        decision="APPROVED",
        operator_id="compat_migration",
        approval_audit_id="compat_setup_migration",
    )


# ── 播前跳过服务查询结果 ──────────────────────────────────────────────


class RoutedPreLiveBusinessService:
    """实现播前 Graph Service Protocol 的兼容 Facade。

    按批次选路，SKILL_RUNTIME 走统一 Executor，
    LEGACY 走原有 PreLiveBusinessFlowService。
    """

    def __init__(
        self,
        policy: RoutePolicy,
        legacy_service: PreLiveBusinessFlowService,
        skill_executor: SyncSkillExecutorAdapter,
        catalog_repository: ProductCatalogRepository,
    ) -> None:
        self.policy = policy
        self._legacy = legacy_service
        self._executor = skill_executor
        self._catalog = catalog_repository

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RoutedPreLiveBusinessService":
        """从 Settings 构造 Facade。"""
        if settings is None:
            settings = Settings()  # type: ignore[call-arg]
        policy = RoutePolicy(
            generation=RouteConfig(settings.skill_route_prelive_generation),
            setup=RouteConfig(settings.skill_route_prelive_setup),
        )
        catalog_repo = ProductCatalogRepository(settings)
        audit_store = ToolCallAuditStore(settings)
        legacy_service = PreLiveBusinessFlowService(catalog_repo, audit_store)
        skill_executor = SyncSkillExecutorAdapter()
        return cls(policy, legacy_service, skill_executor, catalog_repo)

    def query_products(self, room_id: str, trace_id: str) -> list[dict[str, Any]]:
        """查询播前货盘。"""
        if self.policy.generation == RouteConfig.SKILL_RUNTIME:
            ctx = SkillExecutionContext(
                room_id=room_id,
                trace_id=trace_id,
                lifecycle="PRE_LIVE",
                execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            )
            call = SkillCall(
                skill_id="query_products",
                version="1.0.0",
                context=ctx,
                arguments={"room_id": room_id},
            )
            result = self._executor.execute(call)
            if result.status.value == "success" and result.output:
                return result.output.get("products", [])
            return []
        return [p.model_dump(mode="json") for p in self._legacy.query_products(room_id, trace_id)]

    def generate_plan(
        self,
        room_id: str,
        products: list[dict[str, Any]],
        trace_id: str,
    ) -> dict[str, Any]:
        """生成排品计划。"""
        if self.policy.generation == RouteConfig.SKILL_RUNTIME:
            ctx = SkillExecutionContext(
                room_id=room_id,
                trace_id=trace_id,
                lifecycle="PRE_LIVE",
                execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            )
            call = SkillCall(
                skill_id="generate_live_plan",
                version="1.0.0",
                context=ctx,
                arguments={"room_id": room_id, "products": products},
            )
            result = self._executor.execute(call)
            if result.status.value == "success" and result.output:
                return result.output.get("plan", {})
            return {"items": []}
        return self._legacy.generate_plan(
            room_id,
            [self._catalog.get_product(p["product_id"]) if isinstance(p, dict) else self._catalog.get_product(p) for p in products],
            trace_id,
        ).model_dump(mode="json")

    def generate_cards(
        self,
        room_id: str,
        plan: dict[str, Any],
        products: list[dict[str, Any]],
        trace_id: str,
    ) -> list[dict[str, Any]]:
        """生成手卡，使用计划前三个商品。"""
        if self.policy.generation == RouteConfig.SKILL_RUNTIME:
            cards: list[dict[str, Any]] = []
            for item in (plan.get("items", []) or [])[:3]:
                product_id = item.get("product_id", "")
                product = next((p for p in products if p.get("product_id") == product_id), None)
                if product is None:
                    continue
                ctx = SkillExecutionContext(
                    room_id=room_id,
                    trace_id=trace_id,
                    lifecycle="PRE_LIVE",
                    execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                )
                call = SkillCall(
                    skill_id="generate_product_card",
                    version="1.0.0",
                    context=ctx,
                    arguments={"room_id": room_id, "product": product},
                )
                result = self._executor.execute(call)
                if result.status.value == "success" and result.output:
                    cards.append(result.output.get("card", {}))
            return cards
        return [
            c.model_dump(mode="json")
            for c in self._legacy.generate_cards(
                room_id,
                self._make_legacy_plan(plan, products),
                [self._make_legacy_product(p) for p in products],
                trace_id,
            )
        ]

    def setup_live_session(
        self,
        room_id: str,
        plan: dict[str, Any],
        trace_id: str,
        confirmed_setup: bool = False,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """模拟建播。RoutePolicy 决定是否使用 Runtime。"""
        approval = create_compat_approval(confirmed=confirmed_setup)
        if self.policy.setup == RouteConfig.SKILL_RUNTIME:
            ctx = SkillExecutionContext(
                room_id=room_id,
                trace_id=trace_id,
                lifecycle="PRE_LIVE",
                execution_route=SkillExecutionRoute.SKILL_RUNTIME,
                idempotency_key=idempotency_key,
                approval=approval,
            )
            call = SkillCall(
                skill_id="setup_live_session",
                version="1.0.0",
                context=ctx,
                arguments={"room_id": room_id, "plan": plan},
            )
            result = self._executor.execute(call)
            if result.status.value == "success" and result.output:
                return {"allowed": True, "setup_status": "prepared"}, result.output.get("audit_id")
            if result.status.value == "pending":
                return {"allowed": False, "setup_status": "pending"}, None
            return {"allowed": False, "setup_status": "error"}, None

        from src.core.pre_live_business_flow import PreLiveBusinessFlowResult
        legacy_plan = self._make_legacy_plan(plan, [])
        gate, audit_id = self._legacy.setup_live_session(
            room_id=room_id,
            plan=legacy_plan,
            trace_id=trace_id,
            confirmed_setup=confirmed_setup,
            idempotency_key=idempotency_key,
        )
        return {"allowed": gate.allowed, "setup_status": "prepared" if gate.allowed else "blocked"}, audit_id

    def _make_legacy_plan(self, plan_dict: dict, products: list[dict]) -> Any:
        """从字典构造 LivePlanDraft。"""
        from dataclasses import dataclass

        @dataclass
        class PlanItem:
            item_id: str
            product_id: str
            start_time: str = ""
            duration_seconds: int = 60

        @dataclass
        class LivePlanDraft:
            plan_id: str
            items: list[PlanItem]
            room_id: str = ""

        return LivePlanDraft(
            plan_id=plan_dict.get("plan_id", ""),
            items=[PlanItem(**item) for item in (plan_dict.get("items", []) or [])],
            room_id=plan_dict.get("room_id", ""),
        )

    def _make_legacy_product(self, p: dict) -> Any:
        """从字典构造 CatalogProduct。"""
        from src.skills.product_catalog import CatalogProduct
        return CatalogProduct(**p)
