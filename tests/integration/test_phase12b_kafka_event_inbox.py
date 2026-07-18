"""Phase 12B 真实 Kafka 到 PostgreSQL Event Inbox 的提交顺序集成测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
from typing import Any
from uuid import uuid4

from kafka import KafkaConsumer, KafkaProducer, TopicPartition
import psycopg
import pytest

from src.config.settings import Settings, get_settings
from src.plan_engine.event_state_machine import EventInboxState, EventOccurrenceKind
from src.plan_engine.event_store import (
    PostgresEventStore,
    initialize_event_store_schema,
)
from src.plan_engine.store import initialize_plan_engine_schema


pytestmark = pytest.mark.integration
TASK4_EVENT_PREFIX = "phase12b-kafka-"


@pytest.fixture(autouse=True)
def _isolate_task4_event_facts() -> Any:
    """测试前后只清理 Task 4 专用事件，避免全局 claim 队列污染其他测试。"""
    settings = get_settings()
    initialize_plan_engine_schema(settings)
    initialize_event_store_schema(settings)

    def cleanup() -> None:
        """按外键顺序删除专用前缀，不 TRUNCATE 其他阶段的权威事实。"""
        with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                parameters = {"prefix": f"{TASK4_EVENT_PREFIX}%"}
                cursor.execute(
                    "DELETE FROM plan_event_applications WHERE event_id LIKE %(prefix)s;",
                    parameters,
                )
                cursor.execute(
                    "DELETE FROM plan_event_occurrences WHERE event_id LIKE %(prefix)s;",
                    parameters,
                )
                cursor.execute(
                    "DELETE FROM plan_event_inbox WHERE event_id LIKE %(prefix)s;",
                    parameters,
                )
            connection.commit()

    cleanup()
    yield
    cleanup()


def _consumer_type() -> Any:
    """延迟读取 Task 4 Adapter，使缺失实现形成清晰红灯。"""
    module = importlib.import_module("src.gateway.kafka_consumer")
    consumer_type = getattr(module, "DurableInventoryKafkaConsumer", None)
    assert consumer_type is not None, "尚未实现 DurableInventoryKafkaConsumer"
    return consumer_type


def _settings(*, topic: str, group_id: str) -> Settings:
    """复用本机基础设施凭据，只覆盖本测试独占 topic/group 与 Trust Profile。"""
    base = get_settings()
    return Settings(
        POSTGRES_HOST=base.postgres_host,
        POSTGRES_PORT=base.postgres_port,
        POSTGRES_DB=base.postgres_db,
        POSTGRES_USER=base.postgres_user,
        POSTGRES_PASSWORD=base.postgres_password,
        KAFKA_BOOTSTRAP_SERVERS=base.kafka_bootstrap_servers,
        KAFKA_TOPIC_INVENTORY=topic,
        INVENTORY_INGRESS_PROFILE_ID="phase12b-real-kafka-v1",
        INVENTORY_INGRESS_TRUSTED_SOURCES="inventory-service",
        INVENTORY_INGRESS_ENABLED=True,
        KAFKA_INVENTORY_EVENT_GROUP_ID=group_id,
        KAFKA_INVENTORY_AUTO_OFFSET_RESET="earliest",
    )


def _payload(event_id: str, *, observed_version: int = 3) -> dict[str, Any]:
    """构造新 Event Inbox 契约所需的完整售罄事实。"""
    return {
        "event_id": event_id,
        "event_type": "SOLD_OUT",
        "room_id": "room-phase12b-kafka",
        "product_id": "product-phase12b-kafka",
        "observed_version": observed_version,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": "inventory-service",
    }


def _producer(settings: Settings) -> KafkaProducer:
    """创建同步等待 metadata 的真实 Producer，避免测试依赖 sleep 猜测发送完成。"""
    return KafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_server_list,
    )


def _encoded(payload: dict[str, Any]) -> bytes:
    """显式生成 Kafka value bytes，不依赖已弃用的 lambda Serializer 协议。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class AlwaysFailStore:
    """真实 Kafka 测试中的数据库失败替身，不产生任何 Event Inbox 事实。"""

    def register_event(self, *args: Any, **kwargs: Any) -> Any:
        """模拟数据库在事务提交前不可用。"""
        raise RuntimeError("forced store failure")


