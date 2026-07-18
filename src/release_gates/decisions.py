"""Phase 15 ReleaseRun、CaseResult 和双轨发布结论模型。

技术发布与 Decision Support 晋升是两个独立状态机。技术门禁只由冻结 case
结果决定；Copilot 晋升必须同时具备模型和真人证据，最终状态由 Store 按固定
映射生成，调用方不能手工拼接“已发布且未晋升”的矛盾状态。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from src.release_gates.models import EvaluationCaseStatus
from src.specialist_runtime.models import (
    EvidenceRef,
    StrictFrozenModel,
    _freeze_json,
    _plain_json,
    canonical_json_sha256,
)


HASH_PATTERN = r"^[0-9a-f]{64}$"
_SENSITIVE_KEYS = frozenset(
    {"free_text", "raw_text", "chain_of_thought", "prompt", "secret", "token", "embedding"}
)


def _contains_sensitive(value: Any) -> bool:
    """递归检查 Store 输入，避免调用方绕过 Runner 直接持久化敏感载荷。"""

    if isinstance(value, dict):
        return any(
            str(key).lower() in _SENSITIVE_KEYS or _contains_sensitive(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive(item) for item in value)
    return False


class ReleaseMode(StrEnum):
    """三层本地/托管 Release 运行模式。"""

    PR = "PR"
    NIGHTLY = "NIGHTLY"
    RELEASE = "RELEASE"


class ReleaseRunStatus(StrEnum):
    """ReleaseRun 的技术聚合状态。"""

    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class TechnicalReleaseStatus(StrEnum):
    """确定性技术发布结论。"""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class PromotionStatus(StrEnum):
    """Decision Support 晋升结论，不代表技术 Runtime 是否发布。"""

    PROMOTE = "PROMOTE"
    KEEP_DISABLED = "KEEP_DISABLED"
    BLOCKED = "BLOCKED"


class FinalReleaseStatus(StrEnum):
    """技术发布与 Copilot 晋升合成后的唯一对外状态。"""

    RELEASED_DECISION_SUPPORT_ENABLED = "RELEASED_DECISION_SUPPORT_ENABLED"
    RELEASED_DECISION_SUPPORT_DISABLED = "RELEASED_DECISION_SUPPORT_DISABLED"
    NOT_RELEASED = "NOT_RELEASED"


class ReleaseRun(StrictFrozenModel):
    """绑定 Manifest 和完整预期 case 集合的不可变 ReleaseRun。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_run_id: str = Field(..., min_length=1)
    mode: ReleaseMode
    manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    expected_case_ids: tuple[str, ...] = Field(..., min_length=1)
    status: ReleaseRunStatus = ReleaseRunStatus.RUNNING

    @field_validator("expected_case_ids", mode="after")
    @classmethod
    def _unique_case_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """冻结预期 case 顺序和唯一性，缺 case 才能被 Store 明确识别。"""

        if any(not case_id for case_id in value) or len(value) != len(set(value)):
            raise ValueError("expected_case_ids must be non-empty and unique")
        return value


class ReleaseCaseResult(StrictFrozenModel):
    """一次 Release case 的不可变结果，唯一键为 release_run_id/case_id。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_run_id: str = Field(..., min_length=1)
    manifest_digest: str = Field(..., pattern=HASH_PATTERN)
    case_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    subject_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    status: EvaluationCaseStatus
    severe_violation: bool
    rule_codes: tuple[str, ...] = ()
    summary: str = Field(..., min_length=1, max_length=500)
    artifact_digest: str = ""
    output: Any | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_output(cls, value: Any) -> Any:
        """冻结结构化输出，Store 不允许报告层原地改写。"""

        if value is None:
            return None
        plain = _plain_json(value)
        if _contains_sensitive(plain):
            raise ValueError("release case result cannot contain sensitive output")
        return _freeze_json(plain)

    @field_serializer("output", when_used="json")
    def _serialize_output(self, value: Any) -> Any:
        return None if value is None else _plain_json(value)

    @field_validator("evidence_refs", mode="after")
    @classmethod
    def _unique_evidence_refs(cls, value: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
        """保留 Runner 已验证的证据引用，并拒绝同一 case 的重复身份。"""

        ids = tuple(reference.evidence_id for reference in value)
        if len(ids) != len(set(ids)):
            raise ValueError("release case evidence references must be unique")
        return value

    @model_validator(mode="after")
    def _bind_artifact_digest(self) -> "ReleaseCaseResult":
        """把 case 结果身份、状态和输出绑定到 artifact digest。"""

        payload = self.model_dump(mode="json", exclude={"artifact_digest"})
        calculated = canonical_json_sha256(payload)
        if self.artifact_digest and self.artifact_digest != calculated:
            raise ValueError("artifact_digest does not match release case facts")
        object.__setattr__(self, "artifact_digest", calculated)
        return self


class TechnicalReleaseDecision(StrictFrozenModel):
    """由完整 case 集合确定性聚合出的技术发布结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_run_id: str = Field(..., min_length=1)
    status: TechnicalReleaseStatus
    expected_case_count: int = Field(..., ge=1, strict=True)
    completed_case_count: int = Field(..., ge=0, strict=True)
    passed_case_count: int = Field(..., ge=0, strict=True)
    failed_case_count: int = Field(..., ge=0, strict=True)
    blocked_case_count: int = Field(..., ge=0, strict=True)
    severe_violation_count: int = Field(..., ge=0, strict=True)
    blocking_gate_count: int = Field(default=0, ge=0, strict=True)
    case_results_digest: str = Field(..., pattern=HASH_PATTERN)
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_counts(self) -> "TechnicalReleaseDecision":
        """校验计数与状态一致，避免 Store 外部传入自相矛盾的汇总。"""

        if self.completed_case_count != self.passed_case_count + self.failed_case_count + self.blocked_case_count:
            raise ValueError("technical decision counts do not add up")
        if self.completed_case_count > self.expected_case_count:
            raise ValueError("completed cases exceed expected cases")
        if self.status is TechnicalReleaseStatus.PASS:
            if self.completed_case_count != self.expected_case_count or self.failed_case_count or self.blocked_case_count or self.severe_violation_count:
                raise ValueError("technical PASS requires every case to pass without severe violations")
        if (
            self.status is TechnicalReleaseStatus.BLOCKED
            and self.completed_case_count == self.expected_case_count
            and not self.blocked_case_count
            and not self.blocking_gate_count
        ):
            raise ValueError("complete non-blocked cases cannot produce technical BLOCKED")
        return self


