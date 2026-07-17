"""Phase 14 Task 9 PostgreSQL 资格事实和人工确认的 RED 契约。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import psycopg

from src.config.settings import get_settings
from src.memory.candidate_store import MemoryCandidate, PostgresMemoryCandidateStore
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.memory.memory_store import MemoryStore
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data


def test_postgres_review_feedback_survives_restart_and_replays_confirmation(monkeypatch) -> None:
    """真实 PostgreSQL 必须保存资格事实、人工身份和确认命令，重启后可重放。"""

    from src.decision_support.review_feedback import (
        PostgresDecisionTraceResolver,
        PostgresReviewFeedbackStore,
        ReviewFeedbackService,
    )
    from src.memory.promotion_policy import PromotionPolicy

    settings = get_settings()
    monkeypatch.setattr("src.skills.embedding_service.EmbeddingService.embed", lambda _self, _text: [])
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    candidate_store = PostgresMemoryCandidateStore(settings)
    candidate_store.initialize_schema()
    feedback_store = PostgresReviewFeedbackStore(settings)
    feedback_store.initialize_schema()
    scope = uuid4().hex
    anchor_id = f"anchor-phase14-{scope}"
    room_id = f"room-phase14-{scope}"
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO live_agent_anchors(anchor_id,display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (anchor_id, "Phase 14 test anchor"))
            cur.execute("INSERT INTO live_agent_live_rooms(room_id,anchor_id,title,lifecycle,scheduled_at) VALUES (%s,%s,%s,%s,NOW()) ON CONFLICT DO NOTHING", (room_id, anchor_id, "Phase 14 test room", "scheduled"))
        conn.commit()
    trace_ids = (f"phase14-trace-{scope}-a", f"phase14-trace-{scope}-b")
    trace_store = DecisionTraceStore(settings)
    for trace_id in trace_ids:
        trace_store.record_trace(
            DecisionTraceRecord(
                trace_id=trace_id,
                anchor_id=anchor_id,
                room_id=room_id,
                recommendation={"preferred_category": f"phase14-category-{scope}"},
                anchor_action=AnchorAction.ACCEPTED,
                business_result=BusinessResult.GOOD,
                final_trust_score=Decimal("0.90"),
            )
        )
    candidate = candidate_store.stage(
        MemoryCandidate(
            candidate_id=f"phase14-candidate-{scope}",
            idempotency_key=f"phase14-stage-{scope}",
            anchor_id=anchor_id,
            room_id=room_id,
            evidence_ids=trace_ids,
            preferred_category=f"phase14-category-{scope}",
            preferred_tags=("利润款",),
            preferred_product_ids=("p003",),
            confidence=Decimal("0.90"),
        )
    )
    service = ReviewFeedbackService(
        candidate_store=candidate_store,
        feedback_store=feedback_store,
        promotion_policy=PromotionPolicy(
            store=candidate_store,
            active_memory_port=MemoryStore(settings),
            eligibility_store=feedback_store,
            decision_trace_resolver=PostgresDecisionTraceResolver(settings),
        ),
        decision_trace_resolver=PostgresDecisionTraceResolver(settings),
    )

    result = service.evaluate_eligibility(
        command_id=f"phase14-eligibility-{scope}",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=trace_ids,
        product_whitelist={"p003"},
    )

    assert result.status.value == "ELIGIBLE_AWAITING_OPERATOR"
    assert feedback_store.get_eligibility(candidate.candidate_id) == result
    assert candidate_store.get(candidate.candidate_id).status.value == "ELIGIBLE_AWAITING_OPERATOR"

    confirmed = service.confirm_promotion(
        command_id=f"phase14-confirm-{scope}",
        candidate_id=candidate.candidate_id,
        expected_version=result.version,
        operator_id="operator-phase14",
    )
    assert confirmed.status.value == "APPLIED"
    memories = MemoryStore(settings).list_memories(candidate.anchor_id, candidate.room_id)
    assert any(item.metadata.get("promotion_candidate_id") == candidate.candidate_id for item in memories)

    # 模拟服务重启：新的 Store/Policy 只能通过确认账本重放，不再次写 active memory。
    restarted_feedback_store = PostgresReviewFeedbackStore(settings)
    restarted_service = ReviewFeedbackService(
        candidate_store=PostgresMemoryCandidateStore(settings),
        feedback_store=restarted_feedback_store,
        promotion_policy=PromotionPolicy(
            store=PostgresMemoryCandidateStore(settings),
            active_memory_port=MemoryStore(settings),
            eligibility_store=restarted_feedback_store,
            decision_trace_resolver=PostgresDecisionTraceResolver(settings),
        ),
        decision_trace_resolver=PostgresDecisionTraceResolver(settings),
    )
    replay = restarted_service.confirm_promotion(
        command_id=f"phase14-confirm-{scope}",
        candidate_id=candidate.candidate_id,
        expected_version=result.version,
        operator_id="operator-phase14",
    )
    assert replay == confirmed
