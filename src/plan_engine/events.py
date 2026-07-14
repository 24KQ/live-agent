"""Phase 12B 售罄事件事实、规范摘要和可信来源公共模型。

本模块只定义不可变值对象与授权构造边界，不保存事件、不连接 Kafka，也不执行
冻结或售罄写。Event Store、Ingress Adapter 和 PreemptionCoordinator 分别在后续
Task 中建立，避免 Task 1 提前形成半套运行链。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from src.skill_runtime.models import (
    EventAuthorizationContext,
    _build_verified_event_authorization,
)


class InventoryEventType(StrEnum):
    """Phase 12B 首期唯一受支持的库存事实类型。"""

    SOLD_OUT = "SOLD_OUT"


class ImpactScope(StrEnum):
    """确定性 ImpactAnalyzer 可以返回的受控影响范围。"""

    PRODUCT = "PRODUCT"
    ROOM = "ROOM"
    PLATFORM = "PLATFORM"


def _strict_json_copy(value: Any, *, path: str = "$") -> Any:
    """复制严格 JSON 值并拒绝 Python 编码器会隐式转换的类型。

    tuple、非字符串 key、NaN 和 Infinity 虽可能被部分 JSON 库接受或转换，但会让
    不同语言计算出不同摘要，因此在事件权威边界统一拒绝。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} 包含非有限浮点数")
        return value
    if isinstance(value, list):
        return [
            _strict_json_copy(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} 的 JSON object key 必须是字符串")
            copied[key] = _strict_json_copy(item, path=f"{path}.{key}")
        return copied
    raise TypeError(f"{path} 包含非 JSON 类型: {type(value).__name__}")


def canonical_json_sha256(value: Any) -> str:
    """使用 UTF-8、key 排序和紧凑分隔符计算稳定 SHA-256。"""
    canonical = _strict_json_copy(value)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _aware_utc(value: datetime, field_name: str) -> datetime:
    """把 aware datetime 统一为 UTC，拒绝依赖机器本地时区的裸时间。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(timezone.utc)


class InventoryFactEvent(BaseModel, frozen=True):
    """进入 Event Inbox 前已经规范化的不可变售罄事实。"""

    event_id: str = Field(..., min_length=1)
    event_type: InventoryEventType = InventoryEventType.SOLD_OUT
    room_id: str = Field(..., min_length=1)
    product_id: str = Field(..., min_length=1)
    observed_version: int = Field(..., ge=1)
    occurred_at: datetime
    source: str = Field(..., min_length=1)
    payload_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        """事件时间统一为 UTC，确保跨时区生产者得到相同摘要。"""
        return _aware_utc(value, "occurred_at 时区")

    @model_validator(mode="after")
    def _digest_must_match_fact(self) -> "InventoryFactEvent":
        """拒绝调用方提供的错误摘要，首次事实不能由 payload 自报覆盖。"""
        expected = self.calculate_payload_digest()
        if self.payload_digest != expected:
            raise ValueError("payload_digest 与规范事件摘要不一致")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        """返回排除摘要自身后的规范 JSON 事实。"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "room_id": self.room_id,
            "product_id": self.product_id,
            "observed_version": self.observed_version,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "source": self.source,
        }

    def calculate_payload_digest(self) -> str:
        """根据当前冻结字段重新计算摘要，供持久化和授权边界复核。"""
        return canonical_json_sha256(self.canonical_payload())

    @classmethod
    def create_sold_out(
        cls,
        *,
        event_id: str,
        room_id: str,
        product_id: str,
        observed_version: int,
        occurred_at: datetime,
        source: str,
    ) -> "InventoryFactEvent":
        """创建售罄事实并在模型校验前计算规范摘要。"""
        normalized_time = _aware_utc(occurred_at, "occurred_at 时区")
        payload = {
            "event_id": event_id,
            "event_type": InventoryEventType.SOLD_OUT.value,
            "room_id": room_id,
            "product_id": product_id,
            "observed_version": observed_version,
            "occurred_at": normalized_time.isoformat().replace("+00:00", "Z"),
            "source": source,
        }
        return cls(
            event_id=event_id,
            event_type=InventoryEventType.SOLD_OUT,
            room_id=room_id,
            product_id=product_id,
            observed_version=observed_version,
            occurred_at=normalized_time,
            source=source,
            payload_digest=canonical_json_sha256(payload),
        )


class VerifiedIngressProvenance(BaseModel, frozen=True):
    """Ingress Trust Profile 验证后可持久化的来源证据。"""

    provenance_id: str = Field(..., min_length=1)
    profile_id: str = Field(..., min_length=1)
    transport: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    received_at: datetime
    payload_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @field_validator("received_at")
    @classmethod
    def _normalize_received_at(cls, value: datetime) -> datetime:
        """接收时间必须是可跨进程比较的 aware UTC 时间。"""
        return _aware_utc(value, "received_at 时区")


def _build_event_authorization_context(
    event: InventoryFactEvent,
    provenance: VerifiedIngressProvenance,
) -> EventAuthorizationContext:
    """核对事件与来源闭合后构造 Skill Runtime 可信事件授权。

    即使调用方使用 ``model_copy(update=...)`` 绕过 Pydantic 重验证，本边界仍重新
    计算事件摘要并比较 provenance，避免被篡改的模型实例升级为执行权限。
    """
    calculated = event.calculate_payload_digest()
    if event.payload_digest != calculated:
        raise ValueError("事件字段与 payload 摘要不一致")
    if provenance.payload_digest != event.payload_digest:
        raise ValueError("事件与 provenance 摘要不一致")
    if provenance.source != event.source:
        raise ValueError("事件与 provenance source 不一致")
    return _build_verified_event_authorization(
        event_id=event.event_id,
        provenance_id=provenance.provenance_id,
        payload_digest=event.payload_digest,
        observed_version=event.observed_version,
    )
