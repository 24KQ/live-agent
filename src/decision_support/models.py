"""Phase 14 三场景 Workspace 与五类不可变审计事实。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from collections.abc import Mapping
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from src.specialist_runtime.models import (
    EvidenceRef,
    StrictFrozenModel,
    _freeze_json,
    _plain_json,
    canonical_json_sha256,
)


POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


def _contains_nul(value: Any) -> bool:
    """递归检查全部协议字段与冻结 JSON，保持内存/数据库字符语义一致。"""

    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, Mapping):
        return any(
            _contains_nul(key) or _contains_nul(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_nul(item) for item in value)
    if isinstance(value, BaseModel):
        return any(_contains_nul(item) for item in value.__dict__.values())
    return False


class DecisionSupportFrozenModel(StrictFrozenModel):
    """为 Phase 14 协议统一封闭免校验复制与 PostgreSQL NUL 差异。"""

    @model_validator(mode="after")
    def _reject_nul(self) -> "DecisionSupportFrozenModel":
        if _contains_nul(self):
            raise ValueError("decision support facts cannot contain NUL")
        return self


class WorkspaceView(StrEnum):
    """统一直播会话只允许按业务时间向前进入三个视图。"""

    PREPARE = "PREPARE"
    LIVE = "LIVE"
    REVIEW = "REVIEW"


class DecisionKind(StrEnum):
    """运营对结构化方案可作出的封闭决定。"""

    APPROVE = "APPROVE"
    MODIFY = "MODIFY"
    REJECT = "REJECT"


class ConflictAnalysisCode(StrEnum):
    """高冲突选择器和 Analyst 共用的封闭事实代码。"""

    MULTIPLE_VALID_BACKUPS = "MULTIPLE_VALID_BACKUPS"
    AVAILABILITY_NOISE_HIGH = "AVAILABILITY_NOISE_HIGH"
    RHYTHM_PAUSE_REQUIRED = "RHYTHM_PAUSE_REQUIRED"


class ConflictConstraintCode(StrEnum):
    """Analyst 可以报告但不能自行解除的确定性约束。"""

    OPERATOR_CONFIRMATION_REQUIRED = "OPERATOR_CONFIRMATION_REQUIRED"
    BACKUP_AVAILABILITY_UNCERTAIN = "BACKUP_AVAILABILITY_UNCERTAIN"
    HOST_RHYTHM_PAUSE_REQUIRED = "HOST_RHYTHM_PAUSE_REQUIRED"


class ConflictRiskCode(StrEnum):
    """双 Agent 中间事实允许引用的闭合风险代码。"""

    BACKUP_PRODUCT_REQUIRES_CONFIRMATION = "BACKUP_PRODUCT_REQUIRES_CONFIRMATION"
    DANMAKU_HIGH_NOISE = "DANMAKU_HIGH_NOISE"
    HUMAN_CONFIRMATION_REQUIRED = "HUMAN_CONFIRMATION_REQUIRED"
    INVENTORY_CONFLICT_REQUIRES_REVIEW = "INVENTORY_CONFLICT_REQUIRES_REVIEW"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RHYTHM_PAUSE_REQUIRED = "RHYTHM_PAUSE_REQUIRED"
    SIDE_EFFECT_UNKNOWN = "SIDE_EFFECT_UNKNOWN"
    STALE_EVIDENCE = "STALE_EVIDENCE"


class EscalationMode(StrEnum):
    """升级只能来自重放规则或持有当前租约的运营显式请求。"""

    AUTOMATIC = "AUTOMATIC"
    OPERATOR_REQUESTED = "OPERATOR_REQUESTED"


class MultiAgentOutcomeStatus(StrEnum):
    """双 Agent 链路只产出完整可审阅结果或可解释降级事实。"""

    READY = "READY"
    DEGRADED = "DEGRADED"


class MultiAgentFailureCode(StrEnum):
    """失败阶段的稳定代码，禁止把异常栈或模型自由文本写入审计。"""

    ANALYST_MODEL_ERROR = "ANALYST_MODEL_ERROR"
    ANALYST_INVALID_OUTPUT = "ANALYST_INVALID_OUTPUT"
    ANALYST_BUDGET_EXCEEDED = "ANALYST_BUDGET_EXCEEDED"
    PLANNER_MODEL_ERROR = "PLANNER_MODEL_ERROR"
    PLANNER_INVALID_OUTPUT = "PLANNER_INVALID_OUTPUT"
    PLANNER_BUDGET_EXCEEDED = "PLANNER_BUDGET_EXCEEDED"
    VALIDATOR_REJECTED = "VALIDATOR_REJECTED"
    COORDINATOR_TIMEOUT = "COORDINATOR_TIMEOUT"


def _require_safe_display_text(value: str, *, field_name: str) -> str:
    """拒绝控制字符与首尾空白，避免事实摘要被伪装为下游协议内容。"""

    import unicodedata

    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError(f"{field_name} contains unsafe control characters")
    return value


class _DatedMultiAgentFact(DecisionSupportFrozenModel):
    """双 Agent append-only 事实共享 UTC 时间、摘要和严格 JSON 语义。"""

    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_created_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class EscalationRecord(_DatedMultiAgentFact):
    """一次高冲突升级的不可变起点，不包含 Agent 输出或业务写权限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    escalation_id: str = Field(..., min_length=1)
    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    evidence_bundle_id: str = Field(..., min_length=1)
    evidence_bundle_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(..., min_length=1)
    mode: EscalationMode
    trigger_codes: tuple[ConflictAnalysisCode, ...] = Field(default=(), max_length=3)
    operator_id: str | None = Field(default=None, min_length=1)
    escalation_digest: str = ""

    @field_validator("trigger_codes")
    @classmethod
    def _validate_trigger_codes(
        cls, value: tuple[ConflictAnalysisCode, ...]
    ) -> tuple[ConflictAnalysisCode, ...]:
        if len(value) != len(set(value)):
            raise ValueError("trigger_codes must be unique")
        return value

    @model_validator(mode="after")
    def _validate_mode_and_digest(self) -> "EscalationRecord":
        if self.mode is EscalationMode.AUTOMATIC:
            if self.operator_id is not None:
                raise ValueError("AUTOMATIC escalation cannot carry operator_id")
            if len(self.trigger_codes) < 2:
                raise ValueError("AUTOMATIC escalation requires two trigger_codes")
        elif not self.operator_id:
            raise ValueError("OPERATOR_REQUESTED escalation requires operator_id")
        payload = self.model_dump(mode="json", exclude={"escalation_digest"})
        calculated = canonical_json_sha256(payload)
        if self.escalation_digest and self.escalation_digest != calculated:
            raise ValueError("escalation_digest does not match escalation facts")
        object.__setattr__(self, "escalation_digest", calculated)
        return self


