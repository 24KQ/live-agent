"""Phase 11B Attempt Store 单元测试。

这些测试先锁定 Operation 的唯一键、意图先写和终态只能闭合一次的语义。
PostgreSQL 并发与 DDL 细节留给同名 integration 测试，避免单元测试依赖本机数据库。
"""

from __future__ import annotations

import pytest

from src.skill_runtime.attempt_store import (
    AttemptInvariantError,
    AttemptState,
    InMemoryAttemptStore,
    OperationRequest,
)
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState


def _request(*, payload: dict | None = None) -> OperationRequest:
    """构造同一业务幂等操作，供重放和冲突测试复用。"""
    return OperationRequest(
        skill_id="setup_live_session",
        skill_version="1.0.0",
        room_id="room-001",
        idempotency_key="setup-idem-001",
        deadline_at="2026-07-12T10:00:15+00:00",
        intent_payload=payload or {"plan": {"items": ["p001"]}},
    )


def test_second_claim_reuses_attempt_without_new_external_work() -> None:
    """同一幂等 Operation 的重复调用必须复用原 Attempt，而不是制造第二次副作用。"""
    store = InMemoryAttemptStore()

    first = store.claim_or_replay(_request())
    second = store.claim_or_replay(_request())

    assert first.created is True
    assert second.created is False
    assert second.record.operation_id == first.record.operation_id
    assert second.record.attempt_id == first.record.attempt_id
    assert second.record.state == AttemptState.INTENT_RECORDED


def test_conflicting_intent_for_same_operation_fails_closed() -> None:
    """同一业务幂等键对应不同请求事实时，不能静默覆盖首次意图。"""
    store = InMemoryAttemptStore()
    store.claim_or_replay(_request(payload={"plan": {"items": ["p001"]}}))

    with pytest.raises(AttemptInvariantError, match="conflicting operation replay"):
        store.claim_or_replay(_request(payload={"plan": {"items": ["p002"]}}))


def test_terminal_update_requires_intent_state_and_closes_once() -> None:
    """成功终态只能从已记录意图转换一次，第二次完成必须暴露不变量错误。"""
    store = InMemoryAttemptStore()
    claim = store.claim_or_replay(_request())

    completed = store.complete_success(claim.record.attempt_id, {"setup_status": "prepared"})

    assert completed.state == AttemptState.SUCCEEDED
    assert completed.terminal_payload == {"setup_status": "prepared"}
    with pytest.raises(AttemptInvariantError, match="not awaiting terminal result"):
        store.complete_success(claim.record.attempt_id, {"setup_status": "prepared"})


def test_unknown_side_effect_is_a_terminal_replay_state() -> None:
    """发送后结果未知必须保存为终态，重复调用只能读取事实而不能重新 claim。"""
    store = InMemoryAttemptStore()
    claim = store.claim_or_replay(_request())
    fact = FailureFact(
        category=FailureCategory.SIDE_EFFECT_UNKNOWN,
        external_code="fake.unknown_after_send",
        side_effect_state=SideEffectState.UNKNOWN,
        attempt_id=claim.record.attempt_id,
    )

    terminal = store.complete_failure(claim.record.attempt_id, fact)
    replay = store.claim_or_replay(_request())

    assert terminal.state == AttemptState.SIDE_EFFECT_UNKNOWN
    assert replay.created is False
    assert replay.record.failure == fact
