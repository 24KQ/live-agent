"""Phase 15 Task 3 Subject Runner 与规则门禁的 TDD 契约。

这些测试只使用确定性的本地 Subject，不调用模型、数据库、Kafka 或真实平台。
它们先固定五类受限 Runner、版本/权限、证据、CAS/fencing、幂等、敏感信息、
预算和 no-fallback 规则的公开行为。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.release_gates.models import (
    EvaluationCaseStatus,
    SkillInvocation,
    SubjectKind,
    SubjectManifest,
    SubjectObservation,
)
from src.release_gates.rules import RuleCode, evaluate_rules
from src.release_gates.runner import (
    DecisionSupportRunner,
    EventRuntimeRunner,
    LifecycleRunner,
    PlanEngineRunner,
    SkillRuntimeRunner,
)
from src.specialist_runtime.models import EvidenceKind, EvidenceRef
from src.release_gates.dataset import GoldenCase


def _case(domain: str = "RUNTIME_SKILL") -> GoldenCase:
    """构造不含评估标签的最小冻结 case。"""

    split = "validation"
    is_live = domain == "LIVE"
    return GoldenCase.model_validate(
        {
            "case_id": f"phase15-{domain.lower().replace('_', '-')}-{split}-001",
            "split": split,
            "domain": domain,
            "source": "phase14" if is_live else "synthetic",
            "source_case_id": "phase14-case-001" if is_live else None,
            "input": {"facts": {"frozen": True}},
        }
    )


def _evidence() -> EvidenceRef:
    """构造带固定摘要的审计证据引用。"""

    return EvidenceRef(
        kind=EvidenceKind.AUDIT,
        evidence_id="audit-phase15-case-001",
        source_version="1",
        digest="a" * 64,
        room_id="room-phase15",
    )


def _manifest(kind: SubjectKind = SubjectKind.SKILL_RUNTIME) -> SubjectManifest:
    """构造显式版本、Skill 白名单和输出 Schema 的 Subject 身份。"""

    return SubjectManifest(
        subject_id=f"phase15-{kind.value.lower()}",
        subject_version="1.0.0",
        subject_kind=kind,
        allowed_skill_versions={"query_products": "1.0.0"},
        required_evidence_kinds=(EvidenceKind.AUDIT,),
        allowed_plan_states=("SUCCEEDED",),
        allowed_event_states=("APPLIED",),
        result_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["decision"],
            "properties": {"decision": {"type": "string"}},
        },
        max_model_calls=1,
        max_skill_calls=1,
        max_cost_cny=Decimal("0.10"),
    )


def _observation(**overrides: object) -> SubjectObservation:
    """构造通过全部基础门禁的确定性 Subject 观察结果。"""

    values: dict[str, object] = {
        "output": {"decision": "PASS"},
        "evidence_refs": (_evidence(),),
        "skill_invocations": (SkillInvocation(skill_id="query_products", version="1.0.0"),),
        "model_calls": 1,
        "cost_cny": Decimal("0.01"),
        "plan_state": "SUCCEEDED",
        "event_state": "APPLIED",
        "write_attempted": False,
        "cas_conflict": False,
        "fencing_valid": True,
        "idempotency_key": "phase15-idempotency-001",
        "fallback_used": False,
    }
    values.update(overrides)
    return SubjectObservation.model_validate(values)


def test_subject_manifest_digest_and_runner_success_are_stable() -> None:
    """Subject 身份必须绑定权限/Schema，成功结果必须有稳定 artifact digest。"""

    manifest = _manifest()
    assert manifest.profile_digest
    result = SkillRuntimeRunner(manifest).run_case(_case(), _observation())
    assert result.status is EvaluationCaseStatus.PASS
    assert result.severe_violation is False
    assert result.rule_codes == ()
    assert len(result.artifact_digest) == 64


def test_five_constrained_runners_accept_only_their_frozen_domain() -> None:
    """五类 Runner 都必须固定到对应输入域，不能由 case 动态改路由。"""

    runners = (
        (SkillRuntimeRunner(_manifest(SubjectKind.SKILL_RUNTIME)), _case("RUNTIME_SKILL")),
        (PlanEngineRunner(_manifest(SubjectKind.PLAN_ENGINE)), _case("RUNTIME_PLAN")),
        (EventRuntimeRunner(_manifest(SubjectKind.EVENT_RUNTIME)), _case("RUNTIME_EVENT")),
        (DecisionSupportRunner(_manifest(SubjectKind.DECISION_SUPPORT)), _case("LIVE")),
        (LifecycleRunner(_manifest(SubjectKind.LIFECYCLE)), _case("PREPARE")),
    )
    for runner, case in runners:
        result = runner.run_case(case, _observation())
        assert result.status is EvaluationCaseStatus.PASS


def test_runner_rejects_a_case_from_another_domain_before_rules() -> None:
    """调用方不能把 LIVE case 动态投递给 Skill Runtime Runner。"""

    result = SkillRuntimeRunner(_manifest()).run_case(_case("LIVE"), _observation())
    assert result.status is EvaluationCaseStatus.BLOCKED
    assert result.rule_codes == (RuleCode.DOMAIN.value,)


def test_skill_version_and_permission_violation_is_severe_and_blocks_case() -> None:
    """未知 Skill 或精确版本不匹配必须先于评分直接失败。"""

    result = evaluate_rules(
        _case(),
        _manifest(),
        _observation(
            skill_invocations=(SkillInvocation(skill_id="set_product_price", version="1.0.0"),)
        ),
    )
    assert result.severe_violation is True
    assert RuleCode.SKILL_PERMISSION_OR_VERSION in result.codes


def test_no_fallback_sensitive_output_and_budget_are_hard_failures() -> None:
    """fallback、敏感输出和预算超限不能被模型结果或平均分覆盖。"""

    result = evaluate_rules(
        _case(),
        _manifest(),
        _observation(
            output={"decision": "PASS", "raw_text": "must not persist"},
            fallback_used=True,
            cost_cny=Decimal("0.11"),
        ),
    )
    assert result.severe_violation is True
    assert {
        RuleCode.NO_FALLBACK,
        RuleCode.SENSITIVE_OUTPUT,
        RuleCode.BUDGET_EXCEEDED,
    } <= set(result.codes)


def test_schema_evidence_state_cas_fencing_and_idempotency_are_checked() -> None:
    """输出 Schema、EvidenceRef、生命周期、CAS/fencing 和幂等必须全部闭合。"""

    result = evaluate_rules(
        _case(),
        _manifest(),
        _observation(
            output={"unexpected": "field"},
            evidence_refs=(),
            plan_state="RUNNING",
            event_state="RECEIVED",
            write_attempted=True,
            cas_conflict=True,
            fencing_valid=False,
            idempotency_key=None,
        ),
    )
    assert result.severe_violation is True
    assert {
        RuleCode.OUTPUT_SCHEMA,
        RuleCode.EVIDENCE_REF,
        RuleCode.PLAN_STATE,
        RuleCode.EVENT_STATE,
        RuleCode.CAS_CONFLICT,
        RuleCode.FENCING,
        RuleCode.IDEMPOTENCY,
    } <= set(result.codes)


def test_runner_returns_blocked_for_subject_execution_error() -> None:
    """Subject 基础设施异常只能归一化为 BLOCKED，不能伪造通过。"""

    result = SkillRuntimeRunner(_manifest()).run_case(_case(), RuntimeError("offline"))
    assert result.status is EvaluationCaseStatus.BLOCKED
    assert result.severe_violation is True
    assert result.rule_codes == (RuleCode.SUBJECT_ERROR.value,)


def test_manifest_rejects_unknown_subject_kind() -> None:
    """SubjectManifest 只允许冻结的五类受限 Runner 身份。"""

    with pytest.raises(ValueError):
        SubjectManifest(
            subject_id="bad",
            subject_version="1.0.0",
            subject_kind="FREE_AGENT",
            result_schema={"type": "object"},
        )
