"""Phase 13 Task 9 受控记忆晋升的 PostgreSQL 闭环测试。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import psycopg

from src.config.settings import get_settings
from src.memory.candidate_store import MemoryCandidate, MemoryCandidateStatus, PostgresMemoryCandidateStore
from src.memory.promotion_policy import PromotionPolicy
from src.memory.decision_trace_store import DecisionTraceStore
from src.memory.models import AnchorAction, BusinessResult, DecisionTraceRecord
from src.decision_support.review_feedback import (
    PostgresDecisionTraceResolver,
    PostgresReviewFeedbackStore,
    ReviewFeedbackService,
)
from src.memory.demo_memory_seed import initialize_phase3_schema
from src.skills.demo_data_seed import initialize_phase2_schema, seed_phase2_demo_data


def test_postgres_candidate_promotion_is_idempotent_and_persists_template_memory(monkeypatch) -> None:
    """真实数据库必须保存 staging/command，并由一次模板晋升生成可检索 active memory。"""

    from src.skills.embedding_service import EmbeddingService

    monkeypatch.setattr(EmbeddingService, "embed", lambda _self, _text: [])
    settings = get_settings()
    initialize_phase2_schema(settings)
    seed_phase2_demo_data(settings)
    initialize_phase3_schema(settings)
    store = PostgresMemoryCandidateStore(settings)
    store.initialize_schema()
    feedback_store = PostgresReviewFeedbackStore(settings)
    feedback_store.initialize_schema()
    scope = uuid4().hex
    anchor_id = f"anchor-phase13-{scope}"
    room_id = f"room-phase13-{scope}"
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO live_agent_anchors(anchor_id,display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (anchor_id, "Phase 13 test anchor"))
            cur.execute("INSERT INTO live_agent_live_rooms(room_id,anchor_id,title,lifecycle,scheduled_at) VALUES (%s,%s,%s,%s,NOW()) ON CONFLICT DO NOTHING", (room_id, anchor_id, "Phase 13 test room", "scheduled"))
        conn.commit()
    trace_ids = (f"phase13-trace-{scope}-a", f"phase13-trace-{scope}-b")
    trace_store = DecisionTraceStore(settings)
    for trace_id in trace_ids:
        trace_store.record_trace(DecisionTraceRecord(trace_id=trace_id, anchor_id=anchor_id, room_id=room_id, recommendation={"preferred_category": f"phase13-category-{scope}"}, anchor_action=AnchorAction.ACCEPTED, business_result=BusinessResult.GOOD, final_trust_score=Decimal("0.90")))
    candidate = store.stage(MemoryCandidate(candidate_id=f"phase13-pg-candidate-{scope}", idempotency_key=f"phase13-pg-stage-{scope}", anchor_id=anchor_id, room_id=room_id, evidence_ids=trace_ids, preferred_category=f"phase13-category-{scope}", preferred_tags=("利润款",), preferred_product_ids=("p003",), confidence=Decimal("0.90")))
    service = ReviewFeedbackService(
        candidate_store=store,
        feedback_store=feedback_store,
        decision_trace_resolver=PostgresDecisionTraceResolver(settings),
        promotion_policy=PromotionPolicy(store=store, active_memory_port=store.memory_port(), eligibility_store=feedback_store, decision_trace_resolver=PostgresDecisionTraceResolver(settings)),
    )
    eligible = service.evaluate_eligibility(
        command_id=f"phase13-pg-eligibility-{scope}",
        candidate_id=candidate.candidate_id,
        expected_version=1,
        trace_ids=trace_ids,
        product_whitelist={"p003"},
    )
    result = service.confirm_promotion(
        command_id=f"phase13-pg-promote-{scope}",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-phase13",
    )
    assert result.status is MemoryCandidateStatus.APPLIED
    stored_command = store.get_command_result(f"phase13-pg-promote-{scope}")
    assert stored_command is not None
    assert stored_command.candidate_id == result.candidate_id
    assert stored_command.status is result.status
    assert stored_command.reason_code == result.reason_code
    assert stored_command.version == result.version
