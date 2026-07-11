# -*- coding: utf-8 -*-
"""Phase 4D Kafka 弹幕守护进程。

DanmakuDaemon 持续消费弹幕 topic，5s 窗口聚合后写入 PostgreSQL。
原始弹幕不持久化，只存聚合结果。
"""

from __future__ import annotations
import json, signal, uuid
from datetime import datetime, timezone
from typing import Any
import psycopg
from psycopg.types.json import Jsonb
from kafka import KafkaConsumer, TopicPartition
from src.config.settings import get_settings
from src.skills.danmaku_events import DanmakuEvent
from src.skills.danmaku_aggregator import aggregate_danmaku_questions, DanmakuQuestionGroup


class DanmakuDaemon:
    """弹幕守护进程：持续从 Kafka 消费弹幕，5s 窗口聚合后写入 PostgreSQL。"""

    def __init__(self, settings=None):
        if settings is None:
            settings = get_settings()
        self._settings = settings
        self._running = False
        self._window_seconds = 5
        self._buffer = []
        self._window_start = None
        self._current_trace_id = ""

        topic = settings.kafka_topics.get("danmaku", "")
        if not topic:
            raise ValueError("danmaku topic not configured")
        self._consumer = KafkaConsumer(
            bootstrap_servers=settings.kafka_bootstrap_server_list,
            enable_auto_commit=False,
            consumer_timeout_ms=1000,
            value_deserializer=lambda v: v,
        )
        tp = TopicPartition(topic, 0)
        self._consumer.assign([tp])
        self._consumer.seek_to_beginning(tp)
        self._topic = topic

    def run_forever(self):
        self._running = True
        self._setup_signal_handlers()
        self._ensure_schema()
        print("[DanmakuDaemon] 启动, topic=" + str(self._consumer.topics()) + ", 窗口=" + str(self._window_seconds) + "s")
        try:
            while self._running:
                msg_pack = self._consumer.poll(timeout_ms=1000, max_records=500)
                now = datetime.now(timezone.utc)
                for _topic, messages in msg_pack.items():
                    for msg in messages:
                        event = self._parse_message(msg)
                        if event is not None:
                            self._add_to_buffer(event)
                if self._window_start is not None and (now - self._window_start).total_seconds() >= self._window_seconds:
                    self._flush_window()
        finally:
            self._consumer.close()
            print("[DanmakuDaemon] 已停止")

    def graceful_shutdown(self, signum=None, frame=None):
        print("[DanmakuDaemon] 收到关闭信号，完成当前窗口...")
        self._running = False
        if self._buffer:
            self._flush_window()

    def _setup_signal_handlers(self):
        import signal
        signal.signal(signal.SIGINT, self.graceful_shutdown)
        signal.signal(signal.SIGTERM, self.graceful_shutdown)

    def _ensure_schema(self):
        from src.audit.tool_call_audit import initialize_tool_call_audit_schema
        initialize_tool_call_audit_schema(self._settings)
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                from pathlib import Path
                sql = Path(__file__).resolve().parent.parent.parent / "docker" / "init_phase4_danmaku_aggregates.sql"
                cur.execute(sql.read_text(encoding="utf-8"))
            conn.commit()
        print("[DanmakuDaemon] 数据库 schema 已就绪")

    def _parse_message(self, msg):
        try:
            payload = json.loads(msg.value.decode("utf-8"))
            return DanmakuEvent(
                room_id=str(payload.get("room_id", "room-001")),
                viewer_id=str(payload.get("viewer_id", "anonymous")),
                content=str(payload.get("content", "")),
                event_time=datetime.fromisoformat(payload["event_time"]) if "event_time" in payload else datetime.now(timezone.utc),
                trace_id=str(payload.get("trace_id", str(uuid.uuid4()))),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print("[DanmakuDaemon] 消息解析失败: " + str(exc))
            return None

    def _add_to_buffer(self, event):
        if self._window_start is None:
            self._window_start = event.event_time
            self._current_trace_id = event.trace_id
            self._buffer = []
        self._buffer.append(event)

    def _flush_window(self):
        if not self._buffer:
            self._window_start = None
            return
        try:
            groups = aggregate_danmaku_questions(self._buffer, window_seconds=self._window_seconds)
            self._persist_aggregates(groups)
            print("[DanmakuDaemon] 窗口聚合写入: " + str(len(groups)) + " 组, " + str(len(self._buffer)) + " 条弹幕")
        except Exception as exc:
            print("[DanmakuDaemon] 聚合/写入失败: " + str(exc))
        # assign ????? commit offset?partition ???
        self._buffer.clear()
        self._window_start = None
        self._current_trace_id = ""

    def _persist_aggregates(self, groups):
        if not groups:
            return
        sql = """
            INSERT INTO live_agent_danmaku_aggregates
                (room_id, trace_id, category, summary, count, sample_contents, window_start, window_end)
            VALUES (%(room_id)s, %(trace_id)s, %(category)s, %(summary)s,
                    %(count)s, %(sample_contents)s, %(window_start)s, %(window_end)s);
        """
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                for g in groups:
                    cat = g.category.value if hasattr(g.category, "value") else str(g.category)
                    cur.execute(sql, {
                        "room_id": g.room_id,
                        "trace_id": g.trace_id,
                        "category": cat,
                        "summary": g.summary,
                        "count": g.count,
                        "sample_contents": Jsonb(g.sample_contents),
                        "window_start": g.window_start,
                        "window_end": g.window_end,
                    })
            conn.commit()
