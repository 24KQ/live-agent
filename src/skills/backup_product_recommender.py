"""Phase 2B 播中备选商品推荐。

推荐器只基于当前内存状态做确定性选择，不调用 LLM、不查询外部服务。这样售罄
事件处理可以在毫秒级完成，并且测试结果稳定可复现。
"""

from __future__ import annotations

from decimal import Decimal

from src.state.models import LiveRoomState, Product


class BackupProductNotFoundError(ValueError):
    """无法找到可用备选商品或售罄商品不存在。"""


def recommend_backup_product(state: LiveRoomState, sold_out_product_id: str) -> Product:
    """为售罄商品推荐备选商品。

    推荐规则非常保守：跳过售罄商品本身，只考虑仍上架且库存大于 0 的商品。
    排序优先看转化率，其次看库存，最后用商品 ID 保持稳定顺序。
    """

    try:
        state.get_product(sold_out_product_id)
    except KeyError as exc:
        raise BackupProductNotFoundError(f"sold out product not found: {sold_out_product_id}") from exc

    candidates = [
        product
        for product in state.products
        if product.product_id != sold_out_product_id and product.is_active and product.inventory > 0
    ]
    if not candidates:
        raise BackupProductNotFoundError("no available backup product")
    return sorted(candidates, key=_backup_score, reverse=True)[0]


def _backup_score(product: Product) -> tuple[Decimal, int, str]:
    """计算备选商品排序分数。

    返回 tuple 是为了让排序规则清晰可测：高转化率优先，高库存其次，商品 ID
    用于同分时稳定排序。
    """

    return (product.conversion_rate, product.inventory, product.product_id)