class DecisionSupportPromotionDecision(StrictFrozenModel):
    """Copilot 晋升的严格 AND 证据与门槛快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: PromotionStatus
    reason_codes: tuple[str, ...] = ()
    model_evidence_complete: bool = False
    human_evidence_complete: bool = False
    completed_smoke_cases: int = Field(default=0, ge=0, le=10, strict=True)
    severe_violation_count: int = Field(default=0, ge=0, strict=True)
    safety_correctness: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    key_conflict_miss_rate_reduction: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    decision_median_reduction: Decimal = Field(default=Decimal("0"), ge=0, le=1)

    @classmethod
    def blocked(cls, reason_code: str) -> "DecisionSupportPromotionDecision":
        """构造没有外部证据时的 BLOCKED 结论。"""

        return cls(status=PromotionStatus.BLOCKED, reason_codes=(reason_code,))

    @model_validator(mode="after")
    def _check_and_gate(self) -> "DecisionSupportPromotionDecision":
        """固定模型/真人证据、10 case、安全和效率的严格 AND 门。"""

        evidence_complete = self.model_evidence_complete and self.human_evidence_complete
        meets_metrics = (
            self.completed_smoke_cases == 10
            and self.severe_violation_count == 0
            and self.safety_correctness >= Decimal("0.90")
            and self.key_conflict_miss_rate_reduction >= Decimal("0.30")
            and self.decision_median_reduction >= Decimal("0.20")
        )
        if self.status is PromotionStatus.PROMOTE and (not evidence_complete or not meets_metrics):
            raise ValueError("PROMOTE requires complete evidence and all strict AND gates")
        if self.status is PromotionStatus.KEEP_DISABLED and (not evidence_complete or meets_metrics):
            raise ValueError("KEEP_DISABLED requires complete evidence with a failed quality gate")
        if self.status is PromotionStatus.BLOCKED and evidence_complete:
            raise ValueError("complete external evidence cannot be BLOCKED")
        return self


class FinalReleaseDecision(StrictFrozenModel):
    """技术发布与 Copilot 晋升的确定性合成结论。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release_run_id: str = Field(..., min_length=1)
    technical_status: TechnicalReleaseStatus
    promotion_status: PromotionStatus
    status: FinalReleaseStatus
    reason_codes: tuple[str, ...] = ()
    decision_digest: str = ""

    @model_validator(mode="after")
    def _bind_final_status(self) -> "FinalReleaseDecision":
        """禁止手工把技术失败或证据不足写成已启用。"""

        expected = (
            FinalReleaseStatus.NOT_RELEASED
            if self.technical_status is not TechnicalReleaseStatus.PASS
            else FinalReleaseStatus.RELEASED_DECISION_SUPPORT_ENABLED
            if self.promotion_status is PromotionStatus.PROMOTE
            else FinalReleaseStatus.RELEASED_DECISION_SUPPORT_DISABLED
        )
        if self.status is not expected:
            raise ValueError("final status does not match technical/promotion conclusions")
        payload = self.model_dump(mode="json", exclude={"decision_digest"})
        calculated = canonical_json_sha256(payload)
        if self.decision_digest and self.decision_digest != calculated:
            raise ValueError("decision_digest does not match release conclusions")
        object.__setattr__(self, "decision_digest", calculated)
        return self
