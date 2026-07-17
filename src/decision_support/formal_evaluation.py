"""Phase 14 Task 11 的正式模型预检、Scripted rehearsal 和 smoke 结论。

本模块把“可以发送真实请求”的条件集中为一个可审计结果。调用方不能只传
model_id 或 endpoint 就绕过 Prompt/Schema/数据/代码哈希和预算门禁；没有可信
预检结果时，``execute_smoke`` 在第一次 await 前直接拒绝，因此不会产生外部模型
副作用。当前默认演练不访问网络，真实 smoke 仍需由受控环境显式提供 Model Port。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from src.decision_support.evaluation import (
    Phase14Dataset,
    build_phase14_dataset,
    run_scripted_evaluation,
)


HASH_PATTERN = r"^[0-9a-f]{64}$"
PHASE14_MODEL_ID = "deepseek-v4-flash"
PHASE14_ENDPOINT_HOST = "api.deepseek.com"
PHASE14_BUDGET_CNY = Decimal("1.00")
REQUIRED_ARTIFACT_DIGESTS = frozenset(
    {"dataset_digest", "code_digest", "prompt_digest", "schema_digest", "pricing_source_digest"}
)


class FormalEvaluationStatus(StrEnum):
    """Task 11 的严格外部证据结论。"""

    PASS = "PASS"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"


class SmokeUsage(BaseModel):
    """模型返回的可计价 token usage；缺失由调用方显式传 None。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(..., ge=0, strict=True)
    output_tokens: int = Field(..., ge=0, strict=True)


class FormalEvaluationConfig(BaseModel):
    """真实 smoke 的冻结模型、身份、预算和样本上限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(..., min_length=1)
    endpoint_host: str = Field(..., min_length=1)
    temperature: Decimal
    max_smoke_cases: int = Field(..., ge=1, le=10, strict=True)
    budget_cny: Decimal = Field(..., gt=0, le=PHASE14_BUDGET_CNY)
    reserved_case_budget_cny: Decimal = Field(..., gt=0)
    manifest_id: str = Field(..., min_length=1)
    manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    dataset_digest: str = Field(..., pattern=HASH_PATTERN)
    code_digest: str = Field(..., pattern=HASH_PATTERN)
    prompt_digest: str = Field(..., pattern=HASH_PATTERN)
    schema_digest: str = Field(..., pattern=HASH_PATTERN)
    pricing_source_digest: str = Field(..., pattern=HASH_PATTERN)
    usage_required: bool = True

    @model_validator(mode="after")
    def _verify_config(self) -> "FormalEvaluationConfig":
        """发送前锁定零温度和完整 case reservation，阻止预算外 smoke。"""

        if self.temperature != Decimal("0"):
            raise ValueError("formal evaluation temperature must be zero")
        if self.reserved_case_budget_cny * self.max_smoke_cases > self.budget_cny:
            raise ValueError("smoke reservations exceed phase 14 budget")
        if not self.usage_required:
            raise ValueError("formal evaluation requires usage accounting")
        return self


class FormalPreflightResult(BaseModel):
    """由预检产生的不可变发送门；失败时只允许记录 INCONCLUSIVE。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: FormalEvaluationStatus
    can_send: bool
    reason_codes: tuple[str, ...]
    max_smoke_cases: int
    reserved_case_budget_cny: Decimal
    _verified: bool = PrivateAttr(default=False)

    @property
    def provenance_verified(self) -> bool:
        """只有本模块预检工厂创建的结果才有资格打开真实发送门。"""

        return self._verified


def _verified_preflight(**facts: Any) -> FormalPreflightResult:
    """内部构造可信预检结果，阻止调用方伪造 can_send 字段。"""

    result = FormalPreflightResult.model_validate(facts)
    object.__setattr__(result, "_verified", True)
    return result


class SmokeResponse(BaseModel):
    """真实/测试 Model Port 的结构化结果，不保存 chain-of-thought。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    severe_violation: bool
    fallback_used: bool
    usage: SmokeUsage | None = None


class FormalEvaluationReport(BaseModel):
    """Scripted 或 smoke 运行的可审计结论和费用事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: FormalEvaluationStatus
    reason_codes: tuple[str, ...]
    model_call_count: int = Field(..., ge=0, strict=True)
    settled_cost_cny: Decimal = Field(..., ge=0)
    unknown_usage_count: int = Field(..., ge=0, strict=True)
    fallback_count: int = Field(..., ge=0, strict=True)
    severe_violation_count: int = Field(..., ge=0, strict=True)
    scripted_gate_passed: bool


