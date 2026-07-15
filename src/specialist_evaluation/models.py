"""Phase 13 Task 5 的冻结评估身份、Attempt、指标和去留模型。"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from src.specialist_runtime.models import (
    StrictFrozenModel,
    _freeze_json,
    _plain_json,
    canonical_json_sha256,
)


HASH_PATTERN = r"^[0-9a-f]{64}$"
_FORMAL_MANIFEST_AUTHORIZATION_TOKEN = object()
COMMON_AGENT_GATE_IDS = frozenset(
    {"schema_valid", "permission_valid", "evidence_valid", "fallback_absent"}
)


class EvaluationCandidate(StrEnum):
    """Phase 13 三个候选 Specialist 的稳定身份。"""

    LIVE_OPS = "LIVE_OPS"
    PLANNER = "PLANNER"
    REVIEW_MEMORY = "REVIEW_MEMORY"


class EvaluationSplit(StrEnum):
    """评估样本的冻结分层。"""

    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


class EvaluationSubject(StrEnum):
    """同一 case 的确定性 baseline 与 Specialist 配对主体。"""

    BASELINE = "BASELINE"
    AGENT = "AGENT"


class RetentionDecision(StrEnum):
    """候选进入生产装配前唯一允许的三种结论。"""

    RETAINED = "RETAINED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class EvaluationManifestKind(StrEnum):
    """区分可审计的数据集基线与绑定最终 Git 身份的正式评估清单。"""

    DATASET_BASELINE = "DATASET_BASELINE"
    FORMAL_EVALUATION = "FORMAL_EVALUATION"


class FormalManifestAuthorization(StrictFrozenModel):
    """Task 11 完成 Git 与代码预检后生成的进程内可信注册证据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(..., min_length=1)
    manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    source_commit: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    code_digest: str = Field(..., pattern=HASH_PATTERN)
    _verified_identity: tuple[str, str, str, str] | None = PrivateAttr(default=None)

    def _identity(self) -> tuple[str, str, str, str]:
        return (self.manifest_id, self.manifest_digest, self.source_commit, self.code_digest)

    @model_validator(mode="after")
    def _require_internal_factory(self, info: ValidationInfo) -> "FormalManifestAuthorization":
        if self.provenance_verified:
            return self
        if (info.context or {}).get("formal_manifest_token") is not _FORMAL_MANIFEST_AUTHORIZATION_TOKEN:
            raise ValueError("formal manifest authorization requires internal factory")
        object.__setattr__(self, "_verified_identity", self._identity())
        return self

    @property
    def provenance_verified(self) -> bool:
        """字段仍与内部预检时绑定身份完全一致时才保持可信。"""

        return self._verified_identity == self._identity()


def _build_formal_manifest_authorization(
    manifest: "EvaluationManifest",
) -> FormalManifestAuthorization:
    """供已完成 Git/源码核验的 Task 11 预检边界构造注册证据。"""

    if manifest.manifest_kind is not EvaluationManifestKind.FORMAL_EVALUATION:
        raise ValueError("only formal evaluation manifests can be authorized")
    return FormalManifestAuthorization.model_validate(
        {
            "manifest_id": manifest.manifest_id,
            "manifest_digest": manifest.manifest_digest,
            "source_commit": manifest.source_commit,
            "code_digest": manifest.code_digest,
        },
        context={"formal_manifest_token": _FORMAL_MANIFEST_AUTHORIZATION_TOKEN},
    )


