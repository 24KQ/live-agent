"""Phase 16 Task 4 升级事实 PostgreSQL RED/GREEN 契约。"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
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
from src.decision_support.multi_agent import (
    HighConflictEscalationCoordinator,
    build_evidence_analyst_profile,
)
from src.decision_support.store import (
    PostgresDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
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
        # D-147 固定人工路径也由服务端重建 Bundle 信号；该 PostgreSQL fixture 用两个
        # 已知事实覆盖 lease/fencing，而非保留无法形成合法 Analysis 的空码旧语义。
        trigger_codes=(
            ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
            ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,
        ),
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
            # 本用例专门测试 deferred ledger，因此显式模拟生产 Store 已取得的写入上下文。
            conn.execute("SELECT set_config('phase16.analysis_write','store',true)")
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
            # 仅绕过入口上下文以继续验证后续的 EvidenceRef 完整性触发器。
            conn.execute("SELECT set_config('phase16.analysis_write','store',true)")
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


def test_postgres_analysis_trigger_rejects_forged_findings_and_profile_identity() -> None:
    """直接 SQL 即使伪造 ledger 也不能改变升级信号或冒充冻结 Analyst Profile。"""

    suffix = uuid4().hex
    store = _store()
    _workspace_fact, _incident_fact, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    forged_findings = ConflictAnalysis.model_validate(
        {
            **analysis.model_dump(mode="python"),
            "analysis_id": f"{analysis.analysis_id}-findings",
            "idempotency_key": f"{analysis.idempotency_key}-findings",
            "finding_codes": (ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,),
            "analysis_digest": "",
        }
    )
    forged_profile = analysis.model_dump(mode="json")
    forged_profile.update(
        {
            "analysis_id": f"{analysis.analysis_id}-profile",
            "idempotency_key": f"{analysis.idempotency_key}-profile",
            "analyst_profile_digest": "f" * 64,
            # 本测试故意绕过 Pydantic，证明 PostgreSQL 不能只依赖应用层对摘要的检查。
            "analysis_digest": "f" * 64,
        }
    )

    def insert_raw(payload: dict, *, analysis_id: str, profile_digest: str) -> None:
        with psycopg.connect(**_database_kwargs()) as conn:
            with pytest.raises(psycopg.Error):
                # 本回归锁定 finding/Profile 身份，需先进入与 Store 等价的事务上下文。
                conn.execute("SELECT set_config('phase16.analysis_write','store',true)")
                conn.execute(
                    """INSERT INTO phase16_conflict_analyses
                       (analysis_id,live_session_id,incident_id,evidence_bundle_id,
                        evidence_bundle_digest,escalation_id,analyst_profile_id,
                        analyst_profile_version,analyst_profile_digest,
                        expected_workspace_version,payload,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        analysis_id,
                        analysis.live_session_id,
                        analysis.incident_id,
                        analysis.evidence_bundle_id,
                        analysis.evidence_bundle_digest,
                        analysis.escalation_id,
                        analysis.analyst_profile_id,
                        analysis.analyst_profile_version,
                        profile_digest,
                        after_escalation.version,
                        Jsonb(payload),
                        analysis.created_at,
                    ),
                )
            conn.rollback()

    insert_raw(
        forged_findings.model_dump(mode="json"),
        analysis_id=forged_findings.analysis_id,
        profile_digest=forged_findings.analyst_profile_digest,
    )
    insert_raw(
        forged_profile,
        analysis_id=forged_profile["analysis_id"],
        profile_digest=forged_profile["analyst_profile_digest"],
    )
    assert store.get_workspace(escalation.live_session_id).version == after_escalation.version


def test_postgres_analysis_trigger_rejects_direct_write_without_store_context() -> None:
    """即使父链和 payload 合法，外部 SQL 也不能绕过 Store 的完整 Pydantic/canonical 验证。"""

    suffix = uuid4().hex
    store = _store()
    _workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="analysis write authorization"):
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
                    Jsonb(analysis.model_dump(mode="json")),
                    analysis.created_at,
                ),
            )
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


