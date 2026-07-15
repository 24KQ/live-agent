"""售罄写副作用未知后的严格只读对账。

本模块不重发写请求，也不修改 Phase 11B 的 Operation/Attempt 终态。它使用原
Attempt ID 和可信事件版本发起一次只读商品上下文查询，只有商品身份、售罄状态与
版本递增全部闭合时才返回确认成功证据；否则保持 WAITING_RECONCILIATION。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.skill_runtime.models import (
    AdapterRequest,
    AdapterSuccess,
    EventAuthorizationContext,
    FailureCategory,
    FailureFact,
    SideEffectState,
)
from src.skill_runtime.platform_ports import LiveOperationsPort


class SoldOutReconciliationStatus(StrEnum):
    """售罄未知副作用只允许确认成功或继续等待两种结论。"""

    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"


class SoldOutReconciliationRequest(BaseModel, frozen=True):
    """把原未知 Attempt、可信事件和 CAS 输入绑定成不可变对账请求。"""

    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    product_id: str = Field(..., min_length=1)
    expected_version: int = Field(..., ge=1, strict=True)
    event_authorization: EventAuthorizationContext
    original_failure: FailureFact
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def _deadline_is_aware_utc(cls, value: datetime) -> datetime:
        """只读 Adapter 同样使用绝对 UTC deadline，禁止无限等待。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at 必须包含时区")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _facts_are_closed(self) -> "SoldOutReconciliationRequest":
        """拒绝确定失败、伪事件来源和与事件观察版本不一致的 CAS 请求。"""
        if (
            self.original_failure.category is not FailureCategory.SIDE_EFFECT_UNKNOWN
            or self.original_failure.side_effect_state is not SideEffectState.UNKNOWN
        ):
            raise ValueError("严格对账只接受 SIDE_EFFECT_UNKNOWN/UNKNOWN")
        if not self.event_authorization.provenance_verified:
            raise ValueError("event_authorization 来源身份未通过验证")
        if self.event_authorization.observed_version != self.expected_version:
            raise ValueError("事件观察版本与 expected_version 不一致")
        return self


class SoldOutReconciliationResult(BaseModel, frozen=True):
    """可写入 EventApplication/NodeRun 的只读对账结论。"""

    model_config = ConfigDict(arbitrary_types_allowed=False)

    status: SoldOutReconciliationStatus
    original_attempt_id: str = Field(..., min_length=1)
    evidence: dict[str, Any] | None = None
    read_failure: FailureFact | None = None
    reason_code: str = Field(..., min_length=1)

    @field_validator("evidence", mode="after")
    @classmethod
    def _freeze_evidence(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """借 AdapterSuccess 的 JSON 冻结边界隔离调用方持有的商品快照。"""
        if value is None:
            return None
        return AdapterSuccess(output=value).output

    @model_validator(mode="after")
    def _result_shape_matches_status(self) -> "SoldOutReconciliationResult":
        """确认结论必须有证据，等待结论不得携带伪成功证据。"""
        if self.status is SoldOutReconciliationStatus.CONFIRMED_SUCCESS:
            if self.evidence is None or self.read_failure is not None:
                raise ValueError("确认成功必须且只能携带闭合 evidence")
        elif self.evidence is not None:
            raise ValueError("等待对账不得携带成功 evidence")
        return self


class SoldOutSideEffectReconciler:
    """使用 LiveOperationsPort 的只读方法复核原售罄 Attempt。"""

    def __init__(self, port: LiveOperationsPort) -> None:
        """注入同一业务域 Port；服务自身不持有第二份平台状态。"""
        self._port = port

    async def reconcile(
        self,
        request: SoldOutReconciliationRequest,
    ) -> SoldOutReconciliationResult:
        """执行一次只读查询并按保守条件生成确定性结论。"""
        result = await self._port.resolve_product_context(
            AdapterRequest(
                operation_id=f"sold-out-reconciliation:{request.original_failure.attempt_id}",
                attempt_id=request.original_failure.attempt_id,
                room_id=request.room_id,
                idempotency_key=None,
                deadline_at=request.deadline_at,
                payload={"sold_out_product_id": request.product_id},
            )
        )
        if isinstance(result, FailureFact):
            return self._waiting(
                request,
                reason_code="READ_FACT_UNAVAILABLE",
                read_failure=result,
            )

        product = result.output.get("sold_out_product")
        if not isinstance(product, dict):
            return self._waiting(request, reason_code="PRODUCT_EVIDENCE_MISSING")
        version = product.get("version")
        facts_closed = (
            product.get("product_id") == request.product_id
            and product.get("inventory") == 0
            and product.get("is_active") is False
            and type(version) is int
            and version >= request.expected_version + 1
        )
        if not facts_closed:
            return self._waiting(request, reason_code="PRODUCT_FACT_NOT_CLOSED")

        return SoldOutReconciliationResult(
            status=SoldOutReconciliationStatus.CONFIRMED_SUCCESS,
            original_attempt_id=request.original_failure.attempt_id,
            evidence={
                "event_id": request.event_authorization.event_id,
                "provenance_id": request.event_authorization.provenance_id,
                "product_id": request.product_id,
                "expected_version": request.expected_version,
                "confirmed_version": version,
                "sold_out_product": product,
            },
            read_failure=None,
            reason_code="SOLD_OUT_FACT_CONFIRMED",
        )

    @staticmethod
    def _waiting(
        request: SoldOutReconciliationRequest,
        *,
        reason_code: str,
        read_failure: FailureFact | None = None,
    ) -> SoldOutReconciliationResult:
        """构造不携带成功证据、且继续引用原 Attempt 的等待结论。"""
        return SoldOutReconciliationResult(
            status=SoldOutReconciliationStatus.WAITING_RECONCILIATION,
            original_attempt_id=request.original_failure.attempt_id,
            evidence=None,
            read_failure=read_failure,
            reason_code=reason_code,
        )
