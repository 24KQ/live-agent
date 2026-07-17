"""Phase 14 Task 9 规则资格与人工确认记忆晋升的 RED 契约。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.memory.candidate_store import (
    InMemoryMemoryCandidateStore,
    MemoryCandidate,
    MemoryCandidateStatus,
)
from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemorySource
from src.memory.promotion_policy import PromotionPolicy


def _candidate() -> MemoryCandidate:
    """构造不携带模型自由文本的结构化播后候选。"""

    return MemoryCandidate(
        candidate_id="phase14-candidate-001",
        idempotency_key="phase14-stage-001",
        anchor_id="anchor-001",
        room_id="room-001",
        evidence_ids=("trace-a", "trace-b"),
        preferred_category="kitchen",
        preferred_tags=("profit",),
        preferred_product_ids=("p001",),
        confidence=Decimal("0.90"),
    )


def _traces() -> tuple[dict, dict]:
    """返回同主播、同房间且互相独立的两条 DecisionTrace 摘要。"""

    return (
        {"decision_trace_id": "trace-a", "anchor_id": "anchor-001", "room_id": "room-001"},
        {"decision_trace_id": "trace-b", "anchor_id": "anchor-001", "room_id": "room-001"},
    )


class _ActiveMemoryPort:
    """记录唯一 PromotionPolicy 写入，验证人工确认前不会产生 active memory。"""

    def __init__(self) -> None:
        self.entries = []

    def write_memory(self, entry) -> str:
        for index, existing in enumerate(self.entries):
            if existing.memory_key == entry.memory_key:
                self.entries[index] = entry
                return "memory-phase14-001"
        self.entries.append(entry)
        return "memory-phase14-001"

    def list_memories(self, _anchor_id: str, _room_id: str | None = None):
        """提供只读冲突查询，模拟生产 MemoryStore 的作用域读取。"""

        return list(self.entries)

    def promotion_scope_lock(self, _anchor_id: str, _room_id: str | None = None):
        """内存测试替身不需要数据库锁，但保留与生产 Port 相同的协调契约。"""

        from contextlib import nullcontext

        return nullcontext()


def _service(records: tuple[dict, ...] | None = None):
    """构造待实现的 Phase 14 规则资格门面。"""

    from src.decision_support.review_feedback import (
        InMemoryReviewFeedbackStore,
        InMemoryDecisionTraceResolver,
        ReviewFeedbackService,
    )

    candidate_store = InMemoryMemoryCandidateStore()
    active_memory = _ActiveMemoryPort()
    feedback_store = InMemoryReviewFeedbackStore()
    resolver = InMemoryDecisionTraceResolver(records or _traces())
    policy = PromotionPolicy(
        store=candidate_store,
        active_memory_port=active_memory,
        eligibility_store=feedback_store,
        decision_trace_resolver=resolver,
    )
    return (
        ReviewFeedbackService(
            candidate_store=candidate_store,
            feedback_store=feedback_store,
            promotion_policy=policy,
            decision_trace_resolver=resolver,
        ),
        candidate_store,
        active_memory,
    )


def test_qualification_persists_eligible_fact_but_does_not_write_active_memory() -> None:
    """双证据合格只进入等待人工状态，不能因资格计算自动晋升。"""

    service, store, active_memory = _service()
    candidate = store.stage(_candidate())

    result = service.evaluate_eligibility(
        command_id="phase14-eligibility-001",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )

    assert result.status == MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR
    assert store.get(candidate.candidate_id).status == MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR
    assert active_memory.entries == []


@pytest.mark.parametrize(
    ("traces", "whitelist", "reason"),
    [
        ((_traces()[0],), {"p001"}, "INSUFFICIENT_INDEPENDENT_EVIDENCE"),
        (_traces(), {"p999"}, "PRODUCT_WHITELIST_MISMATCH"),
        (
            (
                {**_traces()[0], "room_id": "room-other"},
                _traces()[1],
            ),
            {"p001"},
            "TRACE_SCOPE_CONFLICT",
        ),
        (
            (
                {**_traces()[0], "free_text": "model private text"},
                _traces()[1],
            ),
            {"p001"},
            "SENSITIVE_FIELD_PRESENT",
        ),
    ],
)
def test_ineligible_candidate_cannot_be_forced_by_operator(traces, whitelist, reason) -> None:
    """规则拒绝的候选即使收到人工确认也不能进入 active memory。"""

    service, store, active_memory = _service(records=traces)
    candidate = store.stage(_candidate())
    result = service.evaluate_eligibility(
        command_id=f"phase14-ineligible-{reason}",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=tuple(item.get("trace_id") or item["decision_trace_id"] for item in traces),
        product_whitelist=whitelist,
    )

    assert result.status == MemoryCandidateStatus.STAGED
    assert result.reason_code == reason
    with pytest.raises(ValueError, match="ELIGIBLE_AWAITING_OPERATOR"):
        service.confirm_promotion(
            command_id=f"phase14-confirm-{reason}",
            candidate_id=candidate.candidate_id,
            expected_version=candidate.version,
            operator_id="operator-001",
        )
    assert active_memory.entries == []


def test_operator_confirmation_is_the_only_active_memory_write_and_is_idempotent() -> None:
    """合格候选必须由操作员确认，重复确认只能重放同一条结果。"""

    service, store, active_memory = _service()
    candidate = store.stage(_candidate())
    eligible = service.evaluate_eligibility(
        command_id="phase14-eligibility-002",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )

    result = service.confirm_promotion(
        command_id="phase14-confirm-001",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-001",
    )
    replay = service.confirm_promotion(
        command_id="phase14-confirm-001",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-001",
    )

    assert result.status == MemoryCandidateStatus.APPLIED
    assert replay == result
    assert store.get(candidate.candidate_id).status == MemoryCandidateStatus.APPLIED
    assert len(active_memory.entries) == 1


def test_confirmation_requires_operator_identity_and_expected_version() -> None:
    """确认命令的身份和版本校验必须在调用 PromotionPolicy 前失败。"""

    service, store, _ = _service()
    candidate = store.stage(_candidate())
    eligible = service.evaluate_eligibility(
        command_id="phase14-eligibility-003",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )

    with pytest.raises(ValueError, match="operator_id"):
        service.confirm_promotion(
            command_id="phase14-confirm-no-operator",
            candidate_id=candidate.candidate_id,
            expected_version=eligible.version,
            operator_id="",
        )
    with pytest.raises(ValueError, match="expected_version"):
        service.confirm_promotion(
            command_id="phase14-confirm-stale",
            candidate_id=candidate.candidate_id,
            expected_version=eligible.version + 1,
            operator_id="operator-001",
        )


def test_direct_policy_call_without_persisted_confirmation_intent_is_rejected() -> None:
    """即使拿到合格状态，未经过人工确认账本也不能直接调用 PromotionPolicy。"""

    from src.memory.candidate_store import MemoryPromotionCommand

    service, store, _ = _service()
    candidate = store.stage(_candidate())
    eligible = service.evaluate_eligibility(
        command_id="phase14-eligibility-direct",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )
    with pytest.raises(ValueError, match="confirmation intent"):
        service._promotion_policy.promote(  # noqa: SLF001 - 验证内部唯一写门的绕过失败语义。
            MemoryPromotionCommand(
                command_id="phase14-direct-bypass",
                candidate_id=candidate.candidate_id,
                expected_version=eligible.version,
                expected_status=MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR,
            ),
            operator_id="operator-001",
        )


def test_missing_trusted_trace_cannot_be_replaced_by_caller_summary() -> None:
    """服务只接收 Trace ID；未登记的 ID 不能用调用方自带摘要伪造资格。"""

    service, store, active_memory = _service()
    candidate = store.stage(_candidate())
    result = service.evaluate_eligibility(
        command_id="phase14-eligibility-missing-trace",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-missing"),
        product_whitelist={"p001"},
    )
    assert result.status is MemoryCandidateStatus.STAGED
    assert result.reason_code == "TRACE_NOT_FOUND"
    assert active_memory.entries == []


def test_existing_promoted_memory_conflict_stays_eligible_without_second_write() -> None:
    """已有同作用域同商品模板冲突时，人工确认也只能留下待处理结果。"""

    service, store, active_memory = _service()
    active_memory.entries.append(
        AnchorMemoryEntry(
            memory_key="phase14-existing-memory",
            anchor_id="anchor-001",
            room_id="room-001",
            layer=MemoryLayer.L2,
            content="旧模板",
            metadata={
                "promotion_candidate_id": "other-candidate",
                "preferred_category": "kitchen",
                "preferred_product_ids": ["p001"],
            },
            source=MemorySource.SYSTEM_OBSERVED,
        )
    )
    candidate = store.stage(_candidate())
    eligible = service.evaluate_eligibility(
        command_id="phase14-eligibility-conflict",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )
    result = service.confirm_promotion(
        command_id="phase14-confirm-conflict",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-001",
    )
    assert result.status is MemoryCandidateStatus.ELIGIBLE_AWAITING_OPERATOR
    assert result.reason_code == "ACTIVE_MEMORY_CONFLICT"
    assert len(active_memory.entries) == 1


def test_confirmation_retry_recovers_after_active_write_before_candidate_cas() -> None:
    """active-memory 写入后候选 CAS 失败时，重试必须收敛且不生成第二条记忆。"""

    class _FailingCandidateStore(InMemoryMemoryCandidateStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_apply_once = True

        def transition(self, candidate_id: str, *, status: MemoryCandidateStatus):
            if status is MemoryCandidateStatus.APPLIED and self.fail_apply_once:
                self.fail_apply_once = False
                raise RuntimeError("simulated candidate CAS crash")
            return super().transition(candidate_id, status=status)

    from src.decision_support.review_feedback import (
        InMemoryDecisionTraceResolver,
        InMemoryReviewFeedbackStore,
        ReviewFeedbackService,
    )

    store = _FailingCandidateStore()
    feedback = InMemoryReviewFeedbackStore()
    resolver = InMemoryDecisionTraceResolver(_traces())
    active_memory = _ActiveMemoryPort()
    service = ReviewFeedbackService(
        candidate_store=store,
        feedback_store=feedback,
        decision_trace_resolver=resolver,
        promotion_policy=PromotionPolicy(
            store=store,
            active_memory_port=active_memory,
            eligibility_store=feedback,
            decision_trace_resolver=resolver,
        ),
    )
    candidate = store.stage(_candidate())
    eligible = service.evaluate_eligibility(
        command_id="phase14-eligibility-recovery",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )
    with pytest.raises(RuntimeError, match="simulated"):
        service.confirm_promotion(
            command_id="phase14-confirm-recovery",
            candidate_id=candidate.candidate_id,
            expected_version=eligible.version,
            operator_id="operator-001",
        )
    recovered = service.confirm_promotion(
        command_id="phase14-confirm-recovery",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-001",
    )
    assert recovered.status is MemoryCandidateStatus.APPLIED
    assert len(active_memory.entries) == 1


def test_confirmation_retry_recovers_after_candidate_cas_before_command_ledger() -> None:
    """候选已提交 APPLIED 但命令账本丢失时，服务重试必须补记结果。"""

    class _FailingLedgerStore(InMemoryMemoryCandidateStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_ledger_once = True

        def record_command_result(self, command_id: str, result):
            if self.fail_ledger_once:
                self.fail_ledger_once = False
                raise RuntimeError("simulated command ledger crash")
            return super().record_command_result(command_id, result)

    from src.decision_support.review_feedback import (
        InMemoryDecisionTraceResolver,
        InMemoryReviewFeedbackStore,
        ReviewFeedbackService,
    )

    store = _FailingLedgerStore()
    feedback = InMemoryReviewFeedbackStore()
    resolver = InMemoryDecisionTraceResolver(_traces())
    active_memory = _ActiveMemoryPort()
    service = ReviewFeedbackService(
        candidate_store=store,
        feedback_store=feedback,
        decision_trace_resolver=resolver,
        promotion_policy=PromotionPolicy(
            store=store,
            active_memory_port=active_memory,
            eligibility_store=feedback,
            decision_trace_resolver=resolver,
        ),
    )
    candidate = store.stage(_candidate())
    eligible = service.evaluate_eligibility(
        command_id="phase14-eligibility-ledger-recovery",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )
    with pytest.raises(RuntimeError, match="command ledger"):
        service.confirm_promotion(
            command_id="phase14-confirm-ledger-recovery",
            candidate_id=candidate.candidate_id,
            expected_version=eligible.version,
            operator_id="operator-001",
        )
    assert store.get(candidate.candidate_id).status is MemoryCandidateStatus.APPLIED
    recovered = service.confirm_promotion(
        command_id="phase14-confirm-ledger-recovery",
        candidate_id=candidate.candidate_id,
        expected_version=eligible.version,
        operator_id="operator-001",
    )
    assert recovered.status is MemoryCandidateStatus.APPLIED
    assert len(active_memory.entries) == 1


def test_eligibility_replay_is_bound_to_command_and_candidate_version() -> None:
    """资格事实只能由原命令重放，旧候选版本不能借重放绕过 CAS。"""

    service, store, _ = _service()
    candidate = store.stage(_candidate())
    fact = service.evaluate_eligibility(
        command_id="phase14-eligibility-replay",
        candidate_id=candidate.candidate_id,
        expected_version=candidate.version,
        trace_ids=("trace-a", "trace-b"),
        product_whitelist={"p001"},
    )

    assert service.evaluate_eligibility(
        command_id="phase14-eligibility-replay",
        candidate_id=candidate.candidate_id,
        expected_version=fact.version,
        trace_ids=(),
        product_whitelist=set(),
    ) == fact
    with pytest.raises(ValueError, match="eligibility fact"):
        service.evaluate_eligibility(
            command_id="phase14-eligibility-other-command",
            candidate_id=candidate.candidate_id,
            expected_version=fact.version,
            trace_ids=("trace-a", "trace-b"),
            product_whitelist={"p001"},
        )
    with pytest.raises(ValueError, match="expected_version"):
        service.evaluate_eligibility(
            command_id="phase14-eligibility-replay",
            candidate_id=candidate.candidate_id,
            expected_version=1,
            trace_ids=(),
            product_whitelist=set(),
        )