def test_postgres_raw_analysis_and_unlinked_degraded_outcome_are_serialized() -> None:
    """两个连接不能先分别通过存在性检查，再提交成功 Analysis 与无 Analysis 的降级终态。"""

    suffix = uuid4().hex
    store = _store()
    _workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    analysis = _analysis(escalation, bundle, suffix)
    degraded = MultiAgentOutcome.model_validate(
        {
            **_outcome(escalation, analysis, suffix).model_dump(mode="python"),
            "outcome_id": f"phase16-unlinked-outcome-{suffix}",
            "idempotency_key": f"phase16-unlinked-outcome-idem-{suffix}",
            "analysis_id": None,
            "analysis_digest": None,
            "outcome_digest": "",
        }
    )
    analysis_inserted = Event()
    commit_analysis = Event()

    def write_analysis_then_commit() -> None:
        """保持第一个事务打开，使第二个连接必须穿过同一数据库锁序列化。"""

        payload = analysis.model_dump(mode="json")
        with psycopg.connect(**_database_kwargs()) as conn:
            # 两连接竞态覆盖根锁和终态互斥，而不是重复覆盖 Store 入口拒绝。
            conn.execute("SELECT set_config('phase16.analysis_write','store',true)")
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
                    Jsonb(payload),
                    analysis.created_at,
                ),
            )
            conn.execute(
                """INSERT INTO phase14_workspace_idempotency
                   (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    analysis.live_session_id,
                    analysis.idempotency_key,
                    "conflict_analysis",
                    analysis.analysis_id,
                    Jsonb(payload),
                ),
            )
            analysis_inserted.set()
            assert commit_analysis.wait(timeout=5)
            conn.commit()

    def write_unlinked_degraded() -> str | None:
        """模拟拥有下一版本猜测的直写调用方；安全实现必须仍拒绝矛盾终态。"""

        assert analysis_inserted.wait(timeout=5)
        payload = degraded.model_dump(mode="json")
        try:
            with psycopg.connect(**_database_kwargs()) as conn:
                conn.execute(
                    """INSERT INTO phase16_multi_agent_outcomes
                       (outcome_id,live_session_id,incident_id,evidence_bundle_id,
                        evidence_bundle_digest,escalation_id,escalation_digest,analysis_id,
                        analysis_digest,proposal_id,proposal_digest,status,
                        expected_workspace_version,payload,created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        degraded.outcome_id,
                        degraded.live_session_id,
                        degraded.incident_id,
                        degraded.evidence_bundle_id,
                        degraded.evidence_bundle_digest,
                        degraded.escalation_id,
                        degraded.escalation_digest,
                        None,
                        None,
                        None,
                        None,
                        degraded.status.value,
                        after_escalation.version + 1,
                        Jsonb(payload),
                        degraded.created_at,
                    ),
                )
                conn.execute(
                    """INSERT INTO phase14_workspace_idempotency
                       (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (
                        degraded.live_session_id,
                        degraded.idempotency_key,
                        "multi_agent_outcome",
                        degraded.outcome_id,
                        Jsonb(payload),
                    ),
                )
                conn.commit()
        except psycopg.Error as error:
            return str(error)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis_future = pool.submit(write_analysis_then_commit)
        assert analysis_inserted.wait(timeout=5)
        outcome_future = pool.submit(write_unlinked_degraded)
        commit_analysis.set()
        analysis_future.result(timeout=5)
        error = outcome_future.result(timeout=5)

    assert error is not None
    assert "analysis prevents unlinked degraded outcome" in error
    assert store.list_conflict_analyses(escalation.live_session_id) == (analysis,)
    assert store.list_multi_agent_outcomes(escalation.live_session_id) == ()


class _PostgresScriptedAnalyst:
    """不访问网络的 Analyst 端口，验证协调器向真实 PostgreSQL Store 追加的完整事实链。"""

    def __init__(self) -> None:
        self.calls: list[AgentTask] = []

    def resolve_profile(self, _task: AgentTask):
        """模拟真实受限 Runner 的只读 Registry 查询，返回精确冻结 Analyst Profile。"""

        return build_evidence_analyst_profile()

    async def run(self, task: AgentTask) -> AgentResult:
        """回显 Coordinator 已冻结的触发码和六个引用，模拟 Schema 合法的 FINAL 结果。"""

        self.calls.append(task)
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output={
                "finding_codes": list(task.input_snapshot["trigger_codes"]),
                "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
                "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
                "explanation": "多个冻结冲突信号需要运营确认。",
                "evidence_refs": [
                    item.model_dump(mode="json") for item in task.initial_evidence_refs
                ],
            },
            evidence_refs=task.initial_evidence_refs,
            summary="POSTGRES_SCRIPTED_ANALYST_SUCCEEDED",
        )


class _BlockingPostgresAnalyst(_PostgresScriptedAnalyst):
    """在首个 Analyst 发送期间保持 claim 未闭合，用于验证跨连接并发不会重复 dispatch。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, task: AgentTask) -> AgentResult:
        """首个任务等待测试释放；若第二个协调器越过 claim，调用计数会立即暴露。"""

        self.calls.append(task)
        if len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output={
                "finding_codes": list(task.input_snapshot["trigger_codes"]),
                "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
                "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
                "explanation": "多个冻结冲突信号需要运营确认。",
                "evidence_refs": [
                    item.model_dump(mode="json") for item in task.initial_evidence_refs
                ],
            },
            evidence_refs=task.initial_evidence_refs,
            summary="POSTGRES_BLOCKING_ANALYST_SUCCEEDED",
        )


