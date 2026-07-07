"""Phase 3B Decision Trace 反馈入记忆测试。"""

from decimal import Decimal

from src.memory.decision_memory_feedback import DecisionTraceMemoryFeedbackService
from src.memory.models import AnchorAction, BusinessResult
from src.skills.product_catalog import CatalogProduct
import pytest


def make_product(product_id: str, category: str, tags: list[str]) -> CatalogProduct:
    """构造用于值级白名单校验的商品目录项。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"样例商品 {product_id}",
        category=category,
        price=Decimal("99.00"),
        inventory=10,
        conversion_rate=Decimal("0.10"),
        commission_rate=Decimal("0.20"),
        tags=tags,
        selling_points=["稳定卖点"],
        is_active=True,
    )


def test_feedback_memory_sanitizes_values_against_catalog() -> None:
    """反馈记忆不仅要校验字段名，还要按商品目录过滤字段值。"""

    memory = DecisionTraceMemoryFeedbackService().build_feedback_memory(
        trace_id="trace-feedback-safe",
        anchor_id="anchor-feedback-001",
        room_id="room-feedback-001",
        anchor_action=AnchorAction.ACCEPTED,
        business_result=BusinessResult.GOOD,
        recommendation={
            "preferred_category": "厨房",
            "preferred_tags": ["利润款", "token-secret-should-drop"],
            "preferred_product_ids": ["p003", "C:\\secret\\token"],
            "conflict_group": "primary_category_strategy",
            "raw_script": "这段完整话术不允许进入长期记忆",
        },
        lift=Decimal("0.18"),
        catalog_products=[make_product("p003", "厨房", ["利润款"])],
    )

    assert memory.metadata["preferred_category"] == "厨房"
    assert memory.metadata["preferred_tags"] == ["利润款"]
    assert memory.metadata["preferred_product_ids"] == ["p003"]
    assert "raw_script" not in memory.metadata
    assert "token-secret-should-drop" not in str(memory.metadata)
    assert "C:\\secret\\token" not in str(memory.metadata)


def test_feedback_memory_rejects_missing_catalog_products() -> None:
    """缺少货盘时必须 fail-closed，不能让任意短字符串进入长期记忆。"""

    with pytest.raises(ValueError, match="catalog_products"):
        DecisionTraceMemoryFeedbackService().build_feedback_memory(
            trace_id="trace-feedback-no-catalog",
            anchor_id="anchor-feedback-001",
            room_id="room-feedback-001",
            anchor_action=AnchorAction.ACCEPTED,
            business_result=BusinessResult.GOOD,
            recommendation={
                "preferred_category": "任意类目",
                "preferred_tags": ["任意标签"],
                "preferred_product_ids": ["任意商品"],
                "conflict_group": "primary_category_strategy",
            },
            lift=Decimal("0.18"),
        )
