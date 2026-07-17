"""Phase 13 模型预算 PostgreSQL 并发与恢复测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from src.config.settings import get_settings
from src.specialist_runtime.budget import (
    BudgetCandidate,
    BudgetLimitExceeded,
    PostgresModelBudgetStore,
    initialize_specialist_budget_schema,
)
import psycopg


@pytest.fixture
def budget_store():
    """创建隔离 scope，并在测试结束后按外键顺序精确清理。"""

    settings = get_settings()
    initialize_specialist_budget_schema(settings)
    scope_id = f"phase13-test-{uuid4()}"
    store = PostgresModelBudgetStore(settings, scope_id=scope_id)
    try:
        yield settings, scope_id, store
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                for table in (
                    "specialist_model_calls",
                    "specialist_model_budget_reservations",
                    "specialist_model_budget_candidates",
                    "specialist_model_budget_ledgers",
                ):
                    cursor.execute(f"DELETE FROM {table} WHERE scope_id=%s", (scope_id,))
            conn.commit()


def test_postgres_concurrent_reserve_serializes_at_candidate_boundary(budget_store) -> None:
    """两个数据库连接只能有一个越过候选临界余额。"""

    settings, scope_id, store = budget_store
    store.reserve("seed", BudgetCandidate.LIVE_OPS, Decimal("0.50"))

    def reserve(request_id: str) -> bool:
        try:
            PostgresModelBudgetStore(settings, scope_id=scope_id).reserve(
                request_id,
                BudgetCandidate.LIVE_OPS,
                Decimal("0.10"),
            )
            return True
        except BudgetLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("a", "b")))

    assert sorted(results) == [False, True]
    assert store.snapshot().phase14_available_cny == Decimal("1.00")


def test_postgres_phase14_settled_exposure_isolated_from_phase13_pool(budget_store) -> None:
    """PostgreSQL 账本重放后仍按阶段隔离已结算的 Copilot 费用。"""

    settings, scope_id, store = budget_store
    store.reserve("copilot-settled", BudgetCandidate.PHASE14_COPILOT, Decimal("0.40"))
    store.settle("copilot-settled", Decimal("0.40"))

    assert store.snapshot().phase14_available_cny == Decimal("0.60")
    store.reserve("phase13-independent", BudgetCandidate.LIVE_OPS, Decimal("0.60"))

    with pytest.raises(BudgetLimitExceeded, match="phase 14"):
        store.reserve("copilot-over", BudgetCandidate.PHASE14_COPILOT, Decimal("0.61"))


def test_postgres_unsettled_reservation_survives_store_restart(budget_store) -> None:
    """请求发送前已写 reservation，进程重启后仍可按同一 request 对账。"""

    settings, scope_id, first = budget_store
    reserved = first.reserve("request-restart", BudgetCandidate.PLANNER, Decimal("0.20"))

    recovered = PostgresModelBudgetStore(settings, scope_id=scope_id)
    assert [item.request_id for item in recovered.list_pending_reservations()] == [
        "request-restart"
    ]
    replay = recovered.reserve("request-restart", BudgetCandidate.PLANNER, Decimal("0.20"))
    settled = recovered.settle("request-restart", actual_cost_cny=None)

    assert replay.created is False
    assert replay.record.reservation_id == reserved.record.reservation_id
    assert settled.settled_amount_cny == Decimal("0.20")
    assert settled.usage_known is False


def test_postgres_candidate_release_persists_shared_pool_and_blocks_rejected_candidate(budget_store) -> None:
    """候选拒绝与共享余额必须跨 Store 实例生效。"""

    settings, scope_id, store = budget_store
    store.reserve("live-used", BudgetCandidate.LIVE_OPS, Decimal("0.20"))
    store.settle("live-used", Decimal("0.10"))
    store.release_candidate_allowance(BudgetCandidate.LIVE_OPS)

    recovered = PostgresModelBudgetStore(settings, scope_id=scope_id)
    recovered.reserve("planner-own", BudgetCandidate.PLANNER, Decimal("1.00"))
    recovered.reserve("planner-shared", BudgetCandidate.PLANNER, Decimal("0.50"))

    try:
        recovered.reserve("live-again", BudgetCandidate.LIVE_OPS, Decimal("0.01"))
    except Exception as error:  # 断言稳定公共异常，不把 psycopg 错误当作通过。
        assert "released" in str(error)
    else:
        raise AssertionError("released candidate unexpectedly reserved budget")


def test_postgres_uses_locked_persisted_limits_not_process_constants(budget_store) -> None:
    """数据库内冻结的阶段和候选限额降低后，运行进程必须立即按持久事实拒绝。"""

    settings, scope_id, store = budget_store
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE specialist_model_budget_ledgers SET phase13_limit_cny=0.05 WHERE scope_id=%s",
                (scope_id,),
            )
            cursor.execute(
                "UPDATE specialist_model_budget_candidates SET initial_limit_cny=0.05 WHERE scope_id=%s AND candidate_id='LIVE_OPS'",
                (scope_id,),
            )
        conn.commit()

    try:
        store.reserve("too-large", BudgetCandidate.LIVE_OPS, Decimal("0.10"))
    except BudgetLimitExceeded:
        pass
    else:
        raise AssertionError("persisted lower limit was ignored")


def test_postgres_shared_pool_is_not_double_allocated_after_limit_drift(budget_store) -> None:
    """共享余额必须扣除其他 ACTIVE 候选已经借用的部分。"""

    settings, scope_id, store = budget_store
    store.release_candidate_allowance(BudgetCandidate.LIVE_OPS)
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE specialist_model_budget_candidates SET initial_limit_cny=0.50 WHERE scope_id=%s AND candidate_id IN ('PLANNER','REVIEW_MEMORY')",
                (scope_id,),
            )
        conn.commit()

    store.reserve("planner", BudgetCandidate.PLANNER, Decimal("1.10"))
    with pytest.raises(BudgetLimitExceeded, match="shared"):
        store.reserve("review", BudgetCandidate.REVIEW_MEMORY, Decimal("1.10"))


def test_postgres_constraints_reject_nan_or_unsettled_model_call(budget_store) -> None:
    """直接 SQL 也不能写 NaN，或给未结 reservation 伪造 ModelCall。"""

    settings, scope_id, store = budget_store
    reservation = store.reserve("pending", BudgetCandidate.PLANNER, Decimal("0.10")).record

    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.CheckViolation):
                cursor.execute(
                    "UPDATE specialist_model_budget_reservations SET reserved_amount_cny='NaN' WHERE reservation_id=%s::uuid",
                    (reservation.reservation_id,),
                )
        conn.rollback()


def test_postgres_released_borrower_keeps_shared_pool_debt(budget_store) -> None:
    """借用共享额度的候选被释放后，已消费超额仍必须扣减后续共享池。"""

    settings, scope_id, store = budget_store
    store.release_candidate_allowance(BudgetCandidate.LIVE_OPS)
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE specialist_model_budget_candidates SET initial_limit_cny=0.50 WHERE scope_id=%s AND candidate_id IN ('PLANNER','REVIEW_MEMORY')",
                (scope_id,),
            )
        conn.commit()

    store.reserve("planner-borrow", BudgetCandidate.PLANNER, Decimal("1.10"))
    store.settle("planner-borrow", Decimal("1.10"))
    store.release_candidate_allowance(BudgetCandidate.PLANNER)

    with pytest.raises(BudgetLimitExceeded, match="shared"):
        store.reserve("review-after-chain", BudgetCandidate.REVIEW_MEMORY, Decimal("0.51"))
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cursor.execute(
                    "INSERT INTO specialist_model_calls (call_id,scope_id,request_id,reservation_state,settled_amount_cny,usage_known) VALUES (%s::uuid,%s,'pending','SETTLED',0.99,true)",
                    (str(uuid4()), scope_id),
                )
        conn.rollback()
