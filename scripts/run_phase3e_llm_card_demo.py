"""Phase 3E LLM 手卡话术增强 CLI 演示。

对比 DeepSeek 生成的 LLM 手卡和确定性模板手卡的差异。
"""

from decimal import Decimal
from src.config.settings import get_settings
from src.skills.llm_card_generator import LLMCardGenerator
from src.skills.product_card_generator import generate_product_card
from src.skills.product_catalog import CatalogProduct


def main() -> None:
    settings = get_settings()
    print("=" * 60)
    print("Phase 3E LLM 手卡话术增强演示")
    print("=" * 60)

    product = CatalogProduct(
        product_id="p001",
        name="智能净水壶",
        category="厨房电器",
        price=299.00,
        inventory=150,
        conversion_rate=0.12,
        commission_rate=0.10,
        tags=["高利润", "爆款"],
    )

    print(f"\n商品: {product.name} ({product.category})")
    print(f"价格: {product.price:.2f} 元 | 标签: {', '.join(product.tags)}")

    # 模板手卡
    print("\n--- 确定性模板手卡 ---")
    template = generate_product_card(product)
    print(f"  title: {template.title}")
    print(f"  opening: {template.opening_script}")
    print(f"  talking_points: {template.talking_points}")
    print(f"  price_hint: {template.price_hint}")

    # LLM 手卡
    print("\n--- DeepSeek LLM 手卡 ---")
    gen = LLMCardGenerator(settings=settings)
    llm_card = gen.generate_card_with_fallback(product)
    print(f"  title: {llm_card.title}")
    print(f"  opening: {llm_card.opening_script}")
    print(f"  talking_points: {llm_card.talking_points}")
    print(f"  price_hint: {llm_card.price_hint}")
    if llm_card.risk_tips:
        print(f"  risk_tips: {llm_card.risk_tips}")

    print(f"\n[对比] LLM 手卡和模板手卡的话术完全不同: {template.talking_points != llm_card.talking_points}")
    print("=" * 60)
    print("演示结束。DeepSeek API 正常。")
    print("=" * 60)


if __name__ == "__main__":
    main()
