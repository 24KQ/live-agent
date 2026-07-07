"""Phase 1 状态模型测试。

这些测试定义播前地基层最小领域模型：生命周期、商品、直播间状态、
动作和决策轨迹。模型层必须先把非法数据挡住，后续 Reducer 和安全 Hook
才能建立在可信输入上。
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.state.models import (
    Action,
    ActionType,
    DecisionTrace,
    LifecycleStage,
    LiveRoomState,
    Product,
)


def make_product(product_id: str = "p001") -> Product:
    """构造测试商品，避免每个用例重复声明完整字段。"""

    return Product(
        product_id=product_id,
        name="轻盈保温杯",
        price=Decimal("99.00"),
        inventory=20,
        is_active=True,
        conversion_rate=Decimal("0.12"),
        tags=["引流款"],
    )


def test_product_rejects_negative_price_and_inventory() -> None:
    """商品模型必须拒绝负价格和负库存。"""

    with pytest.raises(ValidationError):
        Product(
            product_id="p001",
            name="轻盈保温杯",
            price=Decimal("-1.00"),
            inventory=20,
        )

    with pytest.raises(ValidationError):
        Product(
            product_id="p001",
            name="轻盈保温杯",
            price=Decimal("99.00"),
            inventory=-1,
        )


def test_live_room_state_defaults_to_pre_live() -> None:
    """直播间状态默认应处于播前阶段。"""

    state = LiveRoomState(room_id="room-001", products=[make_product()])

    assert state.lifecycle == LifecycleStage.PRE_LIVE
    assert state.current_product_id is None


def test_action_accepts_known_action_type_and_payload() -> None:
    """Action 应接受明确的动作类型和 payload。"""

    action = Action(
        action_type=ActionType.SET_PRICE,
        product_id="p001",
        payload={"price": "89.90"},
        trace_id="trace-001",
    )

    assert action.action_type == ActionType.SET_PRICE
    assert action.payload["price"] == "89.90"


def test_action_rejects_empty_product_id() -> None:
    """涉及商品的动作必须带非空商品 ID。"""

    with pytest.raises(ValidationError):
        Action(
            action_type=ActionType.SET_PRICE,
            product_id="",
            payload={"price": "89.90"},
            trace_id="trace-001",
        )


def test_decision_trace_records_operator_decision() -> None:
    """决策轨迹必须能记录主播确认或拒绝结果。"""

    trace = DecisionTrace(
        trace_id="trace-001",
        room_id="room-001",
        tool_name="set_product_price",
        action_type=ActionType.SET_PRICE,
        recommendation="建议将 p001 调整为 89.90",
        operator_decision="approved",
        audit_id="audit-001",
    )

    assert trace.operator_decision == "approved"
    assert trace.action_type == ActionType.SET_PRICE