class ConflictAnalysis(_DatedMultiAgentFact):
    """Analyst 的不可变中间事实，只能表达证据支持的冲突而不能提出经营动作。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    analysis_id: str = Field(..., min_length=1)
    # Store 必须把模型结果写入现有 Workspace idempotency ledger；独立键避免把可变
    # analysis_id 当作重试身份，并让同键异载荷在内存/PostgreSQL 中一致 fail-closed。
    idempotency_key: str = Field(..., min_length=1)
    escalation_id: str = Field(..., min_length=1)
    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    evidence_bundle_id: str = Field(..., min_length=1)
    evidence_bundle_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    analyst_profile_id: str = Field(..., min_length=1)
    analyst_profile_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    analyst_profile_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    finding_codes: tuple[ConflictAnalysisCode, ...] = Field(..., min_length=1, max_length=3)
    constraint_codes: tuple[ConflictConstraintCode, ...] = Field(default=(), max_length=3)
    risk_codes: tuple[ConflictRiskCode, ...] = Field(default=(), max_length=8)
    explanation: str = Field(..., min_length=1, max_length=500)
    evidence_refs: tuple[EvidenceRef, ...] = Field(..., min_length=1, max_length=12)
    analysis_digest: str = ""

    @field_validator("finding_codes", "constraint_codes", "risk_codes")
    @classmethod
    def _require_unique_codes(cls, value: tuple[StrEnum, ...]) -> tuple[StrEnum, ...]:
        if len(value) != len(set(value)):
            raise ValueError("analysis codes must be unique")
        return value

    @field_validator("explanation")
    @classmethod
    def _validate_explanation(cls, value: str) -> str:
        return _require_safe_display_text(value, field_name="explanation")

    @field_validator("evidence_refs")
    @classmethod
    def _require_unique_evidence_refs(
        cls, value: tuple[EvidenceRef, ...]
    ) -> tuple[EvidenceRef, ...]:
        if len({reference.evidence_id for reference in value}) != len(value):
            raise ValueError("analysis evidence_refs must be unique")
        return value

    @model_validator(mode="after")
    def _validate_analysis_identity_and_digest(self) -> "ConflictAnalysis":
        # 延迟导入避免 Profile 工厂加载本领域模型时形成循环；运行时仍强制比较完整身份。
        from src.decision_support.multi_agent import build_evidence_analyst_profile

        expected_profile = build_evidence_analyst_profile()
        if (
            self.analyst_profile_id != expected_profile.profile_id
            or self.analyst_profile_version != expected_profile.profile_version
            or self.analyst_profile_digest != expected_profile.profile_digest
        ):
            raise ValueError("ConflictAnalysis requires exact evidence_analyst profile")
        payload = self.model_dump(mode="json", exclude={"analysis_digest"})
        calculated = canonical_json_sha256(payload)
        if self.analysis_digest and self.analysis_digest != calculated:
            raise ValueError("analysis_digest does not match analysis facts")
        object.__setattr__(self, "analysis_digest", calculated)
        return self


class MultiAgentOutcome(_DatedMultiAgentFact):
    """协调器每次链路只记录一个终态，失败时保留可展示的确定性事实摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome_id: str = Field(..., min_length=1)
    # Outcome 同样是 append-only 终态，网络响应丢失后必须能以稳定键重放而不重复推进版本。
    idempotency_key: str = Field(..., min_length=1)
    escalation_id: str = Field(..., min_length=1)
    # 显式作用域让 Store 可使用 Workspace 外键与统一 ledger，不能仅由 escalation_id 隐式推断。
    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    escalation_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    evidence_bundle_id: str = Field(..., min_length=1)
    evidence_bundle_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    status: MultiAgentOutcomeStatus
    analysis_id: str | None = Field(default=None, min_length=1)
    analysis_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_id: str | None = Field(default=None, min_length=1)
    proposal_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_code: MultiAgentFailureCode | None = None
    fact_summary: str = Field(..., min_length=1, max_length=1000)
    outcome_digest: str = ""

    @field_validator("fact_summary")
    @classmethod
    def _validate_fact_summary(cls, value: str) -> str:
        return _require_safe_display_text(value, field_name="fact_summary")

    @model_validator(mode="after")
    def _validate_outcome_shape_and_digest(self) -> "MultiAgentOutcome":
        analysis_complete = bool(self.analysis_id) == bool(self.analysis_digest)
        if not analysis_complete:
            raise ValueError("analysis_id and analysis_digest must appear together")
        if self.status is MultiAgentOutcomeStatus.READY:
            if not self.analysis_id or not self.proposal_id or not self.proposal_digest:
                raise ValueError("READY outcome requires analysis and proposal lineage")
            if self.failure_code is not None:
                raise ValueError("READY outcome cannot carry failure_code")
        else:
            if self.proposal_id is not None or self.proposal_digest is not None:
                raise ValueError("DEGRADED outcome cannot carry proposal lineage")
            if self.failure_code is None:
                raise ValueError("DEGRADED outcome requires failure_code")
        payload = self.model_dump(mode="json", exclude={"outcome_digest"})
        calculated = canonical_json_sha256(payload)
        if self.outcome_digest and self.outcome_digest != calculated:
            raise ValueError("outcome_digest does not match outcome facts")
        object.__setattr__(self, "outcome_digest", calculated)
        return self


