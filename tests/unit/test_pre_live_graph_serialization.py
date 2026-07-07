"""Phase 2E 播前 Graph 可序列化快照测试。

这些测试先约束 checkpoint 前必须满足的状态形态：LangGraph state 只能保存
JSON 可序列化数据，不能把 Pydantic 对象直接塞进 PostgreSQL checkpoint。
"""

from __future__ import annotations

import json
from decimal import Decimal

from src.core.pre_live_graph import (
    card_from_snapshot,
    card_to_snapshot,
    plan_from_snapshot,
    plan_to_snapshot,
    product_from_snapshot,
    product_to_snapshot,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import ProductCard
from src.skills.product_catalog import CatalogProduct


def _sample_product() -> CatalogProduct:
    """构造一个带 Decimal 字段的样例商品，用于验证 JSON 快照不会丢精度。"""

    return CatalogProduct(
        product_id="p001",
        name="轻盈保温杯",
        category="日用品",
        price=Decimal("89.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流"],
        selling_points=["保温稳定", "杯身轻巧", "适合通勤"],
    )


def test_product_snapshot_round_trips_and_is_json_serializable() -> None:
    """商品快照应可 JSON 序列化，并能恢复成原领域模型。"""

    product = _sample_product()

    snapshot = product_to_snapshot(product)

    assert json.loads(json.dumps(snapshot, ensure_ascii=False)) == snapshot
    assert product_from_snapshot(snapshot) == product
    assert snapshot["price"] == "89.90"


def test_plan_and_card_snapshots_round_trip_without_pydantic_objects() -> None:
    """排品和手卡快照不能携带 Pydantic 对象，便于 PostgresSaver 持久化。"""

    plan = LivePlanDraft(
        room_id="room-demo-001",
        trace_id="trace-phase2e-serialization",
        items=[
            LivePlanItem(
                rank=1,
                product_id="p001",
                product_name="轻盈保温杯",
                role="引流款",
                reason="确定性测试理由",
            )
        ],
    )
    card = ProductCard(
        product_id="p001",
        title="轻盈保温杯｜日用品手卡",
        talking_points=["保温稳定", "杯身轻巧", "适合通勤"],
        opening_script="接下来介绍轻盈保温杯。",
        price_hint="以直播间当前展示为准。",
        risk_tips=["避免绝对化承诺。"],
    )

    plan_snapshot = plan_to_snapshot(plan)
    card_snapshot = card_to_snapshot(card)

    json.dumps({"plan": plan_snapshot, "card": card_snapshot}, ensure_ascii=False)
    assert plan_from_snapshot(plan_snapshot) == plan
    assert card_from_snapshot(card_snapshot) == card
    assert not isinstance(plan_snapshot["items"][0], LivePlanItem)
    assert not isinstance(card_snapshot, ProductCard)

