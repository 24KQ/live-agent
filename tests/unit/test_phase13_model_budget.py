"""Phase 13 Task 3 持久模型预算账本单元测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from src.specialist_runtime.budget import (
    BudgetCandidate,
    BudgetInvariantError,
    BudgetLimitExceeded,
    InMemoryModelBudgetStore,
    ReservationState,
)


def test_budget_limits_and_phase14_reserve_are_frozen() -> None:
    """总额、Phase 13 上限和 Phase 14 保留必须形成固定账本快照。"""

    snapshot = InMemoryModelBudgetStore().snapshot()

    assert snapshot.total_limit_cny == Decimal("4.00")
    assert snapshot.phase13_limit_cny == Decimal("2.40")
    assert snapshot.phase14_reserved_cny == Decimal("1.00")
    assert snapshot.candidate_limits[BudgetCandidate.LIVE_OPS] == Decimal("0.60")
    assert snapshot.candidate_limits[BudgetCandidate.PLANNER] == Decimal("1.00")
    assert snapshot.candidate_limits[BudgetCandidate.REVIEW_MEMORY] == Decimal("0.80")
    assert snapshot.candidate_limits[BudgetCandidate.PHASE14_COPILOT] == Decimal("1.00")


def test_reserve_settle_release_and_unknown_usage_are_conservative() -> None:
    """已知 usage 退还差额，未知 usage 按预留上限结算，未发送请求可释放。"""

    store = InMemoryModelBudgetStore()
    first = store.reserve("req-1", BudgetCandidate.LIVE_OPS, Decimal("0.10"))
    settled = store.settle("req-1", actual_cost_cny=Decimal("0.04"))
    second = store.reserve("req-2", BudgetCandidate.LIVE_OPS, Decimal("0.10"))
    unknown = store.settle("req-2", actual_cost_cny=None)
    third = store.reserve("req-3", BudgetCandidate.LIVE_OPS, Decimal("0.10"))
    released = store.release("req-3")

    assert first.created is True
    assert settled.state is ReservationState.SETTLED
    assert settled.settled_amount_cny == Decimal("0.04")
    assert settled.usage_known is True
    assert unknown.settled_amount_cny == Decimal("0.10")
    assert unknown.usage_known is False
    assert released.state is ReservationState.RELEASED
    snapshot = store.snapshot()
    assert snapshot.phase13_committed_cny == Decimal("0.14")
    assert snapshot.phase13_reserved_cny == Decimal("0.00")


def test_idempotent_replay_and_conflicting_request_are_distinguished() -> None:
    """相同 request 可恢复重放，不同候选或金额不得复用同一身份。"""

    store = InMemoryModelBudgetStore()
    first = store.reserve("req-1", BudgetCandidate.PLANNER, Decimal("0.20"))
    replay = store.reserve("req-1", BudgetCandidate.PLANNER, Decimal("0.20"))

    assert first.record.reservation_id == replay.record.reservation_id
    assert replay.created is False
    with pytest.raises(BudgetInvariantError, match="conflicting"):
        store.reserve("req-1", BudgetCandidate.PLANNER, Decimal("0.21"))
    with pytest.raises(BudgetInvariantError, match="settled"):
        store.release("req-1") if store.settle("req-1", Decimal("0.10")) else None


def test_candidate_and_phase_limits_cannot_consume_phase14_reserve() -> None:
    """候选额度及 2.40 元阶段硬门不能借用 Phase 14 的 1.00 元。"""

    store = InMemoryModelBudgetStore()
    store.reserve("live", BudgetCandidate.LIVE_OPS, Decimal("0.60"))
    store.reserve("planner", BudgetCandidate.PLANNER, Decimal("1.00"))
    store.reserve("review", BudgetCandidate.REVIEW_MEMORY, Decimal("0.80"))

    with pytest.raises(BudgetLimitExceeded, match="candidate|phase"):
        store.reserve("extra", BudgetCandidate.LIVE_OPS, Decimal("0.01"))
    assert store.snapshot().phase14_available_cny == Decimal("1.00")


def test_phase14_settled_exposure_remains_isolated_and_consumes_its_own_reserve() -> None:
    """Phase 14 Copilot 的已知费用扣除自身额度，但不侵占 Phase 13 共享池。"""

    store = InMemoryModelBudgetStore()
    store.reserve("copilot-settled", BudgetCandidate.PHASE14_COPILOT, Decimal("0.40"))
    store.settle("copilot-settled", Decimal("0.40"))

    assert store.snapshot().phase14_available_cny == Decimal("0.60")
    store.reserve("phase13-independent", BudgetCandidate.LIVE_OPS, Decimal("0.60"))

    with pytest.raises(BudgetLimitExceeded, match="phase 14"):
        store.reserve("copilot-over", BudgetCandidate.PHASE14_COPILOT, Decimal("0.61"))


def test_concurrent_reservations_only_one_crosses_candidate_boundary() -> None:
    """两个线程争抢最后额度时只能有一个 reservation 成功。"""

    store = InMemoryModelBudgetStore()
    store.reserve("seed", BudgetCandidate.LIVE_OPS, Decimal("0.50"))

    def reserve(request_id: str) -> bool:
        try:
            store.reserve(request_id, BudgetCandidate.LIVE_OPS, Decimal("0.10"))
            return True
        except BudgetLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("a", "b")))

    assert sorted(results) == [False, True]


def test_actual_cost_overage_is_recorded_instead_of_hidden() -> None:
    """已发生的超额费用必须如实记账，不能用较低预留值掩盖真实消费。"""

    store = InMemoryModelBudgetStore()
    store.reserve("req-1", BudgetCandidate.PLANNER, Decimal("0.10"))

    settled = store.settle("req-1", Decimal("0.11"))
    assert settled.settled_amount_cny == Decimal("0.11")
    assert store.snapshot().phase13_committed_cny == Decimal("0.11")


def test_rejected_candidate_returns_only_unspent_allowance_to_shared_pool() -> None:
    """提前拒绝只共享未消费额度，被拒候选不能再次发起模型请求。"""

    store = InMemoryModelBudgetStore()
    store.reserve("live-used", BudgetCandidate.LIVE_OPS, Decimal("0.20"))
    store.settle("live-used", Decimal("0.10"))
    store.release_candidate_allowance(BudgetCandidate.LIVE_OPS)

    store.reserve("planner-own", BudgetCandidate.PLANNER, Decimal("1.00"))
    store.reserve("planner-shared", BudgetCandidate.PLANNER, Decimal("0.50"))

    with pytest.raises(BudgetInvariantError, match="released"):
        store.reserve("live-again", BudgetCandidate.LIVE_OPS, Decimal("0.01"))
    with pytest.raises(BudgetLimitExceeded, match="shared"):
        store.reserve("planner-too-much", BudgetCandidate.PLANNER, Decimal("0.01"))


def test_pending_reservations_can_be_discovered_without_request_ids() -> None:
    """恢复进程可枚举全部未结请求，而不是依赖崩溃前内存。"""

    store = InMemoryModelBudgetStore()
    store.reserve("pending-1", BudgetCandidate.PLANNER, Decimal("0.10"))
    store.reserve("settled", BudgetCandidate.PLANNER, Decimal("0.10"))
    store.settle("settled", Decimal("0.05"))

    assert [record.request_id for record in store.list_pending_reservations()] == ["pending-1"]


def test_shared_pool_cannot_be_counted_twice_by_two_active_candidates() -> None:
    """同一笔释放额度被一个候选占用后，另一个候选不能再次借用。"""

    store = InMemoryModelBudgetStore()
    store.release_candidate_allowance(BudgetCandidate.LIVE_OPS)
    store.reserve("planner-own", BudgetCandidate.PLANNER, Decimal("1.00"))
    store.reserve("planner-shared", BudgetCandidate.PLANNER, Decimal("0.60"))
    store.reserve("review-own", BudgetCandidate.REVIEW_MEMORY, Decimal("0.10"))

    # 三个初始额度之和等于阶段上限，因此第二次借用也可由更强的阶段门拒绝。
    with pytest.raises(BudgetLimitExceeded):
        store.reserve("review-duplicate-shared", BudgetCandidate.REVIEW_MEMORY, Decimal("0.71"))


@pytest.mark.parametrize("amount", [Decimal("0.1000004"), Decimal("0.0000004")])
def test_budget_rejects_amounts_not_exactly_representable_by_ledger(amount: Decimal) -> None:
    """Python 边界必须拒绝 PostgreSQL NUMERIC(12,6) 会舍入的金额。"""

    with pytest.raises(BudgetInvariantError, match="precision"):
        InMemoryModelBudgetStore().reserve("precision", BudgetCandidate.PLANNER, amount)


def test_budget_rejects_amount_outside_ledger_numeric_range() -> None:
    """超出 NUMERIC(12,6) 整数位上限的值必须转换为稳定领域错误。"""

    with pytest.raises(BudgetInvariantError, match="range"):
        InMemoryModelBudgetStore().reserve(
            "too-large",
            BudgetCandidate.PLANNER,
            Decimal("1E+100"),
        )
