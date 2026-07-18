"""Phase 16 Task 4 升级事实 PostgreSQL RED/GREEN 契约。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import get_settings
from src.decision_support.evidence import EvidenceBundleSnapshot
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
    PostgresDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from tests.phase14_evidence_factory import build_evidence_bundle


NOW = datetime.now(timezone.utc)
_TEST_CONNECTION_KWARGS: dict[str, object] | None = None


@pytest.fixture(scope="module", autouse=True)
def _isolated_phase16_schema():
    """为本模块创建独立 schema，确保触发器绕过测试不会污染开发数据库。"""

    global _TEST_CONNECTION_KWARGS
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_test_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        conn.commit()
    _TEST_CONNECTION_KWARGS = {
        **base_kwargs,
        "options": f"-c search_path={schema_name}",
    }
    try:
        yield
    finally:
        _TEST_CONNECTION_KWARGS = None
        with psycopg.connect(**base_kwargs) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
            conn.commit()


def _database_kwargs() -> dict[str, object]:
    """返回隔离 schema 连接参数，禁止测试误连公共 schema。"""

    if _TEST_CONNECTION_KWARGS is None:
        raise RuntimeError("phase16 isolated PostgreSQL schema is not initialized")
    return dict(_TEST_CONNECTION_KWARGS)


def _store() -> PostgresDecisionSupportStore:
    """用最小 Settings 投影初始化生产 PostgreSQL Store。"""

    store = PostgresDecisionSupportStore(
        SimpleNamespace(postgres_connection_kwargs=_database_kwargs())
    )
    store.initialize_schema()
    return store


def _workspace(suffix: str) -> LiveSessionWorkspace:
    """构造与六角色 Evidence 工厂绑定的唯一 Workspace 身份。"""

    return LiveSessionWorkspace(
        live_session_id=f"phase16-session-{suffix}",
        run_key=f"phase16-run-{suffix}",
        room_id=f"room-phase16-{suffix}",
        trace_id=f"trace-phase16-{suffix}",
        anchor_id="anchor-phase14",
        root_plan_run_id=f"root-plan-phase16-{suffix}",
        event_inbox_scope_id=f"event-scope-phase16-{suffix}",
        decision_trace_scope_id=f"decision-scope-phase16-{suffix}",
        replay_scope_id=f"replay-scope-phase16-{suffix}",
        evaluation_scope_id=f"evaluation-scope-phase16-{suffix}",
    )


def _incident(workspace: LiveSessionWorkspace, suffix: str) -> Incident:
    """构造可进入 LIVE 的售罄复合事故父事实。"""

    return Incident(
        incident_id=f"phase16-incident-{suffix}",
        live_session_id=workspace.live_session_id,
        idempotency_key=f"phase16-incident-idem-{suffix}",
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=(f"event-{suffix}",),
        snapshot={"product_id": "p001", "expected_version": 2},
        created_at=NOW,
    )


def _enter_live(store: PostgresDecisionSupportStore, workspace: LiveSessionWorkspace) -> None:
    """复用正式操作员 lease 状态机，而非直接修改 Workspace 当前视图。"""

    lease = store.acquire_operator_lock(
        workspace.live_session_id, "phase16-transition", 60
    )
    store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=store.get_workspace(workspace.live_session_id).version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )
    store.release_operator_lock(
        workspace.live_session_id,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )


def _seed_live_bundle(
    store: PostgresDecisionSupportStore,
    suffix: str,
    *,
    reconciliation_required: bool = False,
    evidence_time: datetime | None = None,
):
    """按生产路径创建 Workspace、Incident 与受治理六角色 EvidenceBundle。"""

    workspace = _workspace(suffix)
    store.create_workspace(workspace)
    incident = _incident(workspace, suffix)
    store.append_incident(incident, expected_workspace_version=1)
    _enter_live(store, workspace)
    evidence = build_evidence_bundle(
        live_session_id=workspace.live_session_id,
        incident_id=incident.incident_id,
        # Incident 的 source_ref_ids 与工厂内部 event-<suffix> 必须完全一致；
        # 这里保留唯一 suffix，不再人为附加第二层前缀造成父事实绑定失配。
        suffix=suffix,
        idempotency_key=f"phase16-bundle-idem-{suffix}",
        evidence_bundle_id=f"phase16-bundle-{suffix}",
        room_id=workspace.room_id,
        trace_id=workspace.trace_id,
        root_plan_run_id=workspace.root_plan_run_id,
        created_at=NOW,
        reconciliation_required=reconciliation_required,
        evidence_time=evidence_time or datetime.now(timezone.utc),
    )
    after_bundle = store.append_evidence_bundle(
        evidence,
        expected_workspace_version=store.get_workspace(workspace.live_session_id).version,
    )
    return workspace, incident, evidence.bundle, after_bundle


def _escalation(bundle, suffix: str, *, mode: EscalationMode = EscalationMode.AUTOMATIC):
    """从受治理 Bundle 重建升级父身份，避免测试伪造摘要。"""

    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return EscalationRecord(
        escalation_id=f"phase16-escalation-{suffix}",
        live_session_id=bundle.live_session_id,
        incident_id=bundle.incident_id,
        evidence_bundle_id=bundle.evidence_bundle_id,
        evidence_bundle_digest=snapshot.bundle_digest,
        idempotency_key=f"phase16-escalation-idem-{suffix}",
        mode=mode,
        trigger_codes=(
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
            ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,
        ) if mode is EscalationMode.AUTOMATIC else (),
        operator_id="operator-phase16" if mode is EscalationMode.OPERATOR_REQUESTED else None,
        created_at=NOW,
    )


def _analysis(escalation: EscalationRecord, bundle, suffix: str) -> ConflictAnalysis:
    """构造只引用当前升级和 Bundle 的冻结 Analyst 中间事实。"""

    profile = build_evidence_analyst_profile()
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    return ConflictAnalysis(
        analysis_id=f"phase16-analysis-{suffix}",
        idempotency_key=f"phase16-analysis-idem-{suffix}",
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


def _outcome(
    escalation: EscalationRecord, analysis: ConflictAnalysis, suffix: str
) -> MultiAgentOutcome:
    """构造 Task 4 的可解释降级终态，尚不引入 Task 6 Planner Proposal。"""

    return MultiAgentOutcome(
        outcome_id=f"phase16-outcome-{suffix}",
        idempotency_key=f"phase16-outcome-idem-{suffix}",
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


def test_postgres_escalation_facts_replay_and_restart() -> None:
    """三个新事实在重启后仍可读取，同载荷重放不得重复推进 CAS 版本。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    outcome = _outcome(escalation, analysis, suffix)
    after_outcome = store.append_multi_agent_outcome(
        outcome, expected_workspace_version=after_analysis.version
    )

    restarted = _store()
    assert restarted.get_escalation(escalation.escalation_id) == escalation
    assert restarted.get_conflict_analysis(analysis.analysis_id) == analysis
    assert restarted.get_multi_agent_outcome(outcome.outcome_id) == outcome
    assert restarted.append_multi_agent_outcome(
        outcome, expected_workspace_version=999
    ) == after_outcome


