"""Phase 11B 有状态 Fake 平台 Adapter。

Fake 的目标是提供可重放的平台事实，而非模拟 HTTP 外观。每个实例独立保存商品、
会话和调用计数，Fixture 中的故障脚本决定某次操作在发送前、发送后或状态检查时
返回何种 FailureFact，因此测试不会依赖随机网络错误或全局可变单例。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.skill_runtime.models import (
    AdapterRequest,
    AdapterSuccess,
    FailureCategory,
    FailureFact,
    SideEffectState,
)
from src.skill_runtime.platform_ports import (
    AdapterResult,
    LiveOperationsPort,
    LiveSessionPort,
    ProductPricingPort,
)


class FakeFaultKind(StrEnum):
    """声明式故障脚本可表达的受控平台行为。"""

    RATE_LIMITED = "RATE_LIMITED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    DEADLINE_BEFORE_SEND = "DEADLINE_BEFORE_SEND"
    UNKNOWN_AFTER_SEND = "UNKNOWN_AFTER_SEND"


class FakeFaultRule(BaseModel, frozen=True):
    """按操作、资源键和调用序号精确匹配的一次 Fake 故障。"""

    operation_name: str = Field(..., min_length=1)
    resource_key: str = Field(..., min_length=1)
    call_index: int = Field(..., ge=1)
    kind: FakeFaultKind
    retry_after_seconds: int | None = Field(default=None, ge=0)


class FakePlatformProduct(BaseModel, frozen=True):
    """Fake 平台内维护的最小商品状态，version 仅供 CAS 测试使用。"""

    product_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    price: Decimal = Field(..., ge=Decimal("0"))
    inventory: int = Field(..., ge=0)
    version: int = Field(..., ge=1)
    is_active: bool = True


class FakePlatformFixture(BaseModel, frozen=True):
    """创建单个 Fake 实例所需的不可变初始状态和故障脚本。"""

    room_id: str = Field(..., min_length=1)
    products: tuple[FakePlatformProduct, ...] = ()
    faults: tuple[FakeFaultRule, ...] = ()


class FakeLiveCommercePlatform(ProductPricingPort, LiveSessionPort, LiveOperationsPort):
    """同时实现三个 Port 的本地状态 Fake。

    该类不使用全局缓存。每个测试、Demo 或装配调用都必须通过 from_fixture 创建
    新实例，从而保证一个场景的售罄、价格版本或故障序列不会污染另一个场景。
    """

    def __init__(self, fixture: FakePlatformFixture) -> None:
        self._room_id = fixture.room_id
        self._products = {item.product_id: item for item in fixture.products}
        self._faults = tuple(fixture.faults)
        self._call_counts: dict[tuple[str, str], int] = {}
        self._sessions_by_idempotency: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_fixture(cls, fixture: FakePlatformFixture) -> "FakeLiveCommercePlatform":
        """用冻结 Fixture 创建独立状态，避免测试共享平台数据。"""
        return cls(fixture)

    def product(self, product_id: str) -> FakePlatformProduct:
        """读取当前 Fake 商品状态，供测试断言外部副作用是否发生。"""
        return self._products[product_id]

    async def list_products(self, request: AdapterRequest) -> AdapterResult:
        """返回当前可用商品快照，不产生业务副作用。"""
        failure = self._before_send_failure("list_products", request.room_id, request)
        if failure is not None:
            return failure
        return AdapterSuccess(
            output={
                "products": [
                    product.model_dump(mode="json")
                    for product in sorted(self._products.values(), key=lambda item: item.product_id)
                    if product.is_active and product.inventory > 0
                ]
            },
            side_effect_state=SideEffectState.NOT_SENT,
        )

    async def set_price(self, request: AdapterRequest) -> AdapterResult:
        """执行受资源版本保护的改价，并模拟发送后未知的危险边界。"""
        product_id = str(request.payload.get("product_id") or "")
        # 同一次 Adapter 调用只能消耗一次脚本序号。发送前和发送后必须复用同一条
        # 故障规则，否则 UNKNOWN_AFTER_SEND 会被前置检查错误跳过。
        fault = self._matching_fault("set_price", product_id)
        failure = self._before_send_failure("set_price", product_id, request, fault=fault)
        if failure is not None:
            return failure
        product = self._products.get(product_id)
        if product is None:
            return self._failure(
                request,
                FailureCategory.INVALID_INPUT,
                "fake.product_not_found",
                SideEffectState.NOT_SENT,
            )
        expected_version = request.payload.get("expected_version")
        if expected_version != product.version:
            return self._failure(
                request,
                FailureCategory.VERSION_CONFLICT,
                "fake.product_version_conflict",
                SideEffectState.NOT_SENT,
            )
        try:
            price = Decimal(str(request.payload["price"]))
        except (KeyError, ArithmeticError):
            return self._failure(
                request,
                FailureCategory.INVALID_INPUT,
                "fake.invalid_price",
                SideEffectState.NOT_SENT,
            )
        if price < 0:
            return self._failure(
                request,
                FailureCategory.INVALID_INPUT,
                "fake.invalid_price",
                SideEffectState.NOT_SENT,
            )

        updated = product.model_copy(update={"price": price, "version": product.version + 1})
        self._products[product_id] = updated
        if fault is not None and fault.kind == FakeFaultKind.UNKNOWN_AFTER_SEND:
            return self._failure(
                request,
                FailureCategory.SIDE_EFFECT_UNKNOWN,
                "fake.unknown_after_send",
                SideEffectState.UNKNOWN,
            )
        return AdapterSuccess(
            output={"product": updated.model_dump(mode="json")},
            side_effect_state=SideEffectState.CONFIRMED,
        )

    async def prepare_session(self, request: AdapterRequest) -> AdapterResult:
        """按幂等键创建或重放 Fake 建播会话。"""
        failure = self._before_send_failure("prepare_session", request.room_id, request)
        if failure is not None:
            return failure
        key = request.idempotency_key
        if key is None:
            return self._failure(
                request,
                FailureCategory.INVALID_INPUT,
                "fake.idempotency_required",
                SideEffectState.NOT_SENT,
            )
        session = self._sessions_by_idempotency.setdefault(
            key,
            {"session_id": f"session-{len(self._sessions_by_idempotency) + 1}", "status": "prepared"},
        )
        return AdapterSuccess(output={"session": session}, side_effect_state=SideEffectState.CONFIRMED)

    async def mark_sold_out(self, request: AdapterRequest) -> AdapterResult:
        """将商品库存置零并下架，返回可供后续备选/提示逻辑消费的状态事实。"""
        product_id = str(request.payload.get("product_id") or "")
        # 写操作与改价一样必须只消费一次故障规则。UNKNOWN_AFTER_SEND 表示平台
        # 可能已经完成写入，因此先执行状态变更，再将未知事实交给上层对账。
        fault = self._matching_fault("mark_sold_out", product_id)
        failure = self._before_send_failure("mark_sold_out", product_id, request, fault=fault)
        if failure is not None:
            return failure
        product = self._products.get(product_id)
        if product is None:
            return self._failure(
                request,
                FailureCategory.INVALID_INPUT,
                "fake.product_not_found",
                SideEffectState.NOT_SENT,
            )
        updated = product.model_copy(update={"inventory": 0, "is_active": False, "version": product.version + 1})
        self._products[product_id] = updated
        if fault is not None and fault.kind == FakeFaultKind.UNKNOWN_AFTER_SEND:
            return self._failure(
                request,
                FailureCategory.SIDE_EFFECT_UNKNOWN,
                "fake.unknown_after_send",
                SideEffectState.UNKNOWN,
            )
        backup = next(
            (
                item
                for item in sorted(self._products.values(), key=lambda value: value.product_id)
                if item.product_id != product_id and item.is_active and item.inventory > 0
            ),
            None,
        )
        return AdapterSuccess(
            output={
                "sold_out_product": updated.model_dump(mode="json"),
                "backup_product": None if backup is None else backup.model_dump(mode="json"),
            },
            side_effect_state=SideEffectState.CONFIRMED,
        )

    async def current_context(self, request: AdapterRequest) -> AdapterResult:
        """返回 Fake 中可推导的库存告警，弹幕摘要保持由调用方显式提供。"""
        failure = self._before_send_failure("current_context", request.room_id, request)
        if failure is not None:
            return failure
        alerts = [
            {"product_id": item.product_id, "inventory": item.inventory}
            for item in self._products.values()
            if item.inventory <= 0
        ]
        return AdapterSuccess(
            output={"inventory_alerts": alerts, "danmaku_summary": []},
            side_effect_state=SideEffectState.NOT_SENT,
        )

    def _before_send_failure(
        self,
        operation_name: str,
        resource_key: str,
        request: AdapterRequest,
        *,
        fault: FakeFaultRule | None = None,
    ) -> FailureFact | None:
        """处理 deadline、限流和脚本化发送前冲突；未知故障留给写后边界。"""
        if request.deadline_at <= datetime.now(timezone.utc):
            return self._failure(
                request,
                FailureCategory.TRANSIENT_INFRA,
                "fake.deadline_before_send",
                SideEffectState.NOT_SENT,
            )
        resolved_fault = fault or self._matching_fault(operation_name, resource_key)
        if resolved_fault is None or resolved_fault.kind == FakeFaultKind.UNKNOWN_AFTER_SEND:
            return None
        if resolved_fault.kind == FakeFaultKind.RATE_LIMITED:
            return self._failure(
                request,
                FailureCategory.RATE_LIMITED,
                "fake.rate_limited",
                SideEffectState.NOT_SENT,
                retry_after_seconds=resolved_fault.retry_after_seconds or 1,
            )
        if resolved_fault.kind == FakeFaultKind.VERSION_CONFLICT:
            return self._failure(
                request,
                FailureCategory.VERSION_CONFLICT,
                "fake.scripted_version_conflict",
                SideEffectState.NOT_SENT,
            )
        return self._failure(
            request,
            FailureCategory.TRANSIENT_INFRA,
            "fake.deadline_before_send",
            SideEffectState.NOT_SENT,
        )

    def _matching_fault(self, operation_name: str, resource_key: str) -> FakeFaultRule | None:
        """递增独立调用序号，并返回本次精确匹配的声明式故障。"""
        key = (operation_name, resource_key)
        call_index = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = call_index
        return next(
            (
                fault
                for fault in self._faults
                if fault.operation_name == operation_name
                and fault.resource_key == resource_key
                and fault.call_index == call_index
            ),
            None,
        )

    @staticmethod
    def _failure(
        request: AdapterRequest,
        category: FailureCategory,
        external_code: str,
        side_effect_state: SideEffectState,
        *,
        retry_after_seconds: int | None = None,
    ) -> FailureFact:
        """统一构造不泄露请求载荷的 Fake 失败事实。"""
        return FailureFact(
            category=category,
            external_code=external_code,
            side_effect_state=side_effect_state,
            attempt_id=request.attempt_id,
            retry_after_seconds=retry_after_seconds,
        )
