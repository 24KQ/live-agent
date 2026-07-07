"""Phase 2A 确定性排品生成测试。"""

from decimal import Decimal

from src.skills.live_plan_generator import generate_live_plan
from src.skills.product_catalog import CatalogProduct


def make_product(
    product_id: str,
    price: str,
    inventory: int,
    conversion_rate: str,
    commission_rate: str,
    tags: list[str],
) -> CatalogProduct:
    """构造排品测试商品，显式暴露影响排序的业务字段。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"样例商品 {product_id}",
        category="家居",
        price=Decimal(price),
        inventory=inventory,
        conversion_rate=Decimal(conversion_rate),
        commission_rate=Decimal(commission_rate),
        tags=tags,
        selling_points=["直播间常用卖点"],
    )


def test_generate_live_plan_groups_traffic_profit_and_atmosphere_products() -> None:
    """排品草案应优先形成引流、利润、氛围三类可解释商品位。"""

    products = [
        make_product("p-profit", "199.00", 20, "0.08", "0.35", ["利润款"]),
        make_product("p-atmosphere", "59.00", 50, "0.18", "0.12", ["氛围款"]),
        make_product("p-traffic", "29.90", 100, "0.30", "0.05", ["引流款"]),
    ]

    plan = generate_live_plan(room_id="room-demo-001", products=products, trace_id="trace-plan")

    assert plan.room_id == "room-demo-001"
    assert [item.product_id for item in plan.items] == ["p-traffic", "p-profit", "p-atmosphere"]
    assert [item.role for item in plan.items] == ["引流款", "利润款", "氛围款"]
    assert all(item.reason for item in plan.items)


def test_generate_live_plan_keeps_opening_roles_diverse_when_available() -> None:
    """货盘有多种角色时，开场前三个商品位应先覆盖核心角色。"""

    products = [
        make_product("p-traffic-a", "19.90", 100, "0.35", "0.05", ["引流款"]),
        make_product("p-traffic-b", "29.90", 90, "0.30", "0.08", ["引流款"]),
        make_product("p-profit", "199.00", 20, "0.08", "0.35", ["利润款"]),
        make_product("p-atmosphere", "69.00", 60, "0.16", "0.12", ["氛围款"]),
    ]

    plan = generate_live_plan(room_id="room-demo-001", products=products, trace_id="trace-diverse")

    assert [item.role for item in plan.items[:3]] == ["引流款", "利润款", "氛围款"]


def test_generate_live_plan_rejects_empty_catalog() -> None:
    """没有可用货盘时不能生成空方案假装成功。"""

    try:
        generate_live_plan(room_id="room-demo-001", products=[], trace_id="trace-empty")
    except ValueError as exc:
        assert "products" in str(exc)
    else:
        raise AssertionError("generate_live_plan should reject empty product catalog")
