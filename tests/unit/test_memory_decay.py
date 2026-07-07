"""Phase 3B 记忆衰减规则测试。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.memory.memory_decay import MemoryDecayPolicy
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, MemoryStatus


def make_memory(
    *,
    layer: MemoryLayer,
    created_days_ago: int,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> AnchorMemoryEntry:
    """构造只关注衰减字段的记忆，避免测试依赖数据库或排品实现。"""

    reference_time = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    return AnchorMemoryEntry(
        memory_key=f"memory-{layer.value}-{created_days_ago}-{status.value}",
        anchor_id="anchor-decay-001",
        room_id="room-decay-001",
        layer=layer,
        content="脱敏记忆内容",
        metadata={"preferred_category": "厨房"},
        confidence=Decimal("0.90"),
        evidence_weight=Decimal("0.80"),
        source=MemorySource.SYSTEM_OBSERVED,
        status=status,
        created_at=reference_time - timedelta(days=created_days_ago),
    )


def test_newer_memory_has_higher_effective_weight_than_older_memory() -> None:
    """同等证据下，新记忆的有效权重应高于旧记忆。"""

    policy = MemoryDecayPolicy()
    reference_time = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    new_memory = make_memory(layer=MemoryLayer.L2, created_days_ago=1)
    old_memory = make_memory(layer=MemoryLayer.L2, created_days_ago=120)

    new_weight = policy.effective_weight(new_memory, reference_time=reference_time)
    old_weight = policy.effective_weight(old_memory, reference_time=reference_time)

    assert new_weight > old_weight
    assert new_weight <= Decimal("0.72")


def test_l1_memory_decays_slower_than_l2_and_l3_memory() -> None:
    """同样距今天数下，主播显式偏好 L1 应比系统归纳 L2/L3 更抗衰减。"""

    policy = MemoryDecayPolicy()
    reference_time = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    l1 = make_memory(layer=MemoryLayer.L1, created_days_ago=90)
    l2 = make_memory(layer=MemoryLayer.L2, created_days_ago=90)
    l3 = make_memory(layer=MemoryLayer.L3, created_days_ago=90)

    assert policy.effective_weight(l1, reference_time=reference_time) > policy.effective_weight(
        l2,
        reference_time=reference_time,
    )
    assert policy.effective_weight(l2, reference_time=reference_time) > policy.effective_weight(
        l3,
        reference_time=reference_time,
    )


def test_suppressed_memory_keeps_audit_visibility_but_loses_most_impact() -> None:
    """被冲突修正压低的旧记忆仍可回放，但不能继续强烈影响排品。"""

    policy = MemoryDecayPolicy()
    reference_time = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    active = make_memory(layer=MemoryLayer.L1, created_days_ago=1)
    suppressed = make_memory(layer=MemoryLayer.L1, created_days_ago=1, status=MemoryStatus.SUPPRESSED)

    assert policy.effective_weight(suppressed, reference_time=reference_time) < (
        policy.effective_weight(active, reference_time=reference_time) * Decimal("0.25")
    )
    assert policy.effective_weight(suppressed, reference_time=reference_time) > Decimal("0.00")
