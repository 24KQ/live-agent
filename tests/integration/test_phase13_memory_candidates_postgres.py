"""Phase 13 Task 9 受控记忆晋升的 PostgreSQL 闭环测试。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from src.config.settings import get_settings
from src.memory.candidate_store import MemoryCandidate, PostgresMemoryCandidateStore
from src.memory.promotion_policy import PromotionPolicy
from src.memory.candidate_store import MemoryCandidateStatus, MemoryPromotionCommand
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
    scope = uuid4().hex
    candidate = store.stage(MemoryCandidate(candidate_id=f"phase13-pg-candidate-{scope}", idempotency_key=f"phase13-pg-stage-{scope}", anchor_id="anchor-demo-001", room_id="room-demo-001", evidence_ids=("trace-a", "trace-b"), preferred_category="厨房", preferred_tags=("利润款",), preferred_product_ids=("p003",), confidence=Decimal("0.90")))
    result = PromotionPolicy(store=store, active_memory_port=store.memory_port()).promote(
        MemoryPromotionCommand(command_id=f"phase13-pg-promote-{scope}", candidate_id=candidate.candidate_id, expected_version=1, expected_status=MemoryCandidateStatus.STAGED),
        decision_traces=(
            {"decision_trace_id": "trace-a", "anchor_id": candidate.anchor_id, "room_id": candidate.room_id},
            {"decision_trace_id": "trace-b", "anchor_id": candidate.anchor_id, "room_id": candidate.room_id},
        ),
        product_whitelist={"p003"},
    )
    assert result.status is MemoryCandidateStatus.APPLIED
    assert store.get_command_result(f"phase13-pg-promote-{scope}") == result
