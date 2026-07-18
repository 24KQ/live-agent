"""Phase 15 Task 6 PostgreSQL 预算 reservation 与重启边界测试。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import psycopg

from src.config.settings import get_settings
from src.release_gates.budget import PostgresPhase15BudgetStore
from src.release_gates.copilot_smoke import (
    CopilotSmokeConfig,
    CopilotSmokeRunner,
    CopilotSmokeStatus,
    SmokeResponse,
    SmokeUsage,
    preflight_copilot_smoke,
)


HASHES = {
    "dataset_digest": "a" * 64,
    "code_digest": "b" * 64,
    "prompt_digest": "c" * 64,
    "schema_digest": "d" * 64,
    "pricing_source_digest": "e" * 64,
}


class _UnknownUsagePort:
    """集成测试使用的无网络 Port，模拟已发送但 usage 缺失。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, *, request_id: str, case_id: str) -> SmokeResponse:
        self.calls.append(request_id)
        return SmokeResponse(
            success=True,
            severe_violation=False,
            fallback_used=False,
            schema_valid=True,
            usage=SmokeUsage(input_tokens=0, output_tokens=0) if case_id == "known" else None,
        )


def _config() -> CopilotSmokeConfig:
    return CopilotSmokeConfig(
        manifest_id="phase15-runtime-v1",
        manifest_digest="f" * 64,
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        temperature=Decimal("0"),
        max_smoke_cases=10,
        budget_cny=Decimal("0.60"),
        reserved_case_budget_cny=Decimal("0.06"),
        **HASHES,
    )


def test_postgres_smoke_budget_blocks_after_unknown_usage_reservations() -> None:
    """重建 PostgreSQL budget Store 后，保守结算的 0.60 元不能再次发送。"""

    settings = get_settings()
    scope_id = f"phase15-smoke-postgres-{uuid4()}"
    store = PostgresPhase15BudgetStore(settings, scope_id=scope_id)
    config = _config()
    preflight = preflight_copilot_smoke(
        config,
        manifest={"manifest_id": config.manifest_id, "manifest_digest": config.manifest_digest, **HASHES},
        actual_artifacts=HASHES,
        pricing={
            "model_id": "deepseek-v4-flash",
            "endpoint_host": "api.deepseek.com",
            "input_cny_per_million": "1.008000",
            "output_cny_per_million": "2.016000",
            "pricing_source_digest": HASHES["pricing_source_digest"],
        },
        endpoint_available=True,
    )
    first_port = _UnknownUsagePort()
    first = CopilotSmokeRunner(config=config, preflight=preflight, budget_store=store, model_port=first_port)
    report = asyncio.run(first.run(tuple(f"case-{index:03d}" for index in range(1, 11))))
    assert report.status is CopilotSmokeStatus.BLOCKED
    assert store.snapshot().committed_cny == Decimal("0.60")

    restarted = PostgresPhase15BudgetStore(settings, scope_id=scope_id)
    second_port = _UnknownUsagePort()
    second = CopilotSmokeRunner(config=config, preflight=preflight, budget_store=restarted, model_port=second_port)
    blocked = asyncio.run(second.run(("case-011",)))
    assert blocked.status is CopilotSmokeStatus.BLOCKED
    assert second_port.calls == []

    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM phase15_budget_reservations WHERE scope_id=%s;", (scope_id,))
            cursor.execute("DELETE FROM phase15_budget_ledgers WHERE scope_id=%s;", (scope_id,))
        conn.commit()
