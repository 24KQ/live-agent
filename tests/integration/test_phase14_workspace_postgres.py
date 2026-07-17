"""Phase 14 Workspace Store 的真实 PostgreSQL 重启与并发证据。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from inspect import signature
from types import SimpleNamespace
import time
from uuid import uuid4

import pytest
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import get_settings
from src.decision_support.evidence import AssembledEvidenceBundle
from src.decision_support.models import (
    DecisionKind,
    ExecutionCommand,
    Incident,
    LiveSessionWorkspace,
    OperatorDecision,
    Proposal,
    WorkspaceView,
)
from src.decision_support.store import (
    PostgresDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from tests.phase14_evidence_factory import build_evidence_bundle


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
_TEST_CONNECTION_KWARGS: dict[str, object] | None = None


@pytest.fixture(scope="module", autouse=True)
def _isolated_phase14_schema():
    """在独立 schema 中运行 append-only 集成测试，模块结束后整体回收。"""

    global _TEST_CONNECTION_KWARGS
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase14_test_{uuid4().hex}"
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
    """返回当前模块隔离 schema 的连接参数，禁止回退到公共 search_path。"""

    if _TEST_CONNECTION_KWARGS is None:
        raise RuntimeError("phase14 isolated PostgreSQL schema is not initialized")
    return dict(_TEST_CONNECTION_KWARGS)


def _settings(connection_kwargs: dict[str, object] | None = None):
    """为生产 Store 提供仅覆盖连接参数的最小只读 Settings 投影。"""

    return SimpleNamespace(
        postgres_connection_kwargs=connection_kwargs or _database_kwargs()
    )


def _identity() -> tuple[str, str]:
    suffix = uuid4().hex
    return f"phase14-session-{suffix}", f"phase14-run-{suffix}"


def _workspace(session_id: str, run_key: str) -> LiveSessionWorkspace:
    return LiveSessionWorkspace(
        live_session_id=session_id,
        run_key=run_key,
        room_id=f"room-{session_id}",
        trace_id=f"trace-{session_id}",
        anchor_id="anchor-phase14",
        root_plan_run_id=f"plan-root-{session_id}",
        event_inbox_scope_id=f"event-inbox-{session_id}",
        decision_trace_scope_id=f"decision-trace-{session_id}",
        replay_scope_id=f"replay-{session_id}",
        evaluation_scope_id=f"evaluation-{session_id}",
    )


def _incident(
    session_id: str, suffix: str, *, idempotency_key: str | None = None
) -> Incident:
    return Incident(
        incident_id=f"incident-{suffix}",
        live_session_id=session_id,
        idempotency_key=idempotency_key or f"incident-idem-{suffix}",
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=(f"event-{suffix}",),
        snapshot={"product_id": "p001", "expected_version": 2},
        created_at=NOW,
    )


def _evidence(
    session_id: str,
    incident_id: str,
    suffix: str,
    *,
    idempotency_key: str | None = None,
) -> AssembledEvidenceBundle:
    """生成完整六角色快照，避免集成测试绕过严格 EvidenceBundle 重载。"""

    return build_evidence_bundle(
        live_session_id=session_id,
        incident_id=incident_id,
        suffix=suffix,
        idempotency_key=idempotency_key or f"evidence-idem-{suffix}",
        created_at=NOW,
    )


def _proposal(
    session_id: str,
    incident_id: str,
    evidence_bundle_id: str,
    suffix: str,
    *,
    proposal_version: int = 1,
    proposal_key: str = "sold-out-response",
) -> Proposal:
    return Proposal(
        proposal_id=f"proposal-{suffix}",
        live_session_id=session_id,
        incident_id=incident_id,
        evidence_bundle_id=evidence_bundle_id,
        idempotency_key=f"proposal-idem-{suffix}",
        proposal_key=proposal_key,
        proposal_version=proposal_version,
        profile_id="live_ops_decision_support",
        profile_version="1.0.0",
        snapshot={"options": [{"option_id": "option-001"}]},
        created_at=NOW,
    )


def _decision(
    session_id: str,
    proposal_id: str,
    suffix: str,
    *,
    expected_proposal_version: int = 1,
) -> OperatorDecision:
    return OperatorDecision(
        decision_id=f"decision-{suffix}",
        live_session_id=session_id,
        proposal_id=proposal_id,
        idempotency_key=f"decision-idem-{suffix}",
        expected_proposal_version=expected_proposal_version,
        operator_id="operator-001",
        decision_kind=DecisionKind.APPROVE,
        reason_code="CONFIRMED_SAFE",
        snapshot={"option_id": "option-001"},
        created_at=NOW,
    )


def _command(session_id: str, decision_id: str, suffix: str) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=f"command-{suffix}",
        live_session_id=session_id,
        decision_id=decision_id,
        idempotency_key=f"command-idem-{suffix}",
        command_kind="PLAN_COMMAND",
        snapshot={"command": "resume_with_backup", "product_id": "p002"},
        created_at=NOW,
    )


def _store() -> PostgresDecisionSupportStore:
    store = PostgresDecisionSupportStore(_settings())
    store.initialize_schema()
    return store


def _append_live_evidence(
    store: PostgresDecisionSupportStore,
    incident: Incident,
    evidence: AssembledEvidenceBundle,
) -> LiveSessionWorkspace:
    """用正式 lease/状态机进入 LIVE 后追加受治理播中证据。"""

    store.append_incident(
        incident,
        expected_workspace_version=store.get_workspace(
            incident.live_session_id
        ).version,
    )
    live_workspace = _enter_live(store, incident.live_session_id)
    return store.append_evidence_bundle(
        evidence,
        expected_workspace_version=live_workspace.version,
    )


def _enter_live(
    store: PostgresDecisionSupportStore, live_session_id: str
) -> LiveSessionWorkspace:
    """把已有 PREPARE Workspace 经受控 lease 推进到 LIVE。"""

    current = store.get_workspace(live_session_id)
    transition_lease = store.acquire_operator_lock(
        live_session_id,
        "phase14-live-transition",
        10,
    )
    live_workspace = store.advance_view(
        live_session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=current.version,
        operator_id="phase14-live-transition",
        fencing_token=transition_lease.fencing_token,
    )
    store.release_operator_lock(
        live_session_id,
        operator_id="phase14-live-transition",
        fencing_token=transition_lease.fencing_token,
    )
    return live_workspace


def _expire_operator_lock(live_session_id: str) -> None:
    """测试夹具只在数据库侧推进租约，生产 API 始终使用数据库时钟。"""

    with psycopg.connect(**_database_kwargs()) as conn:
        conn.execute(
            """UPDATE phase14_live_session_workspaces
               SET lock_lease_until=NOW()-INTERVAL '1 second'
               WHERE live_session_id=%s""",
            (live_session_id,),
        )
        conn.commit()


def test_postgres_lease_api_does_not_accept_caller_clock() -> None:
    """生产租约只能使用数据库时钟，公共入口不得接受可伪造的 now。"""

    methods = (
        PostgresDecisionSupportStore.acquire_operator_lock,
        PostgresDecisionSupportStore.renew_operator_lock,
        PostgresDecisionSupportStore.release_operator_lock,
        PostgresDecisionSupportStore.advance_view,
        PostgresDecisionSupportStore.append_operator_decision,
        PostgresDecisionSupportStore.append_execution_command,
    )

    assert all("now" not in signature(method).parameters for method in methods)


def test_postgres_rejects_boolean_versions_and_fencing_tokens() -> None:
    """PostgreSQL 入口与内存入口一致拒绝被 Python 当作 1 的布尔控制字段。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    with pytest.raises(ValueError, match="expected_version"):
        store.append_incident(
            _incident(session_id, suffix), expected_workspace_version=True
        )

    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    store.acquire_operator_lock(session_id, "operator-001", 30)
    with pytest.raises(ValueError, match="fencing_token"):
        store.append_operator_decision(
            _decision(session_id, proposal.proposal_id, suffix),
            expected_workspace_version=proposal_workspace.version,
            operator_id="operator-001",
            fencing_token=True,
        )


