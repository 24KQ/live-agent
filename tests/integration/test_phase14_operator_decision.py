"""Phase 14 Task 5 PostgreSQL 决定、命令、租约和幂等重放验收。"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.commands import (
    DecisionExecutionContext,
    DecisionSupportCommandCompiler,
    OperatorDecisionDraft,
)
from src.decision_support.evidence import EvidenceBundleSnapshot
from src.decision_support.models import (
    DecisionKind,
    ExecutionCommand,
    Incident,
    LiveSessionWorkspace,
    Proposal,
    WorkspaceView,
)
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    ProductStrategy,
)
from src.decision_support.store import (
    PostgresDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from src.plan_engine.models import PlanNodeState
from src.specialist_runtime.models import EvidenceKind, EvidenceRef
from tests.phase14_evidence_factory import build_evidence_bundle


NOW = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module", autouse=True)
def _isolated_phase14_task5_schema():
    """Task 5 使用独立 schema，避免并发/重启证据污染其他 Phase 14 测试。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase14_task5_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    try:
        yield SimpleNamespace(
            postgres_connection_kwargs={
                **base_kwargs,
                "options": f"-c search_path={schema_name}",
            }
        )
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.commit()


def _workspace(session_id: str) -> LiveSessionWorkspace:
    return LiveSessionWorkspace(
        live_session_id=session_id,
        run_key=f"task5-run-{session_id}",
        room_id=f"room-{session_id}",
        trace_id=f"trace-{session_id}",
        anchor_id="anchor-task5",
        root_plan_run_id=f"plan-root-{session_id}",
        event_inbox_scope_id=f"event-inbox-{session_id}",
        decision_trace_scope_id=f"decision-trace-{session_id}",
        replay_scope_id=f"replay-{session_id}",
        evaluation_scope_id=f"evaluation-{session_id}",
        view=WorkspaceView.PREPARE,
    )


def _proposal(
    *,
    session_id: str,
    incident_id: str,
    evidence_bundle_id: str,
) -> Proposal:
    """生成真实六角色 EvidenceRef 闭合的 Proposal 持久化快照。"""

    bundle = build_evidence_bundle(
        live_session_id=session_id,
        incident_id=incident_id,
        suffix=session_id,
        idempotency_key=f"evidence-idem-{session_id}",
        room_id=f"room-{session_id}",
        trace_id=f"trace-{session_id}",
        anchor_id="anchor-task5",
        root_plan_run_id=f"plan-root-{session_id}",
        evidence_bundle_id=evidence_bundle_id,
        created_at=NOW,
    ).bundle
    references = tuple(
        component.reference
        for component in EvidenceBundleSnapshot.model_validate(bundle.snapshot).components
    )
    option = DecisionOption(
        option_id="keep-current",
        product_strategy=ProductStrategy.KEEP_CURRENT,
        host_prompt="请运营确认当前节奏后继续。",
        timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
        risk_flags=("HUMAN_CONFIRMATION_REQUIRED",),
        evidence_refs=references,
    )
    snapshot = LiveDecisionProposal(
        proposal_id=f"proposal-{session_id}",
        live_session_id=session_id,
        incident_id=incident_id,
        trace_id=f"trace-{session_id}",
        evidence_bundle_id=evidence_bundle_id,
        status="READY",
        options=(option,),
        evidence_refs=references,
    )
    return Proposal(
        proposal_id=snapshot.proposal_id,
        live_session_id=session_id,
        incident_id=incident_id,
        evidence_bundle_id=evidence_bundle_id,
        idempotency_key=f"proposal-idem-{session_id}",
        proposal_key="sold-out-response",
        proposal_version=1,
        profile_id="live_ops_decision_support",
        profile_version="1.0.0",
        snapshot=snapshot.model_dump(mode="json"),
        created_at=NOW,
    )


