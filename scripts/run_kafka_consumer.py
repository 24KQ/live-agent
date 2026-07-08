"""Phase 3D Kafka Consumer CLI 演示。

一次性消费模式：从 Kafka 拉取消息，路由解析，输出每条事件的元数据和审计摘要。
"""

import argparse
import sys
import time
import uuid

from src.config.settings import get_settings
from src.gateway.kafka_consumer import LiveAgentKafkaConsumer


def main() -> None:
    """主入口：从 Kafka 消费消息并展示解析结果。"""
    parser = argparse.ArgumentParser(description="LiveAgent Kafka Consumer Demo (Phase 3D)")
    parser.add_argument(
        "--max-messages", type=int, default=5,
        help="最大消费消息数（默认 5）"
    )
    parser.add_argument(
        "--timeout", type=int, default=10000,
        help="消费者超时毫秒（默认 10000）"
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"[LiveAgent Kafka Consumer] brokers: {settings.kafka_bootstrap_server_list}")
    print(f"[LiveAgent Kafka Consumer] topics: {settings.kafka_topics}")

    consumer = LiveAgentKafkaConsumer(settings=settings)
    print(f"[LiveAgent Kafka Consumer] 开始消费，最多 {args.max_messages} 条，超时 {args.timeout}ms...")

    events = consumer.consume_batch(
        max_messages=args.max_messages,
        timeout_ms=args.timeout,
    )

    print(f"\n共消费 {len(events)} 条事件:")
    for i, event in enumerate(events, 1):
        summary = (
            f"  [{i}] topic={event.topic} "
            f"partition={event.partition} offset={event.offset} "
        )
        if event.danmaku:
            summary += (
                f"danmaku room={event.danmaku.room_id} "
                f"trace={event.danmaku.trace_id} "
                f"content={event.danmaku.content[:50]}"
            )
        elif event.inventory:
            summary += (
                f"inventory room={event.inventory.room_id} "
                f"product={event.inventory.product_id} "
                f"type={event.inventory.event_type.value} "
                f"trace={event.inventory.trace_id}"
            )
        print(summary)

    if not events:
        print("\n（主题中无待消费消息。用 kafka-console-producer 或其他脚本发送测试事件后重试。）")

    print("\n[LiveAgent Kafka Consumer] 演示结束。")


if __name__ == "__main__":
    main()