class _NeverCompletingPostgresAnalyst(_PostgresScriptedAnalyst):
    """模拟外部请求在 claim 窗口内无响应，使 Coordinator 必须走可恢复的超时终态。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """只记录一次发送并持续等待；`wait_for` 取消证明超时来自 Coordinator 而非 Fake。"""

        self.calls.append(task)
        await asyncio.Event().wait()
        raise AssertionError("unreachable after coordinator cancellation")


class _LatePostgresAnalyst(_PostgresScriptedAnalyst):
    """在真实数据库两秒 claim 到期后才返回，暴露 Worker 本地墙钟不能延长租约。"""

    async def run(self, task: AgentTask) -> AgentResult:
        """先记录唯一发送，再故意跨过 Store 的观察窗；正常路径绝不能采纳这个迟到响应。"""

        self.calls.append(task)
        await asyncio.sleep(2.1)
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output={
                "finding_codes": list(task.input_snapshot["trigger_codes"]),
                "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
                "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
                "explanation": "迟到的冲突分析不得越过数据库租约。",
                "evidence_refs": [
                    item.model_dump(mode="json") for item in task.initial_evidence_refs
                ],
            },
            evidence_refs=task.initial_evidence_refs,
            summary="POSTGRES_LATE_ANALYST_SUCCEEDED",
        )


def test_postgres_slow_worker_clock_cannot_extend_database_dispatch_window() -> None:
    """数据库租约到期后，慢 Worker 墙钟不能让迟到 Analyst 结果被写为有效 Analysis。"""

    suffix = uuid4().hex
    store = _store()
    _workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    runner = _LatePostgresAnalyst()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=runner,
            # 业务 freshness 检查使用的节点时钟故意慢一分钟；PostgreSQL Store 仍以
            # 自己的事务时钟创建两秒 claim，Coordinator 不能再用这里的墙钟反算预算。
            clock=lambda: datetime.now(timezone.utc) - timedelta(minutes=1),
        ).run_automatic(bundle, expected_workspace_version=after_bundle.version)
    )

    assert len(runner.calls) == 1
    assert result.analysis is None
    assert result.outcome is not None
    assert result.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert store.list_conflict_analyses(bundle.live_session_id) == ()
    assert len(store.list_multi_agent_outcomes(bundle.live_session_id)) == 1


class _ReviewBeforeTerminalStore:
    """在第一次终态 CAS 前切换到 REVIEW，重放真实 claim 到期后的跨视图竞争窗口。"""

    def __init__(self, delegate: PostgresDecisionSupportStore) -> None:
        self._delegate = delegate
        self._advanced = False

    def __getattr__(self, name: str) -> Any:
        """除精确终态竞态外委托生产 Store，避免测试替身伪造持久化行为。"""

        return getattr(self._delegate, name)

    def append_multi_agent_outcome(self, fact: MultiAgentOutcome, **kwargs: Any) -> Any:
        """第一次写入故意让旧 CAS 失效，Coordinator 必须仅重试审计写而不重发模型。"""

        if not self._advanced:
            self._advanced = True
            workspace = self._delegate.get_workspace(fact.live_session_id)
            lease = self._delegate.acquire_operator_lock(
                fact.live_session_id, "phase16-review-after-claim", 60
            )
            self._delegate.advance_view(
                fact.live_session_id,
                target_view=WorkspaceView.REVIEW,
                expected_version=workspace.version,
                operator_id=lease.operator_id,
                fencing_token=lease.fencing_token,
            )
        return self._delegate.append_multi_agent_outcome(fact, **kwargs)


def test_postgres_coordinator_persists_analysis_and_restart_reuses_it() -> None:
    """真实 PostgreSQL 重启后必须恢复已有分析，不能再次调用同一冻结 Analyst 任务。"""

    suffix = uuid4().hex
    store = _store()
    workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    first_runner = _PostgresScriptedAnalyst()
    first = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=first_runner,
            clock=lambda: datetime.now(timezone.utc),
        ).run_automatic(bundle, expected_workspace_version=after_bundle.version)
    )

    restarted_store = _store()
    restarted_runner = _PostgresScriptedAnalyst()
    recovered = asyncio.run(
        HighConflictEscalationCoordinator(
            store=restarted_store,
            analyst_runner=restarted_runner,
            clock=lambda: datetime.now(timezone.utc),
        ).run_automatic(bundle, expected_workspace_version=after_bundle.version)
    )

    assert first.analysis is not None
    assert recovered.analysis == first.analysis
    assert len(first_runner.calls) == 1
    assert restarted_runner.calls == []
    assert len(restarted_store.list_escalations(workspace.live_session_id)) == 1
    assert len(restarted_store.list_conflict_analyses(workspace.live_session_id)) == 1
    assert restarted_store.list_multi_agent_outcomes(workspace.live_session_id) == ()


def test_postgres_operator_requested_escalation_reconstructs_server_signals() -> None:
    """真实 PostgreSQL 人工升级只接收 lease/Bundle 输入，并持久化服务端重建的 Analysis。"""

    suffix = uuid4().hex
    store = _store()
    workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    lease = store.acquire_operator_lock(workspace.live_session_id, "phase16-manual", 60)
    runner = _PostgresScriptedAnalyst()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=store,
            analyst_runner=runner,
            clock=lambda: datetime.now(timezone.utc),
        ).run_operator_requested(
            bundle,
            expected_workspace_version=after_bundle.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
        )
    )

    assert result.escalation is not None
    assert result.escalation.mode is EscalationMode.OPERATOR_REQUESTED
    assert result.escalation.trigger_codes == (
        ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
        ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED,
    )
    assert result.analysis is not None
    assert result.outcome is None
    assert len(runner.calls) == 1


def test_postgres_expired_claim_can_close_one_degraded_audit_after_review() -> None:
    """已发送 Analyst 超时后，REVIEW 只能允许同 claim 的降级审计闭合，不能丢失终态或重发。"""

    suffix = uuid4().hex
    delegate = _store()
    _workspace, _incident, bundle, after_bundle = _seed_live_bundle(delegate, suffix)
    runner = _NeverCompletingPostgresAnalyst()

    result = asyncio.run(
        HighConflictEscalationCoordinator(
            store=_ReviewBeforeTerminalStore(delegate),
            analyst_runner=runner,
            clock=lambda: datetime.now(timezone.utc),
        ).run_automatic(bundle, expected_workspace_version=after_bundle.version)
    )

    assert result.analysis is None
    assert result.outcome is not None
    assert result.outcome.status is MultiAgentOutcomeStatus.DEGRADED
    assert result.outcome.failure_code is MultiAgentFailureCode.COORDINATOR_TIMEOUT
    assert delegate.get_workspace(bundle.live_session_id).view is WorkspaceView.REVIEW
    assert len(delegate.list_multi_agent_outcomes(bundle.live_session_id)) == 1
    assert len(runner.calls) == 1


def test_postgres_review_rejects_claim_bound_degraded_outcome_with_analysis() -> None:
    """D-147 的跨视图例外只能闭合未产出 Analysis 的超时审计，不能放宽后续路径。"""

    suffix = uuid4().hex
    store = _store()
    workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    store.claim_analyst_dispatch(
        escalation_id=escalation.escalation_id,
        task_digest="f" * 64,
    )
    analysis = _analysis(escalation, bundle, suffix)
    after_analysis = store.append_conflict_analysis(
        analysis, expected_workspace_version=after_escalation.version
    )
    # dispatch claim 的固定两秒观察窗由 PostgreSQL 权威时钟生成；睡眠只用于让真实
    # 租约自然到期，避免测试通过直写或篡改不可变 claim 制造一个生产中不存在的状态。
    time.sleep(2.1)
    lease = store.acquire_operator_lock(workspace.live_session_id, "phase16-review-linked", 60)
    after_review = store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=after_analysis.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )

    # PostgreSQL 的强制生命周期门禁由 trigger 在 INSERT 时直接拒绝；这里断言数据库
    # 的稳定错误，而不是为单个事实表新增一层不属于 Task 5 的异常转换。
    with pytest.raises(psycopg.Error, match="review degraded closure cannot carry analysis"):
        store.append_multi_agent_outcome(
            _outcome(escalation, analysis, suffix),
            expected_workspace_version=after_review.version,
        )


def test_postgres_concurrent_coordinators_share_one_dispatch_claim() -> None:
    """两个 PostgreSQL Coordinator 同时观察同一升级时，只允许一个进入 Analyst Runner。"""

    async def scenario() -> tuple[Any, Any, _BlockingPostgresAnalyst]:
        suffix = uuid4().hex
        first_store = _store()
        _workspace, _incident, bundle, after_bundle = _seed_live_bundle(first_store, suffix)
        runner = _BlockingPostgresAnalyst()
        first_task = asyncio.create_task(
            HighConflictEscalationCoordinator(
                store=first_store,
                analyst_runner=runner,
                clock=lambda: datetime.now(timezone.utc),
            ).run_automatic(bundle, expected_workspace_version=after_bundle.version)
        )
        await runner.entered.wait()
        second = await HighConflictEscalationCoordinator(
            store=_store(),
            analyst_runner=runner,
            # 第二个应用节点故意携带错误的未来墙钟；claim 是否仍活跃必须由
            # PostgreSQL 创建 claim 时的数据库事务时钟判定，不能被本地时钟抢占降级。
            clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=1),
        ).run_automatic(bundle, expected_workspace_version=after_bundle.version)
        runner.release.set()
        return await first_task, second, runner

    first, second, runner = asyncio.run(scenario())

    assert first.analysis is not None
    assert second.analysis is None
    assert second.outcome is None
    assert len(runner.calls) == 1


def test_postgres_dispatch_claim_rejects_direct_future_lease() -> None:
    """直接 SQL 不得写入任意未来 lease 以永久阻塞同一 Escalation 的 Analyst 路径。"""

    suffix = uuid4().hex
    store = _store()
    _workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    store.append_escalation(escalation, expected_workspace_version=after_bundle.version)
    instant = datetime.now(timezone.utc)
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="dispatch claim lease"):
            # 使用生产 Store 所需的同一事务标记，隔离“未来 lease”门禁而非先被
            # 直接 SQL 缺少写入上下文拒绝。
            conn.execute("SELECT set_config('phase16.claim_write','store',true)")
            conn.execute(
                """INSERT INTO phase16_analyst_dispatch_claims
                   (escalation_id,live_session_id,task_digest,created_at,lease_until)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    escalation.escalation_id,
                    escalation.live_session_id,
                    "a" * 64,
                    instant,
                    instant + timedelta(minutes=10),
                ),
            )
        conn.rollback()


