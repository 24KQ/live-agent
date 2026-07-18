"""Phase 16 Task 3 受控双 Agent 冻结协议的 RED/GREEN 契约。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from src.decision_support.models import (
    ConflictAnalysis,
    ConflictAnalysisCode,
    EscalationMode,
    EscalationRecord,
    MultiAgentOutcome,
    MultiAgentOutcomeStatus,
)
from src.decision_support.multi_agent import (
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    MultiAgentProposalLineage,
    ProposalOrigin,
    ProductStrategy,
    ProposalStatus,
)
from src.specialist_runtime.budget import (
    BudgetCandidate,
    BudgetInvariantError,
    InMemoryModelBudgetStore,
)
from src.specialist_runtime.evidence import (
    EvidenceResolverRegistry,
    ResolvedEvidence,
)
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import (
    AgentTask,
    AgentResultStatus,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import (
    SpecialistOrchestrator,
    SpecialistProfileRegistry,
)
from src.specialist_runtime.runner import (
    BoundedSpecialistRunner,
    budget_candidate_for_task,
)
from src.specialist_runtime.scripted_model import ScriptedAgentModel
from src.skill_runtime.catalog import get_default_skill_catalog


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
_BUNDLE_DIGEST = "a" * 64
_ESCALATION_DIGEST = "b" * 64


def _reference() -> EvidenceRef:
    """构造最小 EvidenceRef，所有新事实只能携带引用而不能嵌入业务正文。"""

    return EvidenceRef(
        kind=EvidenceKind.EVENT,
        evidence_id="event-sold-out-001",
        source_version="1",
        digest="c" * 64,
        anchor_id="anchor-001",
        room_id="room-001",
    )


def _analysis() -> ConflictAnalysis:
    """使用单一 Bundle、升级和 Analyst 身份构造可重放的中间分析事实。"""

    profile = build_evidence_analyst_profile()
    return ConflictAnalysis(
        analysis_id="analysis-001",
        idempotency_key="analysis-idem-001",
        escalation_id="escalation-001",
        live_session_id="live-session-001",
        incident_id="incident-001",
        evidence_bundle_id="bundle-001",
        evidence_bundle_digest=_BUNDLE_DIGEST,
        analyst_profile_id=profile.profile_id,
        analyst_profile_version=profile.profile_version,
        analyst_profile_digest=profile.profile_digest,
        finding_codes=(
            ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
        ),
        constraint_codes=("OPERATOR_CONFIRMATION_REQUIRED",),
        risk_codes=("INVENTORY_CONFLICT_REQUIRES_REVIEW",),
        explanation="两个可用备品与高噪声可用性弹幕同时出现，需要运营审阅。",
        evidence_refs=(_reference(),),
        created_at=NOW,
    )


class _FixtureEvidenceLoader:
    """只返回测试已声明的权威投影，验证 Runner 不通过未解析引用。"""

    def __init__(self, facts: dict[str, ResolvedEvidence]) -> None:
        self._facts = facts

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        return self._facts.get(evidence_id)


class _ScriptedPricing:
    """固定脚本价格只覆盖本地预算协议，不产生真实模型费用。"""

    policy_digest = "f" * 64

    def count_input_tokens(self, _request) -> int:
        return 10

    def worst_case_cost(self, _request, _profile: SpecialistProfile) -> Decimal:
        return Decimal("0.001000")

    def actual_cost(self, _usage, _profile: SpecialistProfile) -> Decimal:
        return Decimal("0.001000")


class _NoSkillPort:
    """零 Skill Profile 若触发调用即代表权限边界回归，测试必须立即失败。"""

    async def invoke(self, **_kwargs):
        raise AssertionError("Phase 16 zero-Skill profile unexpectedly invoked a Skill")


def test_controlled_profiles_are_exact_zero_skill_identities() -> None:
    """两名 Agent 必须各自使用精确的单次、零 Skill、固定预算 Profile。"""

    analyst = build_evidence_analyst_profile()
    planner = build_decision_planner_profile()

    assert analyst.profile_id == "evidence_analyst"
    assert analyst.profile_version == "1.0.0"
    assert analyst.task_kind is SpecialistTaskKind.CONFLICT_ANALYSIS
    assert analyst.max_model_calls == 1
    assert analyst.max_skill_calls == 0
    assert analyst.allowed_skill_ids == ()
    assert analyst.max_total_tokens == 1200
    assert analyst.deadline_seconds == 2
    assert analyst.max_case_cost_cny == Decimal("0.030000")
    assert analyst.result_schema["additionalProperties"] is False

    assert planner.profile_id == "decision_planner"
    assert planner.profile_version == "1.0.0"
    assert planner.task_kind is SpecialistTaskKind.LIVE_DECISION_PLANNING
    assert planner.max_model_calls == 1
    assert planner.max_skill_calls == 0
    assert planner.allowed_skill_ids == ()
    assert planner.max_total_tokens == 2800
    assert planner.deadline_seconds == 2
    assert planner.max_case_cost_cny == Decimal("0.070000")
    assert planner.result_schema["additionalProperties"] is False


def test_phase16_profiles_describe_final_agent_action_and_planner_schema_parity() -> None:
    """Prompt 必须要求共享 Runner 的 FINAL 信封，Planner Schema 不得宽于 Pydantic 选项协议。"""

    analyst = build_evidence_analyst_profile()
    planner = build_decision_planner_profile()
    assert "AgentAction FINAL envelope" in analyst.prompt_text
    assert '"kind":"FINAL"' in analyst.prompt_text
    assert "AgentAction FINAL envelope" in planner.prompt_text

    option_schema = planner.result_schema["properties"]["options"]["items"]
    assert option_schema["properties"]["option_id"].get("pattern") == "^[a-z0-9][a-z0-9-]*$"
    assert option_schema["properties"]["backup_product_id"].get("maxLength") == 128
    assert option_schema["properties"]["host_prompt"].get("pattern") == "^(?!\\s)(?!.*\\s$)[^\\x00-\\x1F\\x7F]+$"
    assert option_schema.get("allOf")


def test_planner_schema_rejects_display_unsafe_outer_whitespace() -> None:
    """JSON Schema 必须在模型输出边界拒绝前后空白，避免把可预防失败留给 Pydantic。"""

    schema = build_decision_planner_profile().result_schema
    unsafe_output = {
        "options": [
            {
                "option_id": "hold-for-operator",
                "product_strategy": "HOLD_AND_ESCALATE",
                "backup_product_id": None,
                "host_prompt": " unsafe host prompt ",
                "timing": "AFTER_OPERATOR_CONFIRMATION",
                "risk_flags": ["HUMAN_CONFIRMATION_REQUIRED"],
                "evidence_refs": [_reference().model_dump(mode="json")],
            }
        ]
    }

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema).validate(unsafe_output)


def test_analyst_final_envelope_is_accepted_by_real_bounded_runner(monkeypatch) -> None:
    """专用 Scripted 预算装配下，真实共享 Runner 必须接受 FINAL 信封而非直接结果 JSON。"""

    profile = build_evidence_analyst_profile()
    reference = _reference()
    task = AgentTask(
        task_id="phase16-analyst-envelope-001",
        task_kind=profile.task_kind,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        room_id="room-001",
        trace_id="trace-001",
        objective="仅根据冻结证据输出冲突分析。",
        input_snapshot={"evidence_bundle_id": "bundle-001"},
        initial_evidence_refs=(reference,),
    )
    action = {
        "kind": "FINAL",
        "final_output": {
            "finding_codes": ["MULTIPLE_VALID_BACKUPS"],
            "constraint_codes": [],
            "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
            "explanation": "存在多个可用备品，需要运营确认。",
            "evidence_refs": [reference.model_dump(mode="json")],
        },
        "evidence_refs": [reference.model_dump(mode="json")],
    }
    request_id = f"{task.task_id}:{task.task_digest}:model:1"
    model = ScriptedAgentModel(
        outcomes={
            request_id: (
                ModelSuccess(
                    request_id=request_id,
                    model_id=profile.model_id,
                    output=action,
                    usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                    response_digest=canonical_json_sha256(action),
                    latency_ms=Decimal("1"),
                ),
            )
        }
    )
    resolved = ResolvedEvidence(
        kind=reference.kind,
        evidence_id=reference.evidence_id,
        source_version=reference.source_version,
        digest=reference.digest,
        anchor_id=reference.anchor_id,
        room_id=reference.room_id,
        payload={"fixture": "phase16-final-envelope"},
    )
    loaders = {
        kind: _FixtureEvidenceLoader(
            {reference.evidence_id: resolved} if kind is reference.kind else {}
        )
        for kind in EvidenceKind
    }
    # 该 mapping 只存在于单元测试，代表 D-143 要求的显式 Scripted budget 装配；
    # 生产旧账本仍由独立用例证明会 fail-closed，且本测试不会访问模型网络。
    monkeypatch.setattr(
        "src.specialist_runtime.runner.budget_candidate_for_task",
        lambda _task: BudgetCandidate.PHASE14_COPILOT,
    )
    runner = BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
        model_port=model,
        budget_store=InMemoryModelBudgetStore(scope_id="phase16-scripted-envelope-test"),
        evidence_registry=EvidenceResolverRegistry(loaders),
        skill_port=_NoSkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda _task: "anchor-001",
        pricing_policy=_ScriptedPricing(),
        clock=lambda: NOW,
    )

    result = asyncio.run(runner.run(task))

    assert result.status is AgentResultStatus.SUCCEEDED
    assert result.model_dump(mode="json")["output"] == action["final_output"]
    assert model.call_count == 1


def test_conflict_analysis_is_closed_to_one_bundle_and_digest() -> None:
    """分析结果必须只引用升级 Bundle，篡改封闭 finding 或摘要都不可重放。"""

    analysis = _analysis()
    assert len(analysis.analysis_digest) == 64
    assert analysis.evidence_bundle_id == "bundle-001"

    forged = analysis.model_dump(mode="json")
    forged["finding_codes"] = ["RHYTHM_PAUSE_REQUIRED"]
    with pytest.raises(ValidationError, match="analysis_digest"):
        ConflictAnalysis.model_validate(forged)

    forged_code = analysis.model_dump(mode="json")
    forged_code["finding_codes"] = ["MODEL_SELECTED_PRODUCT"]
    forged_code["analysis_digest"] = canonical_json_sha256(
        {key: value for key, value in forged_code.items() if key != "analysis_digest"}
    )
    with pytest.raises(ValidationError, match="finding_codes"):
        ConflictAnalysis.model_validate(forged_code)


def test_conflict_analysis_rejects_recomputed_forged_profile_digest() -> None:
    """攻击者即使重算外层摘要，也不能把任意 64 位值伪装为冻结 Analyst Profile。"""

    forged = _analysis().model_dump(mode="json")
    forged["analyst_profile_digest"] = "f" * 64
    forged["analysis_digest"] = canonical_json_sha256(
        {key: value for key, value in forged.items() if key != "analysis_digest"}
    )

    with pytest.raises(ValidationError, match="exact evidence_analyst profile"):
        ConflictAnalysis.model_validate(forged)


def test_escalation_and_outcome_are_append_only_closed_facts() -> None:
    """自动升级与降级 Outcome 只接受冻结触发码和稳定失败事实，不能夹带执行授权。"""

    record = EscalationRecord(
        escalation_id="escalation-001",
        live_session_id="live-session-001",
        incident_id="incident-001",
        evidence_bundle_id="bundle-001",
        evidence_bundle_digest=_BUNDLE_DIGEST,
        idempotency_key="escalation-idempotency-001",
        mode=EscalationMode.AUTOMATIC,
        trigger_codes=(
            ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
        ),
        created_at=NOW,
    )
    assert len(record.escalation_digest) == 64

    outcome = MultiAgentOutcome(
        outcome_id="outcome-001",
        idempotency_key="outcome-idem-001",
        escalation_id=record.escalation_id,
        live_session_id=record.live_session_id,
        incident_id=record.incident_id,
        escalation_digest=record.escalation_digest,
        evidence_bundle_id=record.evidence_bundle_id,
        evidence_bundle_digest=record.evidence_bundle_digest,
        status=MultiAgentOutcomeStatus.DEGRADED,
        failure_code="ANALYST_MODEL_ERROR",
        fact_summary="分析模型不可用，保留确定性证据摘要并等待运营处理。",
        created_at=NOW,
    )
    assert outcome.analysis_id is None
    assert outcome.proposal_id is None

    invalid_ready = outcome.model_dump(mode="json")
    invalid_ready["status"] = "READY"
    invalid_ready["failure_code"] = None
    invalid_ready["proposal_id"] = "proposal-001"
    invalid_ready["analysis_id"] = "analysis-001"
    # 仅保留 proposal 摘要缺失这一变量，避免先被分析 lineage 的成对约束截断。
    invalid_ready["analysis_digest"] = "d" * 64
    invalid_ready["outcome_digest"] = canonical_json_sha256(
        {key: value for key, value in invalid_ready.items() if key != "outcome_digest"}
    )
    with pytest.raises(ValidationError, match="READY"):
        MultiAgentOutcome.model_validate(invalid_ready)


def test_multi_agent_proposal_lineage_closes_over_bundle_and_analysis() -> None:
    """规划方案必须同时携带升级、分析与 Bundle 摘要，禁止跨事故拼接引用。"""

    analysis = _analysis()
    lineage = MultiAgentProposalLineage(
        escalation_id="escalation-001",
        escalation_digest=_ESCALATION_DIGEST,
        analysis_id=analysis.analysis_id,
        analysis_digest=analysis.analysis_digest,
        evidence_bundle_id=analysis.evidence_bundle_id,
        evidence_bundle_digest=analysis.evidence_bundle_digest,
        evidence_refs=(_reference(),),
        planner_profile_id="decision_planner",
        planner_profile_version="1.0.0",
        planner_profile_digest=build_decision_planner_profile().profile_digest,
    )
    proposal = LiveDecisionProposal(
        proposal_id="proposal-001",
        live_session_id="live-session-001",
        incident_id="incident-001",
        trace_id="trace-001",
        evidence_bundle_id="bundle-001",
        evidence_bundle_digest=_BUNDLE_DIGEST,
        proposal_origin=ProposalOrigin.MULTI_AGENT,
        status=ProposalStatus.READY,
        options=(
            DecisionOption(
                option_id="hold-for-operator",
                product_strategy=ProductStrategy.HOLD_AND_ESCALATE,
                host_prompt="请运营确认备品和主播节奏后继续。",
                timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
                risk_flags=("HUMAN_CONFIRMATION_REQUIRED",),
                evidence_refs=(_reference(),),
            ),
        ),
        evidence_refs=(_reference(),),
        multi_agent_lineage=lineage,
    )
    assert proposal.multi_agent_lineage == lineage

    missing_lineage = proposal.model_dump(mode="json")
    missing_lineage["multi_agent_lineage"] = None
    with pytest.raises(ValidationError, match="lineage"):
        LiveDecisionProposal.model_validate(missing_lineage)

    forged = proposal.model_dump(mode="json")
    forged["multi_agent_lineage"]["evidence_bundle_digest"] = "f" * 64
    forged["multi_agent_lineage"]["lineage_digest"] = canonical_json_sha256(
        {
            key: value
            for key, value in forged["multi_agent_lineage"].items()
            if key != "lineage_digest"
        }
    )
    with pytest.raises(ValidationError, match="digest"):
        LiveDecisionProposal.model_validate(forged)

    forged_profile = lineage.model_dump(mode="json")
    forged_profile["planner_profile_digest"] = "f" * 64
    forged_profile["lineage_digest"] = canonical_json_sha256(
        {
            key: value
            for key, value in forged_profile.items()
            if key != "lineage_digest"
        }
    )
    # Planner 也必须比较工厂重建的完整身份，不能只依赖可伪造的外层 lineage 摘要。
    with pytest.raises(ValidationError, match="exact decision_planner profile"):
        MultiAgentProposalLineage.model_validate(forged_profile)

    forged_refs = proposal.model_dump(mode="json")
    forged_refs["multi_agent_lineage"]["evidence_refs"][0]["digest"] = "e" * 64
    forged_refs["multi_agent_lineage"]["lineage_digest"] = canonical_json_sha256(
        {
            key: value
            for key, value in forged_refs["multi_agent_lineage"].items()
            if key != "lineage_digest"
        }
    )
    with pytest.raises(ValidationError, match="evidence_refs"):
        LiveDecisionProposal.model_validate(forged_refs)


@pytest.mark.parametrize(
    ("task_kind", "profile_id"),
    (
        (SpecialistTaskKind.CONFLICT_ANALYSIS, "evidence_analyst"),
        (SpecialistTaskKind.LIVE_DECISION_PLANNING, "decision_planner"),
    ),
)
def test_phase16_task_kinds_fail_closed_until_dedicated_budget_exists(
    task_kind: SpecialistTaskKind,
    profile_id: str,
) -> None:
    """旧账本不得抛出 KeyError 或偷用历史预算，Phase 16 必须得到稳定拒绝。"""

    task = AgentTask(
        task_id=f"task-{task_kind.value.lower()}",
        task_kind=task_kind,
        profile_id=profile_id,
        profile_version="1.0.0",
        room_id="room-001",
        trace_id="trace-001",
        objective="测试专用预算拒绝。",
        input_snapshot={"bundle_id": "bundle-001"},
        initial_evidence_refs=(_reference(),),
    )

    with pytest.raises(BudgetInvariantError, match="dedicated Phase 16 budget"):
        budget_candidate_for_task(task)
