"""Phase 15 Task 6 的 Copilot smoke 发送门与费用事实。

本模块只提供受控的预检、单次 Model Port 调用和 Phase 15 独立预算结算。
它不创建 HTTP 客户端、不读取 API key，也不提供 fallback；真实模型只能由未来
经过环境隔离和官方价格预检的调用方显式注入 ``model_port``。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from enum import StrEnum
from typing import Any, Mapping, Sequence

from pydantic import ConfigDict, Field, PrivateAttr, model_validator

from src.release_gates.budget import (
    PHASE15_BUDGET_CNY,
    Phase15BudgetLimitExceeded,
    Phase15BudgetStore,
    Phase15ReservationState,
)
from src.specialist_runtime.models import StrictFrozenModel


HASH_PATTERN = r"^[0-9a-f]{64}$"
MODEL_ID = "deepseek-v4-flash"
ENDPOINT_HOST = "api.deepseek.com"
TEMPERATURE = Decimal("0")
INPUT_PRICE_CNY_PER_MILLION = Decimal("1.008000")
OUTPUT_PRICE_CNY_PER_MILLION = Decimal("2.016000")
REQUIRED_ARTIFACT_DIGESTS = frozenset(
    {"dataset_digest", "code_digest", "prompt_digest", "schema_digest", "pricing_source_digest"}
)


class CopilotSmokeStatus(StrEnum):
    """Task 6 的发送与 Promotion 证据状态。"""

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"


class CopilotSmokeConfig(StrictFrozenModel):
    """绑定模型、Manifest、价格身份和 0.60 元预算的不可变配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(..., min_length=1)
    manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    dataset_digest: str = Field(..., pattern=HASH_PATTERN)
    code_digest: str = Field(..., pattern=HASH_PATTERN)
    prompt_digest: str = Field(..., pattern=HASH_PATTERN)
    schema_digest: str = Field(..., pattern=HASH_PATTERN)
    pricing_source_digest: str = Field(..., pattern=HASH_PATTERN)
    model_id: str
    endpoint_host: str
    temperature: Decimal
    max_smoke_cases: int = Field(default=10, ge=1, le=10, strict=True)
    budget_cny: Decimal = Field(default=PHASE15_BUDGET_CNY, gt=0)
    reserved_case_budget_cny: Decimal = Field(..., gt=0)
    usage_required: bool = True

    @model_validator(mode="after")
    def _validate_budget_shape(self) -> "CopilotSmokeConfig":
        """在构造时拒绝数学上不可能满足的 case 预留总额。"""

        if self.budget_cny > PHASE15_BUDGET_CNY:
            raise ValueError("phase 15 copilot budget cannot exceed 0.60 CNY")
        if self.reserved_case_budget_cny * self.max_smoke_cases > self.budget_cny:
            raise ValueError("smoke reservations exceed phase 15 budget")
        return self


class SmokeUsage(StrictFrozenModel):
    """Model Port 返回的可计价 token usage；缺失会阻断 Promotion。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(..., ge=0, strict=True)
    output_tokens: int = Field(..., ge=0, strict=True)


class SmokeResponse(StrictFrozenModel):
    """不保存自由文本或推理链的最小模型响应事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    severe_violation: bool
    fallback_used: bool
    schema_valid: bool
    usage: SmokeUsage | None = None


class CopilotSmokePreflight(StrictFrozenModel):
    """由内部预检工厂生成的不可伪造发送门。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CopilotSmokeStatus
    can_send: bool
    reason_codes: tuple[str, ...] = ()
    max_smoke_cases: int = Field(..., ge=1, le=10, strict=True)
    reserved_case_budget_cny: Decimal = Field(..., gt=0)
    _verified: bool = PrivateAttr(default=False)

    @property
    def provenance_verified(self) -> bool:
        """只有模块内部工厂创建的结果才可以打开 Model Port 发送门。"""

        return self._verified


class CopilotSmokeReport(StrictFrozenModel):
    """可重放的 smoke 运行报告；Promotion 资格由本模块确定性计算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CopilotSmokeStatus
    promotion_eligible: bool
    reason_codes: tuple[str, ...] = ()
    model_call_count: int = Field(..., ge=0, strict=True)
    settled_cost_cny: Decimal = Field(..., ge=0)
    unknown_usage_count: int = Field(..., ge=0, strict=True)
    fallback_count: int = Field(..., ge=0, strict=True)
    schema_error_count: int = Field(..., ge=0, strict=True)
    severe_violation_count: int = Field(..., ge=0, strict=True)
    duplicate_request_count: int = Field(..., ge=0, strict=True)


