"""Phase 3B 增强记忆检索测试。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.memory.memory_retrieval import MemoryHit, MemoryRetriever, rank_memory_hits
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, MemoryStatus
from src.skills.product_catalog import CatalogProduct
from src.memory.memory_aware_plan import apply_memory_hits_to_live_plan


REFERENCE_TIME = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def make_memory(
    key: str,
    *,
    layer: MemoryLayer,
    room_id: str | None,
    preferred_category: str,
    created_days_ago: int,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> AnchorMemoryEntry:
    """构造带结构化偏好的记忆，便于验证排序和排品理由。"""

    return AnchorMemoryEntry(
        memory_key=key,
        anchor_id="anchor-retrieval-001",
        room_id=room_id,
        layer=layer,
        content=f"这段完整记忆正文不应出现在排品理由里：{preferred_category}",
        metadata={"preferred_category": preferred_category},
        confidence=Decimal("0.90"),
        evidence_weight=Decimal("0.80"),
        source=MemorySource.SYSTEM_OBSERVED,
        status=status,
        created_at=REFERENCE_TIME - timedelta(days=created_days_ago),
    )


def make_product(product_id: str, category: str, tags: list[str]) -> CatalogProduct:
    """构造最小商品模型，验证记忆命中能改变排品顺序。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"样例商品 {product_id}",
        category=category,
        price=Decimal("99.00"),
        inventory=30,
        conversion_rate=Decimal("0.10"),
        commission_rate=Decimal("0.20"),
        tags=tags,
        selling_points=["稳定卖点"],
        is_active=True,
    )


def test_rank_memory_hits_prefers_room_match_and_fresh_active_memory() -> None:
    """增强检索应优先返回当前直播间、新鲜且 active 的记忆。"""

    memories = [
        make_memory(
            "old-anchor-memory",
            layer=MemoryLayer.L2,
            room_id=None,
            preferred_category="家居",
            created_days_ago=180,
        ),
        make_memory(
            "fresh-room-memory",
            layer=MemoryLayer.L2,
            room_id="room-retrieval-001",
            preferred_category="厨房",
            created_days_ago=2,
        ),
        make_memory(
            "suppressed-room-memory",
            layer=MemoryLayer.L1,
            room_id="room-retrieval-001",
            preferred_category="家电",
            created_days_ago=1,
            status=MemoryStatus.SUPPRESSED,
        ),
    ]

    hits = rank_memory_hits(memories, room_id="room-retrieval-001", reference_time=REFERENCE_TIME)

    assert [hit.memory.memory_key for hit in hits] == [
        "fresh-room-memory",
        "old-anchor-memory",
        "suppressed-room-memory",
    ]
    assert hits[0].effective_weight > hits[-1].effective_weight
    assert "完整记忆正文" not in hits[0].explanation


def test_rank_memory_hits_prefers_newer_memory_when_score_ties() -> None:
    """排序分完全相同时，应把更新的记忆排在旧记忆前面。"""

    older = make_memory(
        "older-memory",
        layer=MemoryLayer.L2,
        room_id="room-retrieval-001",
        preferred_category="厨房",
        created_days_ago=30,
    )
    newer = make_memory(
        "newer-memory",
        layer=MemoryLayer.L2,
        room_id="room-retrieval-001",
        preferred_category="厨房",
        created_days_ago=1,
    )

    class TieDecayPolicy:
        def effective_weight(self, memory, *, reference_time=None):
            return Decimal("0.5000")

    hits = rank_memory_hits(
        [older, newer],
        room_id="room-retrieval-001",
        reference_time=REFERENCE_TIME,
        decay_policy=TieDecayPolicy(),
    )

    assert [hit.memory.memory_key for hit in hits] == ["newer-memory", "older-memory"]


def test_memory_retriever_uses_store_and_returns_structured_hits() -> None:
    """MemoryRetriever 应把 Store 查询结果转换成可解释的检索命中。"""

    class FakeStore:
        def list_memories(self, anchor_id: str, room_id: str | None = None, layer: MemoryLayer | None = None):
            assert anchor_id == "anchor-retrieval-001"
            assert room_id == "room-retrieval-001"
            assert layer is None
            return [
                make_memory(
                    "fresh-room-memory",
                    layer=MemoryLayer.L1,
                    room_id=room_id,
                    preferred_category="厨房",
                    created_days_ago=1,
                )
            ]

    hits = MemoryRetriever(FakeStore()).retrieve(
        anchor_id="anchor-retrieval-001",
        room_id="room-retrieval-001",
        reference_time=REFERENCE_TIME,
    )

    assert isinstance(hits[0], MemoryHit)
    assert hits[0].memory.memory_key == "fresh-room-memory"
    assert hits[0].relevance_score > Decimal("0.00")
    assert "L1" in hits[0].explanation


def test_apply_memory_hits_to_live_plan_uses_effective_weight_and_hides_raw_content() -> None:
    """排品可以消费增强检索结果，并且理由中不能泄漏完整记忆正文。"""

    products = [
        make_product("p-home", "家居", ["引流款"]),
        make_product("p-kitchen", "厨房", ["利润款"]),
    ]
    hit = rank_memory_hits(
        [
            make_memory(
                "fresh-kitchen-memory",
                layer=MemoryLayer.L1,
                room_id="room-retrieval-001",
                preferred_category="厨房",
                created_days_ago=1,
            )
        ],
        room_id="room-retrieval-001",
        reference_time=REFERENCE_TIME,
    )[0]

    plan = apply_memory_hits_to_live_plan(
        room_id="room-retrieval-001",
        products=products,
        trace_id="trace-retrieval-plan",
        memory_hits=[hit],
    )

    assert plan.items[0].product_id == "p-kitchen"
    assert "记忆影响" in plan.items[0].reason
    assert "完整记忆正文" not in plan.items[0].reason