def test_postgres_escalation_single_bundle_cas_and_operator_fencing() -> None:
    """并发同 Bundle 只能有一个升级；运营升级还必须验证当前 lease fencing。"""

    suffix = uuid4().hex
    store = _store()
    workspace, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    first = _escalation(bundle, f"{suffix}-first")
    second = _escalation(bundle, f"{suffix}-second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                store.append_escalation,
                fact,
                expected_workspace_version=after_bundle.version,
            )
            for fact in (first, second)
        ]
    results = [future.exception() for future in futures]
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, WorkspaceConflictError) for result in results) == 1

    requested = _escalation(bundle, f"{suffix}-requested", mode=EscalationMode.OPERATOR_REQUESTED)
    lease = store.acquire_operator_lock(workspace.live_session_id, "operator-phase16", 60)
    with pytest.raises(WorkspaceLeaseError, match="fencing"):
        store.append_escalation(
            requested,
            expected_workspace_version=store.get_workspace(workspace.live_session_id).version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token + 1,
        )


def test_postgres_escalation_trigger_rejects_ledger_bypass() -> None:
    """即使直接 SQL 插入，事实表也必须拒绝缺少对应幂等账本的伪造升级。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    payload = escalation.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="idempotency ledger"):
            conn.execute(
                """INSERT INTO phase16_escalations
                   (escalation_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,mode,operator_id,fencing_token,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    escalation.escalation_id,
                    escalation.live_session_id,
                    escalation.incident_id,
                    escalation.evidence_bundle_id,
                    escalation.evidence_bundle_digest,
                    escalation.mode.value,
                    None,
                    None,
                    after_bundle.version,
                    Jsonb(payload),
                    escalation.created_at,
                ),
            )
            # 账本完整性使用 deferred constraint trigger，目的是允许 Store 在同一
            # 事务先写事实、再写 ledger；因此必须在提交边界断言数据库拒绝旁路。
            conn.commit()
        conn.rollback()
    assert store.get_workspace(escalation.live_session_id).version == after_bundle.version


