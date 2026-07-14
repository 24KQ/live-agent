"""Phase 12B 库存事件可信入站与手动 offset Adapter 单元测试。

记录型 Consumer/Store 会保留调用顺序，使测试能够区分“先写数据库再提交 offset”与
结果偶然相同但顺序错误的实现。这里不连接 Kafka 或 PostgreSQL。
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import json
from typing import Any

from pydantic import ValidationError
import pytest

from src.config.settings import Settings
from src.plan_engine.event_state_machine import EventOccurrenceKind
from src.plan_engine.event_store import InMemoryEventStore
from src.plan_engine.events import InventoryFactEvent


BROKER_TIME_MS = 1784077200000
EVENT_TIME = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)


def _ingress_module() -> Any:
    """延迟导入 Task 4 模块，使缺少实现表现为测试红灯。"""
    return importlib.import_module("src.gateway.inventory_event_ingress")


def _consumer_type() -> Any:
    """延迟读取新 Adapter，同时保持旧 kafka_consumer 模块可正常导入。"""
    module = importlib.import_module("src.gateway.kafka_consumer")
    consumer_type = getattr(module, "DurableInventoryKafkaConsumer", None)
    assert consumer_type is not None, "尚未实现 DurableInventoryKafkaConsumer"
    return consumer_type


def _settings(**overrides: Any) -> Settings:
    """构造不读取全局缓存的 Task 4 配置。"""
    values = {
        "KAFKA_TOPIC_INVENTORY": "inventory-facts",
        "INVENTORY_INGRESS_PROFILE_ID": "inventory-kafka-v1",
        "INVENTORY_INGRESS_TRUSTED_SOURCES": "inventory-service,warehouse-service",
        "INVENTORY_INGRESS_ENABLED": True,
        "KAFKA_INVENTORY_EVENT_GROUP_ID": "live-agent-inventory-test",
        "KAFKA_INVENTORY_AUTO_OFFSET_RESET": "earliest",
    }
    values.update(overrides)
    return Settings(**values)


def _payload(
    *,
    event_id: str = "event-001",
    observed_version: int = 3,
    source: str = "inventory-service",
) -> dict[str, Any]:
    """构造与新 InventoryFactEvent 契约一致的业务 payload。"""
    return {
        "event_id": event_id,
        "event_type": "SOLD_OUT",
        "room_id": "room-001",
        "product_id": "product-001",
        "observed_version": observed_version,
        "occurred_at": EVENT_TIME.isoformat().replace("+00:00", "Z"),
        "source": source,
    }


class FakeKafkaRecord:
    """测试所需的最小 Kafka ConsumerRecord 形状。"""

    def __init__(
        self,
        payload: dict[str, Any] | bytes,
        *,
        topic: str = "inventory-facts",
        partition: int = 2,
        offset: int = 10,
        timestamp: int = BROKER_TIME_MS,
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.timestamp = timestamp
        self.value = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if isinstance(payload, dict)
            else payload
        )


class RecordingStore:
    """包装内存 Store，并把事务完成时机写入共享调用日志。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.inner = InMemoryEventStore()

    def register_event(self, *args: Any, **kwargs: Any) -> Any:
        """只有内部 Store 已成功返回后才记录 store_commit。"""
        result = self.inner.register_event(*args, **kwargs)
        self.calls.append("store_commit")
        return result


