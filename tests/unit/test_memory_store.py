"""Phase 3A MemoryStore 单元测试。

这里不连接真实 PostgreSQL，只验证 Store 暴露的 SQL 边界和输入校验会按预期工作；
真实写入、查询和 upsert 行为放到集成测试覆盖。
"""

from decimal import Decimal

import pytest

from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource


def test_memory_store_requires_valid_entry_before_write() -> None:
    """Store 写入入口必须复用模型校验，不能让空 anchor_id 或空 content 进入数据库层。"""

    invalid_entry = AnchorMemoryEntry.model_construct(
        anchor_id="",
        layer=MemoryLayer.L1,
        content="",
        source=MemorySource.USER_STATED,
    )

    with pytest.raises(ValueError):
        MemoryStore(settings=None).write_memory(invalid_entry)


def test_memory_store_builds_deterministic_filters() -> None:
    """按主播、直播间和层级查询时，应生成稳定的参数字典，避免手写拼接 SQL。"""

    filters = MemoryStore.build_query_filters(
        anchor_id="anchor-001",
        room_id="room-001",
        layer=MemoryLayer.L2,
    )

    assert filters == {
        "anchor_id": "anchor-001",
        "room_id": "room-001",
        "layer": "L2",
    }


def test_memory_store_rejects_empty_anchor_filter() -> None:
    """查询入口也必须 fail-closed，避免空主播查询误扫全库记忆。"""

    with pytest.raises(ValueError, match="anchor_id"):
        MemoryStore.build_query_filters(anchor_id="", room_id=None, layer=None)


def test_memory_store_accepts_weighted_memory_entry() -> None:
    """带置信度和证据权重的记忆应能作为 Store 写入对象，供排品策略解释使用。"""

    entry = AnchorMemoryEntry(
        memory_key="anchor-001-pref-kitchen",
        anchor_id="anchor-001",
        room_id="room-001",
        layer=MemoryLayer.L1,
        content="主播偏好先讲厨房高利润商品",
        metadata={"preferred_category": "厨房", "preferred_tags": ["利润款"]},
        confidence=Decimal("0.90"),
        evidence_weight=Decimal("0.80"),
        source=MemorySource.USER_STATED,
    )

    assert entry.memory_key == "anchor-001-pref-kitchen"
    assert entry.metadata["preferred_category"] == "厨房"
