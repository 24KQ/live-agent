"""Phase 14 Task 2 统一 Workspace 与不可变事实 Store 的单元契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from src.decision_support.evidence import AssembledEvidenceBundle
from src.decision_support.models import (
    DecisionKind,
    EvidenceBundle,
    ExecutionCommand,
    Incident,
    LiveSessionWorkspace,
    OperatorDecision,
    OperatorLease,
    Proposal,
    WorkspaceView,
)
from src.decision_support.store import (
    InMemoryDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)
from tests.phase14_evidence_factory import build_evidence_bundle


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def _workspace(**updates) -> LiveSessionWorkspace:
    values = {
        "live_session_id": "live-session-p001-sold-out-v1",
        "run_key": "phase14-workspace-run-001",
        "room_id": "room-phase14",
        "trace_id": "trace-phase14",
        "anchor_id": "anchor-phase14",
        "root_plan_run_id": "plan-root-phase14",
        "event_inbox_scope_id": "event-inbox-room-phase14",
        "decision_trace_scope_id": "decision-trace-live-session-phase14",
        "replay_scope_id": "replay-live-session-phase14",
        "evaluation_scope_id": "evaluation-live-session-phase14",
        "view": WorkspaceView.PREPARE,
        "version": 1,
    }
    values.update(updates)
    return LiveSessionWorkspace(**values)


def _incident(**updates) -> Incident:
    values = {
        "incident_id": "incident-sold-out-001",
        "live_session_id": "live-session-p001-sold-out-v1",
        "idempotency_key": "incident-idem-001",
        "incident_type": "SOLD_OUT_COMPOSITE",
        "source_ref_ids": ("event-001",),
        "snapshot": {"product_id": "p001", "expected_version": 2},
        "created_at": NOW,
    }
    values.update(updates)
    return Incident(**values)


def _evidence(**updates) -> AssembledEvidenceBundle:
    """使用完整六角色事实，确保 Store 重载也经过 Task 3 的严格校验。"""

    fact = build_evidence_bundle(
        live_session_id="live-session-p001-sold-out-v1",
        incident_id=updates.pop("incident_id", "incident-sold-out-001"),
        suffix="001",
        idempotency_key=updates.pop("idempotency_key", "evidence-idem-001"),
        evidence_bundle_id=updates.pop("evidence_bundle_id", "evidence-bundle-001"),
        room_id="room-phase14",
        trace_id="trace-phase14",
        root_plan_run_id="plan-root-phase14",
        created_at=NOW,
    )
    if updates:
        raise AssertionError(f"unsupported assembled evidence overrides: {updates}")
    return fact


def _proposal(**updates) -> Proposal:
    values = {
        "proposal_id": "proposal-001",
        "live_session_id": "live-session-p001-sold-out-v1",
        "incident_id": "incident-sold-out-001",
        "evidence_bundle_id": "evidence-bundle-001",
        "idempotency_key": "proposal-idem-001",
        "proposal_key": "sold-out-response",
        "proposal_version": 1,
        "profile_id": "live_ops_decision_support",
        "profile_version": "1.0.0",
        "snapshot": {"options": [{"option_id": "option-001"}]},
        "created_at": NOW,
    }
    values.update(updates)
    return Proposal(**values)


def _decision(**updates) -> OperatorDecision:
    values = {
        "decision_id": "decision-001",
        "live_session_id": "live-session-p001-sold-out-v1",
        "proposal_id": "proposal-001",
        "idempotency_key": "decision-idem-001",
        "expected_proposal_version": 1,
        "operator_id": "operator-001",
        "decision_kind": DecisionKind.APPROVE,
        "reason_code": "CONFIRMED_SAFE",
        "snapshot": {"option_id": "option-001"},
        "created_at": NOW,
    }
    values.update(updates)
    return OperatorDecision(**values)


def _command(**updates) -> ExecutionCommand:
    values = {
        "command_id": "command-001",
        "live_session_id": "live-session-p001-sold-out-v1",
        "decision_id": "decision-001",
        "idempotency_key": "command-idem-001",
        "command_kind": "PLAN_COMMAND",
        "snapshot": {"command": "resume_with_backup", "product_id": "p002"},
        "created_at": NOW,
    }
    values.update(updates)
    return ExecutionCommand(**values)


def _create_live_workspace(store: InMemoryDecisionSupportStore) -> LiveSessionWorkspace:
    """按正式状态机进入 LIVE，避免测试在 PREPARE 阶段伪造播中证据。"""

    workspace = store.create_workspace(_workspace())
    lease = store.acquire_operator_lock(
        workspace.live_session_id,
        "operator-001",
        1,
        now=NOW,
    )
    return store.advance_view(
        workspace.live_session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=workspace.version,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        now=NOW,
    )


def _seed_proposal(store: InMemoryDecisionSupportStore) -> int:
    """按外键顺序写入系统事实，返回 Proposal 后的 Workspace 版本。"""

    _create_live_workspace(store)
    store.append_incident(_incident(), expected_workspace_version=2)
    store.append_evidence_bundle(_evidence(), expected_workspace_version=3)
    return store.append_proposal(_proposal(), expected_workspace_version=4).version


def test_models_are_strict_and_deeply_immutable() -> None:
    """冻结模型不得通过额外字段或嵌套 JSON 引用修改历史事实。"""

    source = {"product": {"product_id": "p001"}, "items": [1, 2]}
    incident = _incident(snapshot=source)
    source["product"]["product_id"] = "forged"
    source["items"].append(3)

    assert incident.snapshot["product"]["product_id"] == "p001"
    assert incident.snapshot["items"] == (1, 2)
    with pytest.raises(TypeError):
        incident.snapshot["product_id"] = "p999"
    with pytest.raises(ValidationError):
        _workspace(unknown_field="forbidden")


def test_versions_and_fencing_fit_postgres_bigint() -> None:
    """内存模型不得接受 PostgreSQL 关系列无法持久化的超大整数。"""

    too_large = 9_223_372_036_854_775_808

    with pytest.raises(ValidationError):
        _workspace(version=too_large)
    with pytest.raises(ValidationError):
        _proposal(proposal_version=too_large)
    with pytest.raises(ValidationError):
        _decision(expected_proposal_version=too_large)
    with pytest.raises(ValidationError):
        OperatorLease(
            live_session_id="live-session-p001-sold-out-v1",
            operator_id="operator-001",
            fencing_token=too_large,
            lease_until=NOW,
        )


def test_store_rejects_boolean_versions_and_fencing_tokens() -> None:
    """Python 的 bool 虽是 int 子类，也不能作为 CAS 版本或 fencing token。"""

    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    with pytest.raises(ValueError, match="expected_version"):
        store.append_incident(_incident(), expected_workspace_version=True)
    store.append_incident(_incident(), expected_workspace_version=1)
    with pytest.raises(ValueError, match="expected_version"):
        store.append_incident(_incident(), expected_workspace_version=True)

    seeded = InMemoryDecisionSupportStore()
    _seed_proposal(seeded)
    seeded.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 30, now=NOW
    )
    with pytest.raises(ValueError, match="fencing_token"):
        seeded.append_operator_decision(
            _decision(),
            expected_workspace_version=4,
            operator_id="operator-001",
            fencing_token=True,
            now=NOW,
        )


def test_fact_timestamps_are_normalized_to_utc() -> None:
    """等价时刻必须形成字节稳定的 UTC payload，保持两种 Store 重放一致。"""

    china_time = datetime(2026, 7, 17, 16, 0, tzinfo=timezone(timedelta(hours=8)))
    normalized = _incident(created_at=china_time).created_at

    assert normalized == NOW
    assert normalized.tzinfo is timezone.utc


def test_models_reject_nul_in_identity_and_nested_json() -> None:
    """内存与 PostgreSQL 必须在公共边界一致拒绝 NUL。"""

    with pytest.raises(ValidationError, match="NUL"):
        _workspace(room_id="room\x00forged")
    with pytest.raises(ValidationError, match="NUL"):
        _incident(snapshot={"product": {"name": "bad\x00value"}})
    with pytest.raises(ValidationError, match="NUL"):
        _incident(snapshot={"bad\x00key": "value"})


def test_workspace_create_is_idempotent_but_rejects_conflicting_run_key() -> None:
    store = InMemoryDecisionSupportStore()
    first = store.create_workspace(_workspace())
    replay = store.create_workspace(_workspace())

    assert replay == first
    with pytest.raises(WorkspaceConflictError, match="run_key"):
        store.create_workspace(_workspace(room_id="other-room"))


def test_workspace_requires_all_cross_system_scope_identities() -> None:
    """Plan/Event/DecisionTrace/Replay/Evaluation 关联必须随根事实持久化。"""

    payload = _workspace().model_dump(mode="python")
    payload.pop("evaluation_scope_id")

    with pytest.raises(ValidationError):
        LiveSessionWorkspace.model_validate(payload)


def test_workspace_views_advance_in_order_under_version_and_fencing() -> None:
    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    lease = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1",
        operator_id="operator-001",
        lease_seconds=30,
        now=NOW,
    )

    live = store.advance_view(
        "live-session-p001-sold-out-v1",
        target_view=WorkspaceView.LIVE,
        expected_version=1,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        now=NOW,
    )

    assert live.view is WorkspaceView.LIVE
    assert live.version == 2
    with pytest.raises(WorkspaceConflictError, match="version"):
        store.advance_view(
            live.live_session_id,
            target_view=WorkspaceView.REVIEW,
            expected_version=1,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
            now=NOW,
        )
    with pytest.raises(WorkspaceConflictError, match="transition"):
        store.advance_view(
            live.live_session_id,
            target_view=WorkspaceView.PREPARE,
            expected_version=2,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
            now=NOW,
        )


def test_append_only_chain_enforces_scope_foreign_keys_and_versions() -> None:
    store = InMemoryDecisionSupportStore()
    _create_live_workspace(store)

    incident_workspace = store.append_incident(
        _incident(), expected_workspace_version=2
    )
    evidence_workspace = store.append_evidence_bundle(
        _evidence(), expected_workspace_version=3
    )
    proposal_workspace = store.append_proposal(
        _proposal(), expected_workspace_version=4
    )

    assert [
        incident_workspace.version,
        evidence_workspace.version,
        proposal_workspace.version,
    ] == [3, 4, 5]
    assert store.get_incident("incident-sold-out-001") == _incident()
    assert store.get_evidence_bundle("evidence-bundle-001") == _evidence().bundle
    assert store.get_proposal("proposal-001") == _proposal()
    with pytest.raises(WorkspaceConflictError, match="incident"):
        store.append_evidence_bundle(
            _evidence(incident_id="incident-missing", idempotency_key="evidence-missing"),
            expected_workspace_version=5,
        )


def test_store_rejects_bundle_when_authoritative_incident_fact_differs() -> None:
    """公开 Store 入口也必须拒绝绕过 Assembler 的伪造父事实绑定。"""

    store = InMemoryDecisionSupportStore()
    _create_live_workspace(store)
    store.append_incident(
        _incident(snapshot={"product_id": "p999", "expected_version": 99}),
        expected_workspace_version=2,
    )

    with pytest.raises(WorkspaceConflictError, match="incident binding"):
        store.append_evidence_bundle(_evidence(), expected_workspace_version=3)


def test_store_rejects_raw_bundle_without_governed_assembly_receipt() -> None:
    """可重算 SHA-256 不是写入授权，Store 只能接受受治理 Assembler 的产物。"""

    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    store.append_incident(_incident(), expected_workspace_version=1)

    with pytest.raises(WorkspaceConflictError, match="governed assembly"):
        store.append_evidence_bundle(_evidence().bundle, expected_workspace_version=2)


def test_store_rejects_receipt_not_issued_by_governed_assembler() -> None:
    """精确 Python 类型不足以代表写入授权，伪造 wrapper 也必须被拒绝。"""

    store = InMemoryDecisionSupportStore()
    _create_live_workspace(store)
    store.append_incident(_incident(), expected_workspace_version=2)
    # 外部调用方可以用底层 Python 原语伪造对象布局；Store 必须只信任
    # Assembler 实际签发并登记的 receipt，不能把 type() 检查当作能力边界。
    forged = object.__new__(AssembledEvidenceBundle)
    object.__setattr__(forged, "_bundle", _evidence().bundle)

    with pytest.raises(WorkspaceConflictError, match="governed assembly"):
        store.append_evidence_bundle(forged, expected_workspace_version=3)


def test_store_rejects_issued_receipt_after_bundle_rebinding() -> None:
    """已签发 receipt 的私有字段被重绑定后，Store 必须 fail-closed。"""

    store = InMemoryDecisionSupportStore()
    _create_live_workspace(store)
    store.append_incident(_incident(), expected_workspace_version=2)
    issued = _evidence()
    replacement = EvidenceBundle.model_validate(
        {
            **issued.bundle.model_dump(mode="json"),
            "evidence_bundle_id": "evidence-bundle-rebound",
            "idempotency_key": "evidence-idem-rebound",
        }
    )
    # 即使攻击者复用合法 receipt，也不能把签发时通过权威读取的原始 Bundle
    # 换成另一个结构合法的事实，再借用该 receipt 获得写入授权。
    object.__setattr__(issued, "_bundle", replacement)

    with pytest.raises(WorkspaceConflictError, match="governed assembly"):
        store.append_evidence_bundle(issued, expected_workspace_version=3)


def test_store_rejects_governed_receipt_after_workspace_leaves_live_view() -> None:
    """Assembler 的 LIVE 快照不能授权向已处于 PREPARE/REVIEW 的会话追加事实。"""

    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    store.append_incident(_incident(), expected_workspace_version=1)

    with pytest.raises(WorkspaceConflictError, match="Workspace LIVE"):
        store.append_evidence_bundle(_evidence(), expected_workspace_version=2)


def test_idempotent_fact_replay_reuses_original_without_advancing_version() -> None:
    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    first = store.append_incident(_incident(), expected_workspace_version=1)
    replay = store.append_incident(_incident(), expected_workspace_version=999)

    assert replay == first
    assert store.get_workspace(first.live_session_id).version == 2
    with pytest.raises(WorkspaceConflictError, match="idempotency"):
        store.append_incident(
            _incident(snapshot={"product_id": "p002"}),
            expected_workspace_version=2,
        )


def test_operator_lock_expiry_issues_higher_fencing_and_rejects_old_owner() -> None:
    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    first = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 10, now=NOW
    )

    with pytest.raises(WorkspaceLeaseError, match="locked"):
        store.acquire_operator_lock(
            first.live_session_id, "operator-002", 10, now=NOW + timedelta(seconds=5)
        )
    second = store.acquire_operator_lock(
        first.live_session_id, "operator-002", 10, now=NOW + timedelta(seconds=10)
    )

    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(WorkspaceLeaseError, match="fencing"):
        store.advance_view(
            first.live_session_id,
            target_view=WorkspaceView.LIVE,
            expected_version=1,
            operator_id="operator-001",
            fencing_token=first.fencing_token,
            now=NOW + timedelta(seconds=10),
        )


def test_operator_lock_renew_release_and_fact_listing_are_stable() -> None:
    """续租不换 token，释放后重取递增 token，事实列表按创建顺序稳定。"""

    store = InMemoryDecisionSupportStore()
    store.create_workspace(_workspace())
    first_incident = _incident()
    second_incident = _incident(
        incident_id="incident-sold-out-002",
        idempotency_key="incident-idem-002",
        created_at=NOW + timedelta(seconds=1),
    )
    store.append_incident(first_incident, expected_workspace_version=1)
    store.append_incident(second_incident, expected_workspace_version=2)
    lease = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 10, now=NOW
    )

    renewed = store.renew_operator_lock(
        lease.live_session_id,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        lease_seconds=30,
        now=NOW + timedelta(seconds=5),
    )
    store.release_operator_lock(
        lease.live_session_id,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        now=NOW + timedelta(seconds=6),
    )
    next_lease = store.acquire_operator_lock(
        lease.live_session_id, "operator-002", 10, now=NOW + timedelta(seconds=6)
    )

    assert renewed.fencing_token == lease.fencing_token
    assert renewed.lease_until == NOW + timedelta(seconds=35)
    assert next_lease.fencing_token == lease.fencing_token + 1
    assert store.list_incidents(lease.live_session_id) == (
        first_incident,
        second_incident,
    )


def test_decision_and_command_require_current_operator_lease() -> None:
    store = InMemoryDecisionSupportStore()
    assert _seed_proposal(store) == 5
    lease = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 30, now=NOW
    )

    decision_workspace = store.append_operator_decision(
        _decision(),
        expected_workspace_version=5,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        now=NOW,
    )
    command_workspace = store.append_execution_command(
        _command(),
        expected_workspace_version=6,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        now=NOW,
    )

    assert decision_workspace.version == 6
    assert command_workspace.version == 7
    assert store.get_operator_decision("decision-001") == _decision()
    assert store.get_execution_command("command-001") == _command()
    with pytest.raises(WorkspaceLeaseError, match="expired"):
        store.append_execution_command(
            _command(command_id="command-002", idempotency_key="command-idem-002"),
            expected_workspace_version=6,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
            now=NOW + timedelta(seconds=30),
        )


def test_command_requires_same_fencing_epoch_as_parent_decision() -> None:
    """操作员重新取得锁后不能用新 fencing 为旧决定补造首次执行命令。"""

    store = InMemoryDecisionSupportStore()
    _seed_proposal(store)
    first = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 10, now=NOW
    )
    store.append_operator_decision(
        _decision(),
        expected_workspace_version=5,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
        now=NOW,
    )
    second = store.acquire_operator_lock(
        first.live_session_id,
        "operator-001",
        10,
        now=NOW + timedelta(seconds=10),
    )

    with pytest.raises(WorkspaceLeaseError, match="decision fencing"):
        store.append_execution_command(
            _command(),
            expected_workspace_version=6,
            operator_id="operator-001",
            fencing_token=second.fencing_token,
            now=NOW + timedelta(seconds=10),
        )


def test_decision_and_command_replay_survives_lease_handover() -> None:
    """提交结果未知后的同载荷重试先复用事实，不重新消费旧授权。"""

    store = InMemoryDecisionSupportStore()
    _seed_proposal(store)
    first = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 10, now=NOW
    )
    decision = _decision()
    command = _command()
    store.append_operator_decision(
        decision,
        expected_workspace_version=5,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
        now=NOW,
    )
    store.append_execution_command(
        command,
        expected_workspace_version=6,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
        now=NOW,
    )
    second = store.acquire_operator_lock(
        first.live_session_id,
        "operator-002",
        10,
        now=NOW + timedelta(seconds=10),
    )

    replayed_decision = store.append_operator_decision(
        decision,
        expected_workspace_version=999,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
        now=NOW + timedelta(seconds=10),
    )
    replayed_command = store.append_execution_command(
        command,
        expected_workspace_version=999,
        operator_id="operator-001",
        fencing_token=first.fencing_token,
        now=NOW + timedelta(seconds=10),
    )

    assert second.fencing_token == first.fencing_token + 1
    assert replayed_decision.version == 7
    assert replayed_command.version == 7
    assert store.list_operator_decisions(first.live_session_id) == (decision,)
    assert store.list_execution_commands(first.live_session_id) == (command,)


def test_decision_rejects_stale_proposal_version_and_cross_scope_operator() -> None:
    store = InMemoryDecisionSupportStore()
    _seed_proposal(store)
    lease = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 30, now=NOW
    )

    with pytest.raises(WorkspaceConflictError, match="proposal version"):
        store.append_operator_decision(
            _decision(expected_proposal_version=2),
            expected_workspace_version=5,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
            now=NOW,
        )


def test_decision_rejects_superseded_proposal_lineage_version() -> None:
    """同 proposal_key 已有 v2 后，运营不能再批准不可见的 v1。"""

    store = InMemoryDecisionSupportStore()
    _seed_proposal(store)
    store.append_proposal(
        _proposal(
            proposal_id="proposal-002",
            idempotency_key="proposal-idem-002",
            proposal_version=2,
        ),
        expected_workspace_version=5,
    )
    replayed_first = store.append_proposal(
        _proposal(), expected_workspace_version=999
    )
    lease = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 30, now=NOW
    )

    assert replayed_first.version == 6
    with pytest.raises(WorkspaceConflictError, match="latest proposal"):
        store.append_operator_decision(
            _decision(proposal_id="proposal-001", expected_proposal_version=1),
            expected_workspace_version=6,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
            now=NOW,
        )
    with pytest.raises(WorkspaceLeaseError, match="operator"):
        store.append_operator_decision(
            _decision(operator_id="operator-002", idempotency_key="decision-idem-002"),
            expected_workspace_version=5,
            operator_id="operator-002",
            fencing_token=lease.fencing_token,
            now=NOW,
        )


def test_proposal_accepts_only_one_operator_decision() -> None:
    """同一 Proposal 是一次性人工裁决对象，不能在读取新版本后追加矛盾决定。"""

    store = InMemoryDecisionSupportStore()
    _seed_proposal(store)
    lease = store.acquire_operator_lock(
        "live-session-p001-sold-out-v1", "operator-001", 30, now=NOW
    )
    store.append_operator_decision(
        _decision(),
        expected_workspace_version=5,
        operator_id="operator-001",
        fencing_token=lease.fencing_token,
        now=NOW,
    )

    with pytest.raises(WorkspaceConflictError, match="already has a decision"):
        store.append_operator_decision(
            _decision(
                decision_id="decision-002",
                idempotency_key="decision-idem-002",
                decision_kind=DecisionKind.REJECT,
            ),
            expected_workspace_version=6,
            operator_id="operator-001",
            fencing_token=lease.fencing_token,
            now=NOW,
        )