def test_postgres_workspace_survives_restart_and_replays_fact() -> None:
    session_id, run_key = _identity()
    first = _store()
    first.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, uuid4().hex)
    version_two = first.append_incident(incident, expected_workspace_version=1)

    restarted = _store()
    replay = restarted.append_incident(incident, expected_workspace_version=999)

    assert version_two.version == 2
    assert replay.version == 2
    assert restarted.get_workspace(session_id).version == 2
    assert restarted.get_incident(incident.incident_id) == incident


def test_postgres_workspace_identity_conflict_is_normalized() -> None:
    """数据库唯一约束不能作为调用方可见错误泄漏出 Store 边界。"""

    session_id, run_key = _identity()
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))

    with pytest.raises(WorkspaceConflictError, match="identity"):
        store.create_workspace(_workspace(session_id, f"{run_key}-conflict"))


def test_postgres_workspace_cas_allows_one_concurrent_fact_append() -> None:
    session_id, run_key = _identity()
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incidents = [_incident(session_id, uuid4().hex) for _ in range(2)]

    def append(incident: Incident):
        local = PostgresDecisionSupportStore(_settings())
        try:
            return local.append_incident(incident, expected_workspace_version=1)
        except WorkspaceConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, incidents))

    assert sum(isinstance(item, LiveSessionWorkspace) for item in results) == 1
    assert sum(isinstance(item, WorkspaceConflictError) for item in results) == 1
    assert store.get_workspace(session_id).version == 2