def _verified_preflight(**facts: Any) -> CopilotSmokePreflight:
    """集中设置内部可信标记，调用方不能通过传入 can_send=True 越权。"""

    result = CopilotSmokePreflight.model_validate(facts)
    object.__setattr__(result, "_verified", True)
    return result


def preflight_copilot_smoke(
    config: CopilotSmokeConfig,
    *,
    manifest: Mapping[str, Any],
    actual_artifacts: Mapping[str, str],
    pricing: Mapping[str, str],
    endpoint_available: bool,
) -> CopilotSmokePreflight:
    """在第一次 Model Port 调用前核对全部冻结身份和外部证据。"""

    reasons: list[str] = []
    if config.model_id != MODEL_ID:
        reasons.append("MODEL_ID_MISMATCH")
    if config.endpoint_host != ENDPOINT_HOST:
        reasons.append("ENDPOINT_MISMATCH")
    if not endpoint_available:
        reasons.append("ENDPOINT_UNAVAILABLE")
    if config.temperature != TEMPERATURE:
        reasons.append("TEMPERATURE_NOT_ZERO")
    if not config.usage_required:
        reasons.append("USAGE_ACCOUNTING_DISABLED")
    if manifest.get("manifest_id") != config.manifest_id:
        reasons.append("MANIFEST_ID_MISMATCH")
    if manifest.get("manifest_digest") != config.manifest_digest:
        reasons.append("MANIFEST_DIGEST_MISMATCH")
    for key in sorted(REQUIRED_ARTIFACT_DIGESTS):
        expected = getattr(config, key)
        actual = actual_artifacts.get(key)
        if actual is None:
            reasons.append(f"{key.upper()}_MISSING")
        elif actual != expected:
            reasons.append(f"{key.upper()}_MISMATCH")
        if manifest.get(key) not in {None, expected}:
            # 对外只暴露稳定的 artifact reason code；Manifest 正文和运行时快照
            # 任一漂移都归入同一身份错误，避免调用方通过字符串分支绕过门禁。
            reasons.append(f"{key.upper()}_MISMATCH")
    if pricing.get("model_id") != MODEL_ID or pricing.get("endpoint_host") != ENDPOINT_HOST:
        reasons.append("PRICE_TABLE_MISMATCH")
    if pricing.get("input_cny_per_million") != str(INPUT_PRICE_CNY_PER_MILLION):
        reasons.append("PRICE_TABLE_MISMATCH")
    if pricing.get("output_cny_per_million") != str(OUTPUT_PRICE_CNY_PER_MILLION):
        reasons.append("PRICE_TABLE_MISMATCH")
    if pricing.get("pricing_source_digest") != config.pricing_source_digest:
        reasons.append("PRICE_TABLE_MISMATCH")
    if reasons:
        return _verified_preflight(
            status=CopilotSmokeStatus.BLOCKED,
            can_send=False,
            reason_codes=tuple(sorted(set(reasons))),
            max_smoke_cases=config.max_smoke_cases,
            reserved_case_budget_cny=config.reserved_case_budget_cny,
        )
    return _verified_preflight(
        status=CopilotSmokeStatus.PASS,
        can_send=True,
        reason_codes=(),
        max_smoke_cases=config.max_smoke_cases,
        reserved_case_budget_cny=config.reserved_case_budget_cny,
    )


def _usage_cost(usage: SmokeUsage) -> Decimal:
    """按冻结官方价格计算本次 usage 费用，结果不超过六位小数。"""

    raw = (
        Decimal(usage.input_tokens) * INPUT_PRICE_CNY_PER_MILLION
        + Decimal(usage.output_tokens) * OUTPUT_PRICE_CNY_PER_MILLION
    ) / Decimal("1000000")
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