def test_real_kafka_duplicate_conflict_and_following_event_advance_offset() -> None:
    """冲突可靠落库后必须继续消费后续事件，并在重启时没有已提交消息。"""
    suffix = uuid4().hex
    topic = f"phase12b.inventory.{suffix}"
    group_id = f"phase12b-group-{suffix}"
    settings = _settings(topic=topic, group_id=group_id)
    initialize_plan_engine_schema(settings)
    initialize_event_store_schema(settings)
    store = PostgresEventStore(settings)
    event_id = f"phase12b-kafka-event-{suffix}"
    following_id = f"phase12b-kafka-following-{suffix}"
    ordered_partition_key = f"phase12b-kafka-order-{suffix}".encode("ascii")

    producer = _producer(settings)
    try:
        metadata = [
            # Kafka 只保证同一 partition 内的 offset 顺序。该用例断言 duplicate、
            # conflict 与 following event 的完整消费序列，因此四条测试消息必须使用
            # 同一 key 固定到同一 partition，不能把跨分区 poll 顺序误当成业务语义。
            producer.send(topic, _encoded(payload), key=ordered_partition_key).get(timeout=10)
            for payload in (
                _payload(event_id),
                _payload(event_id),
                _payload(event_id, observed_version=4),
                _payload(following_id),
            )
        ]
        producer.flush()
    finally:
        producer.close()

    results = _consumer_type()(settings=settings, event_store=store).consume_batch(
        max_messages=4,
        timeout_ms=10000,
    )

    assert [result.occurrence.classification for result in results] == [
        EventOccurrenceKind.ACCEPTED,
        EventOccurrenceKind.DUPLICATE,
        EventOccurrenceKind.CONFLICT,
        EventOccurrenceKind.ACCEPTED,
    ]
    assert store.get_inbox(event_id).state is EventInboxState.CONFLICT
    assert store.get_inbox(following_id).state is EventInboxState.VERIFIED
    assert len(store.list_occurrences(event_id)) == 3

    partition = metadata[-1].partition
    verifier = KafkaConsumer(
        topic,
        bootstrap_servers=settings.kafka_bootstrap_server_list,
        group_id=group_id,
        enable_auto_commit=False,
        consumer_timeout_ms=1000,
    )
    try:
        verifier.poll(timeout_ms=1000)
        committed = verifier.committed(TopicPartition(topic, partition))
    finally:
        verifier.close()
    assert committed == metadata[-1].offset + 1

    replay = _consumer_type()(settings=settings, event_store=store).consume_batch(
        max_messages=1,
        timeout_ms=1000,
    )
    assert replay == []


def test_real_kafka_store_failure_leaves_offset_for_same_group_restart() -> None:
    """Store 失败不得提交 offset；同 group 用正常 Store 重启后必须收到原消息。"""
    suffix = uuid4().hex
    topic = f"phase12b.inventory.failure.{suffix}"
    group_id = f"phase12b-failure-group-{suffix}"
    settings = _settings(topic=topic, group_id=group_id)
    initialize_plan_engine_schema(settings)
    initialize_event_store_schema(settings)
    event_id = f"phase12b-kafka-retry-{suffix}"

    producer = _producer(settings)
    try:
        producer.send(topic, _encoded(_payload(event_id))).get(timeout=10)
        producer.flush()
    finally:
        producer.close()

    with pytest.raises(RuntimeError, match="forced store failure"):
        _consumer_type()(
            settings=settings,
            event_store=AlwaysFailStore(),
        ).consume_batch(max_messages=1, timeout_ms=10000)

    store = PostgresEventStore(settings)
    replay = _consumer_type()(settings=settings, event_store=store).consume_batch(
        max_messages=1,
        timeout_ms=10000,
    )
    assert len(replay) == 1
    assert replay[0].inbox.event.event_id == event_id
    assert replay[0].occurrence.classification is EventOccurrenceKind.ACCEPTED