def test_postgres_operator_lock_fences_old_owner_after_expiry() -> None:
    session_id, run_key = _identity()
    first_store = _store()
    second_store = PostgresDecisionSupportStore(_settings())
    first_store.create_workspace(_workspace(session_id, run_key))
    first = first_store.acquire_operator_lock(session_id, "operator-001", 10)

    with pytest.raises(WorkspaceLeaseError, match="locked"):
        second_store.acquire_operator_lock(session_id, "operator-002", 10)
    _expire_operator_lock(session_id)
    second = second_store.acquire_operator_lock(session_id, "operator-002", 10)

    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(WorkspaceLeaseError, match="fencing"):
        first_store.advance_view(
            session_id,
            target_view=WorkspaceView.LIVE,
            expected_version=1,
            operator_id="operator-001",
            fencing_token=first.fencing_token,
        )


def test_postgres_empty_operator_is_rejected_before_lock_mutation() -> None:
    """无效 operator_id 不能留下幽灵锁或消耗 fencing token。"""

    session_id, run_key = _identity()
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))

    with pytest.raises(ValueError, match="operator_id"):
        store.acquire_operator_lock(session_id, "", 10)

    with psycopg.connect(
        **_database_kwargs(), row_factory=dict_row
    ) as conn:
        row = conn.execute(
            """SELECT lock_operator_id,lock_lease_until,fencing_token
               FROM phase14_live_session_workspaces WHERE live_session_id=%s""",
            (session_id,),
        ).fetchone()
    assert row == {
        "lock_operator_id": None,
        "lock_lease_until": None,
        "fencing_token": 0,
    }


def test_postgres_persists_complete_fact_chain_and_restarts() -> None:
    """五类事实必须在重启后保持完整身份、版本和不可变 payload。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id,
        incident.incident_id,
        evidence.evidence_bundle_id,
        suffix,
    )
    decision = _decision(session_id, proposal.proposal_id, suffix)
    command = _command(session_id, decision.decision_id, suffix)

    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    lease = store.acquire_operator_lock(session_id, "operator-001", 30)
    store.append_operator_decision(
        decision,
        expected_workspace_version=proposal_workspace.version,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
    )
    final_workspace = store.append_execution_command(
        command,
        expected_workspace_version=proposal_workspace.version + 1,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
    )

    restarted = _store()
    assert final_workspace.version == proposal_workspace.version + 2
    assert restarted.get_evidence_bundle(evidence.evidence_bundle_id) == evidence.bundle
    assert restarted.get_proposal(proposal.proposal_id) == proposal
    assert restarted.get_operator_decision(decision.decision_id) == decision
    assert restarted.get_execution_command(command.command_id) == command
    assert restarted.list_evidence_bundles(session_id) == (evidence.bundle,)
    assert restarted.list_proposals(session_id) == (proposal,)
    assert restarted.list_operator_decisions(session_id) == (decision,)
    assert restarted.list_execution_commands(session_id) == (command,)


def test_postgres_rejects_bundle_when_authoritative_incident_fact_differs() -> None:
    """数据库 Store 也必须校验 Incident 业务摘要，不能只依赖外键 ID。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    mismatched_incident = Incident(
            incident_id=f"incident-{suffix}",
            live_session_id=session_id,
            idempotency_key=f"incident-idem-{suffix}",
            incident_type="SOLD_OUT_COMPOSITE",
            source_ref_ids=(f"event-{suffix}",),
            snapshot={"product_id": "p999", "expected_version": 99},
            created_at=NOW,
    )

    with pytest.raises(WorkspaceConflictError, match="incident binding"):
        _append_live_evidence(
            store,
            mismatched_incident,
            _evidence(session_id, f"incident-{suffix}", suffix),
        )


