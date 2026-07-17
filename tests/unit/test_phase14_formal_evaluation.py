"""Phase 14 Task 11 正式评估预检与 smoke 预算契约测试。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from src.decision_support.evaluation import build_phase14_dataset
from src.decision_support.formal_evaluation import (
    FormalEvaluationConfig,
    FormalEvaluationStatus,
    FormalPreflightResult,
    SmokeUsage,
    execute_smoke,
    preflight_formal_evaluation,
    run_scripted_formal_rehearsal,
    settle_smoke_cost,
)


ROOT = Path(__file__).parents[2]


def _config(dataset) -> FormalEvaluationConfig:
    """构造测试用完整冻结身份；生产预检仍会重新核对这些摘要。"""

    return FormalEvaluationConfig(
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        temperature=Decimal("0"),
        max_smoke_cases=10,
        budget_cny=Decimal("1.00"),
        reserved_case_budget_cny=Decimal("0.10"),
        manifest_id=dataset.manifest.manifest_id,
        manifest_digest=dataset.manifest.manifest_digest,
        dataset_digest=dataset.manifest.dataset_digest,
        code_digest="a" * 64,
        prompt_digest="b" * 64,
        schema_digest="c" * 64,
        pricing_source_digest="d" * 64,
        usage_required=True,
    )


def test_preflight_blocks_missing_artifact_hashes_before_model_send() -> None:
    """endpoint、价格、usage、Prompt/Schema/数据/代码摘要缺一不可。"""

    dataset = build_phase14_dataset(seed=20260718)
    config = _config(dataset)
    result = preflight_formal_evaluation(
        config,
        manifest=dataset.manifest.model_dump(mode="json"),
        actual_artifacts={"dataset_digest": dataset.manifest.dataset_digest},
    )

    assert result.can_send is False
    assert result.status is FormalEvaluationStatus.INCONCLUSIVE
    assert "ARTIFACT_DIGEST_MISSING" in result.reason_codes


def test_preflight_accepts_exact_frozen_identity_and_enforces_ten_case_cap() -> None:
    """只有精确 Manifest/Artifact 身份和预算内的十例上限才能解锁发送。"""

    dataset = build_phase14_dataset(seed=20260718)
    config = _config(dataset)
    artifacts = {
        "dataset_digest": dataset.manifest.dataset_digest,
        "code_digest": config.code_digest,
        "prompt_digest": config.prompt_digest,
        "schema_digest": config.schema_digest,
        "pricing_source_digest": config.pricing_source_digest,
    }
    result = preflight_formal_evaluation(
        config,
        manifest=dataset.manifest.model_dump(mode="json"),
        actual_artifacts=artifacts,
    )

    assert result.can_send is True
    assert result.status is FormalEvaluationStatus.PASS
    assert result.max_smoke_cases == 10


def test_scripted_rehearsal_is_reproducible_but_not_real_model_pass() -> None:
    """ScriptedModel 只能证明离线流程可重放，不能冒充真实模型正式通过。"""

    first = run_scripted_formal_rehearsal(ROOT / "evaluation")
    second = run_scripted_formal_rehearsal(ROOT / "evaluation")

    assert first == second
    assert first.status is FormalEvaluationStatus.INCONCLUSIVE
    assert first.model_call_count == 0
    assert first.settled_cost_cny == Decimal("0")
    assert "REAL_MODEL_SMOKE_NOT_RUN" in first.reason_codes


def test_unknown_usage_is_conservatively_settled_at_reservation_cap() -> None:
    """已发送但 usage 缺失时按预留上限结算，不能释放未证实费用。"""

    assert settle_smoke_cost(
        reserved_cny=Decimal("0.10"),
        usage=None,
        input_price_cny_per_million=Decimal("1.008"),
        output_price_cny_per_million=Decimal("2.016"),
    ) == Decimal("0.10")
    assert settle_smoke_cost(
        reserved_cny=Decimal("0.10"),
        usage=SmokeUsage(input_tokens=1000, output_tokens=1000),
        input_price_cny_per_million=Decimal("1.008"),
        output_price_cny_per_million=Decimal("2.016"),
    ) == Decimal("0.003024")


def test_invalid_preflight_never_calls_model_port() -> None:
    """无可信预检结果时 execute_smoke 在第一个 await 前 fail-closed。"""

    class _UnexpectedModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _case):
            self.calls += 1
            raise AssertionError("model must not be called")

    dataset = build_phase14_dataset(seed=20260718)
    config = _config(dataset)
    preflight = preflight_formal_evaluation(
        config,
        manifest=dataset.manifest.model_dump(mode="json"),
        actual_artifacts={},
    )
    model = _UnexpectedModel()
    with pytest.raises(ValueError, match="preflight"):
        asyncio.run(execute_smoke(preflight=preflight, cases=dataset.cases[:1], model_port=model))
    assert model.calls == 0


def test_forged_preflight_result_cannot_open_the_model_send_gate() -> None:
    """即使同进程代码构造 can_send=True，也不能伪造正式预检来源。"""

    class _UnexpectedModel:
        async def complete(self, _case):
            raise AssertionError("model must not be called")

    forged = FormalPreflightResult.model_construct(
        status=FormalEvaluationStatus.PASS,
        can_send=True,
        reason_codes=(),
        max_smoke_cases=10,
        reserved_case_budget_cny=Decimal("0.10"),
    )
    with pytest.raises(ValueError, match="preflight"):
        asyncio.run(
            execute_smoke(
                preflight=forged,
                cases=[{"case_id": "forged"}],
                model_port=_UnexpectedModel(),
            )
        )


def test_valid_preflight_records_unknown_usage_as_inconclusive_without_fallback() -> None:
    """可信预检允许 smoke，usage 缺失只降级结论而不伪装为 PASS。"""

    class _UnknownUsageModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, _case):
            self.calls += 1
            return {"success": True, "severe_violation": False, "fallback_used": False}

    dataset = build_phase14_dataset(seed=20260718)
    config = _config(dataset)
    preflight = preflight_formal_evaluation(
        config,
        manifest=dataset.manifest.model_dump(mode="json"),
        actual_artifacts={
            "dataset_digest": dataset.manifest.dataset_digest,
            "code_digest": config.code_digest,
            "prompt_digest": config.prompt_digest,
            "schema_digest": config.schema_digest,
            "pricing_source_digest": config.pricing_source_digest,
        },
    )
    model = _UnknownUsageModel()
    report = asyncio.run(
        execute_smoke(preflight=preflight, cases=dataset.cases[:1], model_port=model)
    )

    assert report.status is FormalEvaluationStatus.INCONCLUSIVE
    assert report.unknown_usage_count == 1
    assert report.settled_cost_cny == Decimal("0.10")
    assert model.calls == 1
