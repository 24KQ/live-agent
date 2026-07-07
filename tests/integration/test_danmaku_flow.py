"""Phase 2C 弹幕聚合与参考回复集成测试。"""

from datetime import datetime, timedelta, timezone

import pytest

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.danmaku_flow import DanmakuFlowService
from src.skills.danmaku_aggregator import DanmakuQuestionCategory
from src.skills.danmaku_events import DanmakuEvent
from src.state.models import LifecycleStage, LiveRoomState


BASE_TIME = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def make_state(lifecycle: LifecycleStage = LifecycleStage.ON_LIVE) -> LiveRoomState:
    """构造播中直播间状态，弹幕流程本阶段不依赖商品状态。"""

    return LiveRoomState(room_id="room-demo-001", lifecycle=lifecycle)


def make_event(content: str, offset_seconds: int, trace_id: str = "trace-danmaku-flow") -> DanmakuEvent:
    """构造同一个 trace 下的脱敏弹幕事件。"""

    return DanmakuEvent(
        room_id="room-demo-001",
        viewer_id=f"viewer_hash_{offset_seconds:03d}",
        content=content,
        event_time=BASE_TIME + timedelta(seconds=offset_seconds),
        trace_id=trace_id,
    )


def test_danmaku_flow_rejects_non_on_live_state() -> None:
    """非 ON_LIVE 生命周期不得处理播中弹幕聚合。"""

    service = DanmakuFlowService(ToolCallAuditStore(get_settings()))

    with pytest.raises(ValueError, match="ON_LIVE"):
        service.handle_danmaku_batch(
            make_state(LifecycleStage.PRE_LIVE),
            [make_event("多少钱？", 0, trace_id="trace-danmaku-reject")],
        )


def test_danmaku_flow_aggregates_replies_and_writes_audit() -> None:
    """弹幕批次应聚合同类问题、生成参考回复，并写入完整审计链路。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    audit_store = ToolCallAuditStore(settings)
    service = DanmakuFlowService(audit_store)

    result = service.handle_danmaku_batch(
        make_state(),
        [
            make_event("这个杯子多少钱？", 0),
            make_event("价格是多少？", 1),
            make_event("还有库存吗？", 2),
            make_event("今天有优惠券吗？", 3),
        ],
    )

    assert result.updated_state == make_state()
    assert result.trace_id == "trace-danmaku-flow"
    assert {group.category for group in result.groups} >= {
        DanmakuQuestionCategory.PRICE,
        DanmakuQuestionCategory.STOCK,
        DanmakuQuestionCategory.PROMOTION,
    }
    assert len(result.replies) >= 3
    assert all("自动发送" not in reply.reply_text for reply in result.replies)

    events = audit_store.list_events_by_trace_id("trace-danmaku-flow")
    assert {event["tool_name"] for event in events} >= {
        "aggregate_danmaku_questions",
        "generate_danmaku_reply",
    }
    assert any(event["result_payload"].get("group_count", 0) >= 3 for event in events)