def test_postgres_rejects_receipt_not_issued_by_governed_assembler() -> None:
    """PostgreSQL 写入口也不能把伪造的精确 wrapper 类型当成授权 receipt。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    incident = _incident(session_id, suffix)
    store.create_workspace(_workspace(session_id, run_key))
    store.append_incident(incident, expected_workspace_version=1)
    live_workspace = _enter_live(store, session_id)
    # 模拟同进程调用方规避公开构造器后伪造对象内存布局；数据库 Store 必须
    # 在访问父事实和开启事实写入前拒绝未登记的 receipt。
    forged = object.__new__(AssembledEvidenceBundle)
    object.__setattr__(forged, "_bundle", _evidence(session_id, incident.incident_id, suffix).bundle)

    with pytest.raises(WorkspaceConflictError, match="governed assembly"):
        store.append_evidence_bundle(
            forged,
            expected_workspace_version=live_workspace.version,
        )


def test_postgres_rejects_issued_receipt_after_bundle_rebinding() -> None:
    """数据库 Store 必须拒绝合法 receipt 被底层反射重绑定后的写入。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    incident = _incident(session_id, suffix)
    store.create_workspace(_workspace(session_id, run_key))
    store.append_incident(incident, expected_workspace_version=1)
    live_workspace = _enter_live(store, session_id)
    issued = _evidence(session_id, incident.incident_id, suffix)
    replacement = _evidence(
        session_id,
        incident.incident_id,
        f"{suffix}-replacement",
    ).bundle
    # 签发登记绑定的是原始不可变 Bundle；替换 wrapper 私有字段不能改变
    # PostgreSQL 事务真正看到的受治理事实，也不能获得另一张写入授权。
    object.__setattr__(issued, "_bundle", replacement)

    with pytest.raises(WorkspaceConflictError, match="governed assembly"):
        store.append_evidence_bundle(
            issued,
            expected_workspace_version=live_workspace.version,
        )


def test_postgres_rejects_cross_scope_and_workspace_wide_idempotency_conflict() -> None:
    """同一 Workspace 的五类事实共享幂等命名空间，不同会话相互隔离。"""

    session_a, run_a = _identity()
    session_b, run_b = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_a, run_a))
    store.create_workspace(_workspace(session_b, run_b))
    incident = _incident(session_a, suffix)
    store.append_incident(incident, expected_workspace_version=1)
    store.append_incident(
        _incident(
            session_b,
            f"{suffix}-other-workspace",
            idempotency_key=incident.idempotency_key,
        ),
        expected_workspace_version=1,
    )
    _enter_live(store, session_a)
    _enter_live(store, session_b)

    with pytest.raises(WorkspaceConflictError, match="scope"):
        store.append_evidence_bundle(
            _evidence(session_b, incident.incident_id, suffix),
            expected_workspace_version=3,
        )
    with pytest.raises(WorkspaceConflictError, match="idempotency"):
        store.append_evidence_bundle(
            _evidence(
                session_a,
                incident.incident_id,
                f"{suffix}-idem",
                idempotency_key=incident.idempotency_key,
            ),
            expected_workspace_version=3,
        )

    assert store.get_workspace(session_a).version == 3
    assert store.get_workspace(session_b).version == 3


