"""Phase 2B 播中事件模型测试。"""

import pytest
from pydantic import ValidationError

from src.skills.on_live_events import InventoryEvent, OnLiveEventType


def test_inventory_event_accepts_sold_out_event() -> None:
    """售罄事件必须携带直播间、商品和 trace_id，方便后续审计回放。"""

    event = InventoryEvent(
        room_id="room-demo-001",
        product_id="p001",
        event_type=OnLiveEventType.SOLD_OUT,
        trace_id="trace-on-live-001",
    )

    assert event.event_type == OnLiveEventType.SOLD_OUT
    assert event.product_id == "p001"


def test_inventory_event_rejects_unknown_event_type() -> None:
    """Phase 2B 只支持 sold_out，未知播中事件必须 fail-closed。"""

    with pytest.raises(ValidationError):
        InventoryEvent(
            room_id="room-demo-001",
            product_id="p001",
            event_type="price_changed",
            trace_id="trace-on-live-002",
        )


def test_inventory_event_rejects_empty_product_id() -> None:
    """商品 ID 为空时不能进入 Reducer，避免误下架错误商品。"""

    with pytest.raises(ValidationError):
        InventoryEvent(
            room_id="room-demo-001",
            product_id="",
            event_type=OnLiveEventType.SOLD_OUT,
            trace_id="trace-on-live-003",
        )