class FailingStore:
    """模拟数据库事务失败，验证 offset 不得越过失败边界。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def register_event(self, *args: Any, **kwargs: Any) -> Any:
        """记录尝试后抛错，不产生持久化成功事实。"""
        self.calls.append("store_failed")
        raise RuntimeError("database unavailable")


class RecordingConsumer:
    """可迭代 Consumer 替身，保存 commit 参数和 close 事实。"""

    def __init__(self, records: list[FakeKafkaRecord], calls: list[str]) -> None:
        self.records = records
        self.calls = calls
        self.commits: list[dict[Any, Any]] = []
        self.closed = False

    def __iter__(self):
        """按给定顺序模拟 poll 后返回的 ConsumerRecord。"""
        return iter(self.records)

    def commit(self, offsets: dict[Any, Any]) -> None:
        """保存精确提交坐标，禁止测试只断言“调用过 commit”。"""
        self.calls.append("offset_commit")
        self.commits.append(offsets)

    def close(self) -> None:
        """记录异常路径也释放 Consumer。"""
        self.closed = True


class CommitFailingConsumer(RecordingConsumer):
    """模拟数据库已提交后进程在 Kafka commit 阶段失败。"""

    def commit(self, offsets: dict[Any, Any]) -> None:
        """保存尝试坐标后抛错，制造 PlanStore 领先传输 offset 的窗口。"""
        super().commit(offsets)
        raise RuntimeError("forced offset commit failure")


class RecordingConsumerFactory:
    """捕获生产 Adapter 传给 kafka-python 的启动冻结参数。"""

    def __init__(self, consumer: RecordingConsumer) -> None:
        self.consumer = consumer
        self.topics: tuple[str, ...] | None = None
        self.kwargs: dict[str, Any] | None = None

    def __call__(self, *topics: str, **kwargs: Any) -> RecordingConsumer:
        """返回预置 Consumer，并保存构造参数供断言。"""
        self.topics = topics
        self.kwargs = kwargs
        return self.consumer


def test_settings_builds_frozen_enabled_trust_profile() -> None:
    """Profile 必须钉住 ID、transport、topic、source 集和启用状态。"""
    module = _ingress_module()
    settings = _settings()

    profile = module.IngressTrustProfile.from_settings(settings)

    assert profile.profile_id == "inventory-kafka-v1"
    assert profile.transport == "KAFKA"
    assert profile.topic == "inventory-facts"
    assert profile.trusted_sources == frozenset(
        {"inventory-service", "warehouse-service"}
    )
    assert profile.enabled is True
    with pytest.raises(ValidationError):
        profile.topic = "attacker-topic"


def test_durable_ingress_defaults_are_fail_closed_and_skip_legacy_history() -> None:
    """未显式启用时不接受事件，新 group 默认不回放旧格式库存历史。"""
    module = _ingress_module()
    assert Settings.model_fields["inventory_ingress_enabled"].default is False
    assert (
        Settings.model_fields["kafka_inventory_auto_offset_reset"].default
        == "latest"
    )
    direct_profile = module.IngressTrustProfile(
        profile_id="direct-profile",
        topic="inventory-facts",
        trusted_sources=frozenset({"inventory-service"}),
    )
    assert direct_profile.enabled is False


def test_trust_profile_rejects_disabled_topic_source_and_transport() -> None:
    """任一启动冻结身份不匹配都不能产生 VerifiedIngressProvenance。"""
    module = _ingress_module()
    profile = module.IngressTrustProfile.from_settings(_settings())

    profile.verify(transport="KAFKA", topic="inventory-facts", source="inventory-service")
    for values in (
        {"transport": "HTTP", "topic": "inventory-facts", "source": "inventory-service"},
        {"transport": "KAFKA", "topic": "other-topic", "source": "inventory-service"},
        {"transport": "KAFKA", "topic": "inventory-facts", "source": "unknown"},
    ):
        with pytest.raises(module.IngressTrustError):
            profile.verify(**values)

    disabled = module.IngressTrustProfile.from_settings(
        _settings(INVENTORY_INGRESS_ENABLED=False)
    )
    with pytest.raises(module.IngressTrustError, match="禁用"):
        disabled.verify(
            transport="KAFKA",
            topic="inventory-facts",
            source="inventory-service",
        )


def test_ingress_computes_digest_and_stable_delivery_from_broker_record() -> None:
    """摘要、provenance 与 occurrence 身份必须由入站边界而非 payload 权限字段生成。"""
    module = _ingress_module()
    store = InMemoryEventStore()
    ingress = module.InventoryEventIngress.from_settings(_settings(), store=store)
    record = FakeKafkaRecord(_payload())

    first = ingress.ingest(record)
    replay = ingress.ingest(record)

    expected_event = InventoryFactEvent.create_sold_out(
        event_id="event-001",
        room_id="room-001",
        product_id="product-001",
        observed_version=3,
        occurred_at=EVENT_TIME,
        source="inventory-service",
    )
    assert first.inbox.event == expected_event
    assert first.inbox.provenance.profile_id == "inventory-kafka-v1"
    assert first.inbox.provenance.payload_digest == expected_event.payload_digest
    assert first.occurrence.occurrence_id == "kafka:inventory-facts:2:10"
    assert replay.created is False
    assert store.list_occurrences("event-001") == (first.occurrence,)
    assert not hasattr(first.occurrence, "raw_message")


def test_ingress_rejects_permission_fields_unknown_fields_and_bad_digest() -> None:
    """payload 不能自报 trusted/approved，也不能用错误摘要覆盖规范事实。"""
    module = _ingress_module()
    store = InMemoryEventStore()
    ingress = module.InventoryEventIngress.from_settings(_settings(), store=store)

    for forbidden_field in ("trusted", "approved", "authorization"):
        payload = _payload(event_id=f"event-{forbidden_field}")
        payload[forbidden_field] = True
        with pytest.raises(module.InventoryEventPayloadError):
            ingress.ingest(FakeKafkaRecord(payload, offset=20))

    unknown = _payload(event_id="event-unknown-field")
    unknown["note"] = "字段未进入契约"
    with pytest.raises(module.InventoryEventPayloadError):
        ingress.ingest(FakeKafkaRecord(unknown, offset=21))

    bad_digest = _payload(event_id="event-bad-digest")
    bad_digest["payload_digest"] = "f" * 64
    with pytest.raises(module.InventoryEventPayloadError, match="摘要"):
        ingress.ingest(FakeKafkaRecord(bad_digest, offset=22))
    assert store.list_inbox() == ()


def test_durable_consumer_commits_exact_next_offset_only_after_store() -> None:
    """成功登记后才提交当前 partition 的 offset + 1，并冻结 consumer 配置。"""
    calls: list[str] = []
    store = RecordingStore(calls)
    raw_consumer = RecordingConsumer([FakeKafkaRecord(_payload())], calls)
    factory = RecordingConsumerFactory(raw_consumer)
    consumer = _consumer_type()(
        settings=_settings(),
        event_store=store,
        consumer_factory=factory,
    )

    results = consumer.consume_batch(max_messages=1, timeout_ms=1234)

    assert len(results) == 1
    assert calls == ["store_commit", "offset_commit"]
    assert raw_consumer.closed is True
    assert factory.topics == ("inventory-facts",)
    assert factory.kwargs is not None
    assert factory.kwargs["enable_auto_commit"] is False
    assert factory.kwargs["group_id"] == "live-agent-inventory-test"
    assert factory.kwargs["auto_offset_reset"] == "earliest"
    assert factory.kwargs["consumer_timeout_ms"] == 1234
    [(topic_partition, committed)] = raw_consumer.commits[0].items()
    assert topic_partition.topic == "inventory-facts"
    assert topic_partition.partition == 2
    assert committed.offset == 11


def test_store_or_payload_failure_never_commits_offset_and_still_closes() -> None:
    """解析、信任或数据库失败都必须在 offset 边界前停止。"""
    for record, store in (
        (FakeKafkaRecord(b"not-json"), RecordingStore([])),
        (FakeKafkaRecord(_payload(source="attacker")), RecordingStore([])),
        (FakeKafkaRecord(_payload()), FailingStore([])),
    ):
        calls = store.calls
        raw_consumer = RecordingConsumer([record], calls)
        consumer = _consumer_type()(
            settings=_settings(),
            event_store=store,
            consumer_factory=RecordingConsumerFactory(raw_consumer),
        )

        with pytest.raises(Exception):
            consumer.consume_batch(max_messages=1)

        assert "offset_commit" not in calls
        assert raw_consumer.commits == []
        assert raw_consumer.closed is True


def test_duplicate_and_conflict_are_durable_results_and_both_commit() -> None:
    """重复和冲突已形成权威 occurrence 后都可前移 offset，避免毒消息阻塞分区。"""
    calls: list[str] = []
    store = RecordingStore(calls)
    records = [
        FakeKafkaRecord(_payload(), offset=30),
        FakeKafkaRecord(_payload(), offset=31),
        FakeKafkaRecord(_payload(observed_version=4), offset=32),
    ]
    raw_consumer = RecordingConsumer(records, calls)
    consumer = _consumer_type()(
        settings=_settings(),
        event_store=store,
        consumer_factory=RecordingConsumerFactory(raw_consumer),
    )

    results = consumer.consume_batch(max_messages=3)

    assert [result.occurrence.classification for result in results] == [
        EventOccurrenceKind.ACCEPTED,
        EventOccurrenceKind.DUPLICATE,
        EventOccurrenceKind.CONFLICT,
    ]
    assert calls == ["store_commit", "offset_commit"] * 3
    assert [next(iter(item.values())).offset for item in raw_consumer.commits] == [
        31,
        32,
        33,
    ]


def test_offset_commit_failure_replays_original_occurrence_on_restart() -> None:
    """Store 领先 offset 时重启只能复用首次 occurrence，不能追加第二条投递。"""
    calls: list[str] = []
    store = RecordingStore(calls)
    record = FakeKafkaRecord(_payload(event_id="event-commit-crash"), offset=40)
    first_consumer = CommitFailingConsumer([record], calls)
    first = _consumer_type()(
        settings=_settings(),
        event_store=store,
        consumer_factory=RecordingConsumerFactory(first_consumer),
    )

    with pytest.raises(RuntimeError, match="offset commit"):
        first.consume_batch(max_messages=1)

    second_consumer = RecordingConsumer([record], calls)
    replay = _consumer_type()(
        settings=_settings(),
        event_store=store,
        consumer_factory=RecordingConsumerFactory(second_consumer),
    ).consume_batch(max_messages=1)

    assert replay[0].created is False
    assert replay[0].occurrence.occurrence_id == "kafka:inventory-facts:2:40"
    assert len(store.inner.list_occurrences("event-commit-crash")) == 1
    assert next(iter(second_consumer.commits[0].values())).offset == 41