def test_postgres_constraints_reject_cross_scope_and_fact_mutation() -> None:
    """绕过 Store 的原始 SQL 仍不能跨作用域关联或覆盖历史事实。"""

    session_a, run_a = _identity()
    session_b, run_b = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_a, run_a))
    store.create_workspace(_workspace(session_b, run_b))
    incident = _incident(session_a, suffix)
    store.append_incident(incident, expected_workspace_version=1)
    invalid_evidence = _evidence(session_b, incident.incident_id, suffix)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_evidence_bundles
                   (evidence_bundle_id,live_session_id,incident_id,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    invalid_evidence.evidence_bundle_id,
                    session_b,
                    incident.incident_id,
                    Jsonb(invalid_evidence.model_dump(mode="json")),
                    invalid_evidence.created_at,
                ),
            )
            conn.commit()

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """UPDATE phase14_incidents SET payload='{}'::jsonb
                   WHERE incident_id=%s""",
                (incident.incident_id,),
            )
            conn.commit()


def test_postgres_constraints_reject_payload_and_ledger_identity_mismatch() -> None:
    """关系身份、JSON payload 与幂等账本必须描述同一份审计事实。"""

    session_a, run_a = _identity()
    session_b, _ = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_a, run_a))
    incident = _incident(session_a, suffix)
    store.append_incident(incident, expected_workspace_version=1)
    forged = _incident(session_b, f"{suffix}-forged")

    with pytest.raises(psycopg.errors.RaiseException, match="payload"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_incidents
                   (incident_id,live_session_id,payload,created_at)
                   VALUES (%s,%s,%s,%s)""",
                (
                    forged.incident_id,
                    session_a,
                    Jsonb(forged.model_dump(mode="json")),
                    forged.created_at,
                ),
            )
            conn.commit()

    forged_payload = incident.model_dump(mode="json")
    forged_payload["snapshot"] = {"product_id": "forged"}
    with pytest.raises(psycopg.errors.RaiseException, match="idempotency ledger"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_workspace_idempotency
                   (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                   VALUES (%s,%s,%s,%s,%s)""",
                (
                    session_a,
                    f"forged-ledger-{suffix}",
                    "incident",
                    incident.incident_id,
                    Jsonb(forged_payload),
                ),
            )
            conn.commit()


def test_postgres_constraints_require_ledger_for_every_fact() -> None:
    """绕过 Store 单写事实时，deferred 约束必须在提交前发现账本缺失。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)

    with pytest.raises(psycopg.errors.RaiseException, match="missing idempotency ledger"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_incidents
                   (incident_id,live_session_id,payload,created_at)
                   VALUES (%s,%s,%s,%s)""",
                (
                    incident.incident_id,
                    session_id,
                    Jsonb(incident.model_dump(mode="json")),
                    incident.created_at,
                ),
            )
            conn.commit()


def test_postgres_constraints_reject_operator_decision_without_live_lease() -> None:
    """原始 SQL 也不能绕过操作员身份、fencing 和未过期 lease。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    decision = _decision(session_id, proposal.proposal_id, suffix)
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    store.append_proposal(proposal, expected_workspace_version=evidence_workspace.version)

    with pytest.raises(psycopg.errors.RaiseException, match="operator lease"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_operator_decisions
                   (decision_id,live_session_id,proposal_id,operator_id,
                    fencing_token,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    decision.decision_id,
                    session_id,
                    proposal.proposal_id,
                    decision.operator_id,
                    1,
                    Jsonb(decision.model_dump(mode="json")),
                    decision.created_at,
                ),
            )
            conn.commit()


def test_postgres_constraints_reject_decision_for_superseded_proposal() -> None:
    """持有有效 lease 的原始 SQL 也不能批准同 lineage 中已经陈旧的 Proposal。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    first = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    second = _proposal(
        session_id,
        incident.incident_id,
        evidence.evidence_bundle_id,
        f"{suffix}-v2",
        proposal_version=2,
    )
    stale_decision = _decision(session_id, first.proposal_id, suffix)
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    first_workspace = store.append_proposal(
        first, expected_workspace_version=evidence_workspace.version
    )
    store.append_proposal(second, expected_workspace_version=first_workspace.version)
    lease = store.acquire_operator_lock(session_id, "operator-001", 30)

    with pytest.raises(psycopg.errors.RaiseException, match="latest proposal"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_operator_decisions
                   (decision_id,live_session_id,proposal_id,operator_id,
                    fencing_token,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    stale_decision.decision_id,
                    session_id,
                    first.proposal_id,
                    stale_decision.operator_id,
                    lease.fencing_token,
                    Jsonb(stale_decision.model_dump(mode="json")),
                    stale_decision.created_at,
                ),
            )
            conn.commit()


