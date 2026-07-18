"""Phase 15 规则优先门禁。

规则在模型文本或平均分之前运行。任何版本、权限、证据、状态、CAS、fencing、
幂等、预算、敏感信息或 no-fallback 违规都会形成稳定规则码；Runner 只要看到
一个规则码就不能把该 case 报告为 PASS。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from src.release_gates.models import SubjectManifest, SubjectObservation
from src.specialist_runtime.models import _plain_json


class RuleCode(StrEnum):
    """所有严重规则失败使用的稳定机器码。"""

    SKILL_PERMISSION_OR_VERSION = "SKILL_PERMISSION_OR_VERSION"
    OUTPUT_SCHEMA = "OUTPUT_SCHEMA"
    EVIDENCE_REF = "EVIDENCE_REF"
    PLAN_STATE = "PLAN_STATE"
    EVENT_STATE = "EVENT_STATE"
    CAS_CONFLICT = "CAS_CONFLICT"
    FENCING = "FENCING"
    IDEMPOTENCY = "IDEMPOTENCY"
    SENSITIVE_OUTPUT = "SENSITIVE_OUTPUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    NO_FALLBACK = "NO_FALLBACK"
    MODEL_CALL_LIMIT = "MODEL_CALL_LIMIT"
    SKILL_CALL_LIMIT = "SKILL_CALL_LIMIT"
    SUBJECT_ERROR = "SUBJECT_ERROR"
    DOMAIN = "DOMAIN"


_SENSITIVE_KEYS = frozenset(
    {"free_text", "raw_text", "chain_of_thought", "prompt", "secret", "token", "embedding"}
)


@dataclass(frozen=True)
class RuleEvaluation:
    """规则聚合结果；保留顺序稳定的规则码，供 artifact 和报告重放。"""

    codes: tuple[RuleCode, ...]
    severe_violation: bool

    @property
    def passed(self) -> bool:
        """只有没有任何规则码才允许 case 进入 PASS。"""

        return not self.codes


def _contains_sensitive(value: Any) -> bool:
    """递归检查结构化输出，不保存或回显命中的敏感值。"""

    if isinstance(value, Mapping):
        if any(str(key).lower() in _SENSITIVE_KEYS for key in value):
            return True
        return any(_contains_sensitive(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive(item) for item in value)
    return False


def _append(codes: list[RuleCode], code: RuleCode) -> None:
    """按首次发现顺序去重，避免一个事实产生不稳定重复码。"""

    if code not in codes:
        codes.append(code)


def evaluate_rules(
    case: Any,
    manifest: SubjectManifest,
    observation: SubjectObservation,
) -> RuleEvaluation:
    """对单个 Subject 观察结果执行完整的确定性硬门禁。"""

    del case  # 当前规则只依赖已经校验过的 Case 身份，域绑定由 Runner 负责。
    codes: list[RuleCode] = []

    if manifest.no_fallback and observation.fallback_used:
        _append(codes, RuleCode.NO_FALLBACK)
    if observation.model_calls > manifest.max_model_calls:
        _append(codes, RuleCode.MODEL_CALL_LIMIT)
    if len(observation.skill_invocations) > manifest.max_skill_calls:
        _append(codes, RuleCode.SKILL_CALL_LIMIT)
    if observation.cost_cny > manifest.max_cost_cny:
        _append(codes, RuleCode.BUDGET_EXCEEDED)

    for invocation in observation.skill_invocations:
        if manifest.allowed_skill_versions.get(invocation.skill_id) != invocation.version:
            _append(codes, RuleCode.SKILL_PERMISSION_OR_VERSION)

    if observation.output is None:
        _append(codes, RuleCode.OUTPUT_SCHEMA)
    else:
        try:
            Draft202012Validator.check_schema(_plain_json(manifest.result_schema))
            Draft202012Validator(_plain_json(manifest.result_schema)).validate(
                _plain_json(observation.output)
            )
        except (SchemaError, ValidationError, TypeError, ValueError):
            _append(codes, RuleCode.OUTPUT_SCHEMA)
        if _contains_sensitive(observation.output):
            _append(codes, RuleCode.SENSITIVE_OUTPUT)

    required_kinds = set(manifest.required_evidence_kinds)
    actual_kinds = {reference.kind for reference in observation.evidence_refs}
    evidence_ids = [reference.evidence_id for reference in observation.evidence_refs]
    if not required_kinds.issubset(actual_kinds) or len(evidence_ids) != len(set(evidence_ids)):
        _append(codes, RuleCode.EVIDENCE_REF)

    if manifest.allowed_plan_states and observation.plan_state not in manifest.allowed_plan_states:
        _append(codes, RuleCode.PLAN_STATE)
    if manifest.allowed_event_states and observation.event_state not in manifest.allowed_event_states:
        _append(codes, RuleCode.EVENT_STATE)

    if observation.write_attempted and observation.cas_conflict:
        _append(codes, RuleCode.CAS_CONFLICT)
    if observation.write_attempted and not observation.fencing_valid:
        _append(codes, RuleCode.FENCING)
    if observation.write_attempted and not observation.idempotency_key:
        _append(codes, RuleCode.IDEMPOTENCY)

    return RuleEvaluation(codes=tuple(codes), severe_violation=bool(codes))
