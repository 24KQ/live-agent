# -*- coding: utf-8 -*-
"""Phase 4D DanmakuDaemon 单元测试。"""
import pytest
from datetime import datetime, timezone
from src.gateway.kafka_daemon import DanmakuDaemon
from src.skills.danmaku_events import DanmakuEvent


class TestDanmakuDaemonParse:
    def setup_method(self):
        from src.config.settings import get_settings
        self.settings = get_settings()

    def _build_parse_only_daemon(self, monkeypatch: pytest.MonkeyPatch) -> DanmakuDaemon:
        """构造只验证报文解析的守护进程，禁止单元测试连接真实 Kafka。"""

        class _ParseOnlyKafkaConsumer:
            """保留构造阶段需要的最小 consumer 协议，不启动 broker 元数据请求。"""

            def __init__(self, **_kwargs) -> None:
                # 解析测试只覆盖 _parse_message；记录参数可避免误把真实客户端带回测试。
                self.connection_arguments = _kwargs

            def assign(self, _partitions) -> None:
                """接收分区绑定，模拟真实 consumer 的本地状态切换。"""

            def seek_to_beginning(self, *_partitions) -> None:
                """解析路径不消费消息；该方法仅满足守护进程初始化契约。"""

        monkeypatch.setattr("src.gateway.kafka_daemon.KafkaConsumer", _ParseOnlyKafkaConsumer)
        return DanmakuDaemon(self.settings)

    def test_parse_valid_message(self, monkeypatch: pytest.MonkeyPatch):
        """验证合法 Kafka 消息能被解析为 DanmakuEvent。"""
        daemon = self._build_parse_only_daemon(monkeypatch)
        class MockMsg:
            topic = "anchor.danmaku"
            value = b'{"room_id":"r1","viewer_id":"v1","content":"hello","event_time":"2026-07-08T12:00:00+00:00","trace_id":"t1"}'
            partition = 0
            offset = 1
        event = daemon._parse_message(MockMsg())
        assert event is not None
        assert event.room_id == "r1"
        assert event.content == "hello"
        assert event.trace_id == "t1"

    def test_parse_invalid_json_returns_none(self, monkeypatch: pytest.MonkeyPatch):
        """非法 JSON 返回 None，不抛异常。"""
        daemon = self._build_parse_only_daemon(monkeypatch)
        class MockMsg:
            topic = "anchor.danmaku"
            value = b"not json"
            partition = 0
            offset = 2
        event = daemon._parse_message(MockMsg())
        assert event is None

    def test_parse_missing_fields_uses_defaults(self, monkeypatch: pytest.MonkeyPatch):
        """缺少字段时使用默认值，不崩溃。"""
        daemon = self._build_parse_only_daemon(monkeypatch)
        class MockMsg:
            topic = "anchor.danmaku"
            value = b'{"content":"test"}'
            partition = 0
            offset = 3
        event = daemon._parse_message(MockMsg())
        assert event is not None
        assert event.room_id == "room-001"


class TestDanmakuDaemonBuffer:
    def test_add_to_buffer_starts_window(self):
        """第一条弹幕启动窗口。"""
        daemon = DanmakuDaemon.__new__(DanmakuDaemon)
        daemon._buffer = []
        daemon._window_start = None
        daemon._current_trace_id = ""
        now = datetime.now(timezone.utc)
        event = DanmakuEvent(room_id="r1", viewer_id="v1", content="test", event_time=now, trace_id="t1")
        daemon._add_to_buffer(event)
        assert daemon._window_start == now
        assert daemon._current_trace_id == "t1"
        assert len(daemon._buffer) == 1

    def test_flush_empty_buffer_does_nothing(self):
        """空 buffer 的 flush 不报错。"""
        daemon = DanmakuDaemon.__new__(DanmakuDaemon)
        daemon._buffer = []
        daemon._window_start = datetime.now(timezone.utc)
        daemon._flush_window()
        assert daemon._window_start is None
