"""Phase 14 Task 5 运营决定、受控修改和命令编译的 RED/GREEN 契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.decision_support.models import (
    DecisionKind,
    LiveSessionWorkspace,
    OperatorLease,
    Proposal,
    WorkspaceView,
)
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    ProductStrategy,
    ProposalStatus,
)
from src.decision_support.commands import (
    DecisionExecutionContext,
    DecisionSupportCommandCompiler,
    OperatorDecisionDraft,
    OperatorModification,
)
from src.plan_engine.models import PlanCommandType, PlanNodeState
from src.specialist_runtime.models import EvidenceKind, EvidenceRef


NOW = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
SESSION_ID = "live-session-p001-sold-out-v1"
PROPOSAL_ID = "proposal-task5-001"


def _workspace() -> LiveSessionWorkspace:
    """创建 Task 5 所需的稳定三场景父身份，不连接任何外部系统。"""

    return LiveSessionWorkspace(
        live_session_id=SESSION_ID,
        run_key="phase14-task5-run-001",
        room_id="room-phase14-task5",
        trace_id="trace-phase14-task5",
        anchor_id="anchor-phase14-task5",
        root_plan_run_id="plan-root-phase14-task5",
        event_inbox_scope_id="event-inbox-phase14-task5",
        decision_trace_scope_id="decision-trace-phase14-task5",
        replay_scope_id="replay-phase14-task5",
        evaluation_scope_id="evaluation-phase14-task5",
        view=WorkspaceView.LIVE,
        version=4,
    )


def _proposal(*, status: ProposalStatus = ProposalStatus.READY) -> Proposal:
    """构造已持久化的结构化 Proposal；Compiler 不信任调用方的自由 JSON。"""

    reference = EvidenceRef(
        kind=EvidenceKind.AUDIT,
        evidence_id="audit-task5-001",
        source_version="1.0.0",
        digest="a" * 64,
        room_id="room-phase14-task5",
        anchor_id="anchor-phase14-task5",
    )
    option = DecisionOption(
        option_id="switch-backup",
        product_strategy=ProductStrategy.SWITCH_TO_BACKUP,
        backup_product_id="p002",
        host_prompt="请运营确认备品后再恢复讲解。",
        timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
        risk_flags=("HUMAN_CONFIRMATION_REQUIRED",),
        evidence_refs=(reference,),
    )
    snapshot = LiveDecisionProposal(
        proposal_id=PROPOSAL_ID,
        live_session_id=SESSION_ID,
        incident_id="incident-task5-001",
        trace_id="trace-phase14-task5",
        evidence_bundle_id="evidence-task5-001",
        status=status,
        options=(option,) if status is ProposalStatus.READY else (),
        evidence_refs=(reference,),
        fact_summary=None if status is ProposalStatus.READY else "等待对账",
        degraded_reason=None if status is ProposalStatus.READY else "PROPOSAL_INELIGIBLE",
    )
    return Proposal(
        proposal_id=PROPOSAL_ID,
        live_session_id=SESSION_ID,
        incident_id="incident-task5-001",
        evidence_bundle_id="evidence-task5-001",
        idempotency_key="proposal-task5-idem",
        proposal_key="sold-out-response",
        proposal_version=3,
        profile_id="live_ops_decision_support",
        profile_version="1.0.0",
        snapshot=snapshot.model_dump(mode="json"),
        created_at=NOW,
    )


def _lease(operator_id: str = "operator-task5") -> OperatorLease:
    return OperatorLease(
        live_session_id=SESSION_ID,
        operator_id=operator_id,
        fencing_token=7,
        lease_until=NOW + timedelta(seconds=60),
    )


def _execution_context() -> DecisionExecutionContext:
    return DecisionExecutionContext(
        plan_run_id="plan-root-phase14-task5",
        expected_plan_version=2,
        node_id="node-resume-task5",
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
    )


def test_approve_compiles_plan_command_without_mutating_proposal() -> None:
    """批准只能把已选方案编译成节点 APPROVE 意图，原 Proposal 保持不可变。"""

    proposal = _proposal()
    draft = OperatorDecisionDraft(
        decision_id="decision-task5-approve",
        proposal_id=PROPOSAL_ID,
        expected_proposal_version=3,
        operator_id="operator-task5",
        decision_kind=DecisionKind.APPROVE,
        reason_code="CONFIRMED_SAFE",
        idempotency_key="decision-task5-approve-idem",
        option_id="switch-backup",
    )

    compiled = DecisionSupportCommandCompiler().compile(
        proposal=proposal,
        draft=draft,
        lease=_lease(),
        execution_context=_execution_context(),
        now=NOW,
    )

    assert compiled.operator_decision.decision_kind is DecisionKind.APPROVE
    assert compiled.execution_command is not None
    assert compiled.plan_command is not None
    assert compiled.plan_command.command_type is PlanCommandType.APPROVE
    assert compiled.plan_command.payload["backup_product_id"] == "p002"
    assert proposal.snapshot["options"][0]["backup_product_id"] == "p002"


def test_modify_allows_only_backup_prompt_priority_and_timing() -> None:
    """MODIFY 只能提交四个结构化字段，不能改变策略、证据或工具参数。"""

    draft = OperatorDecisionDraft(
        decision_id="decision-task5-modify",
        proposal_id=PROPOSAL_ID,
        expected_proposal_version=3,
        operator_id="operator-task5",
        decision_kind=DecisionKind.MODIFY,
        reason_code="OPERATOR_CORRECTION",
        idempotency_key="decision-task5-modify-idem",
        option_id="switch-backup",
        modification=OperatorModification(
            backup_product_id="p003",
            host_prompt="请先说明库存冲突，再等待运营确认。",
            priority=80,
            timing=DecisionTiming.NEXT_BEAT,
        ),
    )

    compiled = DecisionSupportCommandCompiler().compile(
        proposal=_proposal(),
        draft=draft,
        lease=_lease(),
        execution_context=_execution_context(),
        now=NOW,
    )

    assert compiled.operator_decision.snapshot["changes"]["priority"] == 80
    assert compiled.plan_command is not None
    assert compiled.plan_command.payload["backup_product_id"] == "p003"
    assert compiled.plan_command.payload["timing"] == "NEXT_BEAT"


def test_modify_requires_at_least_one_structured_change() -> None:
    """空修改不能伪装成新的人工决定，也不能产生第二个命令。"""

    with pytest.raises(ValueError, match="change"):
        OperatorModification()


def test_reject_records_decision_without_execution_command() -> None:
    """REJECT 只记录运营拒绝事实，不为未批准经营恢复生成命令。"""

    draft = OperatorDecisionDraft(
        decision_id="decision-task5-reject",
        proposal_id=PROPOSAL_ID,
        expected_proposal_version=3,
        operator_id="operator-task5",
        decision_kind=DecisionKind.REJECT,
        reason_code="EVIDENCE_CONFLICT",
        idempotency_key="decision-task5-reject-idem",
    )

    compiled = DecisionSupportCommandCompiler().compile(
        proposal=_proposal(),
        draft=draft,
        lease=_lease(),
        execution_context=_execution_context(),
        now=NOW,
    )

    assert compiled.operator_decision.decision_kind is DecisionKind.REJECT
    assert compiled.execution_command is None
    assert compiled.plan_command is None


def test_degraded_proposal_cannot_be_approved_or_modified() -> None:
    """DEGRADED 方案只允许人工留痕拒绝，不得被编译为经营恢复。"""

    draft = OperatorDecisionDraft(
        decision_id="decision-task5-degraded",
        proposal_id=PROPOSAL_ID,
        expected_proposal_version=3,
        operator_id="operator-task5",
        decision_kind=DecisionKind.APPROVE,
        reason_code="CONFIRMED_SAFE",
        idempotency_key="decision-task5-degraded-idem",
        option_id="switch-backup",
    )

    with pytest.raises(ValueError, match="DEGRADED"):
        DecisionSupportCommandCompiler().compile(
            proposal=_proposal(status=ProposalStatus.DEGRADED),
            draft=draft,
            lease=_lease(),
            execution_context=_execution_context(),
            now=NOW,
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda draft: draft.model_validate(
            {**draft.model_dump(mode="json"), "operator_id": "operator-other"}
        ),
        lambda draft: draft.model_validate(
            {**draft.model_dump(mode="json"), "expected_proposal_version": 2}
        ),
    ],
)
def test_operator_identity_and_expected_version_are_checked(
    mutator,
) -> None:
    """租约操作员和 Proposal 版本都必须由编译器再次闭合。"""

    base = OperatorDecisionDraft(
        decision_id="decision-task5-conflict",
        proposal_id=PROPOSAL_ID,
        expected_proposal_version=3,
        operator_id="operator-task5",
        decision_kind=DecisionKind.APPROVE,
        reason_code="CONFIRMED_SAFE",
        idempotency_key="decision-task5-conflict-idem",
        option_id="switch-backup",
    )
    with pytest.raises(ValueError, match="operator|version"):
        DecisionSupportCommandCompiler().compile(
            proposal=_proposal(),
            draft=mutator(base),
            lease=_lease(),
            execution_context=_execution_context(),
            now=NOW,
        )


def test_draft_rejects_freeform_execution_fields() -> None:
    """OperatorDecision 输入不能携带 tool_calls、SQL 或任意执行参数。"""

    with pytest.raises(ValidationError, match="extra"):
        OperatorDecisionDraft.model_validate(
            {
                "decision_id": "decision-task5-extra",
                "proposal_id": PROPOSAL_ID,
                "expected_proposal_version": 3,
                "operator_id": "operator-task5",
                "decision_kind": "APPROVE",
                "reason_code": "CONFIRMED_SAFE",
                "idempotency_key": "decision-task5-extra-idem",
                "option_id": "switch-backup",
                "tool_calls": ["set_product_price"],
            }
        )
