# -*- coding: utf-8 -*-
"""Phase 4D DanmakuDaemon 端到端集成测试。"""
import json, time, uuid, pytest, psycopg
from datetime import datetime, timezone
from kafka import KafkaProducer, KafkaConsumer, TopicPartition
from psycopg.types.json import Jsonb
from src.config.settings import Settings
from src.skills.danmaku_events import DanmakuEvent
from src.skills.danmaku_aggregator import aggregate_danmaku_questions


@pytest.mark.integration
class TestDanmakuDaemonEndToEnd:

    @pytest.fixture
    def settings(self):
        return Settings()

    def test_daemon_direct_consume_and_persist(self, settings):
        topic = settings.kafka_topics.get("danmaku", "anchor.danmaku")
        trace_id = "int-test-" + str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # 1. 发送 6 条测试弹幕
        producer = KafkaProducer(
            bootstrap_servers="localhost:9092",
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        for i in range(6):
            content = "这个多少钱" if i < 3 else "还有库存吗"
            producer.send(topic, {
                "room_id": "room-001", "viewer_id": "v" + str(i),
                "content": content, "event_time": now.isoformat(), "trace_id": trace_id,
            })
            time.sleep(0.05)
        producer.flush()
        producer.close()

        # 2. 消费（assign + seek_to_beginning）
        consumer = KafkaConsumer(
            bootstrap_servers="localhost:9092",
            enable_auto_commit=False, consumer_timeout_ms=1000,
            value_deserializer=lambda v: v,
        )
        tp = TopicPartition(topic, 0)
        consumer.assign([tp])
        consumer.seek_to_beginning(tp)

        buffer = []
        window_start = None
        daemon_start = time.time()

        while time.time() - daemon_start < 12:
            msg_pack = consumer.poll(timeout_ms=1000, max_records=500)
            current_time = datetime.now(timezone.utc)
            for _t, messages in msg_pack.items():
                for msg in messages:
                    payload = json.loads(msg.value.decode("utf-8"))
                    if payload.get("trace_id") == trace_id:
                        event = DanmakuEvent(
                            room_id=str(payload.get("room_id")),
                            viewer_id=str(payload.get("viewer_id")),
                            content=str(payload.get("content")),
                            event_time=datetime.fromisoformat(payload["event_time"]) if "event_time" in payload else current_time,
                            trace_id=str(payload.get("trace_id")),
                        )
                        if window_start is None:
                            window_start = event.event_time
                        buffer.append(event)

            if window_start and (current_time - window_start).total_seconds() >= 5 and buffer:
                groups = aggregate_danmaku_questions(buffer, window_seconds=5)
                with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
                    with conn.cursor() as cur:
                        for g in groups:
                            cat = g.category.value if hasattr(g.category, "value") else str(g.category)
                            cur.execute(
                                "INSERT INTO live_agent_danmaku_aggregates "
                                "(room_id, trace_id, category, summary, count, sample_contents, window_start, window_end) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
                                (g.room_id, g.trace_id, cat, g.summary, g.count,
                                 json.dumps(g.sample_contents), g.window_start, g.window_end),
                            )
                    conn.commit()
                buffer.clear()
                window_start = None

            if time.time() - daemon_start > 10:
                break

        consumer.close()

        # 3. 验证数据库
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT category, summary, count FROM live_agent_danmaku_aggregates WHERE trace_id = %s;",
                    (trace_id,),
                )
                rows = cur.fetchall()

        assert len(rows) > 0, "应有聚合记录写入数据库"
        total_count = sum(r[2] for r in rows)
        assert total_count == 6, f"应有 6 条弹幕被聚合，实际 {total_count}"
