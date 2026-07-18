"""Phase 16 Task 4 升级事实 Store 的内存 RED/GREEN 契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.decision_support.evidence import AssembledEvidenceBundle, EvidenceBundleSnapshot
from src.decision_support.models import (
    ConflictAnalysis,
    ConflictAnalysisCode,
    EscalationMode,
    EscalationRecord,
    Incident,
    LiveSessionWorkspace,
    MultiAgentFailureCode,
    MultiAgentOutcome,
    MultiAgentOutcomeStatus,
    WorkspaceView,
)
from src.decision_support.multi_agent import build_evidence_analyst_profile
from src.decision_support.store import (
    InMemoryDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from tests.phase14_evidence_factory import build_evidence_bundle


NOW = datetime.now(timezone.utc)


def _workspace() -> LiveSessionWorkspace:
    """构造进入 LIVE 前的最小跨系统 Workspace 身份。"""

    return LiveSessionWorkspace(
        live_session_id="phase16-session-001",
        run_key="phase16-store-run-001",
        room_id="room-phase16",
        trace_id="trace-phase16",
        # 复用六角色受控 Evidence 工厂时，Workspace 必须采用其固定的主播父身份。
        anchor_id="anchor-phase14",
        root_plan_run_id="root-plan-phase16",
        event_inbox_scope_id="event-scope-phase16",
        decision_trace_scope_id="decision-scope-phase16",
        replay_scope_id="replay-scope-phase16",
        evaluation_scope_id="evaluation-scope-phase16",
        view=WorkspaceView.PREPARE,
        version=1,
    )


def _incident() -> Incident:
    """构造与受控 EvidenceBundle 一致的售罄复合事故父事实。"""

    return Incident(
        incident_id="phase16-incident-001",
        live_session_id="phase16-session-001",
        idempotency_key="phase16-incident-idem-001",
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=("event-phase16",),
        snapshot={"product_id": "p001", "expected_version": 2},
        created_at=NOW,
    )


def _evidence(*, reconciliation_required: bool = False) -> AssembledEvidenceBundle:
    """使用既有六角色工厂，避免测试伪造可升级的 EvidenceBundle。"""

    return build_evidence_bundle(
        live_session_id="phase16-session-001",
        incident_id="phase16-incident-001",
        suffix="phase16",
        idempotency_key="phase16-evidence-idem-001",
        evidence_bundle_id="phase16-bundle-001",
        room_id="room-phase16",
        trace_id="trace-phase16",
        root_plan_run_id="root-plan-phase16",
        created_at=NOW,
        reconciliation_required=reconciliation_required,
        # 全量回归可能在模块导入数十秒后才运行本文件；每次构造都取当前 UTC，
        # 保持六角色 Bundle 在其 10 秒 freshness TTL 内，而不是放宽生产门禁。
        evidence_time=datetime.now(timezone.utc),
    )


def _seed_live_bundle(
    store: InMemoryDecisionSupportStore, *, reconciliation_required: bool = False
):
    """按既有状态机进入 LIVE 后写入 Incident 与受控 Bundle。"""

    workspace = store.create_workspace(_workspace())
    lease = store.acquire_operator_lock(
        workspace.live_session_id, "operator-phase16", 60, now=NOW
    )
    workspace = store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=NOW,
    )
    workspace = store.append_incident(_incident(), expected_workspace_version=workspace.version)
    evidence = _evidence(reconciliation_required=reconciliation_required)
    workspace = store.append_evidence_bundle(
        evidence, expected_workspace_version=workspace.version
    )
    return workspace, lease, evidence.bundle


def _escalation(bundle, *, mode: EscalationMode = EscalationMode.AUTOMATIC) -> EscalationRecord:
    """从已校验 Bundle 重建不可伪造的升级父身份和摘要。"""

    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return EscalationRecord(
        escalation_id="phase16-escalation-001",
        live_session_id=bundle.live_session_id,
        incident_id=bundle.incident_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_digest=snapshot.bundle_digest,
        idempotency_key="phase16-escalation-idem-001",
        mode=mode,
        trigger_codes=(
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
            ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,
        ) if mode is EscalationMode.AUTOMATIC else (),
        operator_id="operator-phase16" if mode is EscalationMode.OPERATOR_REQUESTED else None,
        created_at=NOW,
    )


def _analysis(escalation: EscalationRecord, bundle) -> ConflictAnalysis:
    """构造只引用同一升级与 Bundle 的 Analyst 中间事实。"""

    profile = build_evidence_analyst_profile()
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return ConflictAnalysis(
        analysis_id="phase16-analysis-001",
        idempotency_key="phase16-analysis-idem-001",
        escalation_id=escalation.escalation_id,
        live_session_id=escalation.live_session_id,
        incident_id=escalation.incident_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_digest=snapshot.bundle_digest,
        analyst_profile_id=profile.profile_id,
        analyst_profile_version=profile.profile_version,
        analyst_profile_digest=profile.profile_digest,
        finding_codes=escalation.trigger_codes,
        constraint_codes=("OPERATOR_CONFIRMATION_REQUIRED",),
        risk_codes=("INVENTORY_CONFLICT_REQUIRES_REVIEW",),
        explanation="多个备品与库存冲突证据需要运营确认。",
        evidence_refs=tuple(component.reference for component in snapshot.components),
        created_at=NOW,
    )


def _outcome(escalation: EscalationRecord, analysis: ConflictAnalysis) -> MultiAgentOutcome:
    """Task 4 只持久化失败终态，Planner Proposal 仍由后续 Task 追加。"""

    return MultiAgentOutcome(
        outcome_id="phase16-outcome-001",
        idempotency_key="phase16-outcome-idem-001",
        escalation_id=escalation.escalation_id,
        live_session_id=escalation.live_session_id,
        incident_id=escalation.incident_id,
        escalation_digest=escalation.escalation_digest,
        evidence_bundle_id=escalation.evidence_bundle_id,
        evidence_bundle_digest=escalation.evidence_bundle_digest,
        status=MultiAgentOutcomeStatus.DEGRADED,
        analysis_id=analysis.analysis_id,
        analysis_digest=analysis.analysis_digest,
        failure_code=MultiAgentFailureCode.ANALYST_MODEL_ERROR,
        fact_summary="Analyst 失败，保留确定性售罄保护与人工处理。",
        created_at=NOW,
    )


def test_escalation_analysis_and_outcome_append_with_cas_and_replay() -> None:
    """三类新事实必须依次追加、幂等重放且只在首次写入推进 Workspace 版本。"""

    store = InMemoryDecisionSupportStore()
    workspace, _lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)

    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    assert after_escalation.version == workspace.version + 1
    assert store.append_escalation(escalation, expected_workspace_version=999) == after_escalation

    analysis = _analysis(escalation, bundle)
    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    outcome = _outcome(escalation, analysis)
    after_outcome = store.append_multi_agent_outcome(
        outcome, expected_workspace_version=after_analysis.version
    )

    assert after_outcome.version == after_analysis.version + 1
    assert store.get_escalation(escalation.escalation_id) == escalation
    assert store.get_conflict_analysis(analysis.analysis_id) == analysis
    assert store.get_multi_agent_outcome(outcome.outcome_id) == outcome


def test_escalation_requires_matching_bundle_parent_and_operator_fencing() -> None:
    """伪造 Bundle 摘要与过期操作员 fencing 都不得形成新的升级事实。"""

    store = InMemoryDecisionSupportStore()
    workspace, lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    forged_data = escalation.model_dump(mode="python")
    forged_data.update({"evidence_bundle_digest": "f" * 64, "escalation_digest": ""})
    forged = EscalationRecord.model_validate(forged_data)
    with pytest.raises(WorkspaceConflictError, match="bundle"):
        store.append_escalation(forged, expected_workspace_version=workspace.version)

    requested = _escalation(bundle, mode=EscalationMode.OPERATOR_REQUESTED)
    with pytest.raises(WorkspaceLeaseError, match="fencing"):
        store.append_escalation(
            requested,
            expected_workspace_version=workspace.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token + 1,
            now=NOW,
        )


def test_escalation_reconstructs_automatic_signals_and_rejects_manual_trigger_codes() -> None:
    """Store 不能相信调用方自报冲突：自动规则来自 Bundle，人工规则不携带触发码。"""

    store = InMemoryDecisionSupportStore()
    workspace, lease, bundle = _seed_live_bundle(store)
    automatic = _escalation(bundle)
    forged_automatic = EscalationRecord.model_validate(
        {
            **automatic.model_dump(mode="python"),
            "trigger_codes": (
                ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS,
                ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
            ),
            "escalation_digest": "",
        }
    )
    with pytest.raises(WorkspaceConflictError, match="trigger"):
        store.append_escalation(
            forged_automatic, expected_workspace_version=workspace.version
        )
    requested = _escalation(bundle, mode=EscalationMode.OPERATOR_REQUESTED)
    forged_requested = EscalationRecord.model_validate(
        {
            **requested.model_dump(mode="python"),
            "trigger_codes": (ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,),
            "escalation_digest": "",
        }
    )
    with pytest.raises(WorkspaceConflictError, match="trigger"):
        store.append_escalation(
            forged_requested,
            expected_workspace_version=workspace.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )


def test_escalation_requires_proposal_eligible_bundle_for_all_modes() -> None:
    """等待对账或副作用未知的 Bundle 即使有两个冲突信号也不得进入自动或人工升级。"""

    store = InMemoryDecisionSupportStore()
    workspace, lease, bundle = _seed_live_bundle(
        store, reconciliation_required=True
    )
    automatic = _escalation(bundle)
    with pytest.raises(WorkspaceConflictError, match="eligible"):
        store.append_escalation(
            automatic, expected_workspace_version=workspace.version
        )
    requested = _escalation(bundle, mode=EscalationMode.OPERATOR_REQUESTED)
    with pytest.raises(WorkspaceConflictError, match="eligible"):
        store.append_escalation(
            requested,
            expected_workspace_version=workspace.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
            now=NOW,
        )


def test_escalation_requires_unexpired_bundle_for_all_modes() -> None:
    """证据超过 Bundle valid_until 后，三选二信号与操作员 lease 都不能重新开启链路。"""

    store = InMemoryDecisionSupportStore()
    workspace, lease, bundle = _seed_live_bundle(store)
    stale_now = datetime.now(timezone.utc) + timedelta(minutes=1)
    with pytest.raises(WorkspaceConflictError, match="fresh"):
        store.append_escalation(
            _escalation(bundle),
            expected_workspace_version=workspace.version,
            now=stale_now,
        )
    with pytest.raises(WorkspaceConflictError, match="fresh"):
        store.append_escalation(
            _escalation(bundle, mode=EscalationMode.OPERATOR_REQUESTED),
            expected_workspace_version=workspace.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
            now=stale_now,
        )
def test_escalation_allows_one_analysis_per_profile_and_one_terminal_outcome() -> None:
    """同一升级不得重复消耗 Analyst Profile 或记录相互矛盾的终态。"""

    store = InMemoryDecisionSupportStore()
    workspace, _lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    workspace = store.append_escalation(escalation, expected_workspace_version=workspace.version)
    analysis = _analysis(escalation, bundle)
    workspace = store.append_conflict_analysis(analysis, expected_workspace_version=workspace.version)
    with pytest.raises(WorkspaceConflictError, match="analysis"):
        store.append_conflict_analysis(
            ConflictAnalysis.model_validate(
                {
                    **analysis.model_dump(mode="python"),
                    "analysis_id": "phase16-analysis-duplicate",
                    "idempotency_key": "phase16-analysis-idem-duplicate",
                    "analysis_digest": "",
                }
            ),
            expected_workspace_version=workspace.version,
        )
    outcome = _outcome(escalation, analysis)
    store.append_multi_agent_outcome(outcome, expected_workspace_version=workspace.version)
    with pytest.raises(WorkspaceConflictError, match="outcome"):
        store.append_multi_agent_outcome(
            MultiAgentOutcome.model_validate(
                {
                    **outcome.model_dump(mode="python"),
                    "outcome_id": "phase16-outcome-duplicate",
                    "idempotency_key": "phase16-outcome-idem-duplicate",
                    "outcome_digest": "",
                }
            ),
            expected_workspace_version=workspace.version + 1,
        )


def test_ready_outcome_fails_closed_until_task6_persists_verified_proposal() -> None:
    """Task 4 没有 Proposal 事实摘要存储，不能接受无法验证 digest 的 READY 终态。"""

    store = InMemoryDecisionSupportStore()
    workspace, _lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    workspace = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    analysis = _analysis(escalation, bundle)
    workspace = store.append_conflict_analysis(
        analysis, expected_workspace_version=workspace.version
    )
    degraded = _outcome(escalation, analysis)
    ready = MultiAgentOutcome.model_validate(
        {
            **degraded.model_dump(mode="python"),
            "status": MultiAgentOutcomeStatus.READY,
            "proposal_id": "phase16-unverified-proposal-001",
            "proposal_digest": "a" * 64,
            "failure_code": None,
            "outcome_digest": "",
        }
    )

    with pytest.raises(WorkspaceConflictError, match="Task 6"):
        store.append_multi_agent_outcome(
            ready, expected_workspace_version=workspace.version
        )
