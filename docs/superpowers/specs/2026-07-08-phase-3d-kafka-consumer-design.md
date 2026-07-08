# Phase 3D 设计文档：Kafka Consumer 实时播中事件管线

## 1. 概述

Phase 3D 把播中事件从 CLI 模拟升级为从 Kafka 实时消费，
打通真实事件驱动管线。不启动长期守护进程，用一次性消费模式。

## 2. 架构

Kafka topics (4个) -> EventRouter -> parse_danmaku_event / parse_inventory_event
-> KafkaConsumedEvent (含 topic/partition/offset 元数据)
-> OnLiveFlowService / DanmakuFlowService -> 审计

## 3. 模块

- src/gateway/kafka_event_models.py：Kafka 消息到领域事件的解析层
- src/gateway/kafka_consumer.py：EventRouter + LiveAgentKafkaConsumer
- scripts/run_kafka_consumer.py：一次性消费 CLI 演示
