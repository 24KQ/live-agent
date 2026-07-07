"""Phase 2A 商品手卡生成测试。"""

from decimal import Decimal

from src.skills.product_card_generator import generate_product_card
from src.skills.product_catalog import CatalogProduct


def test_generate_product_card_contains_talking_points_script_and_risk_tips() -> None:
    """手卡必须能直接服务主播讲解，并给出合规风险提醒。"""

    product = CatalogProduct(
        product_id="p001",
        name="轻盈保温杯",
        category="家居",
        price=Decimal("89.90"),
        inventory=30,
        conversion_rate=Decimal("0.18"),
        commission_rate=Decimal("0.20"),
        tags=["引流款", "新品"],
        selling_points=["杯身轻", "保温时间长", "适合通勤"],
    )

    card = generate_product_card(product)

    assert card.product_id == "p001"
    assert "轻盈保温杯" in card.opening_script
    assert "89.90" in card.price_hint
    assert card.talking_points == ["杯身轻", "保温时间长", "适合通勤"]
    assert any("绝对化" in tip for tip in card.risk_tips)


def test_generate_product_card_uses_fallback_talking_points() -> None:
    """商品缺少卖点时，也要生成保守且可讲解的兜底手卡。"""

    product = CatalogProduct(
        product_id="p002",
        name="基础收纳盒",
        category="家居",
        price=Decimal("39.90"),
        inventory=12,
        conversion_rate=Decimal("0.06"),
        commission_rate=Decimal("0.10"),
        tags=[],
        selling_points=[],
    )

    card = generate_product_card(product)

    assert len(card.talking_points) == 3
    assert all(point for point in card.talking_points)
