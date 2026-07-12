"""Phase 11A 播前核心 Handler。

四个 Handler 注册到全局 Handler 注册表，使用显式快照输入
并委托现有 PreLiveBusinessFlowService 执行。

每个 Handler 是适配层，不包含独立业务逻辑。
"""

from __future__ import annotations

from typing import Any

from src.skill_runtime.executor import register_handler
from src.skill_runtime.executor import _SkillHandler, _SkillHandlerResult
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

    def __init__(self, service: PreLiveBusinessFlowService | None = None) -> None:
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        """在 Runtime 的原生 async 调用链中查询可信商品快照。"""
        service = self._service or _get_service()
        products = service.query_products(room_id=context.room_id, trace_id=context.trace_id)
        return {"products": [p.model_dump(mode="json") for p in products]}


class _GenerateLivePlanHandler(_SkillHandler):
    """生成确定性播前排品计划。"""

    def __init__(self, service: PreLiveBusinessFlowService | None = None) -> None:
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        """在 Runtime 的原生 async 调用链中生成确定性排品计划。"""
        service = self._service or _get_service()
        # arguments 中的 products 是由 Schema 校验过的不可变快照
        products_raw = arguments.get("products", [])
        from src.skills.product_catalog import CatalogProduct
        products = [CatalogProduct(**p) for p in products_raw]
        plan = service.generate_plan(
            room_id=context.room_id,
            products=products,
            trace_id=context.trace_id,
        )
        return {"plan": plan.model_dump(mode="json")}


class _GenerateProductCardHandler(_SkillHandler):
    """为单商品生成确定性直播手卡。"""

    def __init__(self, service: PreLiveBusinessFlowService | None = None) -> None:
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        """在 Runtime 的原生 async 调用链中生成单商品手卡。"""
        service = self._service or _get_service()
        product_raw = arguments.get("product", {})
        from src.skills.product_catalog import CatalogProduct
        product = CatalogProduct(**product_raw)
        card = service.generate_card(
            room_id=context.room_id,
            product=product,
            trace_id=context.trace_id,
        )
        return {"card": card.model_dump(mode="json")}


class _SetupLiveSessionHandler(_SkillHandler):
    """根据播前排品模拟建播写操作。"""

    def __init__(self, service: PreLiveBusinessFlowService | None = None) -> None:
        self._service = service

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> _SkillHandlerResult:
        """在审批已由 Executor 验证后执行一次兼容建播业务服务调用。"""
        service = self._service or _get_service()
        plan_raw = arguments.get("plan", {})
        from src.skills.live_plan_generator import LivePlanDraft

        # 计划必须由统一领域模型恢复，避免 Runtime 与 Graph 各维护一套计划结构。
        plan = LivePlanDraft.model_validate(plan_raw)

        gate, audit_id = service.setup_live_session(
            room_id=context.room_id,
            plan=plan,
            trace_id=context.trace_id,
            confirmed_setup=True,
            idempotency_key=context.idempotency_key,
        )
        return _SkillHandlerResult(
            output={"allowed": gate.allowed, "setup_status": "prepared"},
            audit_id=audit_id,
        )


# ── 启动与装配注册 ──────────────────────────────────────────────────


def build_pre_live_handlers(
    service: PreLiveBusinessFlowService | None = None,
) -> dict[str, _SkillHandler]:
    """为单个 Executor 创建局部 Handler 映射，不读写全局注册状态。"""
    return {
        "query_products": _QueryProductsHandler(service),
        "generate_live_plan": _GenerateLivePlanHandler(service),
        "generate_product_card": _GenerateProductCardHandler(service),
        "setup_live_session": _SetupLiveSessionHandler(service),
    }


def register_pre_live_handlers(service: PreLiveBusinessFlowService | None = None) -> None:
    """注册四个播前 Handler。

    默认导入路径使用惰性共享服务；Facade 装配时传入其 legacy service，
    让 Runtime 与 legacy 共享相同 Repository 和 AuditStore，便于灰度比较
    和测试隔离。重复注册会按 skill_id 覆盖旧实例。
    """
    for skill_id, handler in build_pre_live_handlers(service).items():
        register_handler(skill_id, handler)


register_pre_live_handlers()
