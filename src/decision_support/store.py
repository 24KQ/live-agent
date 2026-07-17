"""Phase 14 Workspace 不可变事实仓储的内存与 PostgreSQL 等价契约。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, TypeVar

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings
from src.decision_support.evidence import (
    AssembledEvidenceBundle,
    EvidenceBundleSnapshot,
    IncidentEvidenceBinding,
    _require_governed_evidence_receipt,
)
from src.decision_support.models import (
    POSTGRES_BIGINT_MAX,
    EvidenceBundle,
    ExecutionCommand,
    Incident,
    LiveSessionWorkspace,
    OperatorDecision,
    OperatorLease,
    Proposal,
    WorkspaceView,
)


class WorkspaceStoreError(RuntimeError):
    """Workspace Store 的稳定领域错误基类。"""


class WorkspaceNotFoundError(WorkspaceStoreError):
    """目标 Workspace 或事实不存在。"""


class WorkspaceConflictError(WorkspaceStoreError):
    """版本、幂等内容、外键或状态转换冲突。"""


class WorkspaceLeaseError(WorkspaceStoreError):
    """操作员锁、租约或 fencing token 无效。"""


FactT = TypeVar(
    "FactT",
    Incident,
    EvidenceBundle,
    Proposal,
    OperatorDecision,
    ExecutionCommand,
)


def _require_evidence_parent_binding(
    *,
    evidence: EvidenceBundle,
    incident: Incident,
    workspace_scope: dict[str, str],
) -> None:
    """核对 Bundle 内的父事实绑定，防止调用方绕过 Assembler 直接追加。"""

    snapshot = EvidenceBundleSnapshot.model_validate(evidence.snapshot)
    for field, actual in workspace_scope.items():
        if getattr(snapshot.scope, field) != actual:
            raise WorkspaceConflictError("evidence workspace binding is invalid")
    if snapshot.incident_binding != IncidentEvidenceBinding.from_incident(incident):
        raise WorkspaceConflictError("evidence incident binding is invalid")


class InMemoryDecisionSupportStore:
    """测试与无数据库 Demo 使用的线程安全 append-only 事实仓储。

    每次事实追加与 Workspace 版本递增位于同一进程锁内。PostgreSQL 实现必须使用
    相同校验顺序：先识别幂等重放，再锁定 Workspace 校验版本，最后写事实并 CAS。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._workspaces: dict[str, LiveSessionWorkspace] = {}
        self._workspace_by_run_key: dict[str, str] = {}
        self._incidents: dict[str, Incident] = {}
        self._evidence_bundles: dict[str, EvidenceBundle] = {}
        self._proposals: dict[str, Proposal] = {}
        self._decisions: dict[str, OperatorDecision] = {}
        self._decision_fencing: dict[str, tuple[str, int]] = {}
        self._commands: dict[str, ExecutionCommand] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, object]] = {}
        self._leases: dict[str, OperatorLease] = {}
        self._last_fencing: dict[str, int] = {}

    def create_workspace(self, workspace: LiveSessionWorkspace) -> LiveSessionWorkspace:
        """按 run_key 幂等创建首个 PREPARE/version=1 Workspace。"""

        validated = LiveSessionWorkspace.model_validate(workspace.model_dump(mode="python"))
        if validated.view is not WorkspaceView.PREPARE or validated.version != 1:
            raise WorkspaceConflictError("workspace must start at PREPARE version 1")
        with self._lock:
            existing_id = self._workspace_by_run_key.get(validated.run_key)
            if existing_id is not None:
                existing = self._workspaces[existing_id]
                if existing != validated:
                    raise WorkspaceConflictError("run_key conflicts with existing workspace")
                return existing
            if validated.live_session_id in self._workspaces:
                raise WorkspaceConflictError("live_session_id already exists")
            self._workspaces[validated.live_session_id] = validated
            self._workspace_by_run_key[validated.run_key] = validated.live_session_id
            return validated

    def get_workspace(self, live_session_id: str) -> LiveSessionWorkspace:
        with self._lock:
            try:
                return self._workspaces[live_session_id]
            except KeyError as exc:
                raise WorkspaceNotFoundError("workspace not found") from exc

    def acquire_operator_lock(
        self,
        live_session_id: str,
        operator_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> OperatorLease:
        """取得操作员 lease；过期后的新持有者获得严格更大的 fencing token。"""

        if not operator_id:
            raise ValueError("operator_id must not be empty")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        instant = self._normalize_now(now)
        with self._lock:
            self.get_workspace(live_session_id)
            current = self._leases.get(live_session_id)
            if current is not None and instant < current.lease_until:
                if current.operator_id != operator_id:
                    raise WorkspaceLeaseError(
                        f"workspace locked by {current.operator_id}"
                    )
                return current
            token = self._last_fencing.get(live_session_id, 0) + 1
            lease = OperatorLease(
                live_session_id=live_session_id,
                operator_id=operator_id,
                fencing_token=token,
                lease_until=instant + timedelta(seconds=lease_seconds),
            )
            self._leases[live_session_id] = lease
            self._last_fencing[live_session_id] = token
            return lease

    def advance_view(
        self,
        live_session_id: str,
        *,
        target_view: WorkspaceView,
        expected_version: int,
        operator_id: str,
        fencing_token: int,
        now: datetime | None = None,
    ) -> LiveSessionWorkspace:
        """在当前操作员 fencing 下按 PREPARE->LIVE->REVIEW 单向推进投影视图。"""

        instant = self._normalize_now(now)
        with self._lock:
            current = self.get_workspace(live_session_id)
            self._require_lease(
                live_session_id, operator_id, fencing_token, instant
            )
            self._require_version(current, expected_version)
            transitions = {
                WorkspaceView.PREPARE: WorkspaceView.LIVE,
                WorkspaceView.LIVE: WorkspaceView.REVIEW,
            }
            if transitions.get(current.view) is not target_view:
                raise WorkspaceConflictError("illegal workspace view transition")
            updated = LiveSessionWorkspace.model_validate(
                {
                    **current.model_dump(mode="python"),
                    "view": target_view,
                    "version": current.version + 1,
                }
            )
            self._workspaces[live_session_id] = updated
            return updated

    def renew_operator_lock(
        self,
        live_session_id: str,
        *,
        operator_id: str,
        fencing_token: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> OperatorLease:
        """当前未过期 token 可续租，且新截止时间不得缩短原租约。"""

        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        instant = self._normalize_now(now)
        with self._lock:
            current = self._require_lease(
                live_session_id, operator_id, fencing_token, instant
            )
            renewed = OperatorLease(
                live_session_id=live_session_id,
                operator_id=operator_id,
                fencing_token=fencing_token,
                lease_until=max(
                    current.lease_until,
                    instant + timedelta(seconds=lease_seconds),
                ),
            )
            self._leases[live_session_id] = renewed
            return renewed

    def release_operator_lock(
        self,
        live_session_id: str,
        *,
        operator_id: str,
        fencing_token: int,
        now: datetime | None = None,
    ) -> None:
        """当前持有者显式释放 lease，但保留单调 fencing 历史。"""

        instant = self._normalize_now(now)
        with self._lock:
            self._require_lease(
                live_session_id, operator_id, fencing_token, instant
            )
            del self._leases[live_session_id]

    def append_incident(
        self, fact: Incident, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            return self._append(
                "incident",
                Incident.model_validate(fact.model_dump(mode="json")),
                fact.incident_id,
                self._incidents,
                expected_workspace_version,
            )

    def append_evidence_bundle(
        self, fact: AssembledEvidenceBundle, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            try:
                issued_bundle = _require_governed_evidence_receipt(fact)
            except TypeError as exc:
                raise WorkspaceConflictError(
                    "evidence requires governed assembly receipt"
                ) from exc
            validated = EvidenceBundle.model_validate(issued_bundle.model_dump(mode="json"))
            replay = self._replay_workspace("evidence_bundle", validated)
            if replay is not None:
                return replay
            incident = self._incidents.get(validated.incident_id)
            if incident is None or incident.live_session_id != validated.live_session_id:
                raise WorkspaceConflictError("evidence incident scope is invalid")
            workspace = self._workspaces.get(validated.live_session_id)
            if workspace is None:
                raise WorkspaceConflictError("evidence workspace scope is invalid")
            if workspace.view is not WorkspaceView.LIVE:
                raise WorkspaceConflictError("evidence requires Workspace LIVE view")
            _require_evidence_parent_binding(
                evidence=validated,
                incident=incident,
                workspace_scope={
                    "live_session_id": workspace.live_session_id,
                    "room_id": workspace.room_id,
                    "trace_id": workspace.trace_id,
                    "anchor_id": workspace.anchor_id,
                    "root_plan_run_id": workspace.root_plan_run_id,
                },
            )
            return self._append(
                "evidence_bundle",
                validated,
                validated.evidence_bundle_id,
                self._evidence_bundles,
                expected_workspace_version,
            )

    def append_proposal(
        self, fact: Proposal, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            validated = Proposal.model_validate(fact.model_dump(mode="json"))
            replay = self._replay_workspace("proposal", validated)
            if replay is not None:
                return replay
            incident = self._incidents.get(validated.incident_id)
            evidence = self._evidence_bundles.get(validated.evidence_bundle_id)
            if (
                incident is None
                or evidence is None
                or incident.live_session_id != validated.live_session_id
                or evidence.live_session_id != validated.live_session_id
                or evidence.incident_id != validated.incident_id
            ):
                raise WorkspaceConflictError("proposal evidence scope is invalid")
            lineage_versions = [
                item.proposal_version
                for item in self._proposals.values()
                if item.live_session_id == validated.live_session_id
                and item.proposal_key == validated.proposal_key
            ]
            expected_proposal_version = (
                1 if not lineage_versions else max(lineage_versions) + 1
            )
            if validated.proposal_version != expected_proposal_version:
                raise WorkspaceConflictError("proposal lineage version conflict")
            return self._append(
                "proposal",
                validated,
                validated.proposal_id,
                self._proposals,
                expected_workspace_version,
            )

    def append_operator_decision(
        self,
        fact: OperatorDecision,
        *,
        expected_workspace_version: int,
        operator_id: str,
        fencing_token: int,
        now: datetime | None = None,
    ) -> LiveSessionWorkspace:
        self._require_control_integer(expected_workspace_version, "expected_version")
        self._require_control_integer(fencing_token, "fencing_token")
        with self._lock:
            validated = OperatorDecision.model_validate(fact.model_dump(mode="json"))
            replay = self._replay_workspace("operator_decision", validated)
            if replay is not None:
                return replay
            instant = self._normalize_now(now)
            self._require_lease(
                validated.live_session_id, operator_id, fencing_token, instant
            )
            if validated.operator_id != operator_id:
                raise WorkspaceLeaseError("decision operator does not own current lease")
            proposal = self._proposals.get(validated.proposal_id)
            if proposal is None or proposal.live_session_id != validated.live_session_id:
                raise WorkspaceConflictError("decision proposal scope is invalid")
            if proposal.proposal_version != validated.expected_proposal_version:
                raise WorkspaceConflictError("proposal version conflict")
            latest_version = max(
                item.proposal_version
                for item in self._proposals.values()
                if item.live_session_id == validated.live_session_id
                and item.proposal_key == proposal.proposal_key
            )
            if proposal.proposal_version != latest_version:
                raise WorkspaceConflictError("latest proposal version is required")
            if any(
                item.live_session_id == validated.live_session_id
                and item.proposal_id == validated.proposal_id
                for item in self._decisions.values()
            ):
                raise WorkspaceConflictError("proposal already has a decision")
            workspace = self._append(
                "operator_decision",
                validated,
                validated.decision_id,
                self._decisions,
                expected_workspace_version,
            )
            # fencing 是执行控制事实，不进入业务 payload；Store 单独保留它，
            # 使后续命令只能在产生人工决定的同一 lease epoch 内首次落库。
            self._decision_fencing[validated.decision_id] = (
                operator_id,
                fencing_token,
            )
            return workspace

    def append_execution_command(
        self,
        fact: ExecutionCommand,
        *,
        expected_workspace_version: int,
        operator_id: str,
        fencing_token: int,
        now: datetime | None = None,
    ) -> LiveSessionWorkspace:
        self._require_control_integer(expected_workspace_version, "expected_version")
        self._require_control_integer(fencing_token, "fencing_token")
        with self._lock:
            validated = ExecutionCommand.model_validate(fact.model_dump(mode="json"))
            replay = self._replay_workspace("execution_command", validated)
            if replay is not None:
                return replay
            instant = self._normalize_now(now)
            self._require_lease(
                validated.live_session_id, operator_id, fencing_token, instant
            )
            decision = self._decisions.get(validated.decision_id)
            if decision is None or decision.live_session_id != validated.live_session_id:
                raise WorkspaceConflictError("command decision scope is invalid")
            if decision.operator_id != operator_id:
                raise WorkspaceLeaseError("command operator does not own decision")
            if self._decision_fencing.get(validated.decision_id) != (
                operator_id,
                fencing_token,
            ):
                raise WorkspaceLeaseError("command decision fencing mismatch")
            return self._append(
                "execution_command",
                validated,
                validated.command_id,
                self._commands,
                expected_workspace_version,
            )

    def get_incident(self, fact_id: str) -> Incident:
        return self._get_fact(self._incidents, fact_id, "incident")

    def get_evidence_bundle(self, fact_id: str) -> EvidenceBundle:
        return self._get_fact(self._evidence_bundles, fact_id, "evidence bundle")

    def get_proposal(self, fact_id: str) -> Proposal:
        return self._get_fact(self._proposals, fact_id, "proposal")

    def get_operator_decision(self, fact_id: str) -> OperatorDecision:
        return self._get_fact(self._decisions, fact_id, "operator decision")

    def get_execution_command(self, fact_id: str) -> ExecutionCommand:
        return self._get_fact(self._commands, fact_id, "execution command")

    def list_incidents(self, live_session_id: str) -> tuple[Incident, ...]:
        return self._list_facts(self._incidents, live_session_id)

    def list_evidence_bundles(
        self, live_session_id: str
    ) -> tuple[EvidenceBundle, ...]:
        return self._list_facts(self._evidence_bundles, live_session_id)

    def list_proposals(self, live_session_id: str) -> tuple[Proposal, ...]:
        return self._list_facts(self._proposals, live_session_id)

    def list_operator_decisions(
        self, live_session_id: str
    ) -> tuple[OperatorDecision, ...]:
        return self._list_facts(self._decisions, live_session_id)

    def list_execution_commands(
        self, live_session_id: str
    ) -> tuple[ExecutionCommand, ...]:
        return self._list_facts(self._commands, live_session_id)

    def _append(
        self,
        fact_kind: str,
        fact: FactT,
        fact_id: str,
        target: dict[str, FactT],
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """在同一锁内执行幂等重放、CAS、事实插入和 Workspace 版本递增。"""

        key = (fact.live_session_id, fact.idempotency_key)
        replay = self._idempotency.get(key)
        if replay is not None:
            replay_kind, replay_fact = replay
            if replay_kind != fact_kind or replay_fact != fact:
                raise WorkspaceConflictError(
                    "idempotency_key conflicts with existing workspace fact"
                )
            return self.get_workspace(fact.live_session_id)
        workspace = self.get_workspace(fact.live_session_id)
        self._require_version(workspace, expected_workspace_version)
        if fact_id in target:
            raise WorkspaceConflictError(f"{fact_kind} id already exists")
        target[fact_id] = fact
        self._idempotency[key] = (fact_kind, fact)
        updated = LiveSessionWorkspace.model_validate(
            {
                **workspace.model_dump(mode="python"),
                "version": workspace.version + 1,
            }
        )
        self._workspaces[workspace.live_session_id] = updated
        return updated

    def _replay_workspace(
        self, fact_kind: str, fact: FactT
    ) -> LiveSessionWorkspace | None:
        """先解析已提交事实；同键异载荷仍 fail-closed。"""

        replay = self._idempotency.get(
            (fact.live_session_id, fact.idempotency_key)
        )
        if replay is None:
            return None
        replay_kind, replay_fact = replay
        if replay_kind != fact_kind or replay_fact != fact:
            raise WorkspaceConflictError(
                "idempotency_key conflicts with existing workspace fact"
            )
        return self.get_workspace(fact.live_session_id)

    def _require_lease(
        self,
        live_session_id: str,
        operator_id: str,
        fencing_token: int,
        now: datetime,
    ) -> OperatorLease:
        self._require_control_integer(fencing_token, "fencing_token")
        current = self._leases.get(live_session_id)
        if current is None:
            raise WorkspaceLeaseError("operator lease is required")
        if fencing_token != current.fencing_token:
            raise WorkspaceLeaseError("stale fencing token")
        if operator_id != current.operator_id:
            raise WorkspaceLeaseError("operator does not own current lease")
        if now >= current.lease_until:
            raise WorkspaceLeaseError("operator lease expired")
        return current

    @staticmethod
    def _require_version(
        workspace: LiveSessionWorkspace, expected_version: int
    ) -> None:
        InMemoryDecisionSupportStore._require_control_integer(
            expected_version, "expected_version"
        )
        if workspace.version != expected_version:
            raise WorkspaceConflictError("workspace version conflict")

    @staticmethod
    def _require_control_integer(value: int, label: str) -> None:
        """拒绝 bool、非正数和 PostgreSQL BIGINT 范围外的控制字段。"""

        if (
            type(value) is not int
            or value < 1
            or value > POSTGRES_BIGINT_MAX
        ):
            raise ValueError(f"{label} must be a positive PostgreSQL BIGINT")

    @staticmethod
    def _normalize_now(value: datetime | None) -> datetime:
        instant = value or datetime.now(timezone.utc)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return instant

    @staticmethod
    def _get_fact(target: dict[str, FactT], fact_id: str, label: str) -> FactT:
        try:
            return target[fact_id]
        except KeyError as exc:
            raise WorkspaceNotFoundError(f"{label} not found") from exc

    def _list_facts(
        self, target: dict[str, FactT], live_session_id: str
    ) -> tuple[FactT, ...]:
        self.get_workspace(live_session_id)
        facts = (
            fact
            for fact in target.values()
            if fact.live_session_id == live_session_id
        )
        return tuple(
            sorted(facts, key=lambda fact: (fact.created_at, self._fact_id(fact)))
        )

    @staticmethod
    def _fact_id(fact: FactT) -> str:
        for field in (
            "incident_id",
            "evidence_bundle_id",
            "proposal_id",
            "decision_id",
            "command_id",
        ):
            value = getattr(fact, field, None)
            if value is not None:
                return str(value)
        raise WorkspaceConflictError("workspace fact lacks stable identity")


class PostgresDecisionSupportStore:
    """以 Workspace 根行锁串行化 CAS、租约和 append-only 事实的生产 Store。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def initialize_schema(self) -> None:
        """重复执行版本化 DDL；既有事实不会被覆盖或清理。"""

        from pathlib import Path

        sql = (
            Path(__file__).parents[2]
            / "docker"
            / "init_phase14_decision_support.sql"
        ).read_text(encoding="utf-8")
        with psycopg.connect(**self._settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    def create_workspace(self, workspace: LiveSessionWorkspace) -> LiveSessionWorkspace:
        validated = LiveSessionWorkspace.model_validate(workspace.model_dump(mode="python"))
        if validated.view is not WorkspaceView.PREPARE or validated.version != 1:
            raise WorkspaceConflictError("workspace must start at PREPARE version 1")
        sql = """INSERT INTO phase14_live_session_workspaces
            (live_session_id,run_key,room_id,trace_id,anchor_id,
             root_plan_run_id,event_inbox_scope_id,decision_trace_scope_id,
             replay_scope_id,evaluation_scope_id,current_view,version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            ON CONFLICT DO NOTHING RETURNING *"""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        validated.live_session_id,
                        validated.run_key,
                        validated.room_id,
                        validated.trace_id,
                        validated.anchor_id,
                        validated.root_plan_run_id,
                        validated.event_inbox_scope_id,
                        validated.decision_trace_scope_id,
                        validated.replay_scope_id,
                        validated.evaluation_scope_id,
                        validated.view.value,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """SELECT * FROM phase14_live_session_workspaces
                           WHERE run_key=%s""",
                        (validated.run_key,),
                    )
                    row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """SELECT * FROM phase14_live_session_workspaces
                           WHERE live_session_id=%s""",
                        (validated.live_session_id,),
                    )
                    row = cur.fetchone()
            conn.commit()
        if row is None:
            raise WorkspaceConflictError("workspace identity conflict")
        stored = self._workspace_from_row(row)
        if stored != validated:
            raise WorkspaceConflictError("workspace identity conflicts with existing fact")
        return stored

    def get_workspace(self, live_session_id: str) -> LiveSessionWorkspace:
        """按稳定会话身份读取 Workspace 权威根事实。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM phase14_live_session_workspaces
                       WHERE live_session_id=%s""",
                    (live_session_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise WorkspaceNotFoundError("workspace not found")
        return self._workspace_from_row(row)

    def append_incident(
        self, fact: Incident, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        """以 Workspace 版本 CAS 追加不可变事故快照。"""

        validated = Incident.model_validate(fact.model_dump(mode="json"))
        return self._append_fact(
            fact_kind="incident",
            fact_id=validated.incident_id,
            fact=validated,
            table="phase14_incidents",
            id_column="incident_id",
            extra_columns={},
            expected_workspace_version=expected_workspace_version,
        )

    def get_incident(self, fact_id: str) -> Incident:
        """按事故稳定身份读取不可变快照。"""

        return self._get_payload_fact(
            "phase14_incidents", "incident_id", fact_id, Incident, "incident"
        )

    def append_evidence_bundle(
        self,
        fact: AssembledEvidenceBundle,
        *,
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """在根行锁内验证事故作用域并追加证据快照。"""

        try:
            issued_bundle = _require_governed_evidence_receipt(fact)
        except TypeError as exc:
            raise WorkspaceConflictError(
                "evidence requires governed assembly receipt"
            ) from exc
        validated = EvidenceBundle.model_validate(issued_bundle.model_dump(mode="json"))

        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT i.live_session_id,i.payload,w.current_view,w.room_id,
                          w.trace_id,w.anchor_id,w.root_plan_run_id
                   FROM phase14_incidents i
                   JOIN phase14_live_session_workspaces w
                     ON w.live_session_id=i.live_session_id
                   WHERE i.incident_id=%s""",
                (validated.incident_id,),
            )
            row = cur.fetchone()
            if row is None or row["live_session_id"] != validated.live_session_id:
                raise WorkspaceConflictError("evidence incident scope is invalid")
            if row["current_view"] != WorkspaceView.LIVE.value:
                raise WorkspaceConflictError("evidence requires Workspace LIVE view")
            _require_evidence_parent_binding(
                evidence=validated,
                incident=Incident.model_validate(row["payload"]),
                workspace_scope={
                    "live_session_id": row["live_session_id"],
                    "room_id": row["room_id"],
                    "trace_id": row["trace_id"],
                    "anchor_id": row["anchor_id"],
                    "root_plan_run_id": row["root_plan_run_id"],
                },
            )

        return self._append_fact(
            fact_kind="evidence_bundle",
            fact_id=validated.evidence_bundle_id,
            fact=validated,
            table="phase14_evidence_bundles",
            id_column="evidence_bundle_id",
            extra_columns={"incident_id": validated.incident_id},
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
        )

    def append_proposal(
        self,
        fact: Proposal,
        *,
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """同时绑定同一 Workspace 下的事故和 EvidenceBundle。"""

        validated = Proposal.model_validate(fact.model_dump(mode="json"))

        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT e.live_session_id, e.incident_id
                   FROM phase14_evidence_bundles e
                   WHERE e.evidence_bundle_id=%s""",
                (validated.evidence_bundle_id,),
            )
            evidence = cur.fetchone()
            if (
                evidence is None
                or evidence["live_session_id"] != validated.live_session_id
                or evidence["incident_id"] != validated.incident_id
            ):
                raise WorkspaceConflictError("proposal evidence scope is invalid")
            cur.execute(
                """SELECT MAX(proposal_version) AS latest_version
                   FROM phase14_proposals
                   WHERE live_session_id=%s AND proposal_key=%s""",
                (validated.live_session_id, validated.proposal_key),
            )
            latest = cur.fetchone()["latest_version"]
            expected = 1 if latest is None else int(latest) + 1
            if validated.proposal_version != expected:
                raise WorkspaceConflictError("proposal lineage version conflict")

        return self._append_fact(
            fact_kind="proposal",
            fact_id=validated.proposal_id,
            fact=validated,
            table="phase14_proposals",
            id_column="proposal_id",
            extra_columns={
                "incident_id": validated.incident_id,
                "evidence_bundle_id": validated.evidence_bundle_id,
                "proposal_key": validated.proposal_key,
                "proposal_version": validated.proposal_version,
            },
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
        )

    def append_operator_decision(
        self,
        fact: OperatorDecision,
        *,
        expected_workspace_version: int,
        operator_id: str,
        fencing_token: int,
    ) -> LiveSessionWorkspace:
        """在当前操作员 lease 内校验 Proposal 版本并追加人工决定。"""

        validated = OperatorDecision.model_validate(fact.model_dump(mode="json"))
        if validated.operator_id != operator_id:
            raise WorkspaceLeaseError("decision operator does not own current lease")

        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT live_session_id, proposal_key, proposal_version
                   FROM phase14_proposals WHERE proposal_id=%s""",
                (validated.proposal_id,),
            )
            proposal = cur.fetchone()
            if proposal is None or proposal["live_session_id"] != validated.live_session_id:
                raise WorkspaceConflictError("decision proposal scope is invalid")
            if int(proposal["proposal_version"]) != validated.expected_proposal_version:
                raise WorkspaceConflictError("proposal version conflict")
            cur.execute(
                """SELECT MAX(proposal_version) AS latest_version
                   FROM phase14_proposals
                   WHERE live_session_id=%s AND proposal_key=%s""",
                (validated.live_session_id, proposal["proposal_key"]),
            )
            latest = cur.fetchone()["latest_version"]
            if latest is None or int(latest) != int(proposal["proposal_version"]):
                raise WorkspaceConflictError("latest proposal version is required")
            cur.execute(
                """SELECT 1 FROM phase14_operator_decisions
                   WHERE live_session_id=%s AND proposal_id=%s""",
                (validated.live_session_id, validated.proposal_id),
            )
            if cur.fetchone() is not None:
                raise WorkspaceConflictError("proposal already has a decision")

        return self._append_fact(
            fact_kind="operator_decision",
            fact_id=validated.decision_id,
            fact=validated,
            table="phase14_operator_decisions",
            id_column="decision_id",
            extra_columns={
                "proposal_id": validated.proposal_id,
                "operator_id": operator_id,
                "fencing_token": fencing_token,
            },
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
            lease=(operator_id, fencing_token),
        )

    def append_execution_command(
        self,
        fact: ExecutionCommand,
        *,
        expected_workspace_version: int,
        operator_id: str,
        fencing_token: int,
    ) -> LiveSessionWorkspace:
        """只接受当前决定操作员在有效 fencing 下追加的编译命令事实。"""

        validated = ExecutionCommand.model_validate(fact.model_dump(mode="json"))
        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT live_session_id, operator_id, fencing_token
                   FROM phase14_operator_decisions WHERE decision_id=%s""",
                (validated.decision_id,),
            )
            decision = cur.fetchone()
            if decision is None or decision["live_session_id"] != validated.live_session_id:
                raise WorkspaceConflictError("command decision scope is invalid")
            if decision["operator_id"] != operator_id:
                raise WorkspaceLeaseError("command operator does not own decision")
            if int(decision["fencing_token"]) != fencing_token:
                raise WorkspaceLeaseError("command decision fencing mismatch")

        return self._append_fact(
            fact_kind="execution_command",
            fact_id=validated.command_id,
            fact=validated,
            table="phase14_execution_commands",
            id_column="command_id",
            extra_columns={
                "decision_id": validated.decision_id,
                "operator_id": operator_id,
                "fencing_token": fencing_token,
            },
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
            lease=(operator_id, fencing_token),
        )

    def get_evidence_bundle(self, fact_id: str) -> EvidenceBundle:
        return self._get_payload_fact(
            "phase14_evidence_bundles",
            "evidence_bundle_id",
            fact_id,
            EvidenceBundle,
            "evidence bundle",
        )

    def get_proposal(self, fact_id: str) -> Proposal:
        return self._get_payload_fact(
            "phase14_proposals", "proposal_id", fact_id, Proposal, "proposal"
        )

    def get_operator_decision(self, fact_id: str) -> OperatorDecision:
        return self._get_payload_fact(
            "phase14_operator_decisions",
            "decision_id",
            fact_id,
            OperatorDecision,
            "operator decision",
        )

    def get_execution_command(self, fact_id: str) -> ExecutionCommand:
        return self._get_payload_fact(
            "phase14_execution_commands",
            "command_id",
            fact_id,
            ExecutionCommand,
            "execution command",
        )

    def list_incidents(self, live_session_id: str) -> tuple[Incident, ...]:
        return self._list_payload_facts(
            "phase14_incidents", "incident_id", live_session_id, Incident
        )

    def list_evidence_bundles(
        self, live_session_id: str
    ) -> tuple[EvidenceBundle, ...]:
        return self._list_payload_facts(
            "phase14_evidence_bundles",
            "evidence_bundle_id",
            live_session_id,
            EvidenceBundle,
        )

    def list_proposals(self, live_session_id: str) -> tuple[Proposal, ...]:
        return self._list_payload_facts(
            "phase14_proposals", "proposal_id", live_session_id, Proposal
        )

    def list_operator_decisions(
        self, live_session_id: str
    ) -> tuple[OperatorDecision, ...]:
        return self._list_payload_facts(
            "phase14_operator_decisions",
            "decision_id",
            live_session_id,
            OperatorDecision,
        )

    def list_execution_commands(
        self, live_session_id: str
    ) -> tuple[ExecutionCommand, ...]:
        return self._list_payload_facts(
            "phase14_execution_commands",
            "command_id",
            live_session_id,
            ExecutionCommand,
        )

    def acquire_operator_lock(
        self, live_session_id: str, operator_id: str, lease_seconds: int
    ) -> OperatorLease:
        """使用数据库事务时钟获取或续用操作员独占 lease，并单调推进 fencing。"""

        # operator_id 是锁身份的一部分，必须在开启事务前拒绝空值，避免先提交
        # 幽灵锁、再由返回模型校验失败而给调用方造成“失败但已写入”。
        if not operator_id:
            raise ValueError("operator_id must not be empty")
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM phase14_live_session_workspaces
                       WHERE live_session_id=%s FOR UPDATE""",
                    (live_session_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise WorkspaceNotFoundError("workspace not found")
                instant = self._database_now(cur)
                if (
                    row["lock_lease_until"] is not None
                    and instant < row["lock_lease_until"]
                ):
                    if row["lock_operator_id"] != operator_id:
                        raise WorkspaceLeaseError(
                            f"workspace locked by {row['lock_operator_id']}"
                        )
                    return OperatorLease(
                        live_session_id=live_session_id,
                        operator_id=operator_id,
                        fencing_token=int(row["fencing_token"]),
                        lease_until=row["lock_lease_until"],
                    )
                cur.execute(
                    """UPDATE phase14_live_session_workspaces
                       SET lock_operator_id=%s,lock_lease_until=%s,
                           fencing_token=fencing_token+1,updated_at=NOW()
                       WHERE live_session_id=%s RETURNING *""",
                    (
                        operator_id,
                        instant + timedelta(seconds=lease_seconds),
                        live_session_id,
                    ),
                )
                updated = cur.fetchone()
            conn.commit()
        return OperatorLease(
            live_session_id=live_session_id,
            operator_id=operator_id,
            fencing_token=int(updated["fencing_token"]),
            lease_until=updated["lock_lease_until"],
        )

    def advance_view(
        self,
        live_session_id: str,
        *,
        target_view: WorkspaceView,
        expected_version: int,
        operator_id: str,
        fencing_token: int,
    ) -> LiveSessionWorkspace:
        """在有效操作员 lease 下执行 PREPARE->LIVE->REVIEW 单向状态迁移。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                row = self._lock_workspace(cur, live_session_id)
                instant = self._database_now(cur)
                self._require_sql_lease(row, operator_id, fencing_token, instant)
                current = self._workspace_from_row(row)
                InMemoryDecisionSupportStore._require_version(current, expected_version)
                expected_target = {
                    WorkspaceView.PREPARE: WorkspaceView.LIVE,
                    WorkspaceView.LIVE: WorkspaceView.REVIEW,
                }.get(current.view)
                if expected_target is not target_view:
                    raise WorkspaceConflictError("illegal workspace view transition")
                cur.execute(
                    """UPDATE phase14_live_session_workspaces
                       SET current_view=%s,version=version+1,updated_at=NOW()
                       WHERE live_session_id=%s RETURNING *""",
                    (target_view.value, live_session_id),
                )
                updated = cur.fetchone()
            conn.commit()
        return self._workspace_from_row(updated)

    def renew_operator_lock(
        self,
        live_session_id: str,
        *,
        operator_id: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> OperatorLease:
        """在根行锁内续租当前 token，旧 fencing 永久不能延长租约。"""

        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        with self._connection() as conn:
            with conn.cursor() as cur:
                row = self._lock_workspace(cur, live_session_id)
                instant = self._database_now(cur)
                requested_until = instant + timedelta(seconds=lease_seconds)
                self._require_sql_lease(row, operator_id, fencing_token, instant)
                cur.execute(
                    """UPDATE phase14_live_session_workspaces
                       SET lock_lease_until=GREATEST(lock_lease_until,%s),updated_at=NOW()
                       WHERE live_session_id=%s RETURNING *""",
                    (requested_until, live_session_id),
                )
                updated = cur.fetchone()
            conn.commit()
        return OperatorLease(
            live_session_id=live_session_id,
            operator_id=operator_id,
            fencing_token=fencing_token,
            lease_until=updated["lock_lease_until"],
        )

    def release_operator_lock(
        self,
        live_session_id: str,
        *,
        operator_id: str,
        fencing_token: int,
    ) -> None:
        """当前未过期持有者可释放 lease，根行上的 fencing 计数不回退。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                row = self._lock_workspace(cur, live_session_id)
                instant = self._database_now(cur)
                self._require_sql_lease(row, operator_id, fencing_token, instant)
                cur.execute(
                    """UPDATE phase14_live_session_workspaces
                       SET lock_operator_id=NULL,lock_lease_until=NULL,updated_at=NOW()
                       WHERE live_session_id=%s""",
                    (live_session_id,),
                )
            conn.commit()

    def _append_fact(
        self,
        *,
        fact_kind: str,
        fact_id: str,
        fact: FactT,
        table: str,
        id_column: str,
        extra_columns: dict[str, object],
        expected_workspace_version: int,
        validate_parent: Callable[[Any], None] | None = None,
        lease: tuple[str, int] | None = None,
    ) -> LiveSessionWorkspace:
        """在单个根行锁事务中完成门禁、幂等、INSERT 与版本推进。"""

        InMemoryDecisionSupportStore._require_control_integer(
            expected_workspace_version, "expected_version"
        )
        if lease is not None:
            InMemoryDecisionSupportStore._require_control_integer(
                lease[1], "fencing_token"
            )
        payload = fact.model_dump(mode="json")
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    row = self._lock_workspace(cur, fact.live_session_id)
                    cur.execute(
                        """SELECT fact_kind,fact_id,fact_payload
                           FROM phase14_workspace_idempotency
                           WHERE live_session_id=%s AND idempotency_key=%s""",
                        (fact.live_session_id, fact.idempotency_key),
                    )
                    replay = cur.fetchone()
                    if replay is not None:
                        if (
                            replay["fact_kind"] != fact_kind
                            or replay["fact_id"] != fact_id
                            or dict(replay["fact_payload"]) != payload
                        ):
                            raise WorkspaceConflictError(
                                "idempotency_key conflicts with existing workspace fact"
                            )
                        return self._workspace_from_row(row)
                    # 只有首次写入才消费当前授权和父事实。已经提交的同载荷重放
                    # 是纯读取，必须能在响应丢失后跨 lease 过期或换主稳定恢复。
                    if lease is not None:
                        self._require_sql_lease(
                            row, *lease, self._database_now(cur)
                        )
                    if validate_parent is not None:
                        validate_parent(cur)
                    current = self._workspace_from_row(row)
                    InMemoryDecisionSupportStore._require_version(
                        current, expected_workspace_version
                    )
                    columns = [
                        id_column,
                        "live_session_id",
                        *extra_columns,
                        "payload",
                        "created_at",
                    ]
                    values = [
                        fact_id,
                        fact.live_session_id,
                        *extra_columns.values(),
                        Jsonb(payload),
                        fact.created_at,
                    ]
                    placeholders = ",".join(["%s"] * len(values))
                    cur.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                        values,
                    )
                    cur.execute(
                        """INSERT INTO phase14_workspace_idempotency
                           (live_session_id,idempotency_key,fact_kind,fact_id,fact_payload)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (
                            fact.live_session_id,
                            fact.idempotency_key,
                            fact_kind,
                            fact_id,
                            Jsonb(payload),
                        ),
                    )
                    cur.execute(
                        """UPDATE phase14_live_session_workspaces
                           SET version=version+1,updated_at=NOW()
                           WHERE live_session_id=%s RETURNING *""",
                        (fact.live_session_id,),
                    )
                    updated = cur.fetchone()
                conn.commit()
        except (psycopg.errors.UniqueViolation, psycopg.errors.ForeignKeyViolation) as exc:
            raise WorkspaceConflictError("workspace fact constraint conflict") from exc
        return self._workspace_from_row(updated)

    def _connection(self):
        return psycopg.connect(
            **self._settings.postgres_connection_kwargs, row_factory=dict_row
        )

    @staticmethod
    def _lock_workspace(cur, live_session_id: str):
        cur.execute(
            """SELECT * FROM phase14_live_session_workspaces
               WHERE live_session_id=%s FOR UPDATE""",
            (live_session_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkspaceNotFoundError("workspace not found")
        return row

    @staticmethod
    def _workspace_from_row(row) -> LiveSessionWorkspace:
        return LiveSessionWorkspace(
            live_session_id=row["live_session_id"],
            run_key=row["run_key"],
            room_id=row["room_id"],
            trace_id=row["trace_id"],
            anchor_id=row["anchor_id"],
            root_plan_run_id=row["root_plan_run_id"],
            event_inbox_scope_id=row["event_inbox_scope_id"],
            decision_trace_scope_id=row["decision_trace_scope_id"],
            replay_scope_id=row["replay_scope_id"],
            evaluation_scope_id=row["evaluation_scope_id"],
            view=WorkspaceView(row["current_view"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _require_sql_lease(
        row, operator_id: str, fencing_token: int, now: datetime
    ) -> None:
        InMemoryDecisionSupportStore._require_control_integer(
            fencing_token, "fencing_token"
        )
        if int(row["fencing_token"]) != fencing_token:
            raise WorkspaceLeaseError("stale fencing token")
        if row["lock_operator_id"] != operator_id:
            raise WorkspaceLeaseError("operator does not own current lease")
        if row["lock_lease_until"] is None or now >= row["lock_lease_until"]:
            raise WorkspaceLeaseError("operator lease expired")

    @staticmethod
    def _database_now(cur: Any) -> datetime:
        """读取数据库墙钟；行锁等待时间必须计入 lease 到期判断。"""

        cur.execute("SELECT clock_timestamp() AS current_time")
        return cur.fetchone()["current_time"]

    def _get_payload_fact(
        self,
        table: str,
        id_column: str,
        fact_id: str,
        model_type: Any,
        label: str,
    ) -> Any:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT payload FROM {table} WHERE {id_column}=%s", (fact_id,)
                )
                row = cur.fetchone()
        if row is None:
            raise WorkspaceNotFoundError(f"{label} not found")
        return model_type.model_validate(dict(row["payload"]))

    def _list_payload_facts(
        self,
        table: str,
        id_column: str,
        live_session_id: str,
        model_type: Any,
    ) -> tuple[Any, ...]:
        """按创建时间和稳定事实 ID 返回同一 Workspace 的不可变历史。"""

        self.get_workspace(live_session_id)
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT payload FROM {table}
                        WHERE live_session_id=%s ORDER BY created_at,{id_column}""",
                    (live_session_id,),
                )
                rows = cur.fetchall()
        return tuple(
            model_type.model_validate(dict(row["payload"])) for row in rows
        )
