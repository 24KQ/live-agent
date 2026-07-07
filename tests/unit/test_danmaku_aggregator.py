"""Phase 2C 弹幕聚合器测试。

聚合器使用确定性规则把短时间内的同类弹幕问题合并，帮助主播先看高频问题，
后续接入 Kafka 或 LLM 时仍可以复用这层稳定的业务边界。
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.skills.danmaku_aggregator import DanmakuQuestionCategory, aggregate_danmaku_questions
from src.skills.danmaku_events import DanmakuEvent


BASE_TIME = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def make_event(content: str, offset_seconds: int = 0, room_id: str = "room-demo-001", trace_id: str = "trace-danmaku-agg") -> DanmakuEvent:
    """构造测试弹幕事件，默认都落在同一个直播间和 trace 链路内。"""

    return DanmakuEvent(
        room_id=room_id,
        viewer_id=f"viewer_hash_{offset_seconds:03d}",
        content=content,
        event_time=BASE_TIME + timedelta(seconds=offset_seconds),
        trace_id=trace_id,
    )


def test_aggregate_danmaku_questions_groups_similar_questions_in_five_second_window() -> None:
    """5 秒窗口内同类问题应合并计数，并保留样例内容供主播判断语境。"""

    groups = aggregate_danmaku_questions(
        [
            make_event("这个杯子多少钱？", 0),
            make_event("价格是多少呀？", 2),
            make_event("还有库存吗？", 3),
        ],
        window_seconds=5,
    )

    price_group = next(group for group in groups if group.category == DanmakuQuestionCategory.PRICE)
    stock_group = next(group for group in groups if group.category == DanmakuQuestionCategory.STOCK)

    assert price_group.count == 2
    assert price_group.sample_contents == ["这个杯子多少钱？", "价格是多少呀？"]
    assert price_group.summary == "价格相关问题"
    assert stock_group.count == 1
    assert stock_group.summary == "库存相关问题"


def test_aggregate_danmaku_questions_keeps_later_window_separate() -> None:
    """超过 5 秒的同类问题应进入新的时间窗口，避免把不同播中节奏混在一起。"""

    groups = aggregate_danmaku_questions(
        [
            make_event("这个多少钱？", 0),
            make_event("现在价格多少？", 6),
        ],
        window_seconds=5,
    )

    assert [group.count for group in groups] == [1, 1]
    assert all(group.category == DanmakuQuestionCategory.PRICE for group in groups)


def test_aggregate_danmaku_questions_rejects_mixed_room_or_trace() -> None:
    """同一次聚合必须只处理同一个直播间和 trace，避免审计链路被串写。"""

    with pytest.raises(ValueError, match="same room_id"):
        aggregate_danmaku_questions([make_event("多少钱？"), make_event("有优惠吗？", room_id="room-other")])

    with pytest.raises(ValueError, match="same trace_id"):
        aggregate_danmaku_questions([make_event("多少钱？"), make_event("有优惠吗？", trace_id="trace-other")])
