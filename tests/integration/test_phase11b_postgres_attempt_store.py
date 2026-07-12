"""Phase 11B Attempt Store PostgreSQL 集成测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from src.config.settings import get_settings
from src.skill_runtime.attempt_store import (
    AttemptInvariantError,
    OperationRequest,
    PostgresAttemptStore,
    initialize_skill_execution_attempt_schema,
)


def test_postgres_claim_reuses_single_attempt_for_same_operation() -> None:
    """数据库唯一约束必须让重复 claim 读取同一 Attempt，而不是创建第二条记录。"""
    settings = get_settings()
    initialize_skill_execution_attempt_schema(settings)
    store = PostgresAttemptStore(settings)
    request = OperationRequest(
        skill_id="setup_live_session",
        skill_version="1.0.0",
        room_id="room-phase11b-attempt-store",
        idempotency_key=f"attempt-store-{uuid4()}",
        deadline_at="2026-07-12T10:00:15+00:00",
        intent_payload={"plan": {"items": ["p001"]}},
    )

    first = store.claim_or_replay(request)
    second = store.claim_or_replay(request)

    assert first.created is True
    assert second.created is False
    assert second.record.operation_id == first.record.operation_id
    assert second.record.attempt_id == first.record.attempt_id


def test_postgres_concurrent_claims_share_one_attempt() -> None:
    """两个连接同时抢占同一业务幂等键时，数据库只能返回一个 Attempt。"""
    settings = get_settings()
    initialize_skill_execution_attempt_schema(settings)
    request = OperationRequest(
        skill_id="setup_live_session",
        skill_version="1.0.0",
        room_id="room-phase11b-attempt-store",
        idempotency_key=f"attempt-store-concurrent-{uuid4()}",
        deadline_at="2026-07-12T10:00:15+00:00",
        intent_payload={"plan": {"items": ["p001"]}},
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda _unused: PostgresAttemptStore(settings).claim_or_replay(request),
                range(2),
            )
        )

    assert {claim.record.attempt_id for claim in claims}
    assert len({claim.record.attempt_id for claim in claims}) == 1
    assert sorted(claim.created for claim in claims) == [False, True]


def test_postgres_terminal_update_closes_attempt_once() -> None:
    """终态 SQL 必须以 INTENT_RECORDED 为条件，避免迟到调用覆盖首次成功事实。"""
    settings = get_settings()
    initialize_skill_execution_attempt_schema(settings)
    store = PostgresAttemptStore(settings)
    request = OperationRequest(
        skill_id="setup_live_session",
        skill_version="1.0.0",
        room_id="room-phase11b-attempt-store",
        idempotency_key=f"attempt-store-terminal-{uuid4()}",
        deadline_at="2026-07-12T10:00:15+00:00",
        intent_payload={"plan": {"items": ["p001"]}},
    )
    claim = store.claim_or_replay(request)

    terminal = store.complete_success(claim.record.attempt_id, {"setup_status": "prepared"})

    assert terminal.terminal_payload == {"setup_status": "prepared"}
    with pytest.raises(AttemptInvariantError, match="not awaiting terminal result"):
        store.complete_success(claim.record.attempt_id, {"setup_status": "prepared"})