def test_postgres_constraints_bind_command_to_decision_fencing_epoch() -> None:
    """合法新 lease 也不能为旧 fencing 下的人工决定补造首次命令。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    decision = _decision(session_id, proposal.proposal_id, suffix)
    command = _command(session_id, decision.decision_id, suffix)
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    first = store.acquire_operator_lock(session_id, "operator-001", 10)
    store.append_operator_decision(
        decision,
        expected_workspace_version=proposal_workspace.version,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
    )
    _expire_operator_lock(session_id)
    second = store.acquire_operator_lock(session_id, "operator-001", 10)

    with pytest.raises(psycopg.errors.RaiseException, match="decision fencing"):
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                """INSERT INTO phase14_execution_commands
                   (command_id,live_session_id,decision_id,operator_id,
                    fencing_token,payload,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    command.command_id,
                    session_id,
                    decision.decision_id,
                    "operator-001",
                    second.fencing_token,
                    Jsonb(command.model_dump(mode="json")),
                    command.created_at,
                ),
            )
            conn.commit()

def test_postgres_decision_and_command_fail_closed_on_version_and_fencing() -> None:
    """Proposal 版本与操作员 fencing 必须在同一根行锁事务内校验。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id,
        incident.incident_id,
        evidence.evidence_bundle_id,
        suffix,
    )
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    first = store.acquire_operator_lock(session_id, "operator-001", 10)

    with pytest.raises(WorkspaceConflictError, match="proposal version"):
        store.append_operator_decision(
            _decision(
                session_id,
                proposal.proposal_id,
                suffix,
                expected_proposal_version=2,
            ),
            expected_workspace_version=proposal_workspace.version,
            operator_id="operator-001",
            fencing_token=first.fencing_token,
        )

    _expire_operator_lock(session_id)
    second = store.acquire_operator_lock(session_id, "operator-002", 10)
    with pytest.raises(WorkspaceLeaseError, match="fencing"):
        store.append_operator_decision(
            _decision(session_id, proposal.proposal_id, f"{suffix}-old"),
            expected_workspace_version=proposal_workspace.version,
            operator_id="operator-001",
            fencing_token=first.fencing_token,
        )
    assert second.fencing_token == first.fencing_token + 1


def test_postgres_rejects_superseded_proposal_lineage_version() -> None:
    """同一 proposal_key 的最新版本是人工决定唯一可引用的版本。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    first = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    second = _proposal(
        session_id,
        incident.incident_id,
        evidence.evidence_bundle_id,
        f"{suffix}-v2",
        proposal_version=2,
    )
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    first_workspace = store.append_proposal(
        first, expected_workspace_version=evidence_workspace.version
    )
    second_workspace = store.append_proposal(
        second, expected_workspace_version=first_workspace.version
    )
    lease = store.acquire_operator_lock(session_id, "operator-001", 30)

    with pytest.raises(WorkspaceConflictError, match="latest proposal"):
        store.append_operator_decision(
            _decision(session_id, first.proposal_id, suffix),
            expected_workspace_version=second_workspace.version,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
        )


