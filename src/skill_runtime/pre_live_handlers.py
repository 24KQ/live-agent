"""Phase 11A 播前核心 Handler。

四个 Handler 注册到全局 Handler 注册表，使用显式快照输入
并委托现有 PreLiveBusinessFlowService 执行。

每个 Handler 是适配层，不包含独立业务逻辑。
"""

from __future__ import annotations

from typing import Any

from src.skill_runtime.executor import register_handler
from src.skill_runtime.executor import _SkillHandler
from src.skill_runtime.models import SkillExecutionContext

from src.config.settings import Settings
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skills.product_catalog import ProductCatalogRepository
from src.audit.tool_call_audit import ToolCallAuditStore


# ── 内部共享服务 ──────────────────────────────────────────────────────


_service: PreLiveBusinessFlowService | None = None


def _get_service() -> PreLiveBusinessFlowService:
    """获取或创建共享播前业务服务实例。"""
    global _service
    if _service is None:
        settings = Settings()  # type: ignore[call-arg]
        catalog_repo = ProductCatalogRepository(settings)
        audit_store = ToolCallAuditStore(settings)
        _service = PreLiveBusinessFlowService(catalog_repo, audit_store)
    return _service


# ── Handler 实现 ──────────────────────────────────────────────────────


class _QueryProductsHandler(_SkillHandler):
    """查询播前模拟商品货盘。"""

    def execute(self, skill_id: str, arguments: dict[str, Any], context: SkillExecutionContext) -> dict[str, Any]:
        service = _get_service()
        room_id = arguments.get("room_id", context.room_id)
        products = service.query_products(room_id=room_id, trace_id=context.trace_id)
        return {"products": [p.model_dump(mode="json") for p in products]}


class _GenerateLivePlanHandler(_SkillHandler):
    """生成确定性播前排品计划。"""

    def execute(self, skill_id: str, arguments: dict[str, Any], context: SkillExecutionContext) -> dict[str, Any]:
        service = _get_service()
        room_id = arguments.get("room_id", context.room_id)
        # arguments 中的 products 是由 Schema 校验过的不可变快照
        products_raw = arguments.get("products", [])
        from src.skills.product_catalog import CatalogProduct
        products = [CatalogProduct(**p) for p in products_raw]
        plan = service.generate_plan(room_id=room_id, products=products, trace_id=context.trace_id)
        return {"plan": plan.model_dump(mode="json")}


class _GenerateProductCardHandler(_SkillHandler):
    """为单商品生成确定性直播手卡。"""

    def execute(self, skill_id: str, arguments: dict[str, Any], context: SkillExecutionContext) -> dict[str, Any]:
        service = _get_service()
        room_id = arguments.get("room_id", context.room_id)
        product_raw = arguments.get("product", {})
        from src.skills.product_catalog import CatalogProduct
        product = CatalogProduct(**product_raw)
        card = service.generate_card(room_id=room_id, product=product, trace_id=context.trace_id)
        return {"card": card.model_dump(mode="json")}


class _SetupLiveSessionHandler(_SkillHandler):
    """根据播前排品模拟建播写操作。"""

    def execute(self, skill_id: str, arguments: dict[str, Any], context: SkillExecutionContext) -> dict[str, Any]:
        service = _get_service()
        room_id = arguments.get("room_id", context.room_id)
        plan_raw = arguments.get("plan", {})
        idempotency_key = context.idempotency_key or arguments.get("idempotency_key", "")

        # 先构建 LivePlanDraft 用于 setup
        plan_items_raw = plan_raw.get("items", [])
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

        plan = LivePlanDraft(
            plan_id=plan_raw.get("plan_id", ""),
            items=[PlanItem(**item) for item in plan_items_raw],
            room_id=room_id,
        )

        gate, audit_id = service.setup_live_session(
            room_id=room_id,
            plan=plan,
            trace_id=context.trace_id,
            confirmed_setup=True,
            idempotency_key=idempotency_key,
        )
        return {"allowed": gate.allowed, "setup_status": "prepared", "audit_id": audit_id}


# ── 启动时注册 ──────────────────────────────────────────────────────


register_handler("query_products", _QueryProductsHandler())
register_handler("generate_live_plan", _GenerateLivePlanHandler())
register_handler("generate_product_card", _GenerateProductCardHandler())
register_handler("setup_live_session", _SetupLiveSessionHandler())