def test_postgres_dispatch_claim_rejects_direct_valid_duration_spoof() -> None:
    """即使 digest 形状和两秒窗口都合法，缺少 Store 事务上下文的直写仍必须被拒绝。"""

    suffix = uuid4().hex
    store = _store()
    _workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    store.append_escalation(escalation, expected_workspace_version=after_bundle.version)
    instant = datetime.now(timezone.utc)
    with psycopg.connect(**_database_kwargs()) as conn:
        with pytest.raises(psycopg.Error, match="dispatch claim authorization context"):
            conn.execute(
                """INSERT INTO phase16_analyst_dispatch_claims
                   (escalation_id,live_session_id,task_digest,created_at,lease_until)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    escalation.escalation_id,
                    escalation.live_session_id,
                    "b" * 64,
                    instant,
                    instant + timedelta(seconds=2),
                ),
            )
        conn.rollback()


def test_postgres_dispatch_claim_and_live_review_transition_are_linearized() -> None:
    """PostgreSQL 必须让新 claim 与 LIVE->REVIEW 只有一个成功先后，不能在结束直播后发送 Analyst。"""

    suffix = uuid4().hex
    store = _store()
    workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, suffix)
    escalation = _escalation(bundle, suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    _claim, is_new, is_active = store.claim_analyst_dispatch(
        escalation_id=escalation.escalation_id,
        task_digest="d" * 64,
    )

    assert is_new is True
    assert is_active is True
    lease = store.acquire_operator_lock(workspace.live_session_id, "phase16-review", 60)
    with pytest.raises(WorkspaceConflictError, match="active analyst dispatch"):
        store.advance_view(
            workspace.live_session_id,
            target_view=WorkspaceView.REVIEW,
            expected_version=after_escalation.version,
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
        )

    second_suffix = uuid4().hex
    store = _store()
    workspace, _incident, bundle, after_bundle = _seed_live_bundle(store, second_suffix)
    escalation = _escalation(bundle, second_suffix)
    after_escalation = store.append_escalation(
        escalation, expected_workspace_version=after_bundle.version
    )
    lease = store.acquire_operator_lock(workspace.live_session_id, "phase16-review", 60)
    store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.REVIEW,
        expected_version=after_escalation.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
    )
    with pytest.raises(WorkspaceConflictError, match="dispatch workspace is not LIVE"):
        store.claim_analyst_dispatch(
            escalation_id=escalation.escalation_id,
            task_digest="e" * 64,
        )