def test_postgres_analysis_and_outcome_triggers_reject_ledger_bypass() -> None:
    """中间分析与终态也必须在提交时拥有同载荷 ledger，不能只保护升级起点。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    analysis_payload = analysis.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="idempotency ledger"):
            conn.execute(
                """INSERT INTO phase16_conflict_analyses
                   (analysis_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,escalation_id,analyst_profile_id,
                    analyst_profile_version,analyst_profile_digest,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    analysis.analysis_id,
                    analysis.live_session_id,
                    analysis.incident_id,
                    analysis.evidence_bundle_id,
                    analysis.evidence_bundle_digest,
                    analysis.escalation_id,
                    analysis.analyst_profile_id,
                    analysis.analyst_profile_version,
                    analysis.analyst_profile_digest,
                    after_escalation.version,
                    Jsonb(analysis_payload),
                    analysis.created_at,
                ),
            )
            conn.commit()
        conn.rollback()

    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    outcome = _outcome(escalation, analysis, suffix)
    outcome_payload = outcome.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="idempotency ledger"):
            conn.execute(
                """INSERT INTO phase16_multi_agent_outcomes
                   (outcome_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,escalation_id,escalation_digest,analysis_id,
                    analysis_digest,proposal_id,proposal_digest,status,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    outcome.outcome_id,
                    outcome.live_session_id,
                    outcome.incident_id,
                    outcome.evidence_bundle_id,
                    outcome.evidence_bundle_digest,
                    outcome.escalation_id,
                    outcome.escalation_digest,
                    outcome.analysis_id,
                    outcome.analysis_digest,
                    outcome.proposal_id,
                    outcome.proposal_digest,
                    outcome.status.value,
                    after_analysis.version,
                    Jsonb(outcome_payload),
                    outcome.created_at,
                ),
            )
            conn.commit()
        conn.rollback()

    assert store.get_workspace(outcome.live_session_id).version == after_analysis.version


def test_postgres_analysis_trigger_rejects_ledger_backed_partial_bundle_refs() -> None:
    """直接补齐 ledger 也不得把只引用部分 Bundle 的 Analyst 事实写入数据库。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    forged = ConflictAnalysis.model_validate(
        {
            **analysis.model_dump(mode="python"),
            "analysis_id": f"{analysis.analysis_id}-partial",
            "idempotency_key": f"{analysis.idempotency_key}-partial",
            "evidence_refs": (analysis.evidence_refs[0],),
            "analysis_digest": "",
        }
    )
    payload = forged.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="evidence refs"):
            conn.execute(
                """INSERT INTO phase16_conflict_analyses
                   (analysis_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,escalation_id,analyst_profile_id,
                    analyst_profile_version,analyst_profile_digest,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    forged.analysis_id,
                    forged.live_session_id,
                    forged.incident_id,
                    forged.evidence_bundle_id,
                    forged.evidence_bundle_digest,
                    forged.escalation_id,
                    forged.analyst_profile_id,
                    forged.analyst_profile_version,
                    forged.analyst_profile_digest,
                    after_escalation.version,
                    Jsonb(payload),
                    forged.created_at,
                ),
            )
            conn.execute(
                """INSERT INTO phase14_workspace_idempotency
                   (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    forged.live_session_id,
                    forged.idempotency_key,
                    "conflict_analysis",
                    forged.analysis_id,
                    Jsonb(payload),
                ),
            )
            conn.commit()
        conn.rollback()


def test_postgres_escalation_trigger_requires_current_workspace_cas() -> None:
    """即使调用方同时准备事实和 ledger，陈旧 Workspace 版本也必须被数据库拒绝。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    payload = escalation.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="workspace version"):
            conn.execute(
                """INSERT INTO phase16_escalations
                   (escalation_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,mode,operator_id,fencing_token,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    escalation.escalation_id,
                    escalation.live_session_id,
                    escalation.incident_id,
                    escalation.evidence_bundle_id,
                    escalation.evidence_bundle_digest,
                    escalation.mode.value,
                    None,
                    None,
                    after_bundle.version - 1,
                    Jsonb(payload),
                    escalation.created_at,
                ),
            )
        conn.rollback()
    assert store.get_workspace(escalation.live_session_id).version == after_bundle.version


