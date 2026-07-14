"""Phase 12B 库存事件可信入站边界。

Kafka record 在这里完成严格 JSON 解析、启动冻结 Trust Profile 验证、规范摘要计算和
VerifiedIngressProvenance 构造，随后才交给 EventStore。payload 只能表达业务事实，
不能通过 trusted/approved 等字段自行获得执行权限。
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config.settings import Settings
from src.plan_engine.event_store import (
    EventDelivery,
    EventRegistrationResult,
    EventStore,
)
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance


class InventoryEventIngressError(ValueError):
    """可信入站边界拒绝消息时使用的公共错误。"""


class InventoryEventPayloadError(InventoryEventIngressError):
    """Kafka value 不是严格、完整的受支持库存事实。"""


class IngressTrustError(InventoryEventIngressError):
    """消息 transport/topic/source 未通过启动冻结 Profile。"""


class IngressTrustProfile(BaseModel):
    """进程启动时冻结的库存事件来源白名单。

    Profile 只认证消息到达边界的 transport/topic/source，不信任 payload 中任何权限
    声明。启用状态也进入冻结快照，运行中修改 Settings 不会改变已装配 Consumer。
    """

    model_config = ConfigDict(frozen=True)

    profile_id: str = Field(..., min_length=1)
    transport: str = Field(default="KAFKA", pattern=r"^KAFKA$")
    topic: str = Field(..., min_length=1)
    trusted_sources: frozenset[str]
    enabled: bool = False

    @field_validator("trusted_sources")
    @classmethod
    def _trusted_sources_are_nonempty(
        cls,
        value: frozenset[str],
    ) -> frozenset[str]:
        """可信 source 必须是至少一个非空、已去除首尾空白的标识。"""
        normalized = frozenset(source.strip() for source in value if source.strip())
        if not normalized:
            raise ValueError("trusted_sources 不能为空")
        return normalized

    @classmethod
    def from_settings(cls, settings: Settings) -> "IngressTrustProfile":
        """复制 Settings 值形成启动快照，后续不再读取可变配置对象。"""
        return cls(
            profile_id=settings.inventory_ingress_profile_id,
            transport="KAFKA",
            topic=settings.kafka_topic_inventory,
            trusted_sources=settings.inventory_ingress_trusted_source_set,
            enabled=settings.inventory_ingress_enabled,
        )

    def verify(self, *, transport: str, topic: str, source: str) -> None:
        """同时验证启用状态与三个来源维度，任一不匹配均 fail-closed。"""
        if not self.enabled:
            raise IngressTrustError("库存事件可信入站已禁用")
        if transport != self.transport:
            raise IngressTrustError("库存事件 transport 不受信")
        if topic != self.topic:
            raise IngressTrustError("库存事件 topic 不受信")
        if source not in self.trusted_sources:
            raise IngressTrustError("库存事件 source 不受信")


_REQUIRED_PAYLOAD_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "room_id",
        "product_id",
        "observed_version",
        "occurred_at",
        "source",
    }
)
_OPTIONAL_PAYLOAD_FIELDS = frozenset({"payload_digest"})
_PERMISSION_FIELDS = frozenset(
    {
        "trusted",
        "approved",
        "approval",
        "authorization",
        "authorized",
        "is_trusted",
    }
)


class InventoryEventIngress:
    """把单条 Kafka ConsumerRecord 变成已验证 EventStore 登记事务。"""

    def __init__(self, *, profile: IngressTrustProfile, store: EventStore) -> None:
        """钉住 Profile 与 Store；单条消息处理期间不做隐式 fallback。"""
        self._profile = profile
        self._store = store

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        store: EventStore,
    ) -> "InventoryEventIngress":
        """使用启动时 Settings 快照装配可信入站边界。"""
        return cls(
            profile=IngressTrustProfile.from_settings(settings),
            store=store,
        )

    @property
    def profile(self) -> IngressTrustProfile:
        """公开只读 Profile，便于启动审计而不暴露可变 Settings。"""
        return self._profile

    def ingest(self, record: Any) -> EventRegistrationResult:
        """严格解析、验证并持久化一条 Kafka record。

        返回只表示 EventStore 事务已经完成；Kafka offset 必须由外层 Adapter 在本方法
        成功返回后提交。解析、信任或 Store 异常原样越过该边界，调用方不得前移 offset。
        """
        topic, partition, offset = self._record_identity(record)
        payload = self._decode_payload(record.value)
        event = self._event_from_payload(payload)
        self._profile.verify(
            transport="KAFKA",
            topic=topic,
            source=event.source,
        )
        received_at = self._stable_received_at(record, event)
        identity = f"{topic}:{partition}:{offset}"
        provenance = VerifiedIngressProvenance(
            provenance_id=self._stable_id(
                "provenance",
                self._profile.profile_id,
                identity,
                event.payload_digest,
            ),
            profile_id=self._profile.profile_id,
            transport="KAFKA",
            topic=topic,
            source=event.source,
            received_at=received_at,
            payload_digest=event.payload_digest,
        )
        delivery = EventDelivery(
            occurrence_id=f"kafka:{identity}",
            transport="KAFKA",
            topic=topic,
            partition=partition,
            offset=offset,
            received_at=received_at,
        )
        return self._store.register_event(event, provenance, delivery)

    @staticmethod
    def _record_identity(record: Any) -> tuple[str, int, int]:
        """提取 Kafka 权威坐标，拒绝会导致 occurrence 身份不稳定的缺失字段。"""
        try:
            topic = record.topic
            partition = record.partition
            offset = record.offset
        except AttributeError as exc:
            raise InventoryEventPayloadError("Kafka record 缺少传输坐标") from exc
        if not isinstance(topic, str) or not topic:
            raise InventoryEventPayloadError("Kafka topic 必须是非空字符串")
        if type(partition) is not int or partition < 0:
            raise InventoryEventPayloadError("Kafka partition 必须是非负整数")
        if type(offset) is not int or offset < 0:
            raise InventoryEventPayloadError("Kafka offset 必须是非负整数")
        return topic, partition, offset

    @staticmethod
    def _decode_payload(raw_value: Any) -> dict[str, Any]:
        """以严格 UTF-8/JSON 解码，拒绝重复 key、NaN 和非 object 根值。"""
        if isinstance(raw_value, bytes):
            try:
                raw_value = raw_value.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise InventoryEventPayloadError("Kafka value 不是严格 UTF-8") from exc
        if not isinstance(raw_value, str):
            raise InventoryEventPayloadError("Kafka value 必须是 bytes 或字符串")

        def reject_constant(value: str) -> None:
            """Python json 默认接受 NaN/Infinity；权威摘要边界必须拒绝。"""
            raise InventoryEventPayloadError(f"JSON 包含非法常量: {value}")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            """阻止重复 key 被 last-write-wins 静默覆盖。"""
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise InventoryEventPayloadError(f"JSON 包含重复字段: {key}")
                result[key] = value
            return result

        try:
            decoded = json.loads(
                raw_value,
                parse_constant=reject_constant,
                object_pairs_hook=unique_object,
            )
        except InventoryEventPayloadError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InventoryEventPayloadError("Kafka value 不是合法 JSON") from exc
        if not isinstance(decoded, dict):
            raise InventoryEventPayloadError("库存事件 JSON 根值必须是 object")
        return decoded

    @staticmethod
    def _event_from_payload(payload: dict[str, Any]) -> InventoryFactEvent:
        """按精确字段集构造规范事件，并核对调用方可选摘要。"""
        fields = frozenset(payload)
        forbidden = fields & _PERMISSION_FIELDS
        if forbidden:
            raise InventoryEventPayloadError(
                "payload 不得包含权限字段: " + ",".join(sorted(forbidden))
            )
        allowed = _REQUIRED_PAYLOAD_FIELDS | _OPTIONAL_PAYLOAD_FIELDS
        unknown = fields - allowed
        missing = _REQUIRED_PAYLOAD_FIELDS - fields
        if unknown:
            raise InventoryEventPayloadError(
                "payload 包含未知字段: " + ",".join(sorted(unknown))
            )
        if missing:
            raise InventoryEventPayloadError(
                "payload 缺少字段: " + ",".join(sorted(missing))
            )
        for field_name in (
            "event_id",
            "event_type",
            "room_id",
            "product_id",
            "occurred_at",
            "source",
        ):
            if not isinstance(payload[field_name], str) or not payload[field_name]:
                raise InventoryEventPayloadError(f"{field_name} 必须是非空字符串")
        if payload["event_type"] != "SOLD_OUT":
            raise InventoryEventPayloadError("只接受 SOLD_OUT 库存事实")
        if type(payload["observed_version"]) is not int or payload["observed_version"] < 1:
            raise InventoryEventPayloadError("observed_version 必须是正整数")
        try:
            occurred_at = datetime.fromisoformat(
                payload["occurred_at"].replace("Z", "+00:00")
            )
            event = InventoryFactEvent.create_sold_out(
                event_id=payload["event_id"],
                room_id=payload["room_id"],
                product_id=payload["product_id"],
                observed_version=payload["observed_version"],
                occurred_at=occurred_at,
                source=payload["source"],
            )
        except (TypeError, ValueError) as exc:
            raise InventoryEventPayloadError("库存事件业务字段无效") from exc
        supplied_digest = payload.get("payload_digest")
        if supplied_digest is not None and supplied_digest != event.payload_digest:
            raise InventoryEventPayloadError("payload_digest 与规范摘要不一致")
        return event

    @staticmethod
    def _stable_received_at(record: Any, event: InventoryFactEvent) -> datetime:
        """使用 Kafka record timestamp 保证崩溃重放获得相同 delivery 时间。"""
        timestamp = getattr(record, "timestamp", None)
        if type(timestamp) is int and timestamp >= 0:
            return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        # kafka-python 的 ConsumerRecord 正常总有 broker timestamp。缺失时使用业务
        # 事件时间作为稳定降级，不用进程当前时间破坏 exact delivery replay。
        return event.occurred_at

    @staticmethod
    def _stable_id(*parts: str) -> str:
        """为 provenance 派生不含秘密、可跨重启复算的审计 ID。"""
        encoded = "\x1f".join(parts).encode("utf-8")
        return sha256(encoded).hexdigest()
