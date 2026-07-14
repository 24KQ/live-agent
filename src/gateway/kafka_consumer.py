"""Phase 3D Kafka Consumer 与事件路由器。

封装 kafka-python 的 Consumer，订阅四个 LiveAgent topic，
同时提供 EventRouter 根据 topic 名把消息分派到正确的业务服务。

本模块不启动长期守护进程——由 scripts/run_kafka_consumer.py 负责一次性消费演示。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from src.config.settings import Settings
from src.gateway.kafka_event_models import (
    KafkaConsumedEvent,
    parse_danmaku_event,
    parse_inventory_event,
)


class EventRouter:
    """根据 Kafka topic 分派消息到对应的解析器。

    支持四个 topic 的映射：
    - anchor.danmaku -> parse_danmaku_event
    - anchor.inventory -> parse_inventory_event
    - anchor.traffic / anchor.command -> 暂未实现，返回 None 不抛异常
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            from src.config.settings import get_settings
            settings = get_settings()
        topics = settings.kafka_topics
        # 构建 topic -> parser 的查找表
        self._parser_map: dict[str, Any] = {}
        if topics.get("danmaku"):
            self._parser_map[topics["danmaku"]] = parse_danmaku_event
        if topics.get("inventory"):
            self._parser_map[topics["inventory"]] = parse_inventory_event
        # traffic 和 command 留待后续 Phase 实现

    def dispatch(self, msg: Any) -> KafkaConsumedEvent | None:
        """根据消息的 topic 字段分派到对应解析器。

        返回 None 表示未知 topic（不抛异常，不中断消费流程）。
        返回 KafkaConsumedEvent 表示成功解析。
        """
        parser = self._parser_map.get(msg.topic)
        if parser is None:
            print(f"[EventRouter] unknown topic: {msg.topic}, skipping")
            return None
        try:
            return parser(msg)
        except ValueError as exc:
            print(f"[EventRouter] parse error on topic {msg.topic}: {exc}")
            return None


class LiveAgentKafkaConsumer:
    """LiveAgent Kafka 消费者。

    封装 kafka-python KafkaConsumer，附加 topic 订阅和批量拉取逻辑。
    用于一次性消费演示（`consume_batch`），不做长期 offset 管理。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        if settings is None:
            from src.config.settings import get_settings
            settings = get_settings()
        self._bootstrap_servers: list[str] = settings.kafka_bootstrap_server_list
        self._topics: dict[str, str] = settings.kafka_topics
        self._router = EventRouter(settings=settings)

    @property
    def topic_names(self) -> list[str]:
        """所有订阅的 topic 名列表。"""
        return [t for t in self._topics.values() if t]

    def consume_batch(
        self,
        max_messages: int = 10,
        timeout_ms: int = 5000,
    ) -> list[KafkaConsumedEvent]:
        """从 Kafka 批量拉取消息，路由解析后返回。

        每次调用创建新的 Consumer 实例，拉取完就 close。
        解析失败的消息被丢弃（打印错误日志），不中断批量消费。
        """
        from kafka import KafkaConsumer

        consumer = KafkaConsumer(
            *self.topic_names,
            bootstrap_servers=self._bootstrap_servers,
            auto_offset_reset="latest",
            enable_auto_commit=False,
            consumer_timeout_ms=timeout_ms,
            value_deserializer=lambda v: v,  # 保留原始 bytes，由 parser 处理
        )

        results: list[KafkaConsumedEvent] = []
        try:
            for msg in consumer:
                event = self._router.dispatch(msg)
                if event is not None:
                    results.append(event)
                if len(results) >= max_messages:
                    break
        finally:
            consumer.close()

        return results


class DurableInventoryKafkaConsumer:
    """先持久化 Event Inbox、再手动提交 offset 的库存事件 Adapter。

    本类与旧 ``LiveAgentKafkaConsumer`` 并存：旧类继续服务一次性解析演示，新类只
    订阅库存 topic，并以固定 consumer group 驱动权威 EventStore。任何入站或 Store
    异常都会越过 ``consume_batch``，finally 只关闭连接，不提交 offset。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        event_store: Any,
        consumer_factory: Callable[..., Any] | None = None,
    ) -> None:
        """复制启动配置并装配冻结 Trust Profile，不在运行中重新读取 Settings。"""
        from src.gateway.inventory_event_ingress import InventoryEventIngress

        self._bootstrap_servers = tuple(settings.kafka_bootstrap_server_list)
        self._topic = settings.kafka_topic_inventory
        self._group_id = settings.kafka_inventory_event_group_id
        self._auto_offset_reset = settings.kafka_inventory_auto_offset_reset
        self._ingress = InventoryEventIngress.from_settings(
            settings,
            store=event_store,
        )
        self._consumer_factory = consumer_factory

    @property
    def trust_profile(self) -> Any:
        """返回冻结 Profile，供启动审计和健康检查读取。"""
        return self._ingress.profile

    def consume_batch(
        self,
        *,
        max_messages: int = 10,
        timeout_ms: int = 5000,
    ) -> list[Any]:
        """按 record 顺序持久化并提交精确 partition 的下一 offset。

        ``consumer.commit`` 只会在 ``ingest`` 成功返回后调用。重复或冲突 occurrence
        也是可靠持久化结果，因此允许提交；解析、信任和数据库异常均不捕获为成功。
        """
        if type(max_messages) is not int or max_messages < 1:
            raise ValueError("max_messages 必须是正整数")
        if type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError("timeout_ms 必须是正整数")
        from kafka import KafkaConsumer
        from kafka.structs import OffsetAndMetadata, TopicPartition

        factory = self._consumer_factory or KafkaConsumer
        consumer = factory(
            self._topic,
            bootstrap_servers=list(self._bootstrap_servers),
            group_id=self._group_id,
            auto_offset_reset=self._auto_offset_reset,
            enable_auto_commit=False,
            consumer_timeout_ms=timeout_ms,
        )
        results: list[Any] = []
        try:
            for record in consumer:
                result = self._ingress.ingest(record)
                topic_partition = TopicPartition(record.topic, record.partition)
                consumer.commit(
                    offsets={
                        topic_partition: OffsetAndMetadata(
                            record.offset + 1,
                            "",
                            -1,
                        )
                    }
                )
                results.append(result)
                if len(results) >= max_messages:
                    break
        finally:
            consumer.close()
        return results
