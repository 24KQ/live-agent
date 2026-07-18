"""Phase 15 Task 4 独立预算账本的 PostgreSQL 并发与重启测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from src.config.settings import get_settings
from src.release_gates.budget import (
    PHASE15_BUDGET_CNY,
    Phase15BudgetLimitExceeded,
    PostgresPhase15BudgetStore,
    initialize_phase15_budget_schema,
)


@pytest.fixture
def budget_store():
    """隔离 Phase 15 scope，并在测试后清理独立预算表。"""

    settings = get_settings()
    initialize_phase15_budget_schema(settings)
    scope_id = f"phase15-budget-test-{uuid4()}"
    store = PostgresPhase15BudgetStore(settings, scope_id=scope_id)
    try:
        yield settings, scope_id, store
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM phase15_budget_reservations WHERE scope_id=%s;", (scope_id,))
                cursor.execute("DELETE FROM phase15_budget_ledgers WHERE scope_id=%s;", (scope_id,))
            conn.commit()


def test_phase15_budget_is_independent_and_concurrent_reserve_has_one_winner(budget_store) -> None:
    """两个连接共享同一 0.60 元 scope 时只能有一个越过余额边界。"""

    settings, _scope_id, store = budget_store
    store.reserve("seed", Decimal("0.50"))

    def reserve(request_id: str) -> bool:
        try:
            PostgresPhase15BudgetStore(settings, scope_id=store.snapshot().scope_id).reserve(
                request_id, Decimal("0.10")
            )
            return True
        except Phase15BudgetLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("a", "b")))
    assert sorted(results) == [False, True]
    assert store.snapshot().available_cny == Decimal("0.00")
    assert store.snapshot().reserved_cny <= PHASE15_BUDGET_CNY

    recovered = PostgresPhase15BudgetStore(settings, scope_id=store.snapshot().scope_id)
    assert [item.request_id for item in recovered.list_pending_reservations()] == ["a", "seed"] or [item.request_id for item in recovered.list_pending_reservations()] == ["b", "seed"]