def test_postgres_concurrent_operator_decisions_have_one_cas_winner() -> None:
    """相同 Proposal 的并发人工决定只能有一个推进 Workspace 版本。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id,
        incident.incident_id,
        evidence.evidence_bundle_id,
        suffix,
    )
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    lease = store.acquire_operator_lock(session_id, "operator-001", 30)
    decisions = [
        _decision(session_id, proposal.proposal_id, f"{suffix}-{index}")
        for index in range(2)
    ]

    def append(decision: OperatorDecision):
        local = PostgresDecisionSupportStore(_settings())
        try:
            return local.append_operator_decision(
                decision,
                expected_workspace_version=proposal_workspace.version,
                operator_id="operator-001",
                fencing_token=lease.fencing_token,
            )
        except WorkspaceConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, decisions))

    assert sum(isinstance(item, LiveSessionWorkspace) for item in results) == 1
    assert sum(isinstance(item, WorkspaceConflictError) for item in results) == 1
    assert store.get_workspace(session_id).version == proposal_workspace.version + 1


def test_postgres_proposal_accepts_only_one_operator_decision() -> None:
    """CAS 成功后即使读取新版本，同一 Proposal 也不能形成第二个决定。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    lease = store.acquire_operator_lock(session_id, "operator-001", 30)
    store.append_operator_decision(
        _decision(session_id, proposal.proposal_id, suffix),
        expected_workspace_version=proposal_workspace.version,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
    )

    with pytest.raises(WorkspaceConflictError, match="already has a decision"):
        store.append_operator_decision(
            _decision(session_id, proposal.proposal_id, f"{suffix}-second"),
            expected_workspace_version=proposal_workspace.version + 1,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
        )


def test_postgres_lock_wait_cannot_extend_expired_operator_lease() -> None:
    """请求等待根行锁期间 lease 到期后，必须按取得锁后的数据库墙钟拒绝写入。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    decision = _decision(session_id, proposal.proposal_id, suffix)
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    lease = store.acquire_operator_lock(session_id, "operator-001", 3)
    application_name = f"phase14-lock-wait-{suffix}"
    waiter_kwargs = {
        **_database_kwargs(),
        "application_name": application_name,
    }
    waiter_store = PostgresDecisionSupportStore(_settings(waiter_kwargs))

    with ThreadPoolExecutor(max_workers=1) as pool:
        wait_observed = False
        with psycopg.connect(**_database_kwargs()) as blocker:
            blocker.execute(
                """SELECT 1 FROM phase14_live_session_workspaces
                   WHERE live_session_id=%s FOR UPDATE""",
                (session_id,),
            )
            future = pool.submit(
                waiter_store.append_operator_decision,
                decision,
                expected_workspace_version=proposal_workspace.version,
                operator_id="operator-001",
                fencing_token=lease.fencing_token,
            )
            # 先从 PostgreSQL 活动视图确认待测事务确实在等根行锁，再让 lease
            # 过期。这样未来若误改回 transaction_timestamp()，测试必然能复现。
            deadline = time.monotonic() + 2
            with psycopg.connect(**_database_kwargs(), autocommit=True) as observer:
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        """SELECT 1 FROM pg_stat_activity
                           WHERE application_name=%s AND wait_event_type='Lock'""",
                        (application_name,),
                    ).fetchone()
                    if waiting is not None:
                        wait_observed = True
                        break
                    time.sleep(0.02)
            if wait_observed:
                remaining = (
                    lease.lease_until - datetime.now(timezone.utc)
                ).total_seconds()
                time.sleep(max(remaining + 0.2, 0.2))
            blocker.commit()

        # 持锁连接已经退出，任何断言失败都不会让线程池等待一个永远阻塞的 worker。
        assert wait_observed, "operator decision transaction did not enter lock wait"
        with pytest.raises(WorkspaceLeaseError, match="expired"):
            future.result(timeout=5)


def test_postgres_lock_renew_release_and_fact_list_survive_restart() -> None:
    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    store.append_incident(incident, expected_workspace_version=1)
    lease = store.acquire_operator_lock(session_id, "operator-001", 10)

    renewed = store.renew_operator_lock(
        session_id,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        lease_seconds=30,
    )
    store.release_operator_lock(
        session_id,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
    )
    restarted = _store()
    next_lease = restarted.acquire_operator_lock(session_id, "operator-002", 10)

    assert renewed.lease_until > lease.lease_until
    assert next_lease.fencing_token == lease.fencing_token + 1
    assert restarted.list_incidents(session_id) == (incident,)


def test_postgres_decision_and_command_replay_survive_lease_handover() -> None:
    """数据库重试先读取幂等账本，换主后也不创建第二份人工事实。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    evidence = _evidence(session_id, incident.incident_id, suffix)
    proposal = _proposal(
        session_id, incident.incident_id, evidence.evidence_bundle_id, suffix
    )
    decision = _decision(session_id, proposal.proposal_id, suffix)
    command = _command(session_id, decision.decision_id, suffix)
    evidence_workspace = _append_live_evidence(store, incident, evidence)
    proposal_workspace = store.append_proposal(
        proposal, expected_workspace_version=evidence_workspace.version
    )
    first = store.acquire_operator_lock(session_id, "operator-001", 10)
    store.append_operator_decision(
        decision,
        expected_workspace_version=proposal_workspace.version,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
    )
    store.append_execution_command(
        command,
        expected_workspace_version=proposal_workspace.version + 1,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
    )
    _expire_operator_lock(session_id)
    second = store.acquire_operator_lock(session_id, "operator-002", 10)

    replayed_decision = store.append_operator_decision(
        decision,
        expected_workspace_version=999,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
    )
    replayed_command = store.append_execution_command(
        command,
        expected_workspace_version=999,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
    )

    assert second.fencing_token == first.fencing_token + 1
    assert replayed_decision.version == 7
    assert replayed_command.version == 7
    assert store.list_operator_decisions(session_id) == (decision,)
    assert store.list_execution_commands(session_id) == (command,)