def preflight_formal_evaluation(
    config: FormalEvaluationConfig,
    *,
    manifest: Mapping[str, Any],
    actual_artifacts: Mapping[str, str],
) -> FormalPreflightResult:
    """逐项核对模型、Manifest、源码/Prompt/Schema/价格和预算身份。"""

    reasons: list[str] = []
    if config.model_id != PHASE14_MODEL_ID:
        reasons.append("MODEL_ID_MISMATCH")
    if config.endpoint_host != PHASE14_ENDPOINT_HOST:
        reasons.append("ENDPOINT_MISMATCH")
    if config.temperature != Decimal("0"):
        reasons.append("TEMPERATURE_NOT_ZERO")
    if config.max_smoke_cases > 10:
        reasons.append("SMOKE_CASE_LIMIT_EXCEEDED")
    if config.budget_cny > PHASE14_BUDGET_CNY:
        reasons.append("PHASE14_BUDGET_EXCEEDED")
    if not config.usage_required:
        reasons.append("USAGE_ACCOUNTING_DISABLED")
    if manifest.get("manifest_id") != config.manifest_id:
        reasons.append("MANIFEST_ID_MISMATCH")
    if manifest.get("manifest_digest") != config.manifest_digest:
        reasons.append("MANIFEST_DIGEST_MISMATCH")
    if manifest.get("dataset_digest") != config.dataset_digest:
        reasons.append("DATASET_DIGEST_MISMATCH")
    missing = REQUIRED_ARTIFACT_DIGESTS - set(actual_artifacts)
    if missing:
        reasons.append("ARTIFACT_DIGEST_MISSING")
    for key in sorted(REQUIRED_ARTIFACT_DIGESTS & set(actual_artifacts)):
        if actual_artifacts[key] != getattr(config, key):
            reasons.append(f"{key.upper()}_MISMATCH")
    if reasons:
        return _verified_preflight(
            status=FormalEvaluationStatus.INCONCLUSIVE,
            can_send=False,
            reason_codes=tuple(sorted(set(reasons))),
            max_smoke_cases=config.max_smoke_cases,
            reserved_case_budget_cny=config.reserved_case_budget_cny,
        )
    return _verified_preflight(
        status=FormalEvaluationStatus.PASS,
        can_send=True,
        reason_codes=(),
        max_smoke_cases=config.max_smoke_cases,
        reserved_case_budget_cny=config.reserved_case_budget_cny,
    )


def run_scripted_formal_rehearsal(evaluation_root: Path) -> FormalEvaluationReport:
    """重放 Task 10 ScriptedModel；无真实调用时严格记为 INCONCLUSIVE。"""

    dataset = build_phase14_dataset(seed=20260718)
    persisted_manifest_path = Path(evaluation_root) / "phase14_human_support" / "manifest.json"
    if persisted_manifest_path.exists():
        persisted = json.loads(persisted_manifest_path.read_text(encoding="utf-8"))
        if persisted.get("manifest_digest") != dataset.manifest.manifest_digest:
            return FormalEvaluationReport(
                status=FormalEvaluationStatus.INCONCLUSIVE,
                reason_codes=("DATASET_MANIFEST_MISMATCH",),
                model_call_count=0,
                settled_cost_cny=Decimal("0"),
                unknown_usage_count=0,
                fallback_count=0,
                severe_violation_count=0,
                scripted_gate_passed=False,
            )
    summary = run_scripted_evaluation(dataset)
    return FormalEvaluationReport(
        status=FormalEvaluationStatus.INCONCLUSIVE,
        reason_codes=("REAL_MODEL_SMOKE_NOT_RUN",),
        model_call_count=0,
        settled_cost_cny=Decimal("0"),
        unknown_usage_count=0,
        fallback_count=0,
        severe_violation_count=0,
        scripted_gate_passed=summary.meets_acceptance_gate,
    )


def settle_smoke_cost(
    *,
    reserved_cny: Decimal,
    usage: SmokeUsage | None,
    input_price_cny_per_million: Decimal,
    output_price_cny_per_million: Decimal,
) -> Decimal:
    """按已知 usage 计价；usage 缺失则保守结算整个 reservation。"""

    reserved = Decimal(reserved_cny)
    if not reserved.is_finite() or reserved <= 0:
        raise ValueError("reserved smoke amount must be positive and finite")
    if usage is None:
        return reserved
    raw = (
        Decimal(usage.input_tokens) * Decimal(input_price_cny_per_million)
        + Decimal(usage.output_tokens) * Decimal(output_price_cny_per_million)
    ) / Decimal("1000000")
    settled = raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    if settled > reserved:
        raise ValueError("known usage exceeds reserved smoke amount")
    return settled


async def execute_smoke(
    *,
    preflight: FormalPreflightResult,
    cases: Sequence[Any],
    model_port: Any,
) -> FormalEvaluationReport:
    """只在可信预检门打开时执行最多十例，并记录未知 usage/失败状态。"""

    if (
        not preflight.provenance_verified
        or not preflight.can_send
        or preflight.status is not FormalEvaluationStatus.PASS
    ):
        raise ValueError("formal evaluation preflight is required before model send")
    if not 1 <= len(cases) <= preflight.max_smoke_cases:
        raise ValueError("smoke case count exceeds preflight limit")
    settled = Decimal("0")
    unknown_usage = 0
    fallback_count = 0
    severe_count = 0
    reasons: list[str] = []
    for case in cases:
        response = await model_port.complete(case)
        response = response if isinstance(response, SmokeResponse) else SmokeResponse.model_validate(response)
        settled += settle_smoke_cost(
            reserved_cny=preflight.reserved_case_budget_cny,
            usage=response.usage,
            input_price_cny_per_million=Decimal("1.008"),
            output_price_cny_per_million=Decimal("2.016"),
        )
        if response.usage is None:
            unknown_usage += 1
        if response.fallback_used:
            fallback_count += 1
            reasons.append("FALLBACK_USED")
        if response.severe_violation:
            severe_count += 1
            reasons.append("SEVERE_VIOLATION")
    if severe_count or fallback_count:
        status = FormalEvaluationStatus.FAIL
    elif unknown_usage:
        status = FormalEvaluationStatus.INCONCLUSIVE
        reasons.append("USAGE_UNKNOWN_SETTLED_AT_RESERVATION")
    else:
        status = FormalEvaluationStatus.PASS
    return FormalEvaluationReport(
        status=status,
        reason_codes=tuple(sorted(set(reasons))),
        model_call_count=len(cases),
        settled_cost_cny=settled,
        unknown_usage_count=unknown_usage,
        fallback_count=fallback_count,
        severe_violation_count=severe_count,
        scripted_gate_passed=False,
    )