def test_postgres_escalation_rejects_ineligible_bundle_and_reversed_trigger_order() -> None:
    """数据库层必须拒绝等待对账 Bundle 和 ledger-backed 的非规范自动触发码顺序。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(
        store, suffix, reconciliation_required=True
    )
    with pytest.raises(WorkspaceConflictError, match="eligible"):
        store.append_escalation(
            _escalation(bundle, suffix),
            expected_workspace_version=after_bundle.version,
        )
    ineligible_fact = _escalation(bundle, f"{suffix}-direct")
    ineligible_payload = ineligible_fact.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="proposal eligible"):
            conn.execute(
                """INSERT INTO phase16_escalations
                   (escalation_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,mode,operator_id,fencing_token,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    ineligible_fact.escalation_id,
                    ineligible_fact.live_session_id,
                    ineligible_fact.incident_id,
                    ineligible_fact.evidence_bundle_id,
                    ineligible_fact.evidence_bundle_digest,
                    ineligible_fact.mode.value,
                    None,
                    None,
                    after_bundle.version,
                    Jsonb(ineligible_payload),
                    ineligible_fact.created_at,
                ),
            )
        conn.rollback()
    assert store.get_workspace(ineligible_fact.live_session_id).version == after_bundle.version

    ordered_suffix = uuid4().hex
    _workspace_fact, _incident_fact, ordered_bundle, ordered_after_bundle = _seed_live_bundle(
        store, ordered_suffix
    )
    escalation = _escalation(ordered_bundle, ordered_suffix)
    reversed_fact = EscalationRecord.model_validate(
        {
            **escalation.model_dump(mode="python"),
            "trigger_codes": tuple(reversed(escalation.trigger_codes)),
            "escalation_digest": "",
        }
    )
    payload = reversed_fact.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="trigger codes"):
            conn.execute(
                """INSERT INTO phase16_escalations
                   (escalation_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,mode,operator_id,fencing_token,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    reversed_fact.escalation_id,
                    reversed_fact.live_session_id,
                    reversed_fact.incident_id,
                    reversed_fact.evidence_bundle_id,
                    reversed_fact.evidence_bundle_digest,
                    reversed_fact.mode.value,
                    None,
                    None,
                    ordered_after_bundle.version,
                    Jsonb(payload),
                    reversed_fact.created_at,
                ),
            )
            conn.execute(
                """INSERT INTO phase14_workspace_idempotency
                   (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    reversed_fact.live_session_id,
                    reversed_fact.idempotency_key,
                    "escalation",
                    reversed_fact.escalation_id,
                    Jsonb(payload),
                ),
            )
            conn.commit()
        conn.rollback()


def test_postgres_escalation_trigger_rejects_expired_bundle_before_cas() -> None:
    """过期 Bundle 的直写必须在事实插入和 Workspace CAS 前被触发器拒绝。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(
        store,
        suffix,
        evidence_time=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    escalation = _escalation(bundle, suffix)
    payload = escalation.model_dump(mode="json")
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="bundle digest mismatch"):
            conn.execute(
                """INSERT INTO phase16_escalations
                   (escalation_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,mode,operator_id,fencing_token,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    escalation.escalation_id,
                    escalation.live_session_id,
                    escalation.incident_id,
                    escalation.evidence_bundle_id,
                    escalation.evidence_bundle_digest,
                    escalation.mode.value,
                    None,
                    None,
                    after_bundle.version,
                    Jsonb(payload),
                    escalation.created_at,
                ),
            )
        conn.rollback()
    assert store.get_workspace(escalation.live_session_id).version == after_bundle.version


def test_postgres_outcome_trigger_rejects_degraded_shape_bypass() -> None:
    """直写 DEGRADED Outcome 也必须闭合 failure 语义，不能绕过领域模型留下坏审计行。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    outcome = _outcome(escalation, analysis, suffix)
    payload = outcome.model_dump(mode="json")
    payload["failure_code"] = None
    payload["outcome_digest"] = "f" * 64
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="DEGRADED outcome"):
            conn.execute(
                """INSERT INTO phase16_multi_agent_outcomes
                   (outcome_id,live_session_id,incident_id,evidence_bundle_id,
                    evidence_bundle_digest,escalation_id,escalation_digest,analysis_id,
                    analysis_digest,proposal_id,proposal_digest,status,
                    expected_workspace_version,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    outcome.outcome_id,
                    outcome.live_session_id,
                    outcome.incident_id,
                    outcome.evidence_bundle_id,
                    outcome.evidence_bundle_digest,
                    outcome.escalation_id,
                    outcome.escalation_digest,
                    outcome.analysis_id,
                    outcome.analysis_digest,
                    None,
                    None,
                    outcome.status.value,
                    after_analysis.version,
                    Jsonb(payload),
                    outcome.created_at,
                ),
            )
            conn.execute(
                """INSERT INTO phase14_workspace_idempotency
                   (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    outcome.live_session_id,
                    outcome.idempotency_key,
                    "multi_agent_outcome",
                    outcome.outcome_id,
                    Jsonb(payload),
                ),
            )
            conn.commit()
        conn.rollback()
    assert store.get_workspace(outcome.live_session_id).version == after_analysis.version
