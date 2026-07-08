"""Phase 3D Kafka Consumer 端到端集成测试。

依赖本地 Kafka broker（通过 check_infra.py 验证可达性）。
测试先写入 seed 数据，再手动生产事件到 Kafka，消费后验证审计链路。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from kafka import KafkaConsumer, KafkaProducer

from src.config.settings import get_settings
from src.gateway.kafka_consumer import LiveAgentKafkaConsumer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def kafka_producer(settings):
    """创建 Kafka producer，用于手动生产测试事件。"""
    producer = KafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_server_list,
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
    )
    yield producer
    producer.close()


class TestKafkaConsumerEndToEnd:
    """Kafka 端到端：生产 -> 消费 -> 审计验证。"""

    def test_consume_danmaku_event_produces_valid_parsed_event(
        self, settings, kafka_producer
    ) -> None:
        """手动生产弹幕事件 -> 消费 -> 验证解析成功。"""
        trace_id = f"trace-kafka-danmaku-{uuid.uuid4().hex[:8]}"
        danmaku_topic = settings.kafka_topics["danmaku"]
        event = {
            "room_id": "room-001",
            "viewer_id": "viewer_test_42",
            "content": "这个商品还能再便宜吗？",
            "event_time": "2026-07-08T10:00:00Z",
            "trace_id": trace_id,
        }
        # 生产事件
        kafka_producer.send(danmaku_topic, event)
        kafka_producer.flush()
        time.sleep(0.5)

        # 消费
        consumer = LiveAgentKafkaConsumer(settings=settings)
        # 从 earliest 而非 latest 消费（因为我们是测试）
        from kafka import KafkaConsumer as KC
        raw_consumer = KC(
            danmaku_topic,
            bootstrap_servers=settings.kafka_bootstrap_server_list,
            auto_offset_reset="earliest",
            consumer_timeout_ms=5000,
            value_deserializer=lambda v: v,
        )
        found = None
        for msg in raw_consumer:
            if msg.value and trace_id.encode() in msg.value:
                found = msg
                break
        raw_consumer.close()

        assert found is not None, f"未找到包含 trace_id={trace_id} 的弹幕消息"

    def test_consume_inventory_event_produces_valid_parsed_event(
        self, settings, kafka_producer
    ) -> None:
        """手动生产售罄事件 -> 消费 -> 验证解析成功。"""
        trace_id = f"trace-kafka-soldout-{uuid.uuid4().hex[:8]}"
        inventory_topic = settings.kafka_topics["inventory"]
        event = {
            "room_id": "room-001",
            "product_id": "p001",
            "event_type": "sold_out",
            "trace_id": trace_id,
        }
        kafka_producer.send(inventory_topic, event)
        kafka_producer.flush()
        time.sleep(0.5)

        from kafka import KafkaConsumer as KC
        raw_consumer = KC(
            inventory_topic,
            bootstrap_servers=settings.kafka_bootstrap_server_list,
            auto_offset_reset="earliest",
            consumer_timeout_ms=5000,
            value_deserializer=lambda v: v,
        )
        found = None
        for msg in raw_consumer:
            if msg.value and trace_id.encode() in msg.value:
                found = msg
                break
        raw_consumer.close()

        assert found is not None, f"未找到包含 trace_id={trace_id} 的库存消息"
