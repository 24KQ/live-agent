"""Phase 3D Kafka Consumer 与事件路由器。

封装 kafka-python 的 Consumer，订阅四个 LiveAgent topic，
同时提供 EventRouter 根据 topic 名把消息分派到正确的业务服务。

本模块不启动长期守护进程——由 scripts/run_kafka_consumer.py 负责一次性消费演示。
"""

from __future__ import annotations

import json
from typing import Any

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
