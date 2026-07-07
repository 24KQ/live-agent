"""Phase 3A 记忆与信任闭环集成测试。"""

from decimal import Decimal
from uuid import uuid4

import pytest
import psycopg

from src.config.settings import get_settings
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.demo_memory_seed import initialize_phase3_schema, seed_phase3_memory_demo_data
from src.memory.memory_aware_plan import MemoryAwarePlanService
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource
from src.memory.trust_manager import TrustManager
from src.skills.demo_data_seed import (
    DEMO_ANCHOR_ID,
    DEMO_ROOM_ID,
    initialize_phase2_schema,
    seed_phase2_demo_data,
)
from src.skills.product_catalog import ProductCatalogRepository


def test_memory_trust_flow_updates_score_and_records_decision_trace() -> None:
    """完整验证：读取记忆 -> 生成记忆排品 -> 记录反馈 -> 更新 trust_score -> 再次排品受影响。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    seed_phase3_memory_demo_data(settings)

    trace_id = "trace-phase3a-integration"
    memory_store = MemoryStore(settings)
    trace_store = DecisionTraceStore(settings)
    plan_service = MemoryAwarePlanService(memory_store)
    products = ProductCatalogRepository(settings).list_room_products(DEMO_ROOM_ID)
    original_trust = memory_store.get_trust_state(DEMO_ANCHOR_ID)

    first_plan = plan_service.generate_plan(
        anchor_id=DEMO_ANCHOR_ID,
        room_id=DEMO_ROOM_ID,
        products=products,
        trace_id=trace_id,
    )
    update = TrustManager().apply_feedback(
        original_trust,
        AnchorAction.ACCEPTED,
        BusinessResult.GOOD,
    )
    memory_store.upsert_trust_state(update.new_state)
    decision_id = trace_store.record_trace(
        DecisionTraceRecord(
            trace_id=trace_id,
            anchor_id=DEMO_ANCHOR_ID,
            room_id=DEMO_ROOM_ID,
            recommendation={"first_product_id": first_plan.items[0].product_id},
            anchor_action=AnchorAction.ACCEPTED,
            business_result=BusinessResult.GOOD,
            lift=Decimal("0.12"),
            trust_delta=update.trust_delta,
            final_trust_score=update.new_state.trust_score,
        )
    )
    second_plan = plan_service.generate_plan(
        anchor_id=DEMO_ANCHOR_ID,
        room_id=DEMO_ROOM_ID,
        products=products,
        trace_id=f"{trace_id}-second",
    )
    traces = trace_store.list_traces(trace_id)

    assert decision_id
    assert update.new_state.trust_score == original_trust.trust_score + Decimal("0.05")
    assert traces[0].trace_id == trace_id
    assert traces[0].final_trust_score == update.new_state.trust_score
    assert "记忆影响" in first_plan.items[0].reason
    assert second_plan.items[0].product_id == first_plan.items[0].product_id


def test_memory_store_rejects_reusing_memory_key_for_another_anchor() -> None:
    """同一个 memory_key 不能被移动到其他主播名下，避免记忆串号污染排品。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    seed_phase3_memory_demo_data(settings)

    store = MemoryStore(settings)

    with pytest.raises(ValueError, match="memory_key"):
        store.write_memory(
            store.list_memories(anchor_id=DEMO_ANCHOR_ID)[0].model_copy(
                update={
                    "anchor_id": "anchor-other-001",
                    "memory_id": None,
                }
            )
        )


def test_decision_trace_store_rejects_rewriting_existing_trace() -> None:
    """同一 trace_id 的 Decision Trace 只能幂等复用相同内容，不能被不同反馈覆盖。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)

    trace_store = DecisionTraceStore(settings)
    record = DecisionTraceRecord(
        trace_id=f"trace-phase3a-no-rewrite-{uuid4().hex}",
        anchor_id=DEMO_ANCHOR_ID,
        room_id=DEMO_ROOM_ID,
        recommendation={"first_product_id": "p003"},
        anchor_action=AnchorAction.ACCEPTED,
        business_result=BusinessResult.GOOD,
        lift=Decimal("0.12"),
        trust_delta=Decimal("0.05"),
        final_trust_score=Decimal("0.75"),
    )
    first_id = trace_store.record_trace(record)
    same_id = trace_store.record_trace(record)

    with pytest.raises(ValueError, match="trace_id"):
        trace_store.record_trace(
            record.model_copy(
                update={
                    "anchor_action": AnchorAction.REJECTED,
                    "business_result": BusinessResult.ANCHOR_RIGHT,
                    "trust_delta": Decimal("-0.05"),
                    "final_trust_score": Decimal("0.65"),
                }
            )
        )

    assert same_id == first_id


def test_memory_and_trace_reject_room_anchor_mismatch() -> None:
    """记忆和决策轨迹都不能把其他主播的 room_id 绑定到当前主播。"""

    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    _insert_other_anchor(settings, "anchor-other-001")

    memory_store = MemoryStore(settings)
    trace_store = DecisionTraceStore(settings)

    with pytest.raises(ValueError, match="room_id"):
        memory_store.write_memory(
            AnchorMemoryEntry(
                memory_key=f"anchor-other-mismatch-{uuid4().hex}",
                anchor_id="anchor-other-001",
                room_id=DEMO_ROOM_ID,
                layer=MemoryLayer.L1,
                content="其他主播不能绑定样例直播间记忆",
                source=MemorySource.USER_STATED,
            )
        )

    with pytest.raises(ValueError, match="room_id"):
        trace_store.record_trace(
            DecisionTraceRecord(
                trace_id=f"trace-phase3a-room-mismatch-{uuid4().hex}",
                anchor_id="anchor-other-001",
                room_id=DEMO_ROOM_ID,
                recommendation={"first_product_id": "p003"},
                anchor_action=AnchorAction.ACCEPTED,
                business_result=BusinessResult.GOOD,
                lift=Decimal("0.12"),
                trust_delta=Decimal("0.05"),
                final_trust_score=Decimal("0.75"),
            )
        )


def _insert_other_anchor(settings, anchor_id: str) -> None:
    """插入一个额外脱敏主播，用于验证 room_id 与 anchor_id 的组合一致性。"""

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO live_agent_anchors(anchor_id, display_name, style_tags)
                VALUES (%(anchor_id)s, 'Other Demo Anchor', '[]'::jsonb)
                ON CONFLICT (anchor_id)
                DO UPDATE SET display_name = EXCLUDED.display_name;
                """,
                {"anchor_id": anchor_id},
            )
        connection.commit()