def test_postgres_compiled_decision_command_replays_after_restart(
    _isolated_phase14_task5_schema,
) -> None:
    """决定和命令必须按 fencing/CAS 追加，并在新 Store 实例中原样重放。"""

    settings = _isolated_phase14_task5_schema
    session_id = f"task5-session-{uuid4().hex}"
    incident_id = f"incident-{session_id}"
    evidence_bundle_id = f"evidence-{session_id}"
    store = PostgresDecisionSupportStore(settings)
    store.initialize_schema()
    workspace = _workspace(session_id)
    store.create_workspace(workspace)
    transition_lease = store.acquire_operator_lock(session_id, "transition-task5", 30)
    workspace = store.advance_view(
        session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=workspace.version,
        operator_id="transition-task5",
        fencing_token=transition_lease.fencing_token,
    )
    store.release_operator_lock(
        session_id,
        operator_id="transition-task5",
        fencing_token=transition_lease.fencing_token,
    )
    incident = Incident(
        incident_id=incident_id,
        live_session_id=session_id,
        idempotency_key=f"incident-idem-{session_id}",
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=(f"event-{session_id}",),
        snapshot={"product_id": "p001", "expected_version": 2},
        created_at=NOW,
    )
    workspace = store.append_incident(incident, expected_workspace_version=workspace.version)
    evidence = build_evidence_bundle(
        live_session_id=session_id,
        incident_id=incident_id,
        suffix=session_id,
        idempotency_key=f"evidence-idem-{session_id}",
        room_id=f"room-{session_id}",
        trace_id=f"trace-{session_id}",
        anchor_id="anchor-task5",
        root_plan_run_id=f"plan-root-{session_id}",
        evidence_bundle_id=evidence_bundle_id,
        created_at=NOW,
    )
    workspace = store.append_evidence_bundle(
        evidence,
        expected_workspace_version=workspace.version,
    )
    proposal = _proposal(
        session_id=session_id,
        incident_id=incident_id,
        evidence_bundle_id=evidence_bundle_id,
    )
    workspace = store.append_proposal(proposal, expected_workspace_version=workspace.version)
    lease = store.acquire_operator_lock(session_id, "operator-task5", 60)
    draft = OperatorDecisionDraft(
        decision_id=f"decision-{session_id}",
        proposal_id=proposal.proposal_id,
        expected_proposal_version=1,
        operator_id="operator-task5",
        decision_kind=DecisionKind.APPROVE,
        reason_code="CONFIRMED_SAFE",
        idempotency_key=f"decision-idem-{session_id}",
        option_id="keep-current",
    )
    compiled = DecisionSupportCommandCompiler().compile(
        proposal=proposal,
        draft=draft,
        lease=lease,
        execution_context=DecisionExecutionContext(
            plan_run_id=f"plan-root-{session_id}",
            expected_plan_version=2,
            node_id=f"node-{session_id}",
            expected_node_status=PlanNodeState.WAITING_APPROVAL,
        ),
        # PostgreSQL lease 使用数据库墙钟，集成测试不能用固定历史时间冒充当前 lease。
        now=datetime.now(timezone.utc),
    )
    assert compiled.execution_command is not None
    workspace_after_decision = store.append_operator_decision(
        compiled.operator_decision,
        expected_workspace_version=workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )
    workspace_after_command = store.append_execution_command(
        compiled.execution_command,
        expected_workspace_version=workspace_after_decision.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )

    restarted = PostgresDecisionSupportStore(settings)
    assert restarted.get_operator_decision(compiled.operator_decision.decision_id) == compiled.operator_decision
    assert restarted.get_execution_command(compiled.execution_command.command_id) == compiled.execution_command
    assert restarted.append_operator_decision(
        compiled.operator_decision,
        expected_workspace_version=999,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    ).version == workspace_after_command.version
    assert restarted.append_execution_command(
        compiled.execution_command,
        expected_workspace_version=999,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    ).version == workspace_after_command.version

    with pytest.raises(WorkspaceLeaseError):
        restarted.acquire_operator_lock(session_id, "operator-other", 60)

    stale_command_data = compiled.execution_command.model_dump(mode="json")
    stale_command_data["command_id"] = f"stale-command-{session_id}"
    stale_command_data["idempotency_key"] = f"stale-command-idem-{session_id}"
    with pytest.raises(WorkspaceLeaseError):
        restarted.append_execution_command(
            ExecutionCommand.model_validate(stale_command_data),
            expected_workspace_version=999,
            operator_id="operator-other",
            fencing_token=lease.fencing_token,
        )
