"""Phase 3D Kafka 事件模型单元测试。

验证从 Kafka ConsumerRecord 到领域事件模型的 JSON 反序列化和 Pydantic 校验。
使用 mock 消息对象，不依赖真实 Kafka broker。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.gateway.kafka_event_models import (
    KafkaConsumedEvent,
    parse_danmaku_event,
    parse_inventory_event,
)


class TestKafkaEventModels:
    """Kafka 消息反序列化为领域事件的校验测试。"""

    def test_parse_danmaku_event_from_valid_json(self) -> None:
        """合法弹幕 JSON 应正确反序列化为 KafkaConsumedEvent。"""
        msg = _fake_kafka_message(
            topic="anchor.danmaku",
            value={
                "room_id": "room-001",
                "viewer_id": "viewer_hash_abc",
                "content": "这个价格还能便宜吗",
                "event_time": "2026-07-08T10:00:00Z",
                "trace_id": "trace-d-001",
            },
        )
        event = parse_danmaku_event(msg)
        assert event.topic == "anchor.danmaku"
        assert event.partition == 0
        assert event.offset == 42
        assert event.danmaku.content == "这个价格还能便宜吗"
        assert event.danmaku.room_id == "room-001"

    def test_parse_danmaku_event_rejects_missing_room_id(self) -> None:
        """缺少必要字段的弹幕应抛出 ValueError。"""
        msg = _fake_kafka_message(
            topic="anchor.danmaku",
            value={
                "viewer_id": "v1",
                "content": "xxx",
                "event_time": "2026-07-08T10:00:00Z",
                "trace_id": "t1",
            },
        )
        with pytest.raises(ValueError):
            parse_danmaku_event(msg)

    def test_parse_danmaku_event_rejects_blank_content(self) -> None:
        """空内容的弹幕应被拒绝。"""
        msg = _fake_kafka_message(
            topic="anchor.danmaku",
            value={
                "room_id": "r1",
                "viewer_id": "v1",
                "content": "   ",
                "event_time": "2026-07-08T10:00:00Z",
                "trace_id": "t1",
            },
        )
        with pytest.raises(ValueError):
            parse_danmaku_event(msg)

    def test_parse_inventory_event_from_valid_json(self) -> None:
        """合法库存事件 JSON 应正确反序列化。"""
        msg = _fake_kafka_message(
            topic="anchor.inventory",
            value={
                "room_id": "room-001",
                "product_id": "p001",
                "event_type": "sold_out",
                "trace_id": "trace-i-001",
            },
        )
        event = parse_inventory_event(msg)
        assert event.topic == "anchor.inventory"
        assert event.inventory.product_id == "p001"
        assert event.inventory.event_type.value == "sold_out"
        assert event.inventory.room_id == "room-001"

    def test_parse_inventory_event_rejects_unknown_event_type(self) -> None:
        """未知库存事件类型应被拒绝。"""
        msg = _fake_kafka_message(
            topic="anchor.inventory",
            value={
                "room_id": "r1",
                "product_id": "p1",
                "event_type": "unknown_type",
                "trace_id": "t1",
            },
        )
        with pytest.raises(ValueError):
            parse_inventory_event(msg)

    def test_parse_handles_broken_json(self) -> None:
        """非法 JSON 应抛出 ValueError。"""
        msg = _fake_kafka_message(
            topic="anchor.danmaku",
            value=b"not a json",
        )
        with pytest.raises(ValueError):
            parse_danmaku_event(msg)


def _fake_kafka_message(
    topic: str,
    value: dict | bytes,
    partition: int = 0,
    offset: int = 42,
) -> object:
    """构造一个类似 kafka-python ConsumerRecord 的 mock 对象。"""
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False).encode("utf-8")

    class FakeMessage:
        def __init__(self):
            self.topic = topic
            self.partition = partition
            self.offset = offset
            self.value = value

    return FakeMessage()
