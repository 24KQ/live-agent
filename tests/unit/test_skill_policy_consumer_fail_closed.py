"""SkillPolicyView 生产 Flow 的 BLOCK 门禁零副作用测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.danmaku_flow import DanmakuFlowService
from src.core.on_live_flow import OnLiveFlowService
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.security_hooks import GateDecision
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.policy_view import SkillPolicyView
from src.skills.danmaku_events import DanmakuEvent
from src.skills.on_live_events import InventoryEvent, OnLiveEventType
from src.state.models import LifecycleStage, LiveRoomState


class _ForbiddenDependency:
    """任何方法被调用都立即失败，用于证明 BLOCK 在副作用边界前终止。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"blocked flow touched dependency: {name}")


def _blocked_view(skill_id: str) -> SkillPolicyView:
    """从默认 Catalog 派生只修改目标门禁的完整冻结测试快照。"""

    return SkillPolicyView(
        [
            manifest.model_copy(update={"gate_decision": GateDecision.BLOCK})
            if manifest.skill_id == skill_id
            else manifest
            for manifest in get_default_skill_catalog()
        ]
    )


def test_pre_live_business_flow_stops_before_repository_on_block() -> None:
    """播前读取被 BLOCK 时不能访问货盘或审计依赖。"""

    service = PreLiveBusinessFlowService(
        _ForbiddenDependency(),
        _ForbiddenDependency(),
        policy_view=_blocked_view("query_products"),
    )

    with pytest.raises(PermissionError, match="blocked"):
        service.query_products("room-001", "trace-blocked-pre-live")


def test_on_live_flow_stops_before_reducer_on_block() -> None:
    """售罄能力被 BLOCK 时不能修改直播间状态或写审计。"""

    service = OnLiveFlowService(
        _ForbiddenDependency(),
        policy_view=_blocked_view("handle_sold_out_event"),
    )
    state = LiveRoomState(room_id="room-001", lifecycle=LifecycleStage.ON_LIVE)
    event = InventoryEvent(
        room_id="room-001",
        product_id="p001",
        event_type=OnLiveEventType.SOLD_OUT,
        trace_id="trace-blocked-on-live",
    )

    with pytest.raises(PermissionError, match="blocked"):
        service.handle_sold_out_event(state, event)


def test_danmaku_flow_stops_before_aggregation_on_block() -> None:
    """弹幕聚合被 BLOCK 时不能调用确定性处理或写审计。"""

    service = DanmakuFlowService(
        _ForbiddenDependency(),
        policy_view=_blocked_view("aggregate_danmaku_questions"),
    )
    state = LiveRoomState(room_id="room-001", lifecycle=LifecycleStage.ON_LIVE)
    event = DanmakuEvent(
        room_id="room-001",
        viewer_id="viewer-001",
        content="多少钱",
        event_time=datetime(2026, 7, 15, tzinfo=timezone.utc),
        trace_id="trace-blocked-danmaku",
    )

    with pytest.raises(PermissionError, match="blocked"):
        service.handle_danmaku_batch(state, [event])
