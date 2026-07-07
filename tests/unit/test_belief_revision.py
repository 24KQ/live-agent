"""Phase 3B 记忆冲突修正测试。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.memory.belief_revision import BeliefRevisionService, detect_conflicting_memories
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource, MemoryStatus


REFERENCE_TIME = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def make_preference_memory(
    key: str,
    category: str,
    *,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> AnchorMemoryEntry:
    """构造同一 conflict_group 下的类目偏好记忆。"""

    return AnchorMemoryEntry(
        memory_key=key,
        anchor_id="anchor-revision-001",
        room_id="room-revision-001",
        layer=MemoryLayer.L1,
        content=f"主播偏好 {category} 类商品",
        metadata={
            "conflict_group": "primary_category_strategy",
            "preferred_category": category,
        },
        confidence=Decimal("0.95"),
        evidence_weight=Decimal("0.90"),
        source=MemorySource.USER_STATED,
        status=status,
        created_at=REFERENCE_TIME - timedelta(days=10),
    )


def test_detect_conflicting_memories_only_matches_same_group_different_preference() -> None:
    """同组偏好字段不同才算冲突，已 suppressed 的旧记忆不应重复修正。"""

    old_home = make_preference_memory("old-home", "家居")
    old_kitchen = make_preference_memory("old-kitchen", "厨房")
    suppressed_sport = make_preference_memory("old-sport", "运动", status=MemoryStatus.SUPPRESSED)
    new_kitchen = make_preference_memory("new-kitchen", "厨房")

    conflicts = detect_conflicting_memories([old_home, old_kitchen, suppressed_sport], new_kitchen)

    assert [memory.memory_key for memory in conflicts] == ["old-home"]


def test_detect_conflicting_memories_treats_singular_and_plural_fields_as_aliases() -> None:
    """单数字段和复数字段表达同一类偏好时，也应能检测冲突。"""

    old_home = make_preference_memory("old-home", "家居")
    new_kitchen = make_preference_memory("new-kitchen", "厨房").model_copy(
        update={
            "metadata": {
                "conflict_group": "primary_category_strategy",
                "preferred_categories": ["厨房"],
            }
        }
    )

    conflicts = detect_conflicting_memories([old_home], new_kitchen)

    assert [memory.memory_key for memory in conflicts] == ["old-home"]


def test_belief_revision_suppresses_old_conflict_and_writes_new_memory() -> None:
    """冲突修正应保留旧记忆但标记 suppressed，再写入新的 active 记忆。"""

    class FakeStore:
        def __init__(self) -> None:
            self.memories = [make_preference_memory("old-home", "家居")]
            self.written: list[AnchorMemoryEntry] = []
            self.suppressed: list[tuple[str, str]] = []

        def list_memories(self, anchor_id: str, room_id: str | None = None, layer: MemoryLayer | None = None):
            assert anchor_id == "anchor-revision-001"
            assert room_id == "room-revision-001"
            return self.memories

        def suppress_memory(self, memory_key: str, reason: str) -> None:
            self.suppressed.append((memory_key, reason))

        def write_memory(self, entry: AnchorMemoryEntry) -> str:
            self.written.append(entry)
            return "new-memory-id"

        def revise_memories_atomically(self, new_entry: AnchorMemoryEntry, conflicts_with_reasons):
            for memory, reason in conflicts_with_reasons:
                self.suppress_memory(memory.memory_key, reason)
            return self.write_memory(new_entry)

    store = FakeStore()
    service = BeliefRevisionService(store)
    result = service.revise_preference(
        anchor_id="anchor-revision-001",
        room_id="room-revision-001",
        new_memory=make_preference_memory("new-kitchen", "厨房"),
        reason="主播复盘后确认厨房类商品更适合下一场。",
    )

    assert result.new_memory_id == "new-memory-id"
    assert result.suppressed_memory_keys == ["old-home"]
    assert store.suppressed[0][0] == "old-home"
    assert "primary_category_strategy" in store.suppressed[0][1]
    assert store.written[0].status == MemoryStatus.ACTIVE


def test_belief_revision_rejects_cross_anchor_or_room_memory() -> None:
    """新记忆必须属于当前主播和直播间，避免修正时把偏好串到其他画像。"""

    class EmptyStore:
        def list_memories(self, anchor_id: str, room_id: str | None = None, layer: MemoryLayer | None = None):
            return []

    service = BeliefRevisionService(EmptyStore())
    wrong_anchor = make_preference_memory("new-kitchen", "厨房").model_copy(update={"anchor_id": "other-anchor"})

    with pytest.raises(ValueError, match="anchor_id"):
        service.revise_preference(
            anchor_id="anchor-revision-001",
            room_id="room-revision-001",
            new_memory=wrong_anchor,
            reason="不允许跨主播修正。",
        )
