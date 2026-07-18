"""Phase 15 Task 6 Copilot smoke 预检、预算与单次调用的 TDD 契约。"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.release_gates.budget import Phase15BudgetStore
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


def _config(**updates) -> CopilotSmokeConfig:
    """构造完整冻结身份；单例 case 预留总额固定不超过 0.60 元。"""

    values = {
        "manifest_id": "phase15-runtime-v1",
        "manifest_digest": "f" * 64,
        "model_id": "deepseek-v4-flash",
        "endpoint_host": "api.deepseek.com",
        "temperature": Decimal("0"),
        "max_smoke_cases": 10,
        "budget_cny": Decimal("0.60"),
        "reserved_case_budget_cny": Decimal("0.06"),
        **HASHES,
    }
    values.update(updates)
    return CopilotSmokeConfig(**values)


def _manifest() -> dict[str, str]:
    return {"manifest_id": "phase15-runtime-v1", "manifest_digest": "f" * 64, **HASHES}


def _prices() -> dict[str, str]:
    return {
        "model_id": "deepseek-v4-flash",
        "endpoint_host": "api.deepseek.com",
        "input_cny_per_million": "1.008000",
        "output_cny_per_million": "2.016000",
        "pricing_source_digest": HASHES["pricing_source_digest"],
    }


class _ScriptedPort:
    """只在测试内模拟受控 Model Port，绝不访问网络。"""

    def __init__(self, response: SmokeResponse | None = None) -> None:
        self.calls: list[str] = []
        self.response = response or SmokeResponse(
            success=True,
            severe_violation=False,
            fallback_used=False,
            schema_valid=True,
            usage=SmokeUsage(input_tokens=100, output_tokens=50),
        )

    async def complete(self, *, request_id: str, case_id: str) -> SmokeResponse:
        self.calls.append(f"{request_id}:{case_id}")
        return self.response


def _preflight(config: CopilotSmokeConfig | None = None):
    selected = config or _config()
    return preflight_copilot_smoke(
        selected,
        manifest=_manifest(),
        actual_artifacts=HASHES,
        pricing=_prices(),
        endpoint_available=True,
    )


def test_preflight_blocks_missing_identity_before_any_model_call() -> None:
    """缺 endpoint/价格/usage/hash 时只能 BLOCKED，不能伪造发送许可。"""

    port = _ScriptedPort()
    config = _config(endpoint_host="", pricing_source_digest="0" * 64)
    preflight = _preflight(config)
    runner = CopilotSmokeRunner(
        config=config,
        preflight=preflight,
        budget_store=Phase15BudgetStore(scope_id="phase15-smoke-red"),
        model_port=port,
    )

    report = asyncio.run(runner.run(("case-001",)))

    assert report.status is CopilotSmokeStatus.BLOCKED
    assert report.promotion_eligible is False
    assert port.calls == []
    assert "ENDPOINT_MISMATCH" in report.reason_codes


def test_preflight_requires_exact_manifest_and_pricing_hashes() -> None:
    """Manifest、源码、Prompt、Schema 或价格摘要漂移必须 fail-closed。"""

    config = _config()
    preflight = preflight_copilot_smoke(
        config,
        manifest={**_manifest(), "code_digest": "0" * 64},
        actual_artifacts=HASHES,
        pricing={**_prices(), "output_cny_per_million": "9.999000"},
        endpoint_available=True,
    )

    assert preflight.status is CopilotSmokeStatus.BLOCKED
    assert preflight.can_send is False
    assert {"CODE_DIGEST_MISMATCH", "PRICE_TABLE_MISMATCH"} <= set(preflight.reason_codes)


def test_runner_rejects_fallback_schema_error_and_severe_violation_for_promotion() -> None:
    """模型响应的 fallback、Schema 错误或严重违规都不能形成 Promotion 资格。"""

    response = SmokeResponse(
        success=True,
        severe_violation=True,
        fallback_used=True,
        schema_valid=False,
        usage=SmokeUsage(input_tokens=100, output_tokens=50),
    )
    port = _ScriptedPort(response)
    runner = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=Phase15BudgetStore(scope_id="phase15-smoke-unsafe"),
        model_port=port,
    )

    report = asyncio.run(runner.run(("case-001",)))

    assert report.status is CopilotSmokeStatus.FAIL
    assert report.promotion_eligible is False
    assert report.fallback_count == 1
    assert report.schema_error_count == 1
    assert report.severe_violation_count == 1


def test_runner_blocks_unknown_usage_and_never_allows_promotion() -> None:
    """已发送但 usage 不明时按完整 reservation 结算，证据只能 BLOCKED。"""

    port = _ScriptedPort(
        SmokeResponse(
            success=True,
            severe_violation=False,
            fallback_used=False,
            schema_valid=True,
            usage=None,
        )
    )
    runner = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=Phase15BudgetStore(scope_id="phase15-smoke-unknown-usage"),
        model_port=port,
    )

    report = asyncio.run(runner.run(("case-001",)))

    assert report.status is CopilotSmokeStatus.BLOCKED
    assert report.promotion_eligible is False
    assert report.unknown_usage_count == 1
    assert report.settled_cost_cny == Decimal("0.06")


def test_unsuccessful_model_response_is_a_failed_smoke_case() -> None:
    """模型明确返回失败时不能被当成健康的 PASS 结果。"""

    port = _ScriptedPort(
        SmokeResponse(
            success=False,
            severe_violation=False,
            fallback_used=False,
            schema_valid=True,
            usage=SmokeUsage(input_tokens=100, output_tokens=50),
        )
    )
    runner = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=Phase15BudgetStore(scope_id="phase15-smoke-unsuccessful"),
        model_port=port,
    )

    report = asyncio.run(runner.run(("case-001",)))

    assert report.status is CopilotSmokeStatus.FAIL
    assert report.promotion_eligible is False
    assert "MODEL_RESPONSE_FAILED" in report.reason_codes


def test_budget_exhaustion_blocks_a_second_runner_without_model_call() -> None:
    """共享 Phase 15 账本耗尽时第二个 Runner 必须在 Port 前停止。"""

    store = Phase15BudgetStore(scope_id="phase15-smoke-exhausted")
    first_port = _ScriptedPort(
        SmokeResponse(
            success=True,
            severe_violation=False,
            fallback_used=False,
            schema_valid=True,
            usage=None,
        )
    )
    first = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=first_port,
    )
    asyncio.run(first.run(tuple(f"case-{index:03d}" for index in range(1, 11))))

    second_port = _ScriptedPort()
    second = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=second_port,
    )
    report = asyncio.run(second.run(("case-011",)))

    assert report.status is CopilotSmokeStatus.BLOCKED
    assert report.promotion_eligible is False
    assert second_port.calls == []
    assert "PHASE15_BUDGET_EXCEEDED" in report.reason_codes


def test_usage_above_case_reservation_is_capped_and_blocked() -> None:
    """异常高 usage 不能把单 case 结算写穿 reservation。"""

    port = _ScriptedPort(
        SmokeResponse(
            success=True,
            severe_violation=False,
            fallback_used=False,
            schema_valid=True,
            usage=SmokeUsage(input_tokens=100_000, output_tokens=100_000),
        )
    )
    store = Phase15BudgetStore(scope_id="phase15-smoke-over-reservation")
    runner = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=port,
    )

    report = asyncio.run(runner.run(("case-001",)))

    assert report.status is CopilotSmokeStatus.BLOCKED
    assert report.promotion_eligible is False
    assert report.settled_cost_cny == Decimal("0.06")
    assert "USAGE_EXCEEDS_RESERVATION" in report.reason_codes


def test_runner_replays_same_request_without_second_model_call_and_respects_budget() -> None:
    """相同 case 重放只复用结果；共享 0.60 元账本不能越界。"""

    port = _ScriptedPort()
    store = Phase15BudgetStore(scope_id="phase15-smoke-replay")
    runner = CopilotSmokeRunner(
        config=_config(),
        preflight=_preflight(),
        budget_store=store,
        model_port=port,
    )
    cases = tuple(f"case-{index:03d}" for index in range(1, 11))

    first = asyncio.run(runner.run(cases))
    replay = asyncio.run(runner.run(cases))

    assert first.status is CopilotSmokeStatus.PASS
    assert replay.status is CopilotSmokeStatus.PASS
    assert len(port.calls) == 10
    assert replay.duplicate_request_count == 10
    assert store.snapshot().committed_cny <= Decimal("0.60")

    with pytest.raises(ValueError, match="10"):
        asyncio.run(runner.run((*cases, "case-011")))
