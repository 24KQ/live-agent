"""LiveAgent 状态 Reducer。

Reducer 是唯一允许修改内存业务状态的确定性入口。它不访问数据库、不调用
外部服务，只根据当前状态和 Action 返回新状态，方便测试和审计复放。
"""

from decimal import Decimal, InvalidOperation

from src.state.models import Action, ActionType, LiveRoomState


class ReducerError(ValueError):
    """Reducer 无法应用动作时抛出的领域错误。"""


def apply_action(state: LiveRoomState, action: Action) -> LiveRoomState:
    """应用单个 Action 并返回新状态。"""

    if action.action_type == ActionType.SET_PRICE:
        return _set_price(state, action)
    if action.action_type == ActionType.MARK_SOLD_OUT:
        return _mark_sold_out(state, action)
    if action.action_type == ActionType.SWITCH_PRODUCT:
        return _switch_product(state, action)
    raise ReducerError(f"unsupported action type: {action.action_type}")


def _get_product_or_raise(state: LiveRoomState, product_id: str):
    """获取商品并把 KeyError 转换成 ReducerError。"""

    try:
        return state.get_product(product_id)
    except KeyError as exc:
        raise ReducerError(f"product not found: {product_id}") from exc


def _set_price(state: LiveRoomState, action: Action) -> LiveRoomState:
    """执行确定性改价。"""

    product = _get_product_or_raise(state, action.product_id)
    raw_price = action.payload.get("price")
    try:
        new_price = Decimal(str(raw_price))
    except (InvalidOperation, TypeError) as exc:
        raise ReducerError("SET_PRICE requires a valid decimal price") from exc
    if new_price < 0:
        raise ReducerError("SET_PRICE price must be greater than or equal to 0")
    return state.replace_product(product.model_copy(update={"price": new_price}))


def _mark_sold_out(state: LiveRoomState, action: Action) -> LiveRoomState:
    """将商品标记为售罄并下架。"""

    product = _get_product_or_raise(state, action.product_id)
    return state.replace_product(product.model_copy(update={"inventory": 0, "is_active": False}))


def _switch_product(state: LiveRoomState, action: Action) -> LiveRoomState:
    """切换当前讲解商品。"""

    _get_product_or_raise(state, action.product_id)
    return state.model_copy(update={"current_product_id": action.product_id})
