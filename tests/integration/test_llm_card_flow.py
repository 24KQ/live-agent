"""Phase 3E LLM 手卡生成集成测试。

验证真实 DeepSeek API 调用的完整链路和降级逻辑。
"""

import pytest
from src.config.settings import get_settings
from src.skills.llm_card_generator import LLMCardGenerator
from src.skills.product_card_generator import generate_product_card
from src.skills.product_catalog import CatalogProduct
from decimal import Decimal

pytestmark = [pytest.mark.integration, pytest.mark.external]


@pytest.fixture(scope="module")
def product():
    return CatalogProduct(
        product_id="p001",
        name="智能净水壶",
        category="厨房电器",
        price=299.00,
        inventory=150,
        conversion_rate=0.12,
        commission_rate=0.10,
        tags=["高利润", "爆款"],
    )


class TestLLMCardIntegration:
    def test_deepseek_generates_valid_product_card(self, product):
        gen = LLMCardGenerator(settings=get_settings())
        card = gen.generate_card_with_fallback(product)
        assert card.product_id == "p001"
        assert len(card.talking_points) >= 2
        assert len(card.opening_script) > 0
        assert len(card.price_hint) > 0
        print(f"\ntalking_points: {card.talking_points}")
        print(f"opening_script: {card.opening_script}")
        print(f"price_hint: {card.price_hint}")

    def test_deepseek_card_differs_from_template(self, product):
        template_card = generate_product_card(product)
        gen = LLMCardGenerator(settings=get_settings())
        llm_card = gen.generate_card_with_fallback(product)
        # LLM 手卡和模板手卡的话术不应该完全相同
        assert llm_card.talking_points != template_card.talking_points

    def test_bad_api_key_falls_back_to_template(self, product):
        bad_gen = LLMCardGenerator(api_key="bad_key_123")
        card = bad_gen.generate_card_with_fallback(product)
        assert isinstance(card, type(generate_product_card(product)))
        assert card.product_id == "p001"
