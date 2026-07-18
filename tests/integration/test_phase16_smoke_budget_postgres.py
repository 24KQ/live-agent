"""Phase 16 Task 10 PostgreSQL 独立 smoke ledger 的并发和重启契约。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from src.config.settings import get_settings
from src.decision_support.multi_agent_smoke import (
    PHASE16_CASE_RESERVATION_CNY,
    PHASE16_MULTI_AGENT_SMOKE,
    PHASE16_SMOKE_BUDGET_CNY,
    Phase16SmokeBudgetLimitExceeded,
    PostgresPhase16SmokeBudgetStore,
    initialize_phase16_smoke_budget_schema,
)


def test_postgres_phase16_smoke_scope_has_ten_case_limit_and_survives_restart() -> None:
    """并发 11 个完整 case 只能预约 10 个，重建 Store 后仍不能越过一元边界。"""

    settings = get_settings()
    initialize_phase16_smoke_budget_schema(settings)
    scope_id = PHASE16_MULTI_AGENT_SMOKE
    store = PostgresPhase16SmokeBudgetStore(settings, scope_id=scope_id)

    def reserve(case_index: int) -> bool:
        """每个线程建立新 Store，覆盖跨连接 ledger 行锁而不是单进程 Lock。"""

        try:
            PostgresPhase16SmokeBudgetStore(settings, scope_id=scope_id).reserve(
                f"case-{case_index:02d}",
                PHASE16_CASE_RESERVATION_CNY,
            )
            return True
        except Phase16SmokeBudgetLimitExceeded:
            return False

    try:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM phase16_smoke_budget_reservations WHERE scope_id=%s;", (scope_id,))
            conn.commit()
        with ThreadPoolExecutor(max_workers=11) as pool:
            winners = list(pool.map(reserve, range(11)))

        assert sum(winners) == 10
        assert store.snapshot().reserved_cny == PHASE16_SMOKE_BUDGET_CNY

        # 逐例以低于 reservation 的已知 usage 结算后，余额会恢复；但十例 slot 已经
        # 被持久化消费，不能因为真实价格低而发送第十一例。
        for case_index, won in enumerate(winners):
            if won:
                store.settle(f"case-{case_index:02d}", Decimal("0.000001"))
        assert store.snapshot().committed_cny == Decimal("0.000010")

        restarted = PostgresPhase16SmokeBudgetStore(settings, scope_id=scope_id)
        assert restarted.snapshot().available_cny == Decimal("0.999990")
        try:
            restarted.reserve("case-after-restart", PHASE16_CASE_RESERVATION_CNY)
            raise AssertionError("restarted store unexpectedly exceeded Phase 16 smoke budget")
        except Phase16SmokeBudgetLimitExceeded:
            pass
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM phase16_smoke_budget_reservations WHERE scope_id=%s;", (scope_id,))
                cursor.execute("DELETE FROM phase16_smoke_budget_ledgers WHERE scope_id=%s;", (scope_id,))
            conn.commit()


def test_postgres_ledger_rejects_released_pass_even_for_direct_sql() -> None:
    """直接 SQL 也不能把未发送的 release 伪造成 PASS，避免重启后出现零请求成功。"""

    settings = get_settings()
    initialize_phase16_smoke_budget_schema(settings)
    scope_id = PHASE16_MULTI_AGENT_SMOKE
    try:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM phase16_smoke_budget_reservations WHERE scope_id=%s;", (scope_id,))
                cursor.execute(
                    "INSERT INTO phase16_smoke_budget_ledgers (scope_id, limit_cny) VALUES (%s,%s) ON CONFLICT (scope_id) DO NOTHING;",
                    (scope_id, PHASE16_SMOKE_BUDGET_CNY),
                )
                with pytest.raises(psycopg.errors.CheckViolation):
                    cursor.execute(
                        "INSERT INTO phase16_smoke_budget_reservations (reservation_id, scope_id, request_id, reserved_amount_cny, state, version, outcome_status, outcome_reason_code) VALUES (%s::uuid,%s,%s,%s,'RELEASED',1,'PASS','FORGED_RELEASE_PASS');",
                        (str(uuid4()), scope_id, "forged-release-pass", PHASE16_CASE_RESERVATION_CNY),
                    )
            conn.rollback()
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM phase16_smoke_budget_reservations WHERE scope_id=%s;", (scope_id,))
                cursor.execute("DELETE FROM phase16_smoke_budget_ledgers WHERE scope_id=%s;", (scope_id,))
            conn.commit()
