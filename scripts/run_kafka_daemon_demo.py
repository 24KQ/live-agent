# -*- coding: utf-8 -*-
"""Phase 4D Kafka 弹幕守护进程端到端演示。

步骤：
1. 用 Kafka 生产者发送 10 条测试弹幕
2. 启动 DanmakuDaemon 消费
3. 验证 PostgreSQL 中有聚合记录
4. 展示聚合结果
"""
import json, time, uuid
from datetime import datetime, timezone
from kafka import KafkaProducer
from src.config.settings import get_settings
from src.gateway.kafka_daemon import DanmakuDaemon

def main():
    settings = get_settings()
    topic = settings.kafka_topics.get("danmaku", "anchor.danmaku")
    print("[Demo] Kafka broker: " + str(settings.kafka_bootstrap_server_list))

    # 1. 发送测试弹幕
    print("[Demo] 发送 10 条测试弹幕到 topic: " + topic)
    producer = KafkaProducer(bootstrap_servers=settings.kafka_bootstrap_server_list,
                             value_serializer=lambda v: json.dumps(v).encode("utf-8"))
    trace_id = str(uuid.uuid4())
    test_messages = [
        {"room_id": "room-001", "viewer_id": "v1", "content": "这个多少钱", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v2", "content": "还有库存吗", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v3", "content": "怎么使用啊", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v4", "content": "价格还能便宜吗", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v5", "content": "几天能发货", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v6", "content": "能优惠吗", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v7", "content": "退货包运费吗", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v8", "content": "这个多少钱", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v9", "content": "使用方法是什么", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
        {"room_id": "room-001", "viewer_id": "v10", "content": "还有货吗", "event_time": datetime.now(timezone.utc).isoformat(), "trace_id": trace_id},
    ]
    for msg in test_messages:
        producer.send(topic, msg)
        time.sleep(0.1)
    producer.flush()
    print("[Demo] 消息发送完成, trace_id: " + trace_id)

    # 2. 启动守护进程消费（timeout 模式）
    print("[Demo] 启动守护进程消费 5s...")
    daemon = DanmakuDaemon(settings)
    daemon._running = True
    try:
        import signal
        signal.signal(signal.SIGINT, lambda *a: setattr(daemon, "_running", False))
        signal.signal(signal.SIGTERM, lambda *a: setattr(daemon, "_running", False))
    except Exception:
        pass
    daemon._ensure_schema()
    import time as ttime
    start = ttime.time()
    while daemon._running and (ttime.time() - start) < 8:
        msg_pack = daemon._consumer.poll(timeout_ms=1000, max_records=500)
        now = datetime.now(timezone.utc)
        for _topic, messages in msg_pack.items():
            for msg in messages:
                event = daemon._parse_message(msg)
                if event is not None:
                    daemon._add_to_buffer(event)
        if daemon._window_start and (now - daemon._window_start).total_seconds() >= 5:
            daemon._flush_window()
        if ttime.time() - start > 6:
            break
    daemon._consumer.close()

    # 3. 验证数据库
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(**settings.postgres_connection_kwargs, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, room_id, category, summary, count, window_start FROM live_agent_danmaku_aggregates WHERE trace_id = %(trace_id)s ORDER BY window_start DESC;", {"trace_id": trace_id})
            rows = cur.fetchall()

    print("\n[Demo] 数据库聚合结果(" + str(len(rows)) + " 组):")
    for r in rows:
        print("  [id=" + str(r["id"]) + "] " + r["category"] + " | " + r["summary"] + " | count=" + str(r["count"]))

if __name__ == "__main__":
    main()
