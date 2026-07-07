"""Phase 2A 货盘模型与筛选规则测试。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.skills.product_catalog import CatalogProduct, filter_available_products


def make_product(
    product_id: str,
    inventory: int,
    is_active: bool = True,
) -> CatalogProduct:
    """构造最小可用商品，避免每个测试重复填写无关字段。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"样例商品 {product_id}",
        category="家居",
        price=Decimal("99.00"),
        inventory=inventory,
        conversion_rate=Decimal("0.12"),
        commission_rate=Decimal("0.20"),
        tags=["引流款"],
        selling_points=["轻量便携", "适合日常直播讲解"],
        is_active=is_active,
    )


def test_catalog_product_rejects_invalid_business_values() -> None:
    """商品 ID、价格、库存等关键货盘字段必须提前校验。"""

    with pytest.raises(ValidationError):
        CatalogProduct(
            product_id="",
            name="无效商品",
            category="家居",
            price=Decimal("-1"),
            inventory=-1,
            conversion_rate=Decimal("0.1"),
            commission_rate=Decimal("0.2"),
        )


def test_filter_available_products_keeps_only_active_stocked_products() -> None:
    """播前货盘只应包含可上架且有库存的商品。"""

    products = [
        make_product("p003", inventory=0),
        make_product("p001", inventory=10),
        make_product("p002", inventory=8, is_active=False),
    ]

    available = filter_available_products(products)

    assert [product.product_id for product in available] == ["p001"]
