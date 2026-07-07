"""Phase 3A 记忆感知排品测试。"""

from decimal import Decimal

from src.memory.memory_aware_plan import apply_memory_to_live_plan
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource
from src.skills.product_catalog import CatalogProduct


def make_product(
    product_id: str,
    category: str,
    tags: list[str],
    conversion_rate: str,
    commission_rate: str,
) -> CatalogProduct:
    """构造最小可用商品，便于测试排品顺序是否受记忆影响。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"样例商品 {product_id}",
        category=category,
        price=Decimal("99.00"),
        inventory=30,
        conversion_rate=Decimal(conversion_rate),
        commission_rate=Decimal(commission_rate),
        tags=tags,
        selling_points=["稳定卖点"],
        is_active=True,
    )


def test_memory_aware_plan_boosts_preferred_category_and_explains_reason() -> None:
    """当主播偏好厨房高利润商品时，相关商品应前移且理由中包含记忆来源。"""

    products = [
        make_product("p-traffic", "家居", ["引流款"], "0.30", "0.05"),
        make_product("p-profit", "厨房", ["利润款"], "0.08", "0.35"),
    ]
    memories = [
        AnchorMemoryEntry(
            memory_key="anchor-001-pref-kitchen",
            anchor_id="anchor-001",
            room_id="room-001",
            layer=MemoryLayer.L1,
            content="主播明确偏好优先讲厨房类高利润商品",
            metadata={"preferred_category": "厨房", "preferred_tags": ["利润款"]},
            confidence=Decimal("0.95"),
            evidence_weight=Decimal("0.90"),
            source=MemorySource.USER_STATED,
        )
    ]

    plan = apply_memory_to_live_plan(
        room_id="room-001",
        products=products,
        trace_id="trace-memory-plan",
        memories=memories,
    )

    assert plan.items[0].product_id == "p-profit"
    assert "记忆影响" in plan.items[0].reason
    assert "L1" in plan.items[0].reason
    assert "user_stated" in plan.items[0].reason
    assert "主播明确偏好优先讲厨房类高利润商品" not in plan.items[0].reason


def test_memory_aware_plan_keeps_base_order_without_memory() -> None:
    """没有可用记忆时，应退回既有确定性排品结果。"""

    products = [
        make_product("p-traffic", "家居", ["引流款"], "0.30", "0.05"),
        make_product("p-profit", "厨房", ["利润款"], "0.08", "0.35"),
    ]

    plan = apply_memory_to_live_plan(
        room_id="room-001",
        products=products,
        trace_id="trace-no-memory-plan",
        memories=[],
    )

    assert plan.items[0].product_id == "p-traffic"
