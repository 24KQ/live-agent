"""Phase 14 Workspace 不可变事实仓储的内存与 PostgreSQL 等价契约。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, TypeVar

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import Settings
from src.decision_support.evidence import (
    AssembledEvidenceBundle,
    DanmakuNoiseLevel,
    EvidenceBundleSnapshot,
    EvidenceRole,
    IncidentEvidenceBinding,
    ProductInventoryPayload,
    RhythmSignalKind,
    _require_governed_evidence_receipt,
)
from src.decision_support.models import (
    POSTGRES_BIGINT_MAX,
    AnalystDispatchClaim,
    ConflictAnalysis,
    ConflictAnalysisCode,
    ConflictConstraintCode,
    EscalationMode,
    EscalationRecord,
    EvidenceBundle,
    ExecutionCommand,
    Incident,
    LiveSessionWorkspace,
    MultiAgentFailureCode,
    MultiAgentOutcome,
    MultiAgentOutcomeStatus,
    OperatorDecision,
    OperatorLease,
    PlannerDispatchClaim,
    Proposal,
    WorkspaceView,
)
from src.decision_support.proposal import (
    LiveDecisionProposal,
    ProductStrategy,
    ProposalOrigin,
    ProposalStatus,
)
from src.specialist_runtime.models import _plain_json, canonical_json_sha256


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
    EscalationRecord,
    ConflictAnalysis,
    MultiAgentOutcome,
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


def _bundle_digest(evidence: EvidenceBundle) -> str:
    """从已校验的 Bundle 快照重建摘要，禁止相信调用方重复提供的摘要。"""

    return EvidenceBundleSnapshot.model_validate(evidence.snapshot).bundle_digest


def _multi_agent_proposal_snapshot(fact: Proposal) -> LiveDecisionProposal | None:
    """仅将显式标记为 MULTI_AGENT 的快照交给新协议解析，保留历史通用 Proposal 契约。

    Phase 14 的 `Proposal.snapshot` 是通用不可变审计 JSON，并不承诺全部符合
    `LiveDecisionProposal`。Task 6 只能收紧明确声明 `MULTI_AGENT` 的新事实；把旧快照
    强行重载会破坏既有 OperatorDecision/Compiler 链路，也不会提供额外安全性。
    """

    snapshot = _plain_json(fact.snapshot)
    if not isinstance(snapshot, Mapping):
        return None
    if snapshot.get("proposal_origin") != ProposalOrigin.MULTI_AGENT.value:
        return None
    try:
        return LiveDecisionProposal.model_validate(snapshot)
    except Exception as exc:
        raise WorkspaceConflictError("multi-agent proposal snapshot is invalid") from exc


_CONSTRAINT_REQUIRED_RISK_FLAGS = {
    ConflictConstraintCode.OPERATOR_CONFIRMATION_REQUIRED: "HUMAN_CONFIRMATION_REQUIRED",
    ConflictConstraintCode.BACKUP_AVAILABILITY_UNCERTAIN: "INVENTORY_CONFLICT_REQUIRES_REVIEW",
    ConflictConstraintCode.HOST_RHYTHM_PAUSE_REQUIRED: "RHYTHM_PAUSE_REQUIRED",
}


def _validate_multi_agent_proposal(
    *,
    fact: Proposal,
    evidence: EvidenceBundle,
    escalation: EscalationRecord,
    analysis: ConflictAnalysis,
    workspace: LiveSessionWorkspace,
    now: datetime,
) -> LiveDecisionProposal | None:
    """复核完整多 Agent Proposal；任一选项违规都拒绝整份事实而不做局部过滤。

    这里是模型输出进入 append-only Store 前的统一权威边界。模型只能提供 options，
    Proposal 身份、版本、上游摘要、风险继承和可用备品都必须由已持久化的事实重新计算。
    """

    proposal = _multi_agent_proposal_snapshot(fact)
    if proposal is None:
        return None
    if workspace.view is not WorkspaceView.LIVE:
        raise WorkspaceConflictError("multi-agent proposal requires Workspace LIVE view")
    if proposal.status is not ProposalStatus.READY or proposal.multi_agent_lineage is None:
        raise WorkspaceConflictError("multi-agent proposal must be READY with complete lineage")
    snapshot = EvidenceBundleSnapshot.model_validate(evidence.snapshot)
    references = tuple(component.reference for component in snapshot.components)
    lineage = proposal.multi_agent_lineage
    if (
        fact.proposal_id != proposal.proposal_id
        or fact.live_session_id != proposal.live_session_id
        or fact.incident_id != proposal.incident_id
        or fact.evidence_bundle_id != proposal.evidence_bundle_id
        or fact.profile_id != lineage.planner_profile_id
        or fact.profile_version != lineage.planner_profile_version
        or fact.proposal_key != f"phase16-proposal:{escalation.escalation_id}"
        or fact.proposal_version != 1
        or fact.idempotency_key != f"phase16-proposal:{escalation.escalation_id}"
    ):
        raise WorkspaceConflictError("multi-agent proposal fact identity is invalid")
    if (
        proposal.live_session_id != escalation.live_session_id
        or proposal.incident_id != escalation.incident_id
        or proposal.evidence_bundle_id != escalation.evidence_bundle_id
        or proposal.evidence_bundle_digest != snapshot.bundle_digest
        or proposal.trace_id != snapshot.scope.trace_id
        or lineage.escalation_id != escalation.escalation_id
        or lineage.escalation_digest != escalation.escalation_digest
        or lineage.analysis_id != analysis.analysis_id
        or lineage.analysis_digest != analysis.analysis_digest
        or lineage.evidence_bundle_id != evidence.evidence_bundle_id
        or lineage.evidence_bundle_digest != snapshot.bundle_digest
        or lineage.evidence_refs != references
        or proposal.evidence_refs != references
    ):
        raise WorkspaceConflictError("multi-agent proposal lineage is invalid")
    if not snapshot.proposal_eligible or now >= snapshot.valid_until:
        raise WorkspaceConflictError("multi-agent proposal evidence is not fresh")
    inventory_component = next(
        (
            component
            for component in snapshot.components
            if component.role is EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT
        ),
        None,
    )
    if inventory_component is None or not isinstance(
        inventory_component.payload, ProductInventoryPayload
    ):
        raise WorkspaceConflictError("multi-agent proposal inventory evidence is invalid")
    available_backups = {
        product.product_id
        for product in inventory_component.payload.backup_products
        if product.is_active and product.inventory > 0
    }
    required_risks = {item.value for item in analysis.risk_codes}
    required_risks.update(
        _CONSTRAINT_REQUIRED_RISK_FLAGS[constraint]
        for constraint in analysis.constraint_codes
    )
    for option in proposal.options:
        if option.evidence_refs != references:
            raise WorkspaceConflictError("multi-agent proposal option evidence is invalid")
        option_risks = set(option.risk_flags)
        if not required_risks.issubset(option_risks):
            raise WorkspaceConflictError("multi-agent proposal option omits required risk")
        if option.product_strategy is ProductStrategy.SWITCH_TO_BACKUP:
            if option.backup_product_id not in available_backups:
                raise WorkspaceConflictError("multi-agent proposal backup is unavailable")
            if "BACKUP_PRODUCT_REQUIRES_CONFIRMATION" not in option_risks:
                raise WorkspaceConflictError("multi-agent proposal backup risk is missing")
    return proposal


def derive_automatic_escalation_codes(
    evidence: EvidenceBundle,
) -> tuple[ConflictAnalysisCode, ...]:
    """从冻结六角色 Bundle 重建三选二信号，绝不接受调用方自报的冲突代码。

    Store 与 Phase 16 协调器必须共用这一唯一规则来源：协调器据此决定是否可进入
    Analyst，Store 据此再次核对实际写入的自动升级事实。这样普通事件不会被意外
    送往模型，而调用方也无法在写入时用另一套触发规则绕过选择器。
    """

    snapshot = EvidenceBundleSnapshot.model_validate(evidence.snapshot)
    components = {component.role: component for component in snapshot.components}
    inventory = components[EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT].payload
    danmaku = components[EvidenceRole.DANMAKU_AGGREGATE].payload
    rhythm = components[EvidenceRole.RHYTHM_SIGNAL].payload
    codes: list[ConflictAnalysisCode] = []
    # 只有两个以上仍 active 且库存为正的备品才算“多备品”，不能把已售罄或禁用
    # 商品误计为可恢复候选，避免模型链路因脏快照无意义升级。
    if sum(
        product.is_active and product.inventory > 0
        for product in inventory.backup_products
    ) >= 2:
        codes.append(ConflictAnalysisCode.MULTIPLE_VALID_BACKUPS)
    # 高噪声必须同时围绕主商品或备品可用性；单纯的高频闲聊不构成经营冲突。
    if (
        danmaku.noise_level is DanmakuNoiseLevel.HIGH
        and any(
            topic.category in {"PRODUCT_AVAILABILITY", "BACKUP_AVAILABILITY"}
            for topic in danmaku.topics
        )
    ):
        codes.append(ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH)
    if rhythm.signal_kind is RhythmSignalKind.PAUSE_REQUIRED:
        codes.append(ConflictAnalysisCode.RHYTHM_PAUSE_REQUIRED)
    return tuple(codes)


def _require_escalation_trigger_policy(
    *, fact: EscalationRecord, evidence: EvidenceBundle, now: datetime | None = None
) -> None:
    """闭合 D-147：两类升级都记录 Bundle 派生信号，只有自动路径要求三选二。"""

    snapshot = EvidenceBundleSnapshot.model_validate(evidence.snapshot)
    instant = now or datetime.now(timezone.utc)
    if instant >= snapshot.valid_until:
        raise WorkspaceConflictError("escalation requires fresh evidence bundle")
    if not snapshot.proposal_eligible:
        raise WorkspaceConflictError("escalation requires proposal eligible bundle")
    expected_codes = derive_automatic_escalation_codes(evidence)
    minimum_codes = 1 if fact.mode is EscalationMode.OPERATOR_REQUESTED else 2
    if len(expected_codes) < minimum_codes or fact.trigger_codes != expected_codes:
        raise WorkspaceConflictError("escalation trigger codes are invalid")


class InMemoryDecisionSupportStore:
    """测试与无数据库 Demo 使用的线程安全 append-only 事实仓储。

    每次事实追加与 Workspace 版本递增位于同一进程锁内。PostgreSQL 实现必须使用
    相同校验顺序：先识别幂等重放，再锁定 Workspace 校验版本，最后写事实并 CAS。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        """初始化线程安全 Store；claim 的可信墙钟可注入以重放超时边界。"""

        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._workspaces: dict[str, LiveSessionWorkspace] = {}
        self._workspace_by_run_key: dict[str, str] = {}
        self._incidents: dict[str, Incident] = {}
        self._evidence_bundles: dict[str, EvidenceBundle] = {}
        self._escalations: dict[str, EscalationRecord] = {}
        self._analyses: dict[str, ConflictAnalysis] = {}
        self._outcomes: dict[str, MultiAgentOutcome] = {}
        # claim 与 append-only 领域事实分开保存：它不推进 Workspace 版本，也不允许
        # Agent 获得任何写能力，只是为外部 Analyst 调用提供跨重启的单次发送证据。
        self._analyst_dispatch_claims: dict[str, AnalystDispatchClaim] = {}
        # Planner claim 与 Analyst claim 物理分开：它额外绑定不可变 Analysis，避免第二段
        # 模型调用在并发 Coordinator 或 READY 持久化中断后被重复发送。
        self._planner_dispatch_claims: dict[str, PlannerDispatchClaim] = {}
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

    def get_workspace_by_root_plan(self, root_plan_run_id: str) -> LiveSessionWorkspace:
        """按唯一 root PlanRun 反查会话，拒绝零个或多个匹配事实。"""

        if not root_plan_run_id:
            raise ValueError("root_plan_run_id must not be empty")
        with self._lock:
            matches = tuple(
                workspace
                for workspace in self._workspaces.values()
                if workspace.root_plan_run_id == root_plan_run_id
            )
        if len(matches) != 1:
            raise WorkspaceNotFoundError("root PlanRun does not identify one workspace")
        return matches[0]

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
            if (
                current.view is WorkspaceView.LIVE
                and target_view is WorkspaceView.REVIEW
                and any(
                    claim.live_session_id == live_session_id
                    and instant < claim.lease_until
                    for claim in (
                        *self._analyst_dispatch_claims.values(),
                        *self._planner_dispatch_claims.values(),
                    )
                )
            ):
                # dispatch claim 是模型请求的持久化线性化点。短暂阻止 LIVE 结束可使
                # "先切 REVIEW 再发送"与"先 claim 再切 REVIEW"得到同一安全结果：
                # 在两秒观察窗内要么不发送，要么保持 LIVE；不能让外部调用跨视图漂移。
                raise WorkspaceConflictError(
                    "active analyst dispatch prevents leaving LIVE"
                )
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
        """追加普通人机协同 Proposal；多 Agent 快照必须走协调器专用持久化入口。"""

        return self._append_proposal(
            fact, expected_workspace_version=expected_workspace_version, allow_multi_agent=False
        )

    def append_multi_agent_proposal(
        self, fact: Proposal, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        """仅由 Phase 16 Coordinator 调用，追加已完成 Planner 全量验证的多 Agent Proposal。"""

        return self._append_proposal(
            fact, expected_workspace_version=expected_workspace_version, allow_multi_agent=True
        )

    def _append_proposal(
        self,
        fact: Proposal,
        *,
        expected_workspace_version: int,
        allow_multi_agent: bool,
    ) -> LiveSessionWorkspace:
        """在唯一 Store 边界区分通用 Proposal 与 Coordinator 已验证的多 Agent 事实。"""

        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            validated = Proposal.model_validate(fact.model_dump(mode="json"))
            proposal_view = _multi_agent_proposal_snapshot(validated)
            if proposal_view is not None and not allow_multi_agent:
                # D-152：通用 HTTP/工作台创建和错误装配都不能借幂等重放写入或复用
                # MULTI_AGENT 方案；只有 Coordinator 在 Planner/Validator 后可调用专用入口。
                raise WorkspaceConflictError(
                    "multi-agent proposal requires coordinator persistence"
                )
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
            escalation_matches = [
                item
                for item in self._escalations.values()
                if item.live_session_id == validated.live_session_id
                and item.evidence_bundle_id == validated.evidence_bundle_id
            ]
            analysis_matches = [
                item
                for item in self._analyses.values()
                if item.live_session_id == validated.live_session_id
                and item.evidence_bundle_id == validated.evidence_bundle_id
            ]
            if proposal_view is not None:
                if len(escalation_matches) != 1 or len(analysis_matches) != 1:
                    raise WorkspaceConflictError(
                        "multi-agent proposal parent facts are invalid"
                    )
                _validate_multi_agent_proposal(
                    fact=validated,
                    evidence=evidence,
                    escalation=escalation_matches[0],
                    analysis=analysis_matches[0],
                    workspace=self.get_workspace(validated.live_session_id),
                    now=self._normalize_now(self._clock()),
                )
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

    def append_escalation(
        self,
        fact: EscalationRecord,
        *,
        expected_workspace_version: int,
        operator_id: str | None = None,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> LiveSessionWorkspace:
        """追加单一 Bundle 的升级事实，运营请求必须绑定当前 lease epoch。"""

        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            validated = EscalationRecord.model_validate(fact.model_dump(mode="json"))
            replay = self._replay_workspace("escalation", validated)
            if replay is not None:
                return replay
            evidence = self._evidence_bundles.get(validated.evidence_bundle_id)
            if (
                evidence is None
                or evidence.live_session_id != validated.live_session_id
                or evidence.incident_id != validated.incident_id
                or _bundle_digest(evidence) != validated.evidence_bundle_digest
            ):
                raise WorkspaceConflictError("escalation bundle parent is invalid")
            _require_escalation_trigger_policy(
                fact=validated,
                evidence=evidence,
                now=self._normalize_now(now),
            )
            workspace = self.get_workspace(validated.live_session_id)
            if workspace.view is not WorkspaceView.LIVE:
                raise WorkspaceConflictError("escalation requires Workspace LIVE view")
            if validated.mode is EscalationMode.OPERATOR_REQUESTED:
                if operator_id != validated.operator_id or fencing_token is None:
                    raise WorkspaceLeaseError("operator escalation requires current lease")
                self._require_lease(
                    validated.live_session_id,
                    operator_id,
                    fencing_token,
                    self._normalize_now(now),
                )
            elif operator_id is not None or fencing_token is not None:
                raise WorkspaceLeaseError("automatic escalation cannot carry operator lease")
            if any(
                item.live_session_id == validated.live_session_id
                and item.evidence_bundle_id == validated.evidence_bundle_id
                for item in self._escalations.values()
            ):
                raise WorkspaceConflictError("bundle already has an escalation")
            return self._append(
                "escalation",
                validated,
                validated.escalation_id,
                self._escalations,
                expected_workspace_version,
            )

    def append_conflict_analysis(
        self, fact: ConflictAnalysis, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        """追加精确 Analyst Profile 的中间事实，不允许跨升级或跨 Bundle 拼接。"""

        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            validated = ConflictAnalysis.model_validate(fact.model_dump(mode="json"))
            replay = self._replay_workspace("conflict_analysis", validated)
            if replay is not None:
                return replay
            escalation = self._escalations.get(validated.escalation_id)
            evidence = self._evidence_bundles.get(validated.evidence_bundle_id)
            if (
                escalation is None
                or evidence is None
                or escalation.live_session_id != validated.live_session_id
                or escalation.incident_id != validated.incident_id
                or escalation.evidence_bundle_id != validated.evidence_bundle_id
                or escalation.evidence_bundle_digest != validated.evidence_bundle_digest
                or _bundle_digest(evidence) != validated.evidence_bundle_digest
            ):
                raise WorkspaceConflictError("analysis escalation parent is invalid")
            expected_refs = tuple(
                component.reference
                for component in EvidenceBundleSnapshot.model_validate(evidence.snapshot).components
            )
            if validated.evidence_refs != expected_refs:
                raise WorkspaceConflictError("analysis evidence refs do not match bundle")
            if any(item.escalation_id == validated.escalation_id for item in self._analyses.values()):
                raise WorkspaceConflictError("escalation already has an analysis")
            if any(item.escalation_id == validated.escalation_id for item in self._outcomes.values()):
                raise WorkspaceConflictError("terminal outcome prevents later analysis")
            if validated.finding_codes != escalation.trigger_codes:
                raise WorkspaceConflictError("analysis finding codes do not match escalation triggers")
            return self._append(
                "conflict_analysis",
                validated,
                validated.analysis_id,
                self._analyses,
                expected_workspace_version,
            )

    def claim_analyst_dispatch(
        self,
        *,
        escalation_id: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[AnalystDispatchClaim, bool, bool]:
        """原子记录 Analyst 单次发送意图，重复调用只返回原 claim 而不重发模型。"""

        if lease_seconds != 2:
            raise ValueError("lease_seconds must be exactly 2 for analyst dispatch")
        with self._lock:
            escalation = self._escalations.get(escalation_id)
            if escalation is None:
                raise WorkspaceConflictError("dispatch claim escalation parent is invalid")
            # D-146 固定由 Store 自己的时钟生成两秒窗口。保留 now 参数仅为
            # 接口兼容，不能让上游 Coordinator 伪造未来时间延长 pending claim。
            instant = self._normalize_now(self._clock())
            existing = self._analyst_dispatch_claims.get(escalation_id)
            if existing is not None:
                # 不在 Store 内抛出摘要冲突：Coordinator 会把不属于当前冻结任务的
                # 直接写 claim 归一为安全降级；这样它不能永久阻断恢复或泄漏内部异常。
                return existing, False, instant < existing.lease_until
            workspace = self._workspaces[escalation.live_session_id]
            bundle = self._evidence_bundles.get(escalation.evidence_bundle_id)
            if workspace.view is not WorkspaceView.LIVE:
                raise WorkspaceConflictError("dispatch workspace is not LIVE")
            if bundle is None:
                raise WorkspaceConflictError("dispatch claim evidence bundle is invalid")
            try:
                snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
            except Exception as exc:
                raise WorkspaceConflictError(
                    "dispatch claim evidence bundle is invalid"
                ) from exc
            if (
                not snapshot.proposal_eligible
                or snapshot.bundle_digest != escalation.evidence_bundle_digest
                or instant + timedelta(seconds=lease_seconds) >= snapshot.valid_until
            ):
                # claim 的两秒有效期覆盖整个外部 Analyst 等待窗口。若剩余 freshness
                # 不足，则宁可不发送并由上层降级，也不能在 Evidence 过期后才发出请求。
                raise WorkspaceConflictError(
                    "dispatch claim evidence bundle is not fresh for analyst window"
                )
            claim = AnalystDispatchClaim(
                escalation_id=escalation_id,
                live_session_id=escalation.live_session_id,
                task_digest=task_digest,
                created_at=instant,
                lease_until=instant + timedelta(seconds=lease_seconds),
            )
            self._analyst_dispatch_claims[escalation_id] = claim
            return claim, True, True

    def get_analyst_dispatch_claim(self, escalation_id: str) -> AnalystDispatchClaim | None:
        """读取已发送或待观察的单次 claim，恢复逻辑不得从进程内状态猜测。"""

        with self._lock:
            return self._analyst_dispatch_claims.get(escalation_id)

    def get_analyst_dispatch_remaining_seconds(self, escalation_id: str) -> float:
        """仅用 Store 权威时钟计算 Analyst claim 的剩余发送窗口，禁止调用方传入墙钟。"""

        with self._lock:
            claim = self._analyst_dispatch_claims.get(escalation_id)
            if claim is None:
                raise WorkspaceConflictError("dispatch claim is missing")
            instant = self._normalize_now(self._clock())
            # Coordinator 可能运行在时间漂移节点；它只能消费本 Store 返回的短暂预算，
            # 不能把持久化 lease_until 与本地业务时钟相减而把两秒观察窗错误拉长。
            return max(0.0, (claim.lease_until - instant).total_seconds())

    def claim_planner_dispatch(
        self,
        *,
        escalation_id: str,
        analysis_id: str,
        analysis_digest: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[PlannerDispatchClaim, bool, bool]:
        """原子记录绑定 Analysis 的 Planner 单次发送意图，禁止并发或重启重复调用模型。"""

        if lease_seconds != 2:
            raise ValueError("lease_seconds must be exactly 2 for planner dispatch")
        with self._lock:
            escalation = self._escalations.get(escalation_id)
            analysis = self._analyses.get(analysis_id)
            if (
                escalation is None
                or analysis is None
                or analysis.escalation_id != escalation_id
                or analysis.analysis_digest != analysis_digest
                or analysis.live_session_id != escalation.live_session_id
            ):
                raise WorkspaceConflictError("planner dispatch analysis parent is invalid")
            # Store 自身的受控时钟是唯一租约来源。now 仅保留与 Analyst API 对称的调用
            # 形状，不能让 Coordinator 借时间漂移延长已经持久化的发送观察窗口。
            instant = self._normalize_now(self._clock())
            existing = self._planner_dispatch_claims.get(escalation_id)
            if existing is not None:
                if (
                    existing.analysis_id != analysis_id
                    or existing.analysis_digest != analysis_digest
                    or existing.task_digest != task_digest
                ):
                    raise WorkspaceConflictError("planner dispatch claim identity is invalid")
                return existing, False, instant < existing.lease_until
            workspace = self._workspaces[escalation.live_session_id]
            bundle = self._evidence_bundles.get(escalation.evidence_bundle_id)
            if workspace.view is not WorkspaceView.LIVE:
                raise WorkspaceConflictError("planner dispatch workspace is not LIVE")
            if bundle is None:
                raise WorkspaceConflictError("planner dispatch evidence bundle is invalid")
            try:
                snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
            except Exception as exc:
                raise WorkspaceConflictError(
                    "planner dispatch evidence bundle is invalid"
                ) from exc
            if (
                not snapshot.proposal_eligible
                or snapshot.bundle_digest != escalation.evidence_bundle_digest
                or instant + timedelta(seconds=lease_seconds) >= snapshot.valid_until
            ):
                raise WorkspaceConflictError(
                    "planner dispatch evidence bundle is not fresh for planner window"
                )
            claim = PlannerDispatchClaim(
                escalation_id=escalation_id,
                live_session_id=escalation.live_session_id,
                analysis_id=analysis_id,
                analysis_digest=analysis_digest,
                task_digest=task_digest,
                created_at=instant,
                lease_until=instant + timedelta(seconds=lease_seconds),
            )
            self._planner_dispatch_claims[escalation_id] = claim
            return claim, True, True

    def get_planner_dispatch_claim(self, escalation_id: str) -> PlannerDispatchClaim | None:
        """读取 Planner 的持久化发送意图，恢复逻辑只能依据该事实判断 pending 或降级。"""

        with self._lock:
            return self._planner_dispatch_claims.get(escalation_id)

    def get_planner_dispatch_remaining_seconds(self, escalation_id: str) -> float:
        """只以 Store 权威时钟返回 Planner claim 剩余时间，调用方不得自行重算 lease。"""

        with self._lock:
            claim = self._planner_dispatch_claims.get(escalation_id)
            if claim is None:
                raise WorkspaceConflictError("planner dispatch claim is missing")
            instant = self._normalize_now(self._clock())
            return max(0.0, (claim.lease_until - instant).total_seconds())

    def append_multi_agent_outcome(
        self, fact: MultiAgentOutcome, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        """追加每次升级唯一的完整或降级终态，并只读取既有父事实。"""

        self._require_control_integer(expected_workspace_version, "expected_version")
        with self._lock:
            validated = MultiAgentOutcome.model_validate(fact.model_dump(mode="json"))
            replay = self._replay_workspace("multi_agent_outcome", validated)
            if replay is not None:
                return replay
            workspace = self.get_workspace(validated.live_session_id)
            has_analysis = any(
                item.escalation_id == validated.escalation_id
                for item in self._analyses.values()
            )
            if (
                workspace.view is WorkspaceView.REVIEW
                and validated.status is MultiAgentOutcomeStatus.DEGRADED
                and validated.analysis_id is None
                and validated.analysis_digest is None
                and validated.proposal_id is None
                and validated.proposal_digest is None
                and validated.failure_code is not MultiAgentFailureCode.COORDINATOR_TIMEOUT
            ):
                # D-151：播后无父链终态只能记录外部请求已发送但响应状态未知的
                # Coordinator 超时。可明确分类的模型、校验或持久化失败必须在 LIVE
                # 内形成带父链事实，不能利用 REVIEW 例外伪装为未知响应。
                raise WorkspaceConflictError(
                    "review degraded closure requires coordinator timeout"
                )
            review_terminalization_allowed = (
                workspace.view is WorkspaceView.REVIEW
                and validated.status is MultiAgentOutcomeStatus.DEGRADED
                and validated.analysis_id is None
                and validated.analysis_digest is None
                and validated.proposal_id is None
                and validated.proposal_digest is None
                and (
                    # 只有未形成 Analysis 的 Analyst 请求或已发送 Planner 的第二段请求
                    # 可以在 REVIEW 留下无父链审计终态。成功 Analysis 存在时不能仅凭
                    # 历史 Analyst claim 伪造失败，必须证明 Planner 已经离开进程。
                    any(
                        claim.escalation_id == validated.escalation_id
                        for claim in self._planner_dispatch_claims.values()
                    )
                    or (
                        not has_analysis
                        and any(
                            claim.escalation_id == validated.escalation_id
                            for claim in self._analyst_dispatch_claims.values()
                        )
                    )
                )
            )
            if (
                workspace.view is WorkspaceView.REVIEW
                and validated.status is MultiAgentOutcomeStatus.DEGRADED
                and (validated.analysis_id is not None or validated.analysis_digest is not None)
            ):
                # 已发送请求在 REVIEW 后只允许不携带中间/方案父链的失败审计闭合。
                # 无论来源是 Analyst 还是 Planner，带 Analysis 的降级都会把 LIVE 内
                # 的模型后续链路错误扩展到播后视图，必须保持 fail-closed。
                raise WorkspaceConflictError(
                    "review degraded closure cannot carry analysis"
                )
            if workspace.view is not WorkspaceView.LIVE and not review_terminalization_allowed:
                # `REVIEW` 只保留已发送 Analyst 的失败审计闭合，不能借播后视图新增
                # 分析、方案或任意无 claim 的终态。内存 Store 必须和 PostgreSQL CAS
                # trigger 采用相同边界，保证重启前后的恢复语义一致。
                raise WorkspaceConflictError(
                    "degraded outcome requires dispatch claim after LIVE"
                )
            escalation = self._escalations.get(validated.escalation_id)
            if (
                escalation is None
                or escalation.live_session_id != validated.live_session_id
                or escalation.incident_id != validated.incident_id
                or escalation.escalation_digest != validated.escalation_digest
                or escalation.evidence_bundle_id != validated.evidence_bundle_id
                or escalation.evidence_bundle_digest != validated.evidence_bundle_digest
            ):
                raise WorkspaceConflictError("outcome escalation parent is invalid")
            if validated.analysis_id is not None:
                analysis = self._analyses.get(validated.analysis_id)
                if (
                    analysis is None
                    or analysis.escalation_id != validated.escalation_id
                    or analysis.analysis_digest != validated.analysis_digest
                ):
                    raise WorkspaceConflictError("outcome analysis parent is invalid")
            if validated.proposal_id is not None:
                proposal_fact = self._proposals.get(validated.proposal_id)
                if proposal_fact is None:
                    raise WorkspaceConflictError("outcome proposal parent is invalid")
                try:
                    proposal = _multi_agent_proposal_snapshot(proposal_fact)
                    if proposal is None:
                        raise ValueError("proposal is not multi-agent")
                    proposal_digest = canonical_json_sha256(
                        proposal.model_dump(mode="json")
                    )
                except Exception as exc:
                    raise WorkspaceConflictError(
                        "outcome proposal snapshot is invalid"
                    ) from exc
                if (
                    validated.status is not MultiAgentOutcomeStatus.READY
                    or proposal.proposal_origin is not ProposalOrigin.MULTI_AGENT
                    or proposal.status is not ProposalStatus.READY
                    or proposal_fact.live_session_id != validated.live_session_id
                    or proposal_fact.incident_id != validated.incident_id
                    or proposal_fact.evidence_bundle_id != validated.evidence_bundle_id
                    or validated.proposal_digest != proposal_digest
                    or proposal.multi_agent_lineage is None
                    or proposal.multi_agent_lineage.escalation_id
                    != validated.escalation_id
                    or proposal.multi_agent_lineage.escalation_digest
                    != validated.escalation_digest
                    or proposal.multi_agent_lineage.analysis_id != validated.analysis_id
                    or proposal.multi_agent_lineage.analysis_digest
                    != validated.analysis_digest
                ):
                    raise WorkspaceConflictError("outcome proposal parent is invalid")
            if (
                validated.status is MultiAgentOutcomeStatus.DEGRADED
                and validated.analysis_id is None
                and not review_terminalization_allowed
                and has_analysis
            ):
                # 无 Analysis 的 DEGRADED 表示 Analyst 未能产出可持久化事实。已有成功
                # Analysis 时再追加该终态会制造相互矛盾的审计链，必须在内存实现中与
                # PostgreSQL 触发器保持同样的 fail-closed 语义。
                raise WorkspaceConflictError(
                    "analysis prevents unlinked degraded outcome"
                )
            if any(item.escalation_id == validated.escalation_id for item in self._outcomes.values()):
                raise WorkspaceConflictError("escalation already has an outcome")
            return self._append(
                "multi_agent_outcome",
                validated,
                validated.outcome_id,
                self._outcomes,
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

    def get_escalation(self, fact_id: str) -> EscalationRecord:
        """按稳定身份读取不可变升级事实。"""

        return self._get_fact(self._escalations, fact_id, "escalation")

    def get_conflict_analysis(self, fact_id: str) -> ConflictAnalysis:
        """按稳定身份读取不可变 Analyst 中间事实。"""

        return self._get_fact(self._analyses, fact_id, "conflict analysis")

    def get_multi_agent_outcome(self, fact_id: str) -> MultiAgentOutcome:
        """按稳定身份读取不可变双 Agent 终态。"""

        return self._get_fact(self._outcomes, fact_id, "multi-agent outcome")

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

    def list_escalations(self, live_session_id: str) -> tuple[EscalationRecord, ...]:
        """按创建时间与稳定 ID 返回同一直播的全部升级事实。"""

        return self._list_facts(self._escalations, live_session_id)

    def list_conflict_analyses(
        self, live_session_id: str
    ) -> tuple[ConflictAnalysis, ...]:
        """按创建时间与稳定 ID 返回同一直播的 Analyst 中间事实。"""

        return self._list_facts(self._analyses, live_session_id)

    def list_multi_agent_outcomes(
        self, live_session_id: str
    ) -> tuple[MultiAgentOutcome, ...]:
        """按创建时间与稳定 ID 返回同一直播的双 Agent 终态。"""

        return self._list_facts(self._outcomes, live_session_id)

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
            "escalation_id",
            "analysis_id",
            "outcome_id",
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

    def get_workspace_by_root_plan(self, root_plan_run_id: str) -> LiveSessionWorkspace:
        """按数据库中的 root PlanRun 反查唯一 Workspace，避免调用方自报 room。"""

        if not root_plan_run_id:
            raise ValueError("root_plan_run_id must not be empty")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM phase14_live_session_workspaces
                       WHERE root_plan_run_id=%s
                       ORDER BY live_session_id""",
                    (root_plan_run_id,),
                )
                rows = cur.fetchall()
        if len(rows) != 1:
            raise WorkspaceNotFoundError("root PlanRun does not identify one workspace")
        return self._workspace_from_row(rows[0])

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

    def append_escalation(
        self,
        fact: EscalationRecord,
        *,
        expected_workspace_version: int,
        operator_id: str | None = None,
        fencing_token: int | None = None,
    ) -> LiveSessionWorkspace:
        """持久化单 Bundle 升级，并仅为人工请求消费当前 Workspace lease。"""

        validated = EscalationRecord.model_validate(fact.model_dump(mode="json"))
        if validated.mode is EscalationMode.OPERATOR_REQUESTED:
            if operator_id != validated.operator_id or fencing_token is None:
                raise WorkspaceLeaseError("operator escalation requires current lease")
            lease: tuple[str, int] | None = (operator_id, fencing_token)
        else:
            if operator_id is not None or fencing_token is not None:
                raise WorkspaceLeaseError("automatic escalation cannot carry operator lease")
            lease = None

        def validate_parent(cur: Any) -> None:
            # 根行已由 _append_fact 锁定；这里仍显式读取 Bundle 与 Workspace，确保
            # 自动升级不会把 PREPARE/REVIEW 或摘要被替换的证据送往 Agent 链路。
            cur.execute(
                """SELECT evidence.live_session_id,evidence.incident_id,evidence.payload,
                          workspace.current_view
                   FROM phase14_evidence_bundles evidence
                   JOIN phase14_live_session_workspaces workspace
                     ON workspace.live_session_id=evidence.live_session_id
                   WHERE evidence.evidence_bundle_id=%s""",
                (validated.evidence_bundle_id,),
            )
            row = cur.fetchone()
            if (
                row is None
                or row["live_session_id"] != validated.live_session_id
                or row["incident_id"] != validated.incident_id
                or row["current_view"] != WorkspaceView.LIVE.value
                or _bundle_digest(EvidenceBundle.model_validate(dict(row["payload"])))
                != validated.evidence_bundle_digest
            ):
                raise WorkspaceConflictError("escalation bundle parent is invalid")
            _require_escalation_trigger_policy(
                fact=validated,
                evidence=EvidenceBundle.model_validate(dict(row["payload"])),
            )
            cur.execute(
                """SELECT 1 FROM phase16_escalations
                   WHERE live_session_id=%s AND evidence_bundle_id=%s""",
                (validated.live_session_id, validated.evidence_bundle_id),
            )
            if cur.fetchone() is not None:
                raise WorkspaceConflictError("bundle already has an escalation")

        return self._append_fact(
            fact_kind="escalation",
            fact_id=validated.escalation_id,
            fact=validated,
            table="phase16_escalations",
            id_column="escalation_id",
            extra_columns={
                "incident_id": validated.incident_id,
                "evidence_bundle_id": validated.evidence_bundle_id,
                "evidence_bundle_digest": validated.evidence_bundle_digest,
                "mode": validated.mode.value,
                "operator_id": validated.operator_id,
                "fencing_token": fencing_token,
                "expected_workspace_version": expected_workspace_version,
            },
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
            lease=lease,
            workspace_version_advanced_by_trigger=True,
        )

    def append_conflict_analysis(
        self, fact: ConflictAnalysis, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        """持久化精确 Analyst 事实，并禁止跨升级、跨 Bundle 的证据拼接。"""

        validated = ConflictAnalysis.model_validate(fact.model_dump(mode="json"))

        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT escalation.live_session_id,escalation.incident_id,
                          escalation.evidence_bundle_id,escalation.evidence_bundle_digest,
                          escalation.payload AS escalation_payload,evidence.payload
                   FROM phase16_escalations escalation
                   JOIN phase14_evidence_bundles evidence
                     ON evidence.live_session_id=escalation.live_session_id
                    AND evidence.evidence_bundle_id=escalation.evidence_bundle_id
                   WHERE escalation.escalation_id=%s""",
                (validated.escalation_id,),
            )
            row = cur.fetchone()
            if (
                row is None
                or row["live_session_id"] != validated.live_session_id
                or row["incident_id"] != validated.incident_id
                or row["evidence_bundle_id"] != validated.evidence_bundle_id
                or row["evidence_bundle_digest"] != validated.evidence_bundle_digest
                or _bundle_digest(EvidenceBundle.model_validate(dict(row["payload"])))
                != validated.evidence_bundle_digest
            ):
                raise WorkspaceConflictError("analysis escalation parent is invalid")
            if validated.finding_codes != EscalationRecord.model_validate(
                dict(row["escalation_payload"])
            ).trigger_codes:
                raise WorkspaceConflictError(
                    "analysis finding codes do not match escalation triggers"
                )
            snapshot = EvidenceBundleSnapshot.model_validate(
                dict(row["payload"])["snapshot"]
            )
            expected_refs = tuple(component.reference for component in snapshot.components)
            if validated.evidence_refs != expected_refs:
                raise WorkspaceConflictError("analysis evidence refs do not match bundle")
            cur.execute(
                """SELECT 1 FROM phase16_conflict_analyses
                   WHERE live_session_id=%s AND escalation_id=%s""",
                (validated.live_session_id, validated.escalation_id),
            )
            if cur.fetchone() is not None:
                raise WorkspaceConflictError("escalation already has an analysis")
            cur.execute(
                """SELECT 1 FROM phase16_multi_agent_outcomes
                   WHERE live_session_id=%s AND escalation_id=%s""",
                (validated.live_session_id, validated.escalation_id),
            )
            if cur.fetchone() is not None:
                raise WorkspaceConflictError("terminal outcome prevents later analysis")

        return self._append_fact(
            fact_kind="conflict_analysis",
            fact_id=validated.analysis_id,
            fact=validated,
            table="phase16_conflict_analyses",
            id_column="analysis_id",
            extra_columns={
                "incident_id": validated.incident_id,
                "evidence_bundle_id": validated.evidence_bundle_id,
                "evidence_bundle_digest": validated.evidence_bundle_digest,
                "escalation_id": validated.escalation_id,
                "analyst_profile_id": validated.analyst_profile_id,
                "analyst_profile_version": validated.analyst_profile_version,
                "analyst_profile_digest": validated.analyst_profile_digest,
                "expected_workspace_version": expected_workspace_version,
            },
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
            workspace_version_advanced_by_trigger=True,
            write_context=("phase16.analysis_write", "store"),
        )

    def claim_analyst_dispatch(
        self,
        *,
        escalation_id: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[AnalystDispatchClaim, bool, bool]:
        """以数据库唯一键原子创建单次 Analyst claim；数据库墙钟是唯一过期权威。"""

        if lease_seconds != 2:
            raise ValueError("lease_seconds must be exactly 2 for analyst dispatch")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT live_session_id FROM phase16_escalations
                       WHERE escalation_id=%s""",
                    (escalation_id,),
                )
                parent = cur.fetchone()
                if parent is None:
                    raise WorkspaceConflictError("dispatch claim escalation parent is invalid")
                # 与 LIVE->REVIEW 迁移采用同一根 Workspace 行锁。谁先取得锁谁先线性化：
                # 视图已经结束则 claim 被拒绝；claim 已写入则迁移会观察到短暂的 active
                # 观察窗并拒绝。外部模型调用因此不能跨越生命周期边界。
                workspace = self._lock_workspace(cur, parent["live_session_id"])
                instant = self._database_now(cur)
                cur.execute(
                    """SELECT escalation_id,live_session_id,task_digest,created_at,lease_until
                       FROM phase16_analyst_dispatch_claims
                       WHERE escalation_id=%s FOR UPDATE""",
                    (escalation_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    claim = AnalystDispatchClaim(**dict(existing))
                    conn.commit()
                    return claim, False, instant < claim.lease_until
                if workspace["current_view"] != WorkspaceView.LIVE.value:
                    raise WorkspaceConflictError("dispatch workspace is not LIVE")
                cur.execute(
                    """SELECT payload FROM phase14_evidence_bundles
                       WHERE live_session_id=%s
                         AND evidence_bundle_id=(
                             SELECT evidence_bundle_id FROM phase16_escalations
                              WHERE escalation_id=%s
                         )""",
                    (parent["live_session_id"], escalation_id),
                )
                evidence = cur.fetchone()
                try:
                    snapshot = EvidenceBundleSnapshot.model_validate(
                        dict(evidence["payload"])["snapshot"]
                    )
                except Exception as exc:
                    raise WorkspaceConflictError(
                        "dispatch claim evidence bundle is invalid"
                    ) from exc
                if (
                    not snapshot.proposal_eligible
                    or instant + timedelta(seconds=lease_seconds) >= snapshot.valid_until
                ):
                    raise WorkspaceConflictError(
                        "dispatch claim evidence bundle is not fresh for analyst window"
                    )
                # DDL trigger 也要求该事务显式声明 Store 写入上下文。它是防止同一
                # 可信服务中误用直写 SQL 的完整性门禁，不是对任意同进程代码执行的
                # 安全沙箱；D-121 的进程信任边界仍然成立。
                cur.execute("SELECT set_config('phase16.claim_write','store',true)")
                cur.execute(
                    """INSERT INTO phase16_analyst_dispatch_claims
                       (escalation_id,live_session_id,task_digest,created_at,lease_until)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (escalation_id) DO NOTHING
                       RETURNING escalation_id,live_session_id,task_digest,created_at,lease_until""",
                    (
                        escalation_id,
                        parent["live_session_id"],
                        task_digest,
                        instant,
                        instant + timedelta(seconds=lease_seconds),
                    ),
                )
                created = cur.fetchone()
                assert created is not None
                claim = AnalystDispatchClaim(**dict(created))
                is_new = True
                # 数据库时钟是 PostgreSQL claim 的唯一租约权威。Coordinator 可能在
                # 时钟漂移节点上运行，因此绝不能用本地 clock 复算这条边界。
                is_active = is_new or instant < claim.lease_until
            conn.commit()
        return claim, is_new, is_active

    def get_analyst_dispatch_claim(self, escalation_id: str) -> AnalystDispatchClaim | None:
        """读取 PostgreSQL 追加 claim，用于重启后区分 in-flight 与未知响应。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT escalation_id,live_session_id,task_digest,created_at,lease_until
                       FROM phase16_analyst_dispatch_claims WHERE escalation_id=%s""",
                    (escalation_id,),
                )
                row = cur.fetchone()
        return None if row is None else AnalystDispatchClaim(**dict(row))

    def get_analyst_dispatch_remaining_seconds(self, escalation_id: str) -> float:
        """由 PostgreSQL 事务时钟返回 claim 剩余秒数，Worker 的本地墙钟不参与租约判断。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT GREATEST(
                           0,
                           EXTRACT(EPOCH FROM lease_until - clock_timestamp())
                       ) AS remaining_seconds
                       FROM phase16_analyst_dispatch_claims
                       WHERE escalation_id=%s""",
                    (escalation_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise WorkspaceConflictError("dispatch claim is missing")
        # PostgreSQL numeric 可能映射为 Decimal；在端口边界归一成 float，并由 Coordinator
        # 再次限制到冻结 Profile 的两秒上限，防止错误替身或损坏行放大外部等待窗口。
        return float(row["remaining_seconds"])

    def claim_planner_dispatch(
        self,
        *,
        escalation_id: str,
        analysis_id: str,
        analysis_digest: str,
        task_digest: str,
        now: datetime | None = None,
        lease_seconds: int = 2,
    ) -> tuple[PlannerDispatchClaim, bool, bool]:
        """以数据库唯一键创建绑定 Analysis 的 Planner 单次 claim，数据库墙钟是唯一过期权威。"""

        if lease_seconds != 2:
            raise ValueError("lease_seconds must be exactly 2 for planner dispatch")
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT live_session_id,incident_id,evidence_bundle_id
                       FROM phase16_escalations WHERE escalation_id=%s""",
                    (escalation_id,),
                )
                escalation = cur.fetchone()
                if escalation is None:
                    raise WorkspaceConflictError("planner dispatch escalation parent is invalid")
                # 与 LIVE->REVIEW 使用同一根行锁，保证 Planner claim 的线性化点不可能
                # 落在生命周期切换之后。锁内再读取 Analysis，避免跨连接替换父事实。
                workspace = self._lock_workspace(cur, escalation["live_session_id"])
                instant = self._database_now(cur)
                cur.execute(
                    """SELECT live_session_id,incident_id,evidence_bundle_id,
                              escalation_id,payload
                       FROM phase16_conflict_analyses WHERE analysis_id=%s""",
                    (analysis_id,),
                )
                analysis = cur.fetchone()
                if (
                    analysis is None
                    or analysis["live_session_id"] != escalation["live_session_id"]
                    or analysis["incident_id"] != escalation["incident_id"]
                    or analysis["evidence_bundle_id"] != escalation["evidence_bundle_id"]
                    or analysis["escalation_id"] != escalation_id
                    or dict(analysis["payload"])["analysis_digest"] != analysis_digest
                ):
                    raise WorkspaceConflictError("planner dispatch analysis parent is invalid")
                cur.execute(
                    """SELECT escalation_id,live_session_id,analysis_id,analysis_digest,
                              task_digest,created_at,lease_until
                       FROM phase16_planner_dispatch_claims
                       WHERE escalation_id=%s FOR UPDATE""",
                    (escalation_id,),
                )
                existing = cur.fetchone()
                if existing is not None:
                    claim = PlannerDispatchClaim(**dict(existing))
                    if (
                        claim.analysis_id != analysis_id
                        or claim.analysis_digest != analysis_digest
                        or claim.task_digest != task_digest
                    ):
                        raise WorkspaceConflictError("planner dispatch claim identity is invalid")
                    conn.commit()
                    return claim, False, instant < claim.lease_until
                if workspace["current_view"] != WorkspaceView.LIVE.value:
                    raise WorkspaceConflictError("planner dispatch workspace is not LIVE")
                cur.execute(
                    """SELECT payload FROM phase14_evidence_bundles
                       WHERE live_session_id=%s AND evidence_bundle_id=%s""",
                    (escalation["live_session_id"], escalation["evidence_bundle_id"]),
                )
                evidence = cur.fetchone()
                try:
                    snapshot = EvidenceBundleSnapshot.model_validate(
                        dict(evidence["payload"])["snapshot"]
                    )
                except Exception as exc:
                    raise WorkspaceConflictError(
                        "planner dispatch evidence bundle is invalid"
                    ) from exc
                if (
                    not snapshot.proposal_eligible
                    or instant + timedelta(seconds=lease_seconds) >= snapshot.valid_until
                ):
                    raise WorkspaceConflictError(
                        "planner dispatch evidence bundle is not fresh for planner window"
                    )
                # 该本地事务标记与 DDL trigger 共同防止可信服务代码意外绕过 Store 的
                # Analysis/task 绑定；它遵守 D-121，不把同进程上下文伪装成沙箱。
                cur.execute("SELECT set_config('phase16.planner_claim_write','store',true)")
                cur.execute(
                    """INSERT INTO phase16_planner_dispatch_claims
                       (escalation_id,live_session_id,analysis_id,analysis_digest,
                        task_digest,created_at,lease_until)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (escalation_id) DO NOTHING
                       RETURNING escalation_id,live_session_id,analysis_id,analysis_digest,
                                 task_digest,created_at,lease_until""",
                    (
                        escalation_id,
                        escalation["live_session_id"],
                        analysis_id,
                        analysis_digest,
                        task_digest,
                        instant,
                        instant + timedelta(seconds=lease_seconds),
                    ),
                )
                created = cur.fetchone()
                assert created is not None
                claim = PlannerDispatchClaim(**dict(created))
            conn.commit()
        return claim, True, True

    def get_planner_dispatch_claim(self, escalation_id: str) -> PlannerDispatchClaim | None:
        """读取 PostgreSQL Planner claim，用于重启恢复时阻断任何第二次外部发送。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT escalation_id,live_session_id,analysis_id,analysis_digest,
                              task_digest,created_at,lease_until
                       FROM phase16_planner_dispatch_claims WHERE escalation_id=%s""",
                    (escalation_id,),
                )
                row = cur.fetchone()
        return None if row is None else PlannerDispatchClaim(**dict(row))

    def get_planner_dispatch_remaining_seconds(self, escalation_id: str) -> float:
        """由 PostgreSQL `clock_timestamp()` 计算 Planner 剩余观察窗口，禁止 Worker 墙钟参与。"""

        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT GREATEST(0, EXTRACT(EPOCH FROM lease_until-clock_timestamp()))
                       AS remaining_seconds
                       FROM phase16_planner_dispatch_claims WHERE escalation_id=%s""",
                    (escalation_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise WorkspaceConflictError("planner dispatch claim is missing")
        return float(row["remaining_seconds"])

    def append_multi_agent_outcome(
        self, fact: MultiAgentOutcome, *, expected_workspace_version: int
    ) -> LiveSessionWorkspace:
        """持久化每次升级唯一的 READY 或 DEGRADED 终态，且不创建隐式恢复动作。"""

        validated = MultiAgentOutcome.model_validate(fact.model_dump(mode="json"))
        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT live_session_id,incident_id,evidence_bundle_id,
                          evidence_bundle_digest,payload
                   FROM phase16_escalations WHERE escalation_id=%s""",
                (validated.escalation_id,),
            )
            escalation = cur.fetchone()
            if (
                escalation is None
                or escalation["live_session_id"] != validated.live_session_id
                or escalation["incident_id"] != validated.incident_id
                or escalation["evidence_bundle_id"] != validated.evidence_bundle_id
                or escalation["evidence_bundle_digest"] != validated.evidence_bundle_digest
                or dict(escalation["payload"])["escalation_digest"]
                != validated.escalation_digest
            ):
                raise WorkspaceConflictError("outcome escalation parent is invalid")
            if validated.analysis_id is not None:
                cur.execute(
                    """SELECT live_session_id,incident_id,evidence_bundle_id,escalation_id,payload
                       FROM phase16_conflict_analyses WHERE analysis_id=%s""",
                    (validated.analysis_id,),
                )
                analysis = cur.fetchone()
                if (
                    analysis is None
                    or analysis["live_session_id"] != validated.live_session_id
                    or analysis["incident_id"] != validated.incident_id
                    or analysis["evidence_bundle_id"] != validated.evidence_bundle_id
                    or analysis["escalation_id"] != validated.escalation_id
                    or dict(analysis["payload"])["analysis_digest"]
                    != validated.analysis_digest
                ):
                    raise WorkspaceConflictError("outcome analysis parent is invalid")
            if (
                validated.status is MultiAgentOutcomeStatus.DEGRADED
                and validated.analysis_id is None
            ):
                cur.execute(
                    """SELECT current_view FROM phase14_live_session_workspaces
                       WHERE live_session_id=%s""",
                    (validated.live_session_id,),
                )
                workspace = cur.fetchone()
                if (
                    workspace is not None
                    and workspace["current_view"] == WorkspaceView.REVIEW.value
                    and validated.failure_code
                    is not MultiAgentFailureCode.COORDINATOR_TIMEOUT
                ):
                    # D-151：Store 必须在触发器前以与内存 Store 相同的公开错误拒绝
                    # 非超时码的播后无父链闭合；触发器仍保留同一约束以阻断直写旁路。
                    raise WorkspaceConflictError(
                        "review degraded closure requires coordinator timeout"
                    )
                cur.execute(
                    """SELECT 1 FROM phase16_conflict_analyses
                       WHERE live_session_id=%s AND escalation_id=%s""",
                    (validated.live_session_id, validated.escalation_id),
                )
                has_analysis = cur.fetchone() is not None
                if has_analysis:
                    # REVIEW 下只有已经发送过 Planner 的第二段请求可用无父链降级闭合。
                    # 普通成功 Analysis 不能仅凭历史 Analyst claim 被伪造成失败终态。
                    cur.execute(
                        """SELECT 1 FROM phase16_planner_dispatch_claims
                           WHERE live_session_id=%s AND escalation_id=%s""",
                        (validated.live_session_id, validated.escalation_id),
                    )
                    has_planner_claim = cur.fetchone() is not None
                    if (
                        workspace is None
                        or workspace["current_view"] != WorkspaceView.REVIEW.value
                        or not has_planner_claim
                    ):
                        raise WorkspaceConflictError(
                            "analysis prevents unlinked degraded outcome"
                        )
            if validated.proposal_id is not None:
                cur.execute(
                    """SELECT incident_id,evidence_bundle_id,payload
                       FROM phase14_proposals
                       WHERE live_session_id=%s AND proposal_id=%s""",
                    (validated.live_session_id, validated.proposal_id),
                )
                proposal_fact = cur.fetchone()
                if proposal_fact is None:
                    raise WorkspaceConflictError("outcome proposal parent is invalid")
                try:
                    proposal = LiveDecisionProposal.model_validate(
                        dict(proposal_fact["payload"])["snapshot"]
                    )
                    proposal_digest = canonical_json_sha256(
                        proposal.model_dump(mode="json")
                    )
                except Exception as exc:
                    raise WorkspaceConflictError(
                        "outcome proposal snapshot is invalid"
                    ) from exc
                if (
                    validated.status is not MultiAgentOutcomeStatus.READY
                    or proposal.proposal_origin is not ProposalOrigin.MULTI_AGENT
                    or proposal.status is not ProposalStatus.READY
                    or proposal_fact["incident_id"] != validated.incident_id
                    or proposal_fact["evidence_bundle_id"]
                    != validated.evidence_bundle_id
                    or validated.proposal_digest != proposal_digest
                    or proposal.multi_agent_lineage is None
                    or proposal.multi_agent_lineage.escalation_id
                    != validated.escalation_id
                    or proposal.multi_agent_lineage.escalation_digest
                    != validated.escalation_digest
                    or proposal.multi_agent_lineage.analysis_id != validated.analysis_id
                    or proposal.multi_agent_lineage.analysis_digest
                    != validated.analysis_digest
                ):
                    raise WorkspaceConflictError("outcome proposal parent is invalid")
            cur.execute(
                """SELECT 1 FROM phase16_multi_agent_outcomes
                   WHERE live_session_id=%s AND escalation_id=%s""",
                (validated.live_session_id, validated.escalation_id),
            )
            if cur.fetchone() is not None:
                raise WorkspaceConflictError("escalation already has an outcome")

        return self._append_fact(
            fact_kind="multi_agent_outcome",
            fact_id=validated.outcome_id,
            fact=validated,
            table="phase16_multi_agent_outcomes",
            id_column="outcome_id",
            extra_columns={
                "incident_id": validated.incident_id,
                "evidence_bundle_id": validated.evidence_bundle_id,
                "evidence_bundle_digest": validated.evidence_bundle_digest,
                "escalation_id": validated.escalation_id,
                "escalation_digest": validated.escalation_digest,
                "analysis_id": validated.analysis_id,
                "analysis_digest": validated.analysis_digest,
                "proposal_id": validated.proposal_id,
                "proposal_digest": validated.proposal_digest,
                "status": validated.status.value,
                "expected_workspace_version": expected_workspace_version,
            },
            expected_workspace_version=expected_workspace_version,
            validate_parent=validate_parent,
            workspace_version_advanced_by_trigger=True,
            write_context=("phase16.outcome_write", "store"),
        )

    def append_proposal(
        self,
        fact: Proposal,
        *,
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """追加普通 Proposal；多 Agent 快照只能由 Coordinator 使用专用写入上下文持久化。"""

        return self._append_proposal(
            fact, expected_workspace_version=expected_workspace_version, allow_multi_agent=False
        )

    def append_multi_agent_proposal(
        self,
        fact: Proposal,
        *,
        expected_workspace_version: int,
    ) -> LiveSessionWorkspace:
        """以独立 PostgreSQL 写入上下文追加完整验证过的多 Agent Proposal。"""

        return self._append_proposal(
            fact, expected_workspace_version=expected_workspace_version, allow_multi_agent=True
        )

    def _append_proposal(
        self,
        fact: Proposal,
        *,
        expected_workspace_version: int,
        allow_multi_agent: bool,
    ) -> LiveSessionWorkspace:
        """保持 Proposal 父链校验，同时把多 Agent 来源收束到唯一 Coordinator 入口。"""

        validated = Proposal.model_validate(fact.model_dump(mode="json"))
        multi_agent_proposal = _multi_agent_proposal_snapshot(validated) is not None
        if multi_agent_proposal and not allow_multi_agent:
            raise WorkspaceConflictError(
                "multi-agent proposal requires coordinator persistence"
            )

        def validate_parent(cur: Any) -> None:
            cur.execute(
                """SELECT e.live_session_id, e.incident_id, e.payload
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
            proposal_view = _multi_agent_proposal_snapshot(validated)
            if proposal_view is not None:
                cur.execute(
                    """SELECT payload FROM phase16_escalations
                       WHERE live_session_id=%s AND evidence_bundle_id=%s""",
                    (validated.live_session_id, validated.evidence_bundle_id),
                )
                escalation_rows = cur.fetchall()
                cur.execute(
                    """SELECT payload FROM phase16_conflict_analyses
                       WHERE live_session_id=%s AND evidence_bundle_id=%s""",
                    (validated.live_session_id, validated.evidence_bundle_id),
                )
                analysis_rows = cur.fetchall()
                if len(escalation_rows) != 1 or len(analysis_rows) != 1:
                    raise WorkspaceConflictError(
                        "multi-agent proposal parent facts are invalid"
                    )
                cur.execute(
                    """SELECT * FROM phase14_live_session_workspaces
                       WHERE live_session_id=%s""",
                    (validated.live_session_id,),
                )
                workspace_row = cur.fetchone()
                assert workspace_row is not None
                _validate_multi_agent_proposal(
                    fact=validated,
                    evidence=EvidenceBundle.model_validate(dict(evidence["payload"])),
                    escalation=EscalationRecord.model_validate(
                        dict(escalation_rows[0]["payload"])
                    ),
                    analysis=ConflictAnalysis.model_validate(
                        dict(analysis_rows[0]["payload"])
                    ),
                    workspace=self._workspace_from_row(workspace_row),
                    now=self._database_now(cur),
                )
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
            write_context=("phase16.multi_agent_proposal_write", "coordinator")
            if multi_agent_proposal
            else None,
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

    def get_escalation(self, fact_id: str) -> EscalationRecord:
        """按稳定升级身份读取 PostgreSQL append-only 事实。"""

        return self._get_payload_fact(
            "phase16_escalations", "escalation_id", fact_id, EscalationRecord, "escalation"
        )

    def get_conflict_analysis(self, fact_id: str) -> ConflictAnalysis:
        """按稳定分析身份读取 Analyst 的受治理中间事实。"""

        return self._get_payload_fact(
            "phase16_conflict_analyses",
            "analysis_id",
            fact_id,
            ConflictAnalysis,
            "conflict analysis",
        )

    def get_multi_agent_outcome(self, fact_id: str) -> MultiAgentOutcome:
        """按稳定终态身份读取 READY 或 DEGRADED 的可审计结果。"""

        return self._get_payload_fact(
            "phase16_multi_agent_outcomes",
            "outcome_id",
            fact_id,
            MultiAgentOutcome,
            "multi-agent outcome",
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

    def list_escalations(self, live_session_id: str) -> tuple[EscalationRecord, ...]:
        """按确定性创建时间返回当前直播的全部升级审计事实。"""

        return self._list_payload_facts(
            "phase16_escalations", "escalation_id", live_session_id, EscalationRecord
        )

    def list_conflict_analyses(
        self, live_session_id: str
    ) -> tuple[ConflictAnalysis, ...]:
        """按确定性顺序返回当前直播的 Analyst 中间事实。"""

        return self._list_payload_facts(
            "phase16_conflict_analyses",
            "analysis_id",
            live_session_id,
            ConflictAnalysis,
        )

    def list_multi_agent_outcomes(
        self, live_session_id: str
    ) -> tuple[MultiAgentOutcome, ...]:
        """按确定性顺序返回当前直播已形成的每次升级终态。"""

        return self._list_payload_facts(
            "phase16_multi_agent_outcomes",
            "outcome_id",
            live_session_id,
            MultiAgentOutcome,
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
                if current.view is WorkspaceView.LIVE and target_view is WorkspaceView.REVIEW:
                    cur.execute(
                        """SELECT 1 FROM (
                               SELECT live_session_id,lease_until
                                 FROM phase16_analyst_dispatch_claims
                               UNION ALL
                               SELECT live_session_id,lease_until
                                 FROM phase16_planner_dispatch_claims
                           ) active_claim
                           WHERE live_session_id=%s AND lease_until>%s LIMIT 1""",
                        (live_session_id, instant),
                    )
                    if cur.fetchone() is not None:
                        raise WorkspaceConflictError(
                            "active analyst dispatch prevents leaving LIVE"
                        )
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
        workspace_version_advanced_by_trigger: bool = False,
        write_context: tuple[str, str] | None = None,
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
                    if write_context is not None:
                        # PostgreSQL 无法与 Python 完全等价地实现 Unicode category 与
                        # canonical JSON 摘要。把写入收束到 Store 上下文可保证 Pydantic
                        # 在同一事务前完成完整验证；D-121/D-147 明确这不是插件沙箱。
                        cur.execute(
                            "SELECT set_config(%s,%s,true)", write_context
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
                    if workspace_version_advanced_by_trigger:
                        # Phase 16 的数据库触发器在事实 INSERT 中已锁定并推进根版本；
                        # 这里只读取同一事务里的结果，绝不能再加一次版本。
                        cur.execute(
                            """SELECT * FROM phase14_live_session_workspaces
                               WHERE live_session_id=%s""",
                            (fact.live_session_id,),
                        )
                    else:
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
        except psycopg.errors.RaiseException as exc:
            # 人工升级先在应用层检查 lease，随后数据库触发器仍会以事务墙钟复查。
            # 两次检查之间刚好到期时必须保持公开错误协议，而非把 PostgreSQL 异常泄漏给 HTTP。
            if "operator lease is invalid or expired" in str(exc):
                raise WorkspaceLeaseError("operator lease expired") from exc
            # 其他触发器异常可能是既有的事务故障注入或调用方可观察的数据库
            # 完整性错误；保留原始异常类型，避免意外改变 Phase 14 的回滚契约。
            raise
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
