"""Phase 16 覆盖率收口用例。

这些用例只验证已经冻结的协议拒绝、身份闭合和错误归一化分支，不新增业务
能力，也不触发真实模型或外部网络。每个测试都保留可审计的失败断言，避免
为了提高数字而只执行无意义的 happy path。
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
import math
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
import json

import pytest
from pydantic import ValidationError

from src.decision_support.models import (
    ConflictAnalysisCode,
    ConflictAnalysis,
    EscalationMode,
    EscalationRecord,
    MultiAgentFailureCode,
    MultiAgentOutcome,
    MultiAgentOutcomeStatus,
    _require_safe_display_text,
    AnalystDispatchClaim,
    PlannerDispatchClaim,
    MultiAgentProposalLineage,
    EvidenceBundle,
    Incident,
    OperatorLease,
)
from src.decision_support.multi_agent import (
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.multi_agent import (
    HighConflictCoordinationResult,
    HighConflictEscalationCoordinator,
)
from src.decision_support import multi_agent_evaluation as evaluation
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    ProductStrategy,
    ProposalStatus,
    ProposalOrigin,
)
from src.specialist_runtime.live_ops import LiveOpsAgentAdapter, build_live_ops_profile
from src.specialist_runtime.models import (
    AgentAction,
    AgentActionKind,
    AgentFailure,
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    SpecialistTaskKind,
    _freeze_json,
    canonical_json_sha256,
    FrozenDict,
    _plain_json,
)
from src.specialist_runtime.profiles import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    SpecialistProfile,
    normalize_endpoint_host,
)


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def _reference() -> EvidenceRef:
    """构造协议边界所需的最小证据引用，正文永远不进入本测试。"""

    return EvidenceRef(
        kind=EvidenceKind.EVENT,
        evidence_id="event-coverage",
        source_version="2.0.0",
        digest=HASH,
        anchor_id="anchor-coverage",
        room_id="room-coverage",
    )


def _task() -> AgentTask:
    """用真实 AgentTask 触发输入冻结和任务摘要重算。"""

    return AgentTask(
        task_id="task-coverage",
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        profile_id="evidence_analyst",
        profile_version="1.0.0",
        room_id="room-coverage",
        trace_id="trace-coverage",
        objective="验证协议拒绝分支。",
        input_snapshot={"nested": {"value": 1}},
        initial_evidence_refs=(_reference(),),
    )


def _option(**updates) -> DecisionOption:
    """构造完整建议选项，后续用 model_dump 只改变一个约束字段。"""

    values = {
        "option_id": "hold-for-operator",
        "product_strategy": ProductStrategy.HOLD_AND_ESCALATE,
        "host_prompt": "请运营确认后再继续。",
        "timing": DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
        "risk_flags": ("HUMAN_CONFIRMATION_REQUIRED",),
        "evidence_refs": (_reference(),),
    }
    values.update(updates)
    return DecisionOption(**values)


def test_strict_json_helpers_reject_non_json_values_and_serialize_frozen_values() -> None:
    """严格 JSON 辅助函数必须拒绝 NaN、非字符串 key、tuple 和任意对象。"""

    assert _freeze_json({"items": [1, True, None]})["items"] == (1, True, None)
    assert canonical_json_sha256({"b": 1, "a": 2}) == canonical_json_sha256(
        {"a": 2, "b": 1}
    )
    with pytest.raises(ValueError, match="finite"):
        _freeze_json(math.nan)
    with pytest.raises(ValueError, match="keys"):
        _freeze_json({1: "not-json"})
    with pytest.raises(ValueError, match="unsupported"):
        _freeze_json((1, 2))
    with pytest.raises(ValueError, match="unsupported"):
        canonical_json_sha256(object())


@pytest.mark.parametrize(
    "action",
    [
        {"kind": AgentActionKind.CALL_SKILL},
        {"kind": AgentActionKind.FINAL},
        {"kind": AgentActionKind.FINAL, "final_output": {}, "arguments": {"x": 1}},
        {"kind": AgentActionKind.ABSTAIN},
        {"kind": AgentActionKind.ABSTAIN, "reason_code": "STOP", "final_output": {}},
    ],
)
def test_agent_action_rejects_each_mutually_exclusive_shape(action: dict) -> None:
    """CALL_SKILL、FINAL、ABSTAIN 三种动作都不能夹带其他阶段字段。"""

    with pytest.raises(ValidationError):
        AgentAction(**action)


def test_agent_result_accepts_fallback_and_rejects_each_terminal_shape_violation() -> None:
    """成功、fallback、失败结果共享 token 计数并严格互斥输出与错误。"""

    fallback = AgentResult(
        task_id="task-coverage",
        profile_id="evidence_analyst",
        profile_version="1.0.0",
        status=AgentResultStatus.FALLBACK,
        output={"status": "DEGRADED"},
        summary="确定性降级。",
        input_tokens=1,
        output_tokens=2,
        total_tokens=3,
    )
    assert fallback.model_dump(mode="json")["output"] == {"status": "DEGRADED"}
    for payload in (
        {"status": AgentResultStatus.SUCCEEDED, "output": None},
        {"status": AgentResultStatus.SUCCEEDED, "output": {}, "failure": AgentFailure(code="X")},
        {"status": AgentResultStatus.MODEL_ERROR, "output": {}},
        {"status": AgentResultStatus.MODEL_ERROR},
    ):
        with pytest.raises(ValidationError):
            AgentResult(
                task_id="task-coverage",
                profile_id="evidence_analyst",
                profile_version="1.0.0",
                summary="结果形状测试。",
                total_tokens=0,
                **payload,
            )


def test_profile_and_endpoint_guards_cover_normalization_and_permission_edges() -> None:
    """Profile 的 hostname、模型、温度、技能白名单和版本映射都必须封闭。"""

    assert normalize_endpoint_host("API.DeepSeek.COM") == FORMAL_ENDPOINT_HOST
    for host in ("api.deepseek.com:443", "https://api.deepseek.com", "api", " api.deepseek.com"):
        with pytest.raises(ValueError):
            normalize_endpoint_host(host)

    profile = build_evidence_analyst_profile()
    base = profile.model_dump(mode="json")
    for field, value in (
        ("model_id", "other-model"),
        ("temperature", "0.1"),
        ("allowed_skill_ids", ["same", "same"]),
        ("skill_versions", {"skill": "bad"}),
    ):
        with pytest.raises(ValidationError):
            SpecialistProfile.model_validate(base | {field: value})
    assert profile.model_id == FORMAL_MODEL_ID


def test_agent_task_digest_and_live_ops_adapter_reject_invalid_cases() -> None:
    """任务摘要漂移和 LiveOps case 缺失身份时必须在 Runner 前失败。"""

    task = _task()
    forged = task.model_dump(mode="json") | {"task_digest": "b" * 64}
    with pytest.raises(ValidationError, match="task_digest"):
        AgentTask.model_validate(forged)

    profile = build_evidence_analyst_profile()
    with pytest.raises(ValueError, match="LIVE_OPS_ADVICE"):
        LiveOpsAgentAdapter(runner=object(), profile=profile)

    adapter = LiveOpsAgentAdapter(
        runner=object(),
        profile=build_live_ops_profile(Path(__file__).resolve().parents[2] / "evaluation"),
    )
    for case in (
        {"candidate": "live_ops"},
        {"candidate": "live_ops", "case_id": "case"},
        {"candidate": "live_ops", "case_id": "case", "input": []},
        {"candidate": "live_ops", "case_id": "case", "input": {"room_id": "r"}},
    ):
        with pytest.raises(ValueError):
            adapter.build_task(case)


def test_models_reject_nul_and_append_only_fact_shape_drift() -> None:
    """领域事实拒绝 NUL、重复触发码、错误模式和不完整 Outcome lineage。"""

    with pytest.raises(ValidationError, match="NUL"):
        EscalationRecord(
            escalation_id="e",
            live_session_id="s\x00",
            incident_id="i",
            evidence_bundle_id="b",
            evidence_bundle_digest=HASH,
            idempotency_key="idem",
            mode=EscalationMode.OPERATOR_REQUESTED,
            operator_id="operator",
            trigger_codes=(ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,),
            created_at=NOW,
        )

    with pytest.raises(ValidationError, match="unique"):
        EscalationRecord(
            escalation_id="e",
            live_session_id="s",
            incident_id="i",
            evidence_bundle_id="b",
            evidence_bundle_digest=HASH,
            idempotency_key="idem",
            mode=EscalationMode.AUTOMATIC,
            trigger_codes=(ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,) * 2,
            created_at=NOW,
        )

    with pytest.raises(ValidationError, match="operator_id"):
        EscalationRecord(
            escalation_id="e",
            live_session_id="s",
            incident_id="i",
            evidence_bundle_id="b",
            evidence_bundle_digest=HASH,
            idempotency_key="idem",
            mode=EscalationMode.OPERATOR_REQUESTED,
            trigger_codes=(ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,),
            created_at=NOW,
        )

    with pytest.raises(ValidationError, match="failure_code"):
        MultiAgentOutcome(
            outcome_id="o",
            idempotency_key="o-idem",
            escalation_id="e",
            live_session_id="s",
            incident_id="i",
            escalation_digest=HASH,
            evidence_bundle_id="b",
            evidence_bundle_digest=HASH,
            status=MultiAgentOutcomeStatus.DEGRADED,
            fact_summary="降级。",
            created_at=NOW,
        )


def test_proposal_protocol_rejects_strategy_text_and_state_lineage_drift() -> None:
    """建议协议拒绝未知风险、备品语义冲突和 READY/DEGRADED 状态越界。"""

    with pytest.raises(ValidationError, match="backup_product_id"):
        _option(product_strategy=ProductStrategy.SWITCH_TO_BACKUP)
    with pytest.raises(ValidationError, match="only allowed"):
        _option(backup_product_id="p002")
    with pytest.raises(ValidationError, match="unknown"):
        _option(risk_flags=("UNKNOWN_RISK",))
    with pytest.raises(ValidationError, match="control"):
        _option(host_prompt="请运营\n确认")

    base = {
        "proposal_id": "proposal-coverage",
        "live_session_id": "s",
        "incident_id": "i",
        "trace_id": "t",
        "evidence_bundle_id": "b",
        "status": ProposalStatus.READY,
        "evidence_refs": (_reference(),),
    }
    with pytest.raises(ValidationError, match="one to three"):
        LiveDecisionProposal(**base)
    degraded = base | {
        "status": ProposalStatus.DEGRADED,
        "fact_summary": "确定性事实。",
        "degraded_reason": "MODEL_ERROR",
        "options": (_option(),),
    }
    with pytest.raises(ValidationError, match="cannot carry options"):
        LiveDecisionProposal(**degraded)


def test_coordinator_result_and_startup_budget_guards_cover_fail_closed_shapes() -> None:
    """协调器结果、启动预算和依赖冻结都不能由调用方临时扩展。"""

    invalid_shapes = (
        {"selected": False, "escalation": object()},
        {"selected": True, "analysis": object()},
        {"selected": True, "proposal": object()},
        {"selected": True, "outcome": object()},
        {
            "selected": True,
            "outcome": SimpleNamespace(status=MultiAgentOutcomeStatus.READY),
        },
        {
            "selected": True,
            "escalation": object(),
            "outcome": SimpleNamespace(status=MultiAgentOutcomeStatus.DEGRADED),
            "proposal": object(),
        },
    )
    for shape in invalid_shapes:
        with pytest.raises(ValueError):
            HighConflictCoordinationResult(**shape)

    with pytest.raises(TypeError, match="clock"):
        HighConflictEscalationCoordinator(
            store=object(), analyst_runner=object(), clock="not-callable"
        )
    with pytest.raises(TypeError, match="monotonic"):
        HighConflictEscalationCoordinator(
            store=object(), analyst_runner=object(), monotonic_clock="not-callable"
        )
    coordinator = HighConflictEscalationCoordinator(
        store=object(), analyst_runner=object(), monotonic_clock=lambda: 10.0
    )
    with pytest.raises(TypeError, match="startup-frozen"):
        coordinator._store = object()
    assert coordinator._coordinator_budget_available(15.0) is True
    assert coordinator._coordinator_budget_available(16.0) is False
    assert coordinator._coordinator_budget_available(9.0) is False


def test_coordinator_error_code_and_clock_helpers_are_closed() -> None:
    """Runner 开放状态必须归一为有限失败码，事实时间必须带时区。"""

    def result(status: AgentResultStatus) -> AgentResult:
        return AgentResult(
            task_id="task-coverage",
            profile_id="evidence_analyst",
            profile_version="1.0.0",
            status=status,
            failure=AgentFailure(code="MODEL_FAILURE")
            if status not in {AgentResultStatus.SUCCEEDED, AgentResultStatus.FALLBACK}
            else None,
            output={"ok": True}
            if status in {AgentResultStatus.SUCCEEDED, AgentResultStatus.FALLBACK}
            else None,
            summary="状态归一化。",
        )

    assert HighConflictEscalationCoordinator._failure_code_for_result(
        result(AgentResultStatus.BUDGET_EXCEEDED)
    ) is MultiAgentFailureCode.ANALYST_BUDGET_EXCEEDED
    assert HighConflictEscalationCoordinator._failure_code_for_result(
        result(AgentResultStatus.MODEL_ERROR)
    ) is MultiAgentFailureCode.ANALYST_MODEL_ERROR
    assert HighConflictEscalationCoordinator._failure_code_for_result(
        result(AgentResultStatus.INVALID_OUTPUT)
    ) is MultiAgentFailureCode.ANALYST_INVALID_OUTPUT
    assert HighConflictEscalationCoordinator._planner_failure_code_for_result(
        result(AgentResultStatus.BUDGET_EXCEEDED)
    ) is MultiAgentFailureCode.PLANNER_BUDGET_EXCEEDED
    assert HighConflictEscalationCoordinator._planner_failure_code_for_result(
        result(AgentResultStatus.MODEL_ERROR)
    ) is MultiAgentFailureCode.PLANNER_MODEL_ERROR
    coordinator = HighConflictEscalationCoordinator(
        store=object(), analyst_runner=object(), clock=lambda: datetime(2026, 7, 17, tzinfo=timezone.utc)
    )
    assert coordinator._utc_now().tzinfo is not None
    invalid_clock = HighConflictEscalationCoordinator(
        store=object(), analyst_runner=object(), clock=lambda: datetime(2026, 7, 17)
    )
    with pytest.raises(ValueError, match="timezone"):
        invalid_clock._utc_now()


def test_remaining_frozen_protocol_edges_are_exercised_directly() -> None:
    """补齐不会被正常请求组合触发的底层冻结协议分支。"""

    frozen = FrozenDict({"key": "value"})
    with pytest.raises(TypeError, match="mutated"):
        frozen.extra = "forbidden"
    with pytest.raises(ValueError, match="unsupported"):
        _plain_json(object())
    assert _freeze_json(1.25) == 1.25
    assert _plain_json(_task())["task_id"] == "task-coverage"
    with pytest.raises(ValueError, match="control"):
        _require_safe_display_text("unsafe\ntext", field_name="summary")

    valid_escalation = EscalationRecord(
        escalation_id="e-edge",
        live_session_id="s",
        incident_id="i",
        evidence_bundle_id="b",
        evidence_bundle_digest=HASH,
        idempotency_key="idem-edge",
        mode=EscalationMode.OPERATOR_REQUESTED,
        operator_id="operator",
        trigger_codes=(ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,),
        created_at=NOW,
    )
    for invalid in (
        valid_escalation.model_dump(mode="json") | {"created_at": "2026-07-17T12:00:00"},
        valid_escalation.model_dump(mode="json") | {"escalation_digest": "f" * 64},
    ):
        with pytest.raises(ValidationError):
            EscalationRecord.model_validate(invalid)
    automatic = valid_escalation.model_dump(mode="json") | {
        "mode": EscalationMode.AUTOMATIC,
        "operator_id": "operator",
        "trigger_codes": [ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS.value,
                           ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH.value],
        "escalation_digest": "",
    }
    with pytest.raises(ValidationError, match="cannot carry"):
        EscalationRecord.model_validate(automatic)
    automatic["operator_id"] = None
    automatic["trigger_codes"] = [ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS.value]
    with pytest.raises(ValidationError, match="two trigger"):
        EscalationRecord.model_validate(automatic)
    with pytest.raises(ValidationError, match="empty"):
        SpecialistProfile.model_validate(
            build_evidence_analyst_profile().model_dump(mode="json")
            | {"allowed_skill_ids": [""], "skill_versions": {"": "1.0.0"}}
        )
    with pytest.raises(ValueError, match="object"):
        SpecialistProfile._freeze_skill_versions([])

    valid_call = AgentAction(kind=AgentActionKind.CALL_SKILL, skill_id="read-only")
    assert valid_call.skill_id == "read-only"
    with pytest.raises(ValidationError, match="CALL_SKILL"):
        AgentAction(
            kind=AgentActionKind.CALL_SKILL,
            skill_id="read-only",
            final_output={},
        )
    valid_abstain = AgentAction(kind=AgentActionKind.ABSTAIN, reason_code="STOP")
    assert valid_abstain.reason_code == "STOP"
    with pytest.raises(ValidationError, match="total_tokens"):
        AgentResult(
            task_id="task-coverage",
            profile_id="evidence_analyst",
            profile_version="1.0.0",
            status=AgentResultStatus.MODEL_ERROR,
            failure=AgentFailure(code="MODEL_ERROR"),
            summary="token mismatch",
            input_tokens=1,
            total_tokens=0,
        )


def test_analysis_outcome_claim_and_lineage_shape_edges_are_closed() -> None:
    """中间分析、Outcome、dispatch claim 和 lineage 的成对字段必须同时存在。"""

    from tests.unit.test_phase16_controlled_multi_agent_contracts import _analysis

    analysis = _analysis()
    duplicate_codes = analysis.model_dump(mode="json")
    duplicate_codes["finding_codes"] = [
        ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS.value,
        ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS.value,
    ]
    with pytest.raises(ValidationError, match="unique"):
        ConflictAnalysis.model_validate(duplicate_codes)
    duplicate_refs = analysis.model_dump(mode="json")
    duplicate_refs["evidence_refs"] = [
        duplicate_refs["evidence_refs"][0],
        duplicate_refs["evidence_refs"][0],
    ]
    with pytest.raises(ValidationError, match="unique"):
        ConflictAnalysis.model_validate(duplicate_refs)

    def outcome(**updates):
        values = {
            "outcome_id": "outcome-edge",
            "idempotency_key": "outcome-edge-idem",
            "escalation_id": "e",
            "live_session_id": "s",
            "incident_id": "i",
            "escalation_digest": HASH,
            "evidence_bundle_id": "b",
            "evidence_bundle_digest": HASH,
            "status": MultiAgentOutcomeStatus.DEGRADED,
            "failure_code": MultiAgentFailureCode.ANALYST_MODEL_ERROR,
            "fact_summary": "确定性降级。",
            "created_at": NOW,
        }
        values.update(updates)
        return MultiAgentOutcome.model_construct(**values)

    with pytest.raises(ValueError, match="appear together"):
        outcome(analysis_id="a")._validate_outcome_shape_and_digest()
    with pytest.raises(ValueError, match="failure_code"):
        outcome(
            status=MultiAgentOutcomeStatus.READY,
            analysis_id="a",
            analysis_digest=HASH,
            proposal_id="p",
            proposal_digest=HASH,
            failure_code=MultiAgentFailureCode.ANALYST_MODEL_ERROR,
        )._validate_outcome_shape_and_digest()
    with pytest.raises(ValueError, match="proposal lineage"):
        outcome(
            proposal_id="p",
            proposal_digest=HASH,
        )._validate_outcome_shape_and_digest()
    with pytest.raises(ValueError, match="requires failure"):
        outcome(failure_code=None)._validate_outcome_shape_and_digest()
    with pytest.raises(ValueError, match="outcome_digest"):
        outcome(outcome_digest="f" * 64)._validate_outcome_shape_and_digest()

    for claim_type in (AnalystDispatchClaim, PlannerDispatchClaim):
        with pytest.raises(ValidationError, match="timezone"):
            claim_type(
                escalation_id="e",
                live_session_id="s",
                task_digest=HASH,
                **({} if claim_type is AnalystDispatchClaim else {
                    "analysis_id": "a",
                    "analysis_digest": HASH,
                }),
                created_at="2026-07-17T12:00:00",
                lease_until="2026-07-17T12:00:01",
            )

    planner_claim = PlannerDispatchClaim.model_construct(
        escalation_id="e",
        live_session_id="s",
        analysis_id="a",
        analysis_digest=HASH,
        task_digest=HASH,
        created_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        lease_until=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="follow"):
        planner_claim._require_positive_claim_window()
    analyst_claim = AnalystDispatchClaim.model_construct(
        escalation_id="e",
        live_session_id="s",
        task_digest=HASH,
        created_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        lease_until=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="follow"):
        analyst_claim._require_positive_claim_window()

    planner = build_decision_planner_profile()
    lineage_values = {
        "escalation_id": "e",
        "escalation_digest": HASH,
        "analysis_id": "a",
        "analysis_digest": HASH,
        "evidence_bundle_id": "b",
        "evidence_bundle_digest": HASH,
        "evidence_refs": (_reference(),),
        "planner_profile_id": planner.profile_id,
        "planner_profile_version": planner.profile_version,
        "planner_profile_digest": planner.profile_digest,
    }
    lineage = MultiAgentProposalLineage(**lineage_values)
    with pytest.raises(ValidationError, match="unique"):
        MultiAgentProposalLineage(**(lineage_values | {"evidence_refs": (_reference(), _reference())}))
    with pytest.raises(ValidationError, match="lineage_digest"):
        MultiAgentProposalLineage.model_validate(
            lineage.model_dump(mode="json") | {"lineage_digest": "f" * 64}
        )


def test_proposal_validator_rejects_all_state_and_risk_code_edges() -> None:
    """Proposal 的风险集合和 READY/DEGRADED 状态边界必须逐项锁死。"""

    with pytest.raises(ValidationError, match="uppercase"):
        _option(risk_flags=("unsafe-risk",))
    with pytest.raises(ValidationError, match="unique"):
        _option(risk_flags=("HUMAN_CONFIRMATION_REQUIRED",) * 2)
    ready_base = {
        "proposal_id": "proposal-edge",
        "live_session_id": "s",
        "incident_id": "i",
        "trace_id": "t",
        "evidence_bundle_id": "b",
        "status": ProposalStatus.READY,
        "options": (_option(),),
        "evidence_refs": (_reference(),),
    }
    with pytest.raises(ValidationError, match="degraded_reason"):
        LiveDecisionProposal(**(ready_base | {"degraded_reason": "MODEL_ERROR"}))
    with pytest.raises(ValidationError, match="requires reason"):
        LiveDecisionProposal(
            **(ready_base | {"status": ProposalStatus.DEGRADED, "options": ()})
        )
    with pytest.raises(ValidationError, match="unique"):
        LiveDecisionProposal(
            **(ready_base | {"status": ProposalStatus.DEGRADED, "options": (),
                              "fact_summary": "事实摘要。", "degraded_reason": "MODEL_ERROR",
                              "evidence_refs": (_reference(), _reference())})
        )
    with pytest.raises(ValidationError, match="multi-agent lineage"):
        LiveDecisionProposal(
            **(ready_base | {"proposal_origin": ProposalOrigin.MULTI_AGENT})
        )
    planner = build_decision_planner_profile()
    lineage = MultiAgentProposalLineage(
        escalation_id="e",
        escalation_digest=HASH,
        analysis_id="a",
        analysis_digest=HASH,
        evidence_bundle_id="b",
        evidence_bundle_digest=HASH,
        evidence_refs=(_reference(),),
        planner_profile_id=planner.profile_id,
        planner_profile_version=planner.profile_version,
        planner_profile_digest=planner.profile_digest,
    )
    with pytest.raises(ValidationError, match="evidence_bundle_digest"):
        LiveDecisionProposal(
            **(ready_base | {
                "proposal_origin": ProposalOrigin.MULTI_AGENT,
                "multi_agent_lineage": lineage,
            })
        )
    with pytest.raises(ValidationError, match="evidence_bundle_id"):
        LiveDecisionProposal(
            **(ready_base | {
                "proposal_origin": ProposalOrigin.MULTI_AGENT,
                "evidence_bundle_digest": HASH,
                "multi_agent_lineage": MultiAgentProposalLineage.model_validate(
                    lineage.model_dump(mode="json")
                    | {"evidence_bundle_id": "other", "lineage_digest": ""}
                ),
            })
        )
    with pytest.raises(ValidationError, match="single-copilot"):
        LiveDecisionProposal(
            **(ready_base | {"multi_agent_lineage": lineage})
        )


def test_evaluation_assets_and_task_input_guards_fail_closed(tmp_path: Path) -> None:
    """评估资产、任务输入和模板脚本的异常形状必须在模型调用前停止。"""

    root = tmp_path / "dataset"
    manifest = evaluation.generate_phase16_controlled_multi_agent_dataset(root)
    dataset = evaluation.load_phase16_controlled_multi_agent_dataset(root)

    with pytest.raises(ValueError, match="manifest digest"):
        evaluation.Phase16Manifest.model_validate(
            manifest.model_dump(mode="json") | {"manifest_digest": "f" * 64}
        )
    with pytest.raises(ValueError, match="source code digest"):
        evaluation._validate_dataset_for_run(
            replace(
                dataset,
                manifest=manifest.model_copy(
                    update={"manifest_digest": "", "source_code_digest": "f" * 64}
                ),
            )
        )

    asset = tmp_path / "asset.jsonl"
    for raw, message in (
        (b"\xef\xbb\xbf{}\n", "without BOM"),
        (b"{}\r\n", "without BOM"),
        (b"\xff\n", "valid UTF-8"),
        (b"{}\n\n", "one LF"),
    ):
        asset.write_bytes(raw)
        with pytest.raises(ValueError, match=message):
            evaluation._load_jsonl(asset)

    unsupported = SimpleNamespace(task_kind=SimpleNamespace(value="LIVE_OPS_ADVICE"))
    with pytest.raises(ValueError, match="controlled multi-agent"):
        evaluation._profile_for(unsupported)

    manifest_payload = manifest.model_dump(mode="json")
    missing_split = dict(manifest_payload)
    missing_split["case_ids"] = {"development": manifest_payload["case_ids"]["development"]}
    with pytest.raises(ValueError, match="all frozen splits"):
        evaluation.Phase16Manifest.model_validate(missing_split)
    wrong_count = dict(manifest_payload)
    wrong_count["case_ids"] = dict(manifest_payload["case_ids"])
    wrong_count["case_ids"]["validation"] = wrong_count["case_ids"]["validation"][:-1]
    with pytest.raises(ValueError, match="split counts"):
        evaluation.Phase16Manifest.model_validate(wrong_count)
    with pytest.raises(ValueError, match="generator digest"):
        evaluation._validate_dataset_for_run(
            replace(dataset, manifest=manifest.model_copy(update={"manifest_digest": "", "generator_digest": "f" * 64}))
        )
    with pytest.raises(ValueError, match="case digests"):
        evaluation._validate_dataset_for_run(
            replace(dataset, manifest=manifest.model_copy(update={"manifest_digest": "", "case_digests": {}}))
        )
    with pytest.raises(ValueError, match="dataset digest"):
        evaluation._validate_dataset_for_run(
            replace(dataset, manifest=manifest.model_copy(update={"manifest_digest": "", "dataset_digest": "f" * 64}))
        )

    conflict_task = _task()
    for input_snapshot in (
        None,
        {},
        {"unexpected": True},
        {"escalation_id": "e", "escalation_digest": HASH,
         "trigger_codes": ["ONE"], "evidence_bundle": {}},
        {"escalation_id": "e", "escalation_digest": HASH,
         "trigger_codes": ["ONE", "TWO"], "evidence_bundle": {}},
    ):
        assert evaluation._is_governed_task_input(conflict_task, input_snapshot) is False
    planner_task = AgentTask.model_validate(
        conflict_task.model_dump(mode="json")
        | {
            "task_kind": SpecialistTaskKind.LIVE_DECISION_PLANNING,
            "profile_id": "decision_planner",
            "input_snapshot": {"analysis": "not-object", "evidence_bundle": {}},
            "task_digest": "",
        }
    )
    assert evaluation._is_governed_task_input(planner_task, planner_task.input_snapshot) is False

    with pytest.raises(ValueError, match="exactly one template"):
        evaluation._render_scripted_output(conflict_task, {})
    with pytest.raises(ValueError, match="unknown"):
        evaluation._render_scripted_output(conflict_task, {"template": "UNKNOWN"})
    with pytest.raises(ValueError, match="unsupported task"):
        evaluation._scripted_outcome_for_task(
            request=SimpleNamespace(),
            profile=SimpleNamespace(),
            task=unsupported,
            script=SimpleNamespace(),
        )

    assert evaluation._actual_route(SimpleNamespace(selected=False)) is evaluation.Phase16ExpectedRoute.SINGLE_COPILOT
    assert evaluation._actual_route(SimpleNamespace(selected=True, outcome=None)) is evaluation.Phase16ExpectedRoute.NO_SEND
    assert evaluation._lineage_identity_correct(
        SimpleNamespace(selected=True, escalation=None, outcome=None), object()
    ) is False

    runner = evaluation._EvaluationScriptedRunner(
        case=dataset.cases[0],
        script=dataset.scripts[dataset.cases[0].case_id],
        now=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    denied = AgentTask.model_validate(
        conflict_task.model_dump(mode="json")
        | {"input_snapshot": {}, "task_digest": ""}
    )
    denied_result = __import__("asyncio").run(runner.run(denied))
    assert denied_result.status is AgentResultStatus.POLICY_DENIED


def test_persisted_snapshot_and_lease_scope_checks_are_revalidated() -> None:
    """Store 模型重载时必须重新核对 Bundle 父作用域、组件身份和租约时区。"""

    from tests.unit.test_phase14_evidence import _assembly

    assembler, request, _ = _assembly()
    bundle = assembler.assemble(request).bundle
    for field, value, message in (
        ("live_session_id", "other-session", "live_session_id"),
        ("incident_id", "other-incident", "incident_id"),
    ):
        forged = bundle.model_dump(mode="json")
        forged[field] = value
        forged["input_fingerprint"] = canonical_json_sha256(forged["snapshot"])
        with pytest.raises(ValidationError, match=message):
            EvidenceBundle.model_validate(forged)
    forged_refs = bundle.model_dump(mode="json")
    forged_refs["evidence_ref_ids"] = ["foreign-reference"]
    forged_refs["input_fingerprint"] = canonical_json_sha256(forged_refs["snapshot"])
    with pytest.raises(ValidationError, match="evidence_ref_ids"):
        EvidenceBundle.model_validate(forged_refs)
    with pytest.raises(ValidationError, match="timezone"):
        Incident(
            incident_id="i",
            live_session_id="s",
            idempotency_key="i-idem",
            incident_type="SOLD_OUT_COMPOSITE",
            source_ref_ids=("event",),
            snapshot={},
            created_at="2026-07-17T12:00:00",
        )
    with pytest.raises(ValidationError, match="timezone"):
        OperatorLease(
            live_session_id="s",
            operator_id="operator",
            fencing_token=1,
            lease_until="2026-07-17T12:00:00",
        )


def test_evaluation_loader_rejects_each_frozen_asset_integrity_mutation(tmp_path: Path) -> None:
    """加载器必须分别拒绝 artifact、数量、身份、split、摘要和 case kind 漂移。"""

    def write_jsonl(path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
            newline="\n",
        )

    def refresh_manifest(root: Path, *, update_case_digests: bool = False) -> None:
        path = root / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for name in ("cases.jsonl", "labels.jsonl", "scripts.jsonl"):
            payload["artifact_digests"][name] = evaluation._file_digest(root / name)
        cases = evaluation._load_jsonl(root / "cases.jsonl")
        if update_case_digests:
            payload["case_digests"] = {
                case["case_id"]: evaluation._sha256(evaluation._canonical_bytes(case))
                for case in cases
            }
            payload["dataset_digest"] = evaluation._sha256(
                b"".join(evaluation._canonical_bytes(case) for case in cases)
            )
        payload["manifest_digest"] = ""
        payload["manifest_digest"] = evaluation._sha256(
            evaluation._canonical_bytes(
                {key: value for key, value in payload.items() if key != "manifest_digest"}
            )
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    root = tmp_path / "artifact"
    evaluation.generate_phase16_controlled_multi_agent_dataset(root)
    with (root / "cases.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ValueError, match="artifact digest"):
        evaluation.load_phase16_controlled_multi_agent_dataset(root)

    mutations = {
        "count": (lambda records: records.pop(), False, "exactly 48"),
        "identity": (
            lambda records: records.__setitem__(1, records[0]),
            False,
            "identities",
        ),
        "split": (lambda records: records.reverse(), False, "split IDs"),
        "case_digest": (
            lambda records: records[0]["input"].__setitem__("scenario", "tampered"),
            False,
            "case digests",
        ),
        "dataset_digest": (lambda records: None, False, "dataset digest"),
        "kind": (
            lambda records: records[0].__setitem__("kind", "HIGH_CONFLICT_PAIRED"),
            True,
            "case kinds",
        ),
    }
    for name, (mutate, refresh_cases, message) in mutations.items():
        case_root = tmp_path / name
        evaluation.generate_phase16_controlled_multi_agent_dataset(case_root)
        records = evaluation._load_jsonl(case_root / "cases.jsonl")
        mutate(records)
        write_jsonl(case_root / "cases.jsonl", records)
        if name == "dataset_digest":
            payload = json.loads((case_root / "manifest.json").read_text(encoding="utf-8"))
            payload["dataset_digest"] = "f" * 64
            (case_root / "manifest.json").write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            payload["manifest_digest"] = evaluation._sha256(
                evaluation._canonical_bytes(
                    {key: value for key, value in payload.items() if key != "manifest_digest"}
                )
            )
            (case_root / "manifest.json").write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        else:
            refresh_manifest(case_root, update_case_digests=refresh_cases)
        with pytest.raises(ValueError, match=message):
            evaluation.load_phase16_controlled_multi_agent_dataset(case_root)