class AnalystDispatchClaim(DecisionSupportFrozenModel):
    """发送 Analyst 前的持久化单次 claim，不承载模型正文或业务写权限。

    claim 在外部请求发送前追加，因而模型响应丢失或 Coordinator 崩溃后也能知道该冻结
    task 已经可能离开进程。lease 只定义其他调用方应等待多久；到期后只能降级，绝不能
    用相同 task 再次发送，避免把未知副作用重试成重复模型调用。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    escalation_id: str = Field(..., min_length=1)
    live_session_id: str = Field(..., min_length=1)
    task_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    lease_until: datetime

    @field_validator("created_at", "lease_until")
    @classmethod
    def _require_claim_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("dispatch claim times must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _require_positive_claim_window(self) -> "AnalystDispatchClaim":
        if self.lease_until <= self.created_at:
            raise ValueError("dispatch claim lease_until must follow created_at")
        return self


class MultiAgentProposalLineage(DecisionSupportFrozenModel):
    """Planner 方案绑定上游升级、分析、Bundle 与精确 Profile，禁止跨事实拼接。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    escalation_id: str = Field(..., min_length=1)
    escalation_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    analysis_id: str = Field(..., min_length=1)
    analysis_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    evidence_bundle_id: str = Field(..., min_length=1)
    evidence_bundle_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[EvidenceRef, ...] = Field(..., min_length=1, max_length=12)
    planner_profile_id: str = Field(..., min_length=1)
    planner_profile_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    planner_profile_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    lineage_digest: str = ""

    @field_validator("evidence_refs")
    @classmethod
    def _require_unique_lineage_evidence_refs(
        cls, value: tuple[EvidenceRef, ...]
    ) -> tuple[EvidenceRef, ...]:
        if len({reference.evidence_id for reference in value}) != len(value):
            raise ValueError("lineage evidence_refs must be unique")
        return value

    @model_validator(mode="after")
    def _validate_planner_profile_and_digest(self) -> "MultiAgentProposalLineage":
        # 延迟导入避免 Profile 工厂加载本领域模型时形成循环；运行时仍比较完整冻结身份。
        from src.decision_support.multi_agent import build_decision_planner_profile

        expected_profile = build_decision_planner_profile()
        if (
            self.planner_profile_id != expected_profile.profile_id
            or self.planner_profile_version != expected_profile.profile_version
            or self.planner_profile_digest != expected_profile.profile_digest
        ):
            raise ValueError("lineage requires exact decision_planner profile")
        payload = self.model_dump(mode="json", exclude={"lineage_digest"})
        calculated = canonical_json_sha256(payload)
        if self.lineage_digest and self.lineage_digest != calculated:
            raise ValueError("lineage_digest does not match lineage facts")
        object.__setattr__(self, "lineage_digest", calculated)
        return self