class CopilotSmokeRunner:
    """执行最多十个受控 case，并把每次发送绑定到独立预算 reservation。"""

    def __init__(
        self,
        *,
        config: CopilotSmokeConfig,
        preflight: CopilotSmokePreflight,
        budget_store: Phase15BudgetStore,
        model_port: Any,
    ) -> None:
        self._config = config
        self._preflight = preflight
        self._budget = budget_store
        self._model_port = model_port
        self._responses: dict[str, SmokeResponse] = {}

    async def run(self, case_ids: Sequence[str]) -> CopilotSmokeReport:
        """在可信预检后执行单次调用；失败只返回报告，不 fallback 到其他 Subject。"""

        if not self._preflight.provenance_verified or not self._preflight.can_send:
            return self._report(
                CopilotSmokeStatus.BLOCKED,
                ("COPILOT_PREFLIGHT_REQUIRED", *self._preflight.reason_codes),
                0,
                Decimal("0"),
                0,
                0,
                0,
                0,
                0,
            )
        if not 1 <= len(case_ids) <= self._preflight.max_smoke_cases:
            raise ValueError("smoke case count exceeds 10")
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("smoke case IDs must be unique")
        reasons: list[str] = []
        settled_cost = Decimal("0")
        model_calls = unknown = fallback = schema_errors = severe = duplicates = 0
        for case_id in case_ids:
            request_id = f"{self._config.manifest_id}:{case_id}"
            if request_id in self._responses:
                duplicates += 1
                continue
            try:
                claim = self._budget.reserve(request_id, self._config.reserved_case_budget_cny)
            except Phase15BudgetLimitExceeded:
                reasons.append("PHASE15_BUDGET_EXCEEDED")
                break
            if not claim.created and claim.record.state is Phase15ReservationState.SETTLED:
                duplicates += 1
                reasons.append("DUPLICATE_REQUEST_REPLAYED")
                continue
            response = await self._model_port.complete(request_id=request_id, case_id=case_id)
            if not isinstance(response, SmokeResponse):
                response = SmokeResponse.model_validate(response)
            self._responses[request_id] = response
            model_calls += 1
            if not response.success:
                reasons.append("MODEL_RESPONSE_FAILED")
            if response.usage is None:
                unknown += 1
                settled = self._budget.settle(request_id, None)
                reasons.append("USAGE_UNKNOWN_SETTLED_AT_RESERVATION")
            else:
                cost = _usage_cost(response.usage)
                if cost > self._config.reserved_case_budget_cny:
                    # 已知 usage 超过发送前 reservation 时不能把超额写入账本；
                    # 以 reservation 封顶关闭事实，并阻断 Promotion，避免模型
                    # 返回异常 usage 后突破本阶段 0.60 元硬上限。
                    settled = self._budget.settle(request_id, None)
                    reasons.append("USAGE_EXCEEDS_RESERVATION")
                else:
                    settled = self._budget.settle(request_id, cost)
            settled_cost += settled.settled_amount_cny or Decimal("0")
            if response.fallback_used:
                fallback += 1
                reasons.append("FALLBACK_USED")
            if not response.schema_valid:
                schema_errors += 1
                reasons.append("SCHEMA_INVALID")
            if response.severe_violation:
                severe += 1
                reasons.append("SEVERE_VIOLATION")
        if "PHASE15_BUDGET_EXCEEDED" in reasons:
            status = CopilotSmokeStatus.BLOCKED
        elif not model_calls and duplicates == len(case_ids):
            # 完整重放只复用此前已经结算的事实；没有新的模型调用不应被当作失败。
            status = CopilotSmokeStatus.PASS
        elif any(code in reasons for code in ("MODEL_RESPONSE_FAILED", "FALLBACK_USED", "SCHEMA_INVALID", "SEVERE_VIOLATION")):
            status = CopilotSmokeStatus.FAIL
        elif unknown or "USAGE_EXCEEDS_RESERVATION" in reasons:
            status = CopilotSmokeStatus.BLOCKED
        else:
            status = CopilotSmokeStatus.PASS
        eligible = status is CopilotSmokeStatus.PASS and model_calls + duplicates == len(case_ids) and len(case_ids) == 10
        return self._report(
            status,
            tuple(sorted(set(reasons))),
            model_calls,
            settled_cost,
            unknown,
            fallback,
            schema_errors,
            severe,
            duplicates,
            promotion_eligible=eligible,
        )

    @staticmethod
    def _report(
        status: CopilotSmokeStatus,
        reason_codes: tuple[str, ...],
        model_calls: int,
        settled_cost: Decimal,
        unknown: int,
        fallback: int,
        schema_errors: int,
        severe: int,
        duplicates: int,
        *,
        promotion_eligible: bool = False,
    ) -> CopilotSmokeReport:
        return CopilotSmokeReport(
            status=status,
            promotion_eligible=promotion_eligible,
            reason_codes=reason_codes,
            model_call_count=model_calls,
            settled_cost_cny=settled_cost,
            unknown_usage_count=unknown,
            fallback_count=fallback,
            schema_error_count=schema_errors,
            severe_violation_count=severe,
            duplicate_request_count=duplicates,
        )
