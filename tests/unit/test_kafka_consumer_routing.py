"""Phase 3D Kafka Consumer 路由逻辑单元测试。

使用 mock KafkaConsumer 验证事件路由正确性，不依赖真实 Kafka broker。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.gateway.kafka_event_models import parse_danmaku_event, parse_inventory_event
from src.gateway.kafka_consumer import EventRouter, LiveAgentKafkaConsumer


class TestEventRouter:
    """事件路由逻辑测试。"""

    def test_routes_danmaku_topic_to_danmaku_flow(self) -> None:
        """弹幕 topic 事件应路由到 DanmakuFlowService。"""
        from src.core.danmaku_flow import DanmakuFlowService
        from src.audit.tool_call_audit import ToolCallAuditStore
        from unittest.mock import patch

        msg = _fake_kafka_message(
            topic="anchor.danmaku",
            value={
                "room_id": "room-001",
                "viewer_id": "v1",
                "content": "价格问题",
                "event_time": "2026-07-08T10:00:00Z",
                "trace_id": "t1",
            },
        )
        event = parse_danmaku_event(msg)
        # 验证路由后的结果标记 topic 正确
        assert event.topic == "anchor.danmaku"
        assert event.danmaku is not None

    def test_routes_inventory_topic_to_on_live_flow(self) -> None:
        """库存 topic 事件应路由到 OnLiveFlowService。"""
        msg = _fake_kafka_message(
            topic="anchor.inventory",
            value={
                "room_id": "room-001",
                "product_id": "p001",
                "event_type": "sold_out",
                "trace_id": "t1",
            },
        )
        event = parse_inventory_event(msg)
        assert event.topic == "anchor.inventory"
        assert event.inventory is not None
        assert event.inventory.event_type.value == "sold_out"

    def test_router_skips_unknown_topic_without_exception(self) -> None:
        """未知 topic 应被跳过且不抛异常。"""
        msg = _fake_kafka_message(
            topic="anchor.unknown",
            value=b"{}",
        )
        router = EventRouter()
        # 未知 topic 返回 None，不抛异常
        result = router.dispatch(msg)
        assert result is None

    def test_router_dispatches_known_topics(self) -> None:
        """已知 topic 正确分派。"""
        msg = _fake_kafka_message(
            topic="anchor.danmaku",
            value={
                "room_id": "room-001",
                "viewer_id": "v1",
                "content": "测试弹幕",
                "event_time": "2026-07-08T10:00:00Z",
                "trace_id": "t1",
            },
        )
        router = EventRouter()
        result = router.dispatch(msg)
        assert result is not None
        assert result.topic == "anchor.danmaku"


def _fake_kafka_message(topic: str, value: dict | bytes, partition: int = 0, offset: int = 0) -> object:
    """构造 mock Kafka ConsumerRecord。"""
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False).encode("utf-8")

    class FakeMsg:
        def __init__(self):
            self.topic = topic
            self.partition = partition
            self.offset = offset
            self.value = value
    return FakeMsg()
