"""Reducer 状态更新测试。

Reducer 只做确定性内存状态更新，不访问数据库、不调用外部工具。
这让高风险动作可以在执行前后被准确测试和审计。
"""

from decimal import Decimal

import pytest

from src.state.models import Action, ActionType, LiveRoomState, Product
from src.state.reducer import ReducerError, apply_action


def make_state() -> LiveRoomState:
    """构造包含两个商品的播前状态。"""

    return LiveRoomState(
        room_id="room-001",
        products=[
            Product(product_id="p001", name="轻盈保温杯", price=Decimal("99.00"), inventory=20),
            Product(product_id="p002", name="便携咖啡杯", price=Decimal("129.00"), inventory=10),
        ],
    )


def test_set_price_updates_product_price_without_mutating_original_state() -> None:
    """SET_PRICE 应返回新状态，并保持原状态不变。"""

    state = make_state()
    action = Action(
        action_type=ActionType.SET_PRICE,
        product_id="p001",
        payload={"price": "89.90"},
        trace_id="trace-001",
    )

    new_state = apply_action(state, action)

    assert state.get_product("p001").price == Decimal("99.00")
    assert new_state.get_product("p001").price == Decimal("89.90")


def test_mark_sold_out_sets_inventory_zero_and_deactivates_product() -> None:
    """MARK_SOLD_OUT 应把商品库存清零并下架。"""

    state = make_state()
    action = Action(action_type=ActionType.MARK_SOLD_OUT, product_id="p001", trace_id="trace-002")

    new_state = apply_action(state, action)

    product = new_state.get_product("p001")
    assert product.inventory == 0
    assert product.is_active is False


def test_switch_product_updates_current_product() -> None:
    """SWITCH_PRODUCT 应切换当前讲解商品。"""

    state = make_state()
    action = Action(action_type=ActionType.SWITCH_PRODUCT, product_id="p002", trace_id="trace-003")

    new_state = apply_action(state, action)

    assert new_state.current_product_id == "p002"


def test_reducer_rejects_missing_product() -> None:
    """商品不存在时 Reducer 必须返回明确失败。"""

    with pytest.raises(ReducerError):
        apply_action(
            make_state(),
            Action(action_type=ActionType.SET_PRICE, product_id="p999", payload={"price": "88.00"}, trace_id="trace-004"),
        )


def test_reducer_rejects_invalid_price_payload() -> None:
    """改价 payload 缺少合法 price 时必须失败。"""

    with pytest.raises(ReducerError):
        apply_action(
            make_state(),
            Action(action_type=ActionType.SET_PRICE, product_id="p001", payload={"price": "-1.00"}, trace_id="trace-005"),
        )
