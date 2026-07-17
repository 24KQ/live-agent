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

from src.specialist_runtime.models import StrictFrozenModel, _freeze_json, _plain_json


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