class LiveSessionWorkspace(DecisionSupportFrozenModel):
    """串联播前、播中、播后事实的稳定会话身份与当前投影视图。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_session_id: str = Field(..., min_length=1)
    run_key: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    anchor_id: str = Field(..., min_length=1)
    root_plan_run_id: str = Field(..., min_length=1)
    event_inbox_scope_id: str = Field(..., min_length=1)
    decision_trace_scope_id: str = Field(..., min_length=1)
    replay_scope_id: str = Field(..., min_length=1)
    evaluation_scope_id: str = Field(..., min_length=1)
    view: WorkspaceView = WorkspaceView.PREPARE
    version: int = Field(
        default=1, ge=1, le=POSTGRES_BIGINT_MAX, strict=True
    )


class _SnapshotFact(DecisionSupportFrozenModel):
    """为所有 append-only 事实统一深冻结 JSON，并把有时区时间规范化为 UTC。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_session_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    snapshot: Any
    created_at: datetime

    @field_validator("snapshot", mode="after")
    @classmethod
    def _freeze_snapshot(cls, value: Any) -> Any:
        """复制并递归冻结调用方 JSON，防止落库后从外部引用篡改事实。"""

        return _freeze_json(value)

    @field_serializer("snapshot", when_used="json")
    def _serialize_snapshot(self, value: Any) -> Any:
        return _plain_json(value)

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class Incident(_SnapshotFact):
    """不可变事故事实；业务状态变化通过后续事实表达，不覆盖原事件。"""

    incident_id: str = Field(..., min_length=1)
    incident_type: str = Field(..., min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    source_ref_ids: tuple[str, ...] = Field(..., min_length=1)


class EvidenceBundle(_SnapshotFact):
    """绑定事故的证据快照；Task 3 负责构造与验证其业务内容。"""

    evidence_bundle_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    evidence_ref_ids: tuple[str, ...] = Field(..., min_length=1)
    input_fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _fingerprint_matches_snapshot(self) -> "EvidenceBundle":
        """持久化重载时执行完整内层校验，拒绝摘要重算后的伪造快照。"""

        plain_snapshot = _plain_json(self.snapshot)
        if self.input_fingerprint != canonical_json_sha256(plain_snapshot):
            raise ValueError("input_fingerprint does not match evidence snapshot")
        # evidence.py 依赖本模块的父事实模型，因此在实例校验时延迟导入，
        # 既避免模块循环，又确保内存/PostgreSQL Store 的统一重载入口不会
        # 绕过 scope、TTL、组件顺序和 bundle_digest 校验。
        from src.decision_support.evidence import EvidenceBundleSnapshot

        validated = EvidenceBundleSnapshot.model_validate(plain_snapshot)
        if validated.scope.live_session_id != self.live_session_id:
            raise ValueError("snapshot scope does not match live_session_id")
        if validated.scope.incident_id != self.incident_id:
            raise ValueError("snapshot scope does not match incident_id")
        expected_refs = tuple(
            component.reference.evidence_id for component in validated.components
        )
        if expected_refs != self.evidence_ref_ids:
            raise ValueError("snapshot components do not match evidence_ref_ids")
        return self


class Proposal(_SnapshotFact):
    """Copilot 原始结构化方案的版本化不可变快照。"""

    proposal_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    evidence_bundle_id: str = Field(..., min_length=1)
    proposal_key: str = Field(..., min_length=1)
    proposal_version: int = Field(
        default=1, ge=1, le=POSTGRES_BIGINT_MAX, strict=True
    )
    profile_id: str = Field(..., min_length=1)
    profile_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")


class OperatorDecision(_SnapshotFact):
    """运营批准、受控修改或拒绝的原始事实，不等同于可执行命令。"""

    decision_id: str = Field(..., min_length=1)
    proposal_id: str = Field(..., min_length=1)
    expected_proposal_version: int = Field(
        ..., ge=1, le=POSTGRES_BIGINT_MAX, strict=True
    )
    operator_id: str = Field(..., min_length=1)
    decision_kind: DecisionKind
    reason_code: str = Field(..., min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")


class ExecutionCommand(_SnapshotFact):
    """确定性 Compiler 产出的命令事实；Task 2 仅持久化，不执行命令。"""

    command_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    command_kind: str = Field(..., min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")


class OperatorLease(DecisionSupportFrozenModel):
    """操作员锁的只读租约视图；fencing token 在每次重新取得锁时递增。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    live_session_id: str = Field(..., min_length=1)
    operator_id: str = Field(..., min_length=1)
    fencing_token: int = Field(..., ge=1, le=POSTGRES_BIGINT_MAX, strict=True)
    lease_until: datetime

    @field_validator("lease_until")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease_until must be timezone-aware")
        return value.astimezone(timezone.utc)
