"""Phase 3D Kafka 事件模型。

定义 Kafka 消息到领域事件的转换层。每条 Kafka 消息被包装为 KafkaConsumedEvent，
包含原始 topic/partition/offset 元数据和解析后的领域事件（DanmakuEvent 或 InventoryEvent）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.skills.danmaku_events import DanmakuEvent
from src.skills.on_live_events import InventoryEvent


@dataclass(frozen=True)
class KafkaConsumedEvent:
    """一条 Kafka 消息的完整消费上下文。

    包含 Kafka 元数据（topic、partition、offset）和已校验的领域事件对象。
    danmaku 和 inventory 互斥——根据消息来源 topic 填充其中一个。
    """

    topic: str
    partition: int
    offset: int
    danmaku: DanmakuEvent | None = None
    inventory: InventoryEvent | None = None

    def __post_init__(self) -> None:
        """确保 danmaku 和 inventory 互斥且至少有一个。"""
        has_danmaku = self.danmaku is not None
        has_inventory = self.inventory is not None
        if not (has_danmaku ^ has_inventory):
            raise ValueError(
                "KafkaConsumedEvent must have exactly one of danmaku or inventory"
            )


def parse_danmaku_event(msg: Any) -> KafkaConsumedEvent:
    """把 Kafka ConsumerRecord 解析为包含 DanmakuEvent 的 KafkaConsumedEvent。

    解析失败时统一抛出 ValueError，由上游 consumer 负责记录错误并继续消费下一条。
    """
    try:
        raw = _safe_json_load(msg.value)
    except (json.JSONDecodeError, UnicodeDecodeError, Exception) as exc:
        raise ValueError(f"failed to parse danmaku json: {exc}") from exc

    try:
        danmaku = DanmakuEvent(
            room_id=raw["room_id"],
            viewer_id=raw["viewer_id"],
            content=raw["content"],
            event_time=_parse_datetime(raw["event_time"]),
            trace_id=raw["trace_id"],
        )
    except Exception as exc:
        raise ValueError(f"invalid danmaku event: {exc}") from exc

    return KafkaConsumedEvent(
        topic=msg.topic,
        partition=msg.partition,
        offset=msg.offset,
        danmaku=danmaku,
    )


def parse_inventory_event(msg: Any) -> KafkaConsumedEvent:
    """把 Kafka ConsumerRecord 解析为包含 InventoryEvent 的 KafkaConsumedEvent。"""
    try:
        raw = _safe_json_load(msg.value)
    except (json.JSONDecodeError, UnicodeDecodeError, Exception) as exc:
        raise ValueError(f"failed to parse inventory json: {exc}") from exc

    try:
        inventory = InventoryEvent(
            room_id=raw["room_id"],
            product_id=raw["product_id"],
            event_type=raw["event_type"],
            trace_id=raw["trace_id"],
        )
    except Exception as exc:
        raise ValueError(f"invalid inventory event: {exc}") from exc

    return KafkaConsumedEvent(
        topic=msg.topic,
        partition=msg.partition,
        offset=msg.offset,
        inventory=inventory,
    )


def _safe_json_load(raw: Any) -> dict[str, Any]:
    """安全地加载 Kafka 消息 value（bytes 或 str -> dict）。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    raise ValueError(f"unexpected kafka message value type: {type(raw)}")


def _parse_datetime(value: str) -> datetime:
    """解析 ISO 8601 时间字符串，返回 offset-aware datetime。"""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