class EvaluationManifest(StrictFrozenModel):
    """绑定数据、代码、Schema、价格和模型身份的不可变评估清单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str = Field(..., min_length=1)
    manifest_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    manifest_kind: EvaluationManifestKind
    source_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    dataset_digest: str = Field(..., pattern=HASH_PATTERN)
    schema_digest: str = Field(..., pattern=HASH_PATTERN)
    generator_digest: str = Field(..., pattern=HASH_PATTERN)
    seed: int = Field(..., ge=0, le=9_223_372_036_854_775_807, strict=True)
    development_case_ids: tuple[str, ...] = Field(..., min_length=1)
    validation_case_ids: tuple[str, ...] = Field(..., min_length=1)
    holdout_case_ids: tuple[str, ...] = Field(..., min_length=1)
    case_candidate_map: Any
    profile_bundle_digest: str = Field(..., pattern=HASH_PATTERN)
    prompt_bundle_digest: str = Field(..., pattern=HASH_PATTERN)
    result_schema_bundle_digest: str = Field(..., pattern=HASH_PATTERN)
    pricing_source_digest: str = Field(..., pattern=HASH_PATTERN)
    temperature: Decimal
    code_digest: str = Field(..., pattern=HASH_PATTERN)
    price_policy_digest: str = Field(..., pattern=HASH_PATTERN)
    endpoint_host: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    candidate_ids: tuple[str, ...] = Field(..., min_length=1)
    manifest_digest: str = Field(default="", pattern=HASH_PATTERN)

    @field_validator("candidate_ids", mode="after")
    @classmethod
    def _normalize_candidates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = {item.value for item in EvaluationCandidate}
        if set(value) != expected or len(value) != len(expected):
            raise ValueError("candidate_ids must exactly match frozen Phase 13 candidates")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def _verify_digest(self) -> "EvaluationManifest":
        if (
            self.manifest_kind is EvaluationManifestKind.FORMAL_EVALUATION
            and self.source_commit is None
        ):
            raise ValueError("formal evaluation manifest requires source_commit")
        if (
            self.manifest_kind is EvaluationManifestKind.DATASET_BASELINE
            and self.source_commit is not None
        ):
            raise ValueError("dataset baseline manifest cannot claim source_commit")
        all_case_ids = (
            *self.development_case_ids,
            *self.validation_case_ids,
            *self.holdout_case_ids,
        )
        if len(all_case_ids) != len(set(all_case_ids)):
            raise ValueError("manifest case IDs must be unique across splits")
        if (
            len(self.development_case_ids) != 60
            or len(self.validation_case_ids) != 120
            or len(self.holdout_case_ids) != 60
        ):
            raise ValueError("manifest split sizes must be 60/120/60")
        if self.temperature != Decimal("0"):
            raise ValueError("formal evaluation temperature must be zero")
        candidate_map = _plain_json(self.case_candidate_map)
        if set(candidate_map) != set(all_case_ids):
            raise ValueError("case_candidate_map must cover every case exactly once")
        for split_ids, expected_count in (
            (self.development_case_ids, 20),
            (self.validation_case_ids, 40),
            (self.holdout_case_ids, 20),
        ):
            for candidate in EvaluationCandidate:
                if sum(candidate_map[case_id] == candidate.value for case_id in split_ids) != expected_count:
                    raise ValueError("case_candidate_map must preserve candidate 20/40/20 splits")
        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        calculated = canonical_json_sha256(payload)
        if self.manifest_digest and self.manifest_digest != calculated:
            raise ValueError("manifest_digest does not match manifest facts")
        object.__setattr__(self, "manifest_digest", calculated)
        return self

    @field_validator("temperature", mode="after")
    @classmethod
    def _normalize_temperature(cls, value: Decimal) -> Decimal:
        if value != Decimal("0"):
            raise ValueError("formal evaluation temperature must be zero")
        return Decimal("0")

    @field_validator("case_candidate_map", mode="after")
    @classmethod
    def _freeze_candidate_map(cls, value: Any) -> Any:
        return _freeze_json(value)

    @field_serializer("case_candidate_map", when_used="json")
    def _serialize_candidate_map(self, value: Any) -> Any:
        return _plain_json(value)


class EvaluationRun(StrictFrozenModel):
    """一次候选评估运行；重跑产生新 run 或新 Attempt，不覆盖旧事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    manifest_id: str = Field(..., min_length=1)
    manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    candidate: EvaluationCandidate
    status: str = Field(default="RUNNING", pattern=r"^(RUNNING|COMPLETED|FAILED|CANCELLED)$")


