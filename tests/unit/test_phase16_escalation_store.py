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
        # D-147 后两类升级都记录服务端从默认冻结 Bundle 重建的真实信号；本夹具的
        # 两项固定信号使各测试能独立验证 lease、freshness 和父链，而非被空 finding
        # 的旧人工占位语义提前拦截。
        trigger_codes=(
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
            ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,
        ),
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


def test_escalation_reconstructs_signals_for_all_modes_and_rejects_manual_mismatch() -> None:
    """Store 不能相信调用方自报冲突：自动与人工触发码都必须精确来自 Bundle。"""

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


def test_ready_outcome_requires_a_matching_persisted_multi_agent_proposal() -> None:
    """READY 必须引用完整且可重验的多 Agent Proposal，不能只携带调用方自报摘要。"""

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

    with pytest.raises(WorkspaceConflictError, match="proposal parent"):
        store.append_multi_agent_outcome(
            ready, expected_workspace_version=workspace.version
        )


def test_analysis_store_rejects_findings_that_differ_from_frozen_escalation_triggers() -> None:
    """直接 Store 调用同样不能把模型新增或删减的 finding 伪装成同一高冲突升级。"""

    store = InMemoryDecisionSupportStore()
    workspace, _lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    workspace = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    analysis = _analysis(escalation, bundle)
    forged = ConflictAnalysis.model_validate(
        {
            **analysis.model_dump(mode="python"),
            "finding_codes": (ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,),
            "analysis_digest": "",
        }
    )

    with pytest.raises(WorkspaceConflictError, match="finding codes"):
        store.append_conflict_analysis(
            forged, expected_workspace_version=workspace.version
        )


def test_dispatch_claim_uses_store_clock_and_fixed_two_second_window() -> None:
    """内存 Store 不得让 Coordinator 传入任意时钟或租约长度来延长模型观察窗口。"""

    instant = datetime.now(timezone.utc)
    store = InMemoryDecisionSupportStore(clock=lambda: instant)
    workspace, _lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    store.append_escalation(escalation, expected_workspace_version=workspace.version)

    with pytest.raises(ValueError, match="exactly 2"):
        store.claim_analyst_dispatch(
            escalation_id=escalation.escalation_id,
            task_digest="a" * 64,
            now=instant + timedelta(days=1),
            lease_seconds=3,
        )


def test_new_dispatch_claim_requires_live_workspace_and_blocks_review_during_window() -> None:
    """新 claim 与 LIVE->REVIEW 迁移必须在同一 Store 锁下线性化，不能在已结束直播中发送 Analyst。"""

    store = InMemoryDecisionSupportStore()
    workspace, lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    claim, is_new, is_active = store.claim_analyst_dispatch(
        escalation_id=escalation.escalation_id,
        task_digest="b" * 64,
    )

    assert is_new is True
    assert is_active is True
    with pytest.raises(WorkspaceConflictError, match="active analyst dispatch"):
        store.advance_view(
            bundle.live_session_id,
            target_view=WorkspaceView.REVIEW,
            expected_version=after_escalation.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
            now=NOW + timedelta(seconds=1),
        )

    store = InMemoryDecisionSupportStore()
    workspace, lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    store.advance_view(
        bundle.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=after_escalation.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(WorkspaceConflictError, match="workspace is not LIVE"):
        store.claim_analyst_dispatch(
            escalation_id=escalation.escalation_id,
            task_digest="c" * 64,
        )


def test_degraded_outcome_without_analysis_cannot_follow_a_persisted_analysis() -> None:
    """成功 Analyst 事实已经存在时，不能再用无 Analysis 的降级终态覆盖同一升级的审计含义。"""

    store = InMemoryDecisionSupportStore()
    workspace, _lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    analysis = _analysis(escalation, bundle)
    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    degraded = MultiAgentOutcome.model_validate(
        {
            **_outcome(escalation, analysis).model_dump(mode="python"),
            "analysis_id": None,
            "analysis_digest": None,
            "outcome_digest": "",
        }
    )

    with pytest.raises(WorkspaceConflictError, match="analysis prevents unlinked degraded outcome"):
        store.append_multi_agent_outcome(
            degraded, expected_workspace_version=after_analysis.version
        )


def test_review_can_only_close_claim_with_unlinked_degraded_outcome() -> None:
    """D-147 的 REVIEW 例外不能把已完成 Analysis 的后续失败伪装为超时审计闭合。"""

    instant = datetime.now(timezone.utc)
    store = InMemoryDecisionSupportStore(clock=lambda: instant)
    workspace, lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    store.claim_analyst_dispatch(
        escalation_id=escalation.escalation_id,
        task_digest="d" * 64,
    )
    analysis = _analysis(escalation, bundle)
    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    after_review = store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=after_analysis.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        # claim 的两秒观察窗结束后运营可以切换视图，但只能保留“不含 Analysis”的
        # 超时审计闭合；这个带 Analysis 的 Outcome 属于 LIVE 内的后续阶段失败。
        now=instant + timedelta(seconds=3),
    )

    with pytest.raises(WorkspaceConflictError, match="review degraded closure cannot carry analysis"):
        store.append_multi_agent_outcome(
            _outcome(escalation, analysis), expected_workspace_version=after_review.version
        )


def test_review_rejects_unlinked_degraded_outcome_without_coordinator_timeout() -> None:
    """播后无父链闭合只能表达未知响应超时，不能把可分类 Planner 错误伪装为超时。"""

    instant = datetime.now(timezone.utc)
    store_clock = [instant]
    store = InMemoryDecisionSupportStore(clock=lambda: store_clock[0])
    workspace, lease, bundle = _seed_live_bundle(store)
    escalation = _escalation(bundle)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=workspace.version
    )
    # 不写 Analysis，仅建立已离开进程的 Analyst claim，使测试只覆盖 D-147 允许的
    # REVIEW 无父链闭合来源；若 failure code 不是协调器超时，Store 必须仍然拒绝。
    store.claim_analyst_dispatch(
        escalation_id=escalation.escalation_id,
        task_digest="e" * 64,
    )
    store_clock[0] = instant + timedelta(seconds=3)
    after_review = store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=after_escalation.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=store_clock[0],
    )
    invalid = MultiAgentOutcome.model_validate(
        {
            **_outcome(escalation, _analysis(escalation, bundle)).model_dump(mode="python"),
            "analysis_id": None,
            "analysis_digest": None,
            "failure_code": MultiAgentFailureCode.PLANNER_MODEL_ERROR,
            "outcome_digest": "",
        }
    )

    with pytest.raises(
        WorkspaceConflictError, match="review degraded closure requires coordinator timeout"
    ):
        store.append_multi_agent_outcome(
            invalid, expected_workspace_version=after_review.version
        )
