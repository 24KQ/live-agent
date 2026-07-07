"""Phase 2C 弹幕事件模型测试。

弹幕事件是后续 Kafka consumer 或本地 CLI 模拟进入系统的第一道结构化边界。
这里优先验证脱敏用户标识、房间号、弹幕内容和 trace_id 的最小合法性。
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.skills.danmaku_events import DanmakuEvent


def test_danmaku_event_accepts_desensitized_viewer_and_content() -> None:
    """合法弹幕事件应保留房间、脱敏观众、内容、事件时间和追踪 ID。"""

    event_time = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)

    event = DanmakuEvent(
        room_id="room-demo-001",
        viewer_id="viewer_hash_001",
        content="这个杯子多少钱？",
        event_time=event_time,
        trace_id="trace-danmaku-001",
    )

    assert event.room_id == "room-demo-001"
    assert event.viewer_id == "viewer_hash_001"
    assert event.content == "这个杯子多少钱？"
    assert event.event_time == event_time
    assert event.trace_id == "trace-danmaku-001"


@pytest.mark.parametrize(
    "field,value",
    [
        ("room_id", ""),
        ("viewer_id", ""),
        ("content", "   "),
        ("trace_id", ""),
    ],
)
def test_danmaku_event_rejects_empty_required_fields(field: str, value: str) -> None:
    """关键字段为空时必须在入站模型层拒绝，避免脏数据进入聚合与审计。"""

    payload = {
        "room_id": "room-demo-001",
        "viewer_id": "viewer_hash_001",
        "content": "还有库存吗？",
        "event_time": datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc),
        "trace_id": "trace-danmaku-001",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        DanmakuEvent(**payload)
