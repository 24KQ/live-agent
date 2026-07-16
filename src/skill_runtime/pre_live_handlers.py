"""Phase 11B 播前 Handler 兼容装配。

旧导入路径仍由 Graph、Facade 和测试使用，但这里不再维护第二套播前 Handler
业务逻辑。实际 Handler 均来自统一 `build_skill_handlers()` 工厂；本文件只负责
把既有 PreLiveBusinessFlowService 包装成兼容 Platform Port，并注册原有 13 个兼容入口。
Phase 13 的记忆读取必须由显式 MemoryStore 装配，不能在旧外观中注册一个无依赖 Handler。
Phase 11A 播前外观保持不变；Phase 11B 批次一路由打开时，
AgentToolExecutor 默认装配也能执行已迁移的批次一 Handler。
"""

from __future__ import annotations

from typing import Any

from src.audit.tool_call_audit import ToolCallAuditStore
from src.config.settings import Settings
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.skill_runtime.executor import _SkillHandler, register_handler
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import (
    AdapterRequest,
    AdapterSuccess,
    FailureCategory,
    FailureFact,
    SideEffectState,
)
from src.skill_runtime.platform_ports import AdapterResult
from src.skills.live_plan_generator import LivePlanDraft
from src.skills.product_catalog import ProductCatalogRepository


_service: PreLiveBusinessFlowService | None = None


def _get_service() -> PreLiveBusinessFlowService:
    """获取共享旧播前服务，仅用于兼容默认导入注册。"""
    global _service
    if _service is None:
        settings = Settings()  # type: ignore[call-arg]
        catalog_repo = ProductCatalogRepository(settings)
        audit_store = ToolCallAuditStore(settings)
        _service = PreLiveBusinessFlowService(catalog_repo, audit_store)
    return _service


class _PreLiveServiceProductPort:
    """把既有播前服务限制为兼容 Platform Port。

    统一 Handler 查询货盘和解析播中只读商品上下文时复用旧播前货盘；建播在
    Runtime 完成审批、Schema、幂等和 Attempt 门禁后，允许经本 Port 调用旧服务以
    保留 Phase 11A 审计链。改价和售罄仍显式拒绝，不能借兼容装配绕过专用平台 Port。
    """

    def __init__(self, service: PreLiveBusinessFlowService) -> None:
        self._service = service

    async def list_products(self, request: AdapterRequest) -> AdapterResult:
        """通过旧服务读取商品，并转成 AdapterSuccess 事实。"""
        products = self._service.query_products(
            room_id=request.room_id,
            trace_id=str(request.payload.get("__trace_id") or request.operation_id),
        )
        return AdapterSuccess(
            output={"products": [product.model_dump(mode="json") for product in products]},
            side_effect_state=SideEffectState.NOT_SENT,
        )

    async def set_price(self, request: AdapterRequest) -> AdapterResult:
        """播前兼容 Port 不允许执行价格写入。"""
        return FailureFact(
            category=FailureCategory.POLICY_DENIED,
            external_code="pre_live_compat.set_price_denied",
            side_effect_state=SideEffectState.NOT_SENT,
            attempt_id=request.attempt_id,
        )

    async def prepare_session(self, request: AdapterRequest) -> AdapterResult:
        """通过旧播前服务执行建播，保持 Phase 11A 审计链路。

        Runtime Handler 已经完成可信审批、Schema、幂等和 Attempt 意图校验。兼容
        Port 在这里只负责把计划快照恢复为领域对象，并调用旧服务的受控建播入口。
        """
        plan = LivePlanDraft.model_validate(request.payload.get("plan", {}))
        trace_id = str(request.payload.get("__trace_id") or request.operation_id)
        gate, audit_id = self._service.setup_live_session(
            room_id=request.room_id,
            plan=plan,
            trace_id=trace_id,
            confirmed_setup=True,
            idempotency_key=request.idempotency_key,
        )
        return AdapterSuccess(
            output={
                "session": {"session_id": audit_id or request.operation_id, "status": "prepared"},
                "allowed": gate.allowed,
                "audit_id": audit_id,
            },
            side_effect_state=SideEffectState.CONFIRMED,
        )

    async def mark_sold_out(self, request: AdapterRequest) -> AdapterResult:
        """兼容 Port 不执行售罄状态写入，避免 Task 7 前产生隐式副作用。"""
        return FailureFact(
            category=FailureCategory.POLICY_DENIED,
            external_code="pre_live_compat.mark_sold_out_denied",
            side_effect_state=SideEffectState.NOT_SENT,
            attempt_id=request.attempt_id,
        )

    async def resolve_product_context(self, request: AdapterRequest) -> AdapterResult:
        """从旧货盘只读解析售罄商品和可选备选商品快照。

        该方法只服务批次一的备选推荐和主播提示生成，不修改商品库存或直播状态。
        如果商品缺失，返回结构化失败事实而不是伪造上下文。
        """
        products = self._service.query_products(
            room_id=request.room_id,
            trace_id=str(request.payload.get("__trace_id") or request.operation_id),
        )
        product_by_id = {product.product_id: product for product in products}
        sold_out_product_id = str(request.payload.get("sold_out_product_id") or "")
        sold_out = product_by_id.get(sold_out_product_id)
        if sold_out is None:
            return FailureFact(
                category=FailureCategory.INVALID_INPUT,
                external_code="pre_live_compat.product_not_found",
                side_effect_state=SideEffectState.NOT_SENT,
                attempt_id=request.attempt_id,
            )
        backup_id = request.payload.get("backup_product_id")
        backup = product_by_id.get(str(backup_id)) if backup_id else next(
            (
                product
                for product in products
                if product.product_id != sold_out.product_id
                and product.is_active
                and product.inventory > 0
            ),
            None,
        )
        return AdapterSuccess(
            output={
                "sold_out_product": sold_out.model_dump(mode="json"),
                "backup_product": None if backup is None else backup.model_dump(mode="json"),
            },
            side_effect_state=SideEffectState.NOT_SENT,
        )

    async def current_context(self, request: AdapterRequest) -> AdapterResult:
        """返回旧入口显式传入的上下文摘要，不访问外部平台状态。"""
        return AdapterSuccess(
            output={
                "inventory_alerts": list(request.payload.get("inventory_alerts") or []),
                "danmaku_summary": list(request.payload.get("danmaku_summary") or []),
            },
            side_effect_state=SideEffectState.NOT_SENT,
        )


def build_pre_live_handlers(
    service: PreLiveBusinessFlowService | None = None,
) -> dict[str, _SkillHandler]:
    """为单个 Executor 创建 13 个 Skill 的局部兼容映射。"""
    resolved_service = service or _get_service()
    handlers = build_skill_handlers(
        SkillRuntimeDependencies(
            platform=_PreLiveServiceProductPort(resolved_service),
            legacy_pre_live_service=resolved_service,
        )
    )
    # 兼容 Facade 只拥有旧播前服务，不能伪造 MemoryStore 依赖。若未来调用方需要
    # retrieve_anchor_memory，必须直接使用统一工厂并显式传入受控的 memory_port。
    handlers.pop("retrieve_anchor_memory")
    return handlers


def register_pre_live_handlers(service: PreLiveBusinessFlowService | None = None) -> None:
    """注册 13 个旧兼容 Handler，保持既有导入副作用兼容。"""
    for skill_id, handler in build_pre_live_handlers(service).items():
        register_handler(skill_id, handler)


register_pre_live_handlers()
