"""Phase 2B 播中售罄闭环集成测试。"""

from decimal import Decimal

import pytest

from src.audit.tool_call_audit import ToolCallAuditStore, initialize_tool_call_audit_schema
from src.config.settings import get_settings
from src.core.on_live_flow import OnLiveFlowService
from src.skills.on_live_events import InventoryEvent, OnLiveEventType
from src.state.models import LifecycleStage, LiveRoomState, Product


def make_on_live_state() -> LiveRoomState:
    """构造包含当前商品和备选商品的播中状态。"""

    return LiveRoomState(
        room_id="room-demo-001",
        lifecycle=LifecycleStage.ON_LIVE,
        current_product_id="p001",
        products=[
            Product(product_id="p001", name="轻盈保温杯", price=Decimal("89.90"), inventory=12, is_active=True),
            Product(product_id="p002", name="桌面理线器", price=Decimal("29.90"), inventory=30, is_active=True, conversion_rate=Decimal("0.30")),
            Product(product_id="p003", name="已下架商品", price=Decimal("59.90"), inventory=20, is_active=False),
        ],
    )


def make_event(trace_id: str = "trace-on-live-flow") -> InventoryEvent:
    """构造售罄事件。"""

    return InventoryEvent(
        room_id="room-demo-001",
        product_id="p001",
        event_type=OnLiveEventType.SOLD_OUT,
        trace_id=trace_id,
    )


def test_on_live_flow_rejects_pre_live_state() -> None:
    """非 ON_LIVE 生命周期不得处理播中售罄事件。"""

    service = OnLiveFlowService(ToolCallAuditStore(get_settings()))
    pre_live_state = make_on_live_state().model_copy(update={"lifecycle": LifecycleStage.PRE_LIVE})

    with pytest.raises(ValueError):
        service.handle_sold_out_event(pre_live_state, make_event("trace-reject-pre-live"))


def test_on_live_flow_marks_sold_out_switches_backup_and_writes_audit() -> None:
    """售罄事件应下架当前商品、切换备选商品并写入完整审计链路。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    audit_store = ToolCallAuditStore(settings)
    service = OnLiveFlowService(audit_store)

    result = service.handle_sold_out_event(make_on_live_state(), make_event())

    sold_out_product = result.updated_state.get_product("p001")
    assert sold_out_product.inventory == 0
    assert sold_out_product.is_active is False
    assert result.backup_product is not None
    assert result.backup_product.product_id == "p002"
    assert result.updated_state.current_product_id == "p002"
    assert "桌面理线器" in result.prompt.message

    events = audit_store.list_events_by_trace_id("trace-on-live-flow")
    assert {event["tool_name"] for event in events} >= {
        "handle_sold_out_event",
        "recommend_backup_product",
        "generate_on_live_prompt",
    }


def test_on_live_flow_keeps_state_and_requests_manual_takeover_without_backup() -> None:
    """没有备选商品时仍下架售罄商品，但不切换当前讲解商品。"""

    settings = get_settings()
    initialize_tool_call_audit_schema(settings)
    audit_store = ToolCallAuditStore(settings)
    service = OnLiveFlowService(audit_store)
    state = LiveRoomState(
        room_id="room-demo-001",
        lifecycle=LifecycleStage.ON_LIVE,
        current_product_id="p001",
        products=[
            Product(product_id="p001", name="轻盈保温杯", price=Decimal("89.90"), inventory=12, is_active=True),
        ],
    )

    result = service.handle_sold_out_event(state, make_event("trace-on-live-no-backup"))

    assert result.backup_product is None
    assert result.updated_state.get_product("p001").inventory == 0
    assert result.updated_state.current_product_id == "p001"
    assert "人工接管" in result.prompt.message
