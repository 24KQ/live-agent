"""Phase 3B 记忆修正闭环集成测试。"""

from decimal import Decimal

import pytest
import psycopg

from src.config.settings import get_settings
from src.memory.belief_revision import BeliefRevisionService
from src.memory.decision_memory_feedback import DecisionTraceMemoryFeedbackService
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.memory.demo_memory_seed_phase3b import (
    PHASE3B_ANCHOR_ID,
    PHASE3B_ROOM_ID,
    initialize_phase3b_demo_data,
)
from src.memory.memory_aware_plan import MemoryAwarePlanService
from src.memory.memory_retrieval import MemoryRetriever
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorAction, AnchorMemoryEntry, BusinessResult, MemoryLayer, MemorySource, MemoryStatus
from src.skills.embedding_service import EmbeddingService
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data
from src.skills.product_catalog import ProductCatalogRepository


def test_memory_revision_flow_suppresses_old_preference_and_changes_next_plan() -> None:
    """端到端验证：旧偏好影响首次排品，新反馈修正旧记忆，下一轮排品转向新偏好。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    initialize_phase3b_demo_data(settings)

    memory_store = MemoryStore(settings)
    plan_service = MemoryAwarePlanService(memory_store)
    products = ProductCatalogRepository(settings).list_room_products(PHASE3B_ROOM_ID)

    first_plan = plan_service.generate_plan(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        products=products,
        trace_id="trace-phase3b-before-revision",
    )

    feedback_service = DecisionTraceMemoryFeedbackService(memory_store)
    new_memory = feedback_service.build_feedback_memory(
        trace_id="trace-phase3b-feedback",
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        anchor_action=AnchorAction.ACCEPTED,
        business_result=BusinessResult.GOOD,
        recommendation={
            "preferred_category": "厨房",
            "preferred_product_ids": ["p003"],
            "preferred_tags": ["利润款"],
            "conflict_group": "primary_category_strategy",
        },
        lift=Decimal("0.18"),
        catalog_products=products,
    )
    revision = BeliefRevisionService(memory_store).revise_preference(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        new_memory=new_memory,
        reason="Phase 3B 集成测试：主播采纳厨房类策略且效果好。",
    )
    second_plan = plan_service.generate_plan(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        products=products,
        trace_id="trace-phase3b-after-revision",
    )
    memories = memory_store.list_memories(PHASE3B_ANCHOR_ID, PHASE3B_ROOM_ID)
    old_memory = next(memory for memory in memories if memory.memory_key == "phase3b-old-home-preference")
    hits = MemoryRetriever(memory_store).retrieve(
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
    )

    assert first_plan.items[0].product_id == "p001"
    assert revision.suppressed_memory_keys == ["phase3b-old-home-preference"]
    assert old_memory.status == MemoryStatus.SUPPRESSED
    assert old_memory.suppressed_reason is not None
    assert second_plan.items[0].product_id == "p003"
    assert hits[0].memory.memory_key == new_memory.memory_key


def test_memory_key_cannot_move_between_rooms_for_same_anchor() -> None:
    """同一主播下相同 memory_key 不能从一个直播间被 upsert 到另一个直播间。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    initialize_phase3b_demo_data(settings)
    _insert_second_phase3b_room(settings, "room-phase3b-002")

    memory_store = MemoryStore(settings)
    original = next(
        memory
        for memory in memory_store.list_memories(PHASE3B_ANCHOR_ID, PHASE3B_ROOM_ID)
        if memory.memory_key == "phase3b-old-home-preference"
    )

    with pytest.raises(ValueError, match="room_id"):
        memory_store.write_memory(original.model_copy(update={"room_id": "room-phase3b-002"}))

    reloaded = next(
        memory
        for memory in memory_store.list_memories(PHASE3B_ANCHOR_ID, PHASE3B_ROOM_ID)
        if memory.memory_key == "phase3b-old-home-preference"
    )
    assert reloaded.room_id == PHASE3B_ROOM_ID
    assert reloaded.status == MemoryStatus.ACTIVE


def test_revision_rolls_back_suppression_when_new_memory_write_fails(monkeypatch) -> None:
    """新记忆写入失败时，旧记忆不能被永久压制。"""

    # 本用例只验证 JSON 序列化异常触发的数据库回滚；固定本地 embedding 结果，避免
    # 测试路径访问外部模型端点，也避免网络等待掩盖真正要断言的事务语义。
    monkeypatch.setattr(EmbeddingService, "embed", lambda _self, _content: [])
    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    initialize_phase3b_demo_data(settings)

    memory_store = MemoryStore(settings)
    invalid_memory = AnchorMemoryEntry(
        memory_key="phase3b-invalid-json-feedback",
        anchor_id=PHASE3B_ANCHOR_ID,
        room_id=PHASE3B_ROOM_ID,
        layer=MemoryLayer.L2,
        content="这条记忆故意包含不可 JSON 序列化 metadata，用于验证事务回滚。",
        metadata={
            "conflict_group": "primary_category_strategy",
            "preferred_category": "厨房",
            "bad_object": object(),
        },
        confidence=Decimal("0.90"),
        evidence_weight=Decimal("0.80"),
        source=MemorySource.SYSTEM_OBSERVED,
    )

    with pytest.raises(TypeError):
        BeliefRevisionService(memory_store).revise_preference(
            anchor_id=PHASE3B_ANCHOR_ID,
            room_id=PHASE3B_ROOM_ID,
            new_memory=invalid_memory,
            reason="验证原子事务回滚。",
        )

    old_memory = next(
        memory
        for memory in memory_store.list_memories(PHASE3B_ANCHOR_ID, PHASE3B_ROOM_ID)
        if memory.memory_key == "phase3b-old-home-preference"
    )
    assert old_memory.status == MemoryStatus.ACTIVE
    assert old_memory.suppressed_reason is None


def _insert_second_phase3b_room(settings, room_id: str) -> None:
    """插入同主播的第二个脱敏直播间，用于验证 memory_key 不能跨房间移动。"""

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO live_agent_live_rooms(room_id, anchor_id, title, lifecycle, scheduled_at)
                VALUES (%(room_id)s, %(anchor_id)s, 'Phase 3B 第二样例场', 'PRE_LIVE', NOW())
                ON CONFLICT (room_id)
                DO UPDATE SET anchor_id = EXCLUDED.anchor_id, title = EXCLUDED.title;
                """,
                {"room_id": room_id, "anchor_id": PHASE3B_ANCHOR_ID},
            )
        connection.commit()