class EvaluationRunClaim(StrictFrozenModel):
    """Worker 对 EvaluationRun 的有界租约，不改变冻结 Run 身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    lease_until: datetime
    claim_version: int = Field(..., ge=1, strict=True)

    @field_validator("lease_until")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease_until requires timezone")
        return value


class CaseAttempt(StrictFrozenModel):
    """单个 case/subject 的一次尝试；失败重跑只追加 Attempt。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    manifest_id: str = Field(..., min_length=1)
    candidate: EvaluationCandidate
    case_id: str = Field(..., min_length=1)
    split: EvaluationSplit
    subject: EvaluationSubject
    attempt_number: int = Field(..., ge=1, le=2_147_483_647, strict=True)
    success: bool
    severe_violation: bool
    infrastructure_failure: bool
    latency_ms: Decimal = Field(..., ge=0, le=Decimal("999999999.999"))
    input_tokens: int = Field(..., ge=0, le=2_147_483_647, strict=True)
    output_tokens: int = Field(..., ge=0, le=2_147_483_647, strict=True)
    cost_cny: Decimal = Field(..., ge=0, le=Decimal("999999.999999"))
    result_digest: str = Field(..., pattern=HASH_PATTERN)
    metric_outcomes: Any
    gate_results: Any
    output: Any | None = None

    @field_validator("metric_outcomes", mode="after")
    @classmethod
    def _freeze_metric_outcomes(cls, value: Any) -> Any:
        """冻结每个业务指标的独立布尔事实，禁止把所有指标折叠为 success。"""

        plain = _plain_json(value)
        if (
            not isinstance(plain, dict)
            or not plain
            or any(not isinstance(key, str) or not key for key in plain)
            or any(type(outcome) is not bool for outcome in plain.values())
        ):
            raise ValueError("metric_outcomes requires non-empty boolean facts")
        return _freeze_json(plain)

    @field_validator("gate_results", mode="after")
    @classmethod
    def _freeze_gate_results(cls, value: Any) -> Any:
        """冻结共同安全门事实；Store 只接受可由 Attempt 证据重算的汇总。"""

        plain = _plain_json(value)
        if not isinstance(plain, dict) or any(type(result) is not bool for result in plain.values()):
            raise ValueError("gate_results requires boolean facts")
        return _freeze_json(plain)

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_output(cls, value: Any) -> Any:
        return None if value is None else _freeze_json(value)

    @field_validator("cost_cny", "latency_ms", mode="after")
    @classmethod
    def _finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("numeric metric must be finite")
        return value

    @field_validator("latency_ms", mode="after")
    @classmethod
    def _latency_precision(cls, value: Decimal) -> Decimal:
        if value != value.quantize(Decimal("0.001")):
            raise ValueError("latency_ms exceeds database precision")
        return value

    @field_validator("cost_cny", mode="after")
    @classmethod
    def _cost_precision(cls, value: Decimal) -> Decimal:
        if value != value.quantize(Decimal("0.000001")):
            raise ValueError("cost_cny exceeds database precision")
        return value

    @model_validator(mode="after")
    def _verify_result_digest(self) -> "CaseAttempt":
        if self.result_digest != canonical_json_sha256(
            None if self.output is None else _plain_json(self.output)
        ):
            raise ValueError("result_digest does not match output")
        return self

    @field_validator("output", mode="before")
    @classmethod
    def _plain_output(cls, value: Any) -> Any:
        return _plain_json(value) if value is not None else None

    @field_serializer("output", when_used="json")
    def _serialize_output(self, value: Any) -> Any:
        return None if value is None else _plain_json(value)

    @field_serializer("metric_outcomes", "gate_results", when_used="json")
    def _serialize_attempt_facts(self, value: Any) -> Any:
        return _plain_json(value)

    @model_validator(mode="after")
    def _verify_outcome_shape(self) -> "CaseAttempt":
        if self.success and self.infrastructure_failure:
            raise ValueError("infrastructure failure cannot be a successful result")
        gate_results = _plain_json(self.gate_results)
        if self.subject is EvaluationSubject.AGENT and set(gate_results) != COMMON_AGENT_GATE_IDS:
            raise ValueError("agent attempt must record every common gate")
        if self.subject is EvaluationSubject.BASELINE and gate_results:
            raise ValueError("baseline attempt cannot claim agent gate results")
        return self


