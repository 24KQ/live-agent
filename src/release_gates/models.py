"""Phase 15 Subject Runner 的冻结身份、观察结果和评估结果模型。

本模块只保存可审计的结构化事实，不执行 Skill、不调用模型，也不允许调用方
通过 ``model_copy(update=...)`` 修改已经绑定摘要的身份。实际执行入口位于
``runner.py``，规则优先判定位于 ``rules.py``。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
import re
from typing import Any, Mapping

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from src.specialist_runtime.models import (
    EvidenceKind,
    EvidenceRef,
    StrictFrozenModel,
    _freeze_json,
    _plain_json,
    canonical_json_sha256,
)


class SubjectKind(StrEnum):
    """Release Gate 允许执行的五类封闭 Subject。"""

    SKILL_RUNTIME = "SKILL_RUNTIME"
    PLAN_ENGINE = "PLAN_ENGINE"
    EVENT_RUNTIME = "EVENT_RUNTIME"
    DECISION_SUPPORT = "DECISION_SUPPORT"
    LIFECYCLE = "LIFECYCLE"


class EvaluationCaseStatus(StrEnum):
    """单个 Golden case 的技术评估终态。"""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class SkillInvocation(StrictFrozenModel):
    """Subject 声明的一次 Skill 调用快照，只包含版本和最小审计事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_id: str = Field(..., min_length=1)
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    arguments: Any = Field(default_factory=dict)

    @field_validator("arguments", mode="after")
    @classmethod
    def _freeze_arguments(cls, value: Any) -> Any:
        """冻结参数，避免规则校验后调用方再改变审计输入。"""

        return _freeze_json(_plain_json(value))

    @field_serializer("arguments", when_used="json")
    def _serialize_arguments(self, value: Any) -> Any:
        return _plain_json(value)


class SubjectManifest(StrictFrozenModel):
    """一次 Release Subject 的版本、权限、输出和资源预算快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str = Field(..., min_length=1)
    subject_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    subject_kind: SubjectKind
    allowed_skill_versions: Mapping[str, str] = Field(default_factory=dict)
    required_evidence_kinds: tuple[EvidenceKind, ...] = ()
    allowed_plan_states: tuple[str, ...] = ()
    allowed_event_states: tuple[str, ...] = ()
    result_schema: Any = Field(default_factory=lambda: {"type": "object"})
    max_model_calls: int = Field(default=0, ge=0, strict=True)
    max_skill_calls: int = Field(default=0, ge=0, strict=True)
    max_cost_cny: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    no_fallback: bool = True
    manifest_digest: str = ""

    @field_validator("allowed_skill_versions", mode="after")
    @classmethod
    def _freeze_skill_versions(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """冻结 Skill 版本白名单，并拒绝空身份或非语义版本。"""

        if not isinstance(value, Mapping):
            raise ValueError("allowed_skill_versions must be an object")
        if any(
            not isinstance(skill_id, str)
            or not skill_id
            or not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            for skill_id, version in value.items()
        ):
            raise ValueError("allowed_skill_versions must map IDs to semantic versions")
        return _freeze_json(dict(sorted(value.items())))

    @field_validator("required_evidence_kinds", mode="after")
    @classmethod
    def _unique_evidence_kinds(cls, value: tuple[EvidenceKind, ...]) -> tuple[EvidenceKind, ...]:
        """把证据类型收敛为稳定的无重复集合语义。"""

        if len(value) != len(set(value)):
            raise ValueError("required_evidence_kinds must be unique")
        return tuple(sorted(value, key=lambda item: item.value))

    @field_validator("result_schema", mode="after")
    @classmethod
    def _freeze_schema(cls, value: Any) -> Any:
        """冻结 JSON Schema，防止摘要计算后替换输出约束。"""

        return _freeze_json(value)

    @field_serializer("allowed_skill_versions", when_used="json")
    def _serialize_skill_versions(self, value: Any) -> Any:
        return _plain_json(value)

    @field_serializer("result_schema", when_used="json")
    def _serialize_schema(self, value: Any) -> Any:
        return _plain_json(value)

    @model_validator(mode="after")
    def _bind_manifest_digest(self) -> "SubjectManifest":
        """绑定整个 Subject 身份，任何字段变化都必须形成新版本摘要。"""

        payload = self.model_dump(mode="json", exclude={"manifest_digest"})
        calculated = canonical_json_sha256(payload)
        if self.manifest_digest and self.manifest_digest != calculated:
            raise ValueError("manifest_digest does not match subject facts")
        object.__setattr__(self, "manifest_digest", calculated)
        return self

    @property
    def subject_digest(self) -> str:
        """提供语义化别名，统一外部报告对 Subject 身份摘要的称呼。"""

        return self.manifest_digest

    @property
    def profile_digest(self) -> str:
        """兼容 Phase 13 Profile 报告中的摘要命名，不复制或修改身份事实。"""

        return self.manifest_digest


class SubjectObservation(StrictFrozenModel):
    """确定性 Subject 适配器返回的结构化观察事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output: Any | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    skill_invocations: tuple[SkillInvocation, ...] = ()
    model_calls: int = Field(default=0, ge=0, strict=True)
    cost_cny: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    plan_state: str | None = Field(default=None, min_length=1)
    event_state: str | None = Field(default=None, min_length=1)
    write_attempted: bool = False
    cas_conflict: bool = False
    fencing_valid: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1)
    fallback_used: bool = False

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_output(cls, value: Any) -> Any:
        """冻结 Subject 输出，规则层只能读取不能改写。"""

        return None if value is None else _freeze_json(_plain_json(value))

    @field_serializer("output", when_used="json")
    def _serialize_output(self, value: Any) -> Any:
        return None if value is None else _plain_json(value)


class EvaluationCaseResult(StrictFrozenModel):
    """规则优先的单 case 结果和不可变 artifact 摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    subject_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    status: EvaluationCaseStatus
    severe_violation: bool
    rule_codes: tuple[str, ...] = ()
    output: Any | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    summary: str = Field(..., min_length=1, max_length=500)
    artifact_digest: str = ""

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_result_output(cls, value: Any) -> Any:
        return None if value is None else _freeze_json(_plain_json(value))

    @field_serializer("output", when_used="json")
    def _serialize_result_output(self, value: Any) -> Any:
        return None if value is None else _plain_json(value)

    @model_validator(mode="after")
    def _bind_artifact_digest(self) -> "EvaluationCaseResult":
        """摘要绑定结果事实，避免报告层对状态或规则码进行静默替换。"""

        payload = self.model_dump(mode="json", exclude={"artifact_digest"})
        calculated = canonical_json_sha256(payload)
        if self.artifact_digest and self.artifact_digest != calculated:
            raise ValueError("artifact_digest does not match case result facts")
        object.__setattr__(self, "artifact_digest", calculated)
        return self