def test_postgres_fact_ledger_and_workspace_version_rollback_together() -> None:
    """账本写入失败时，前置事实 INSERT 和 Workspace 版本推进必须全部回滚。"""

    session_id, run_key = _identity()
    suffix = uuid4().hex
    store = _store()
    store.create_workspace(_workspace(session_id, run_key))
    incident = _incident(session_id, suffix)
    trigger_name = f"trg_phase14_test_rollback_{suffix}"
    function_name = f"phase14_test_rollback_{suffix}"

    # 故障注入只匹配本测试的随机幂等键，并且发生在事实表 INSERT 之后。
    # PostgreSQL 的事务边界应保证触发器异常撤销此前的事实写入，账本与根版本也不变。
    try:
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                sql.SQL(
                    """CREATE FUNCTION {}() RETURNS trigger AS $$
                       BEGIN
                           IF NEW.idempotency_key = {} THEN
                               RAISE EXCEPTION 'phase14 atomicity fault injection';
                           END IF;
                           RETURN NEW;
                       END;
                       $$ LANGUAGE plpgsql"""
                ).format(
                    sql.Identifier(function_name),
                    sql.Literal(incident.idempotency_key),
                )
            )
            conn.execute(
                sql.SQL(
                    """CREATE TRIGGER {}
                       BEFORE INSERT ON phase14_workspace_idempotency
                       FOR EACH ROW EXECUTE FUNCTION {}()"""
                ).format(
                    sql.Identifier(trigger_name),
                    sql.Identifier(function_name),
                )
            )
            conn.commit()

        with pytest.raises(psycopg.errors.RaiseException, match="atomicity fault"):
            store.append_incident(incident, expected_workspace_version=1)

        with psycopg.connect(
            **_database_kwargs(), row_factory=dict_row
        ) as conn:
            fact_count = conn.execute(
                "SELECT COUNT(*) AS count FROM phase14_incidents WHERE incident_id=%s",
                (incident.incident_id,),
            ).fetchone()["count"]
            ledger_count = conn.execute(
                """SELECT COUNT(*) AS count FROM phase14_workspace_idempotency
                   WHERE live_session_id=%s AND idempotency_key=%s""",
                (session_id, incident.idempotency_key),
            ).fetchone()["count"]
            workspace_version = conn.execute(
                """SELECT version FROM phase14_live_session_workspaces
                   WHERE live_session_id=%s""",
                (session_id,),
            ).fetchone()["version"]

        assert fact_count == 0
        assert ledger_count == 0
        assert workspace_version == 1
    finally:
        with psycopg.connect(**_database_kwargs()) as conn:
            conn.execute(
                sql.SQL(
                    "DROP TRIGGER IF EXISTS {} ON phase14_workspace_idempotency"
                ).format(sql.Identifier(trigger_name))
            )
            conn.execute(
                sql.SQL("DROP FUNCTION IF EXISTS {}() CASCADE").format(
                    sql.Identifier(function_name)
                )
            )
            conn.commit()
