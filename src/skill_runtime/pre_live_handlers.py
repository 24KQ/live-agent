"""Phase 11B 播前 Handler 兼容装配。

旧导入路径仍由 Graph、Facade 和测试使用，但这里不再维护第二套播前 Handler
业务逻辑。实际 Handler 均来自统一 `build_skill_handlers()` 工厂；本文件只负责
把既有 PreLiveBusinessFlowService 包装成只读 ProductPricingPort，并注册四个
播前入口，保持 Phase 11A 外观不变。
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
    """把既有播前服务限制为只读商品 Port。

    统一 Handler 查询货盘时只需要 ProductPricingPort.list_products。这里显式拒绝
    set_price，避免兼容装配被误用为高风险改价路径；真正改价会在批次三通过平台
    Port 和审批门禁实现。
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


def build_pre_live_handlers(
    service: PreLiveBusinessFlowService | None = None,
) -> dict[str, _SkillHandler]:
    """为单个 Executor 创建播前四个 Handler 的局部兼容映射。"""
    resolved_service = service or _get_service()
    handlers = build_skill_handlers(
        SkillRuntimeDependencies(
            platform=_PreLiveServiceProductPort(resolved_service),
            legacy_pre_live_service=resolved_service,
        )
    )
    return {
        "query_products": handlers["query_products"],
        "generate_live_plan": handlers["generate_live_plan"],
        "generate_product_card": handlers["generate_product_card"],
        "setup_live_session": handlers["setup_live_session"],
    }


def register_pre_live_handlers(service: PreLiveBusinessFlowService | None = None) -> None:
    """注册四个播前 Handler，保持既有导入副作用兼容。"""
    for skill_id, handler in build_pre_live_handlers(service).items():
        register_handler(skill_id, handler)


register_pre_live_handlers()