class PairedMetric(StrictFrozenModel):
    """同一 case 配对后的绝对率、差值、胜负和 Wilson 区间。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_id: str = Field(..., min_length=1)
    case_ids: tuple[str, ...] = Field(..., min_length=1)
    sample_count: int = Field(..., ge=1, strict=True)
    baseline_success_count: int = Field(..., ge=0, strict=True)
    agent_success_count: int = Field(..., ge=0, strict=True)
    baseline_rate: Decimal = Field(..., ge=0, le=1)
    agent_rate: Decimal = Field(..., ge=0, le=1)
    delta_percentage_points: Decimal
    paired_wins: int = Field(..., ge=0, strict=True)
    paired_losses: int = Field(..., ge=0, strict=True)
    tied: int = Field(..., ge=0, strict=True)
    severe_violation_count: int = Field(..., ge=0, strict=True)
    baseline_wilson_low: Decimal = Field(..., ge=0, le=1)
    baseline_wilson_high: Decimal = Field(..., ge=0, le=1)
    agent_wilson_low: Decimal = Field(..., ge=0, le=1)
    agent_wilson_high: Decimal = Field(..., ge=0, le=1)
    metric_facts_digest: str = Field(..., pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def _verify_counts(self) -> "PairedMetric":
        if len(self.case_ids) != self.sample_count or len(set(self.case_ids)) != self.sample_count:
            raise ValueError("case_ids must uniquely match sample_count")
        if self.baseline_success_count > self.sample_count or self.agent_success_count > self.sample_count:
            raise ValueError("success count exceeds sample_count")
        if self.paired_wins + self.paired_losses + self.tied != self.sample_count:
            raise ValueError("paired outcome counts must equal sample_count")
        if self.severe_violation_count > self.sample_count:
            raise ValueError("severe violation count exceeds sample_count")
        if self.baseline_wilson_low > self.baseline_wilson_high or self.agent_wilson_low > self.agent_wilson_high:
            raise ValueError("Wilson interval lower bound exceeds upper bound")
        expected_delta = ((self.agent_rate - self.baseline_rate) * 100).quantize(Decimal("0.000001"))
        if self.delta_percentage_points != expected_delta:
            raise ValueError("delta does not match absolute rates")
        for value in (
            self.baseline_rate, self.agent_rate, self.delta_percentage_points,
            self.baseline_wilson_low, self.baseline_wilson_high,
            self.agent_wilson_low, self.agent_wilson_high,
        ):
            if not value.is_finite() or value != value.quantize(Decimal("0.000001")):
                raise ValueError("paired metric decimals require finite six-place precision")
        return self


class RetentionDecisionRecord(StrictFrozenModel):
    """绑定指标摘要的候选去留结论，禁止把证据不足伪装成指标失败。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(..., min_length=1)
    run_id: str = Field(..., min_length=1)
    candidate: EvaluationCandidate
    decision: RetentionDecision
    reason_code: str = Field(..., min_length=1)
    external_evidence_sufficient: bool
    severe_violation_count: int = Field(..., ge=0, strict=True)
    metrics_digest: str = Field(..., pattern=HASH_PATTERN)
    completed_validation_cases: int = Field(default=0, ge=0, le=40, strict=True)
    completed_holdout_cases: int = Field(default=0, ge=0, le=20, strict=True)
    hard_gates_passed: bool = False

    @model_validator(mode="after")
    def _verify_gate_semantics(self) -> "RetentionDecisionRecord":
        if self.decision is RetentionDecision.INCONCLUSIVE and self.external_evidence_sufficient:
            raise ValueError("INCONCLUSIVE requires insufficient external evidence")
        if self.decision is not RetentionDecision.INCONCLUSIVE and not self.external_evidence_sufficient:
            raise ValueError("insufficient external evidence requires INCONCLUSIVE")
        if self.decision is RetentionDecision.RETAINED and self.severe_violation_count != 0:
            raise ValueError("RETAINED requires zero severe violations")
        if self.decision is RetentionDecision.RETAINED and (
            not self.external_evidence_sufficient
            or self.completed_validation_cases != 40
            or self.completed_holdout_cases != 20
            or not self.hard_gates_passed
        ):
            raise ValueError("RETAINED requires 40 validation, 20 holdout and all hard gates")
        return self


__all__ = [
    "CaseAttempt",
    "EvaluationCandidate",
    "EvaluationManifest",
    "EvaluationRun",
    "EvaluationRunClaim",
    "EvaluationSplit",
    "EvaluationSubject",
    "PairedMetric",
    "RetentionDecision",
    "RetentionDecisionRecord",
    "canonical_json_sha256",
]
