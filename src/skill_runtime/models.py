"""Phase 11A Skill Runtime 公共模型与契约。

定义受控枚举、冻结 Manifest、审批证据、执行上下文、
调用记录和执行结果模型。

信任边界：
- ApprovalContext 的来源和必填证据在模型层校验。
- SkillExecutionContext 由可信代码构造，业务 arguments 中不可见的字段
  不在 LLM 控制范围内。
- SkillCall 开始后不可修改路由或版本。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ── 受控枚举 ──────────────────────────────────────────────────────────


class SkillExecutionRoute(StrEnum):
    """执行路由：LEGACY 走旧 ToolRegistry 路径，SKILL_RUNTIME 走新 Executor。"""

    LEGACY = "LEGACY"
    SKILL_RUNTIME = "SKILL_RUNTIME"


class ApprovalSource(StrEnum):
    """审批来源：人工中断恢复或可信兼容适配。"""

    HUMAN_INTERRUPT = "HUMAN_INTERRUPT"
    TRUSTED_COMPAT = "TRUSTED_COMPAT"


class SkillExecutionStatus(StrEnum):
    """单次 Skill 执行的状态。"""

    SUCCESS = "success"
    PENDING = "pending"
    ERROR = "error"


class SkillErrorCode(StrEnum):
    """受控错误码，不暴露内部异常细节。"""

    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    LIFECYCLE_MISMATCH = "LIFECYCLE_MISMATCH"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    IDEMPOTENCY_REQUIRED = "IDEMPOTENCY_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    HANDLER_NOT_FOUND = "HANDLER_NOT_FOUND"
    HANDLER_FAILED = "HANDLER_FAILED"


class LifecycleStage(StrEnum):
    """与 src.state.models.LifecycleStage 对齐的能力生命周期。"""

    PRE_LIVE = "PRE_LIVE"
    ON_LIVE = "ON_LIVE"
    POST_LIVE = "POST_LIVE"


class RiskLevel(StrEnum):
    """与 src.state.models.RiskLevel 对齐的风险等级。"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class GateDecision(StrEnum):
    """与 src.core.security_hooks.GateDecision 对齐的门禁策略。"""

    AUTO = "AUTO"
    SOFT_GATE = "SOFT_GATE"
    HARD_GATE = "HARD_GATE"


# ── 元数据模型 ─────────────────────────────────────────────────────────


class SkillManifest(BaseModel, frozen=True):
    """能力元数据事实源。

    skill_id 在 Catalog 中唯一；version 精确钉住；首版固定 1.0.0。
    该模型不可变，启动后不允许修改。
    """

    skill_id: str = Field(..., description="唯一能力 ID")
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$", description="精确保本号")
    description: str = Field(default="", description="能力说明")
    lifecycle: set[LifecycleStage] = Field(..., description="允许的生命周期")
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="风险等级")
    parameter_schema: dict[str, Any] = Field(default_factory=dict, description="Draft 2020-12 JSON Schema")
    gate_decision: GateDecision = Field(default=GateDecision.AUTO, description="门禁策略")
    requires_idempotency_key: bool = Field(default=False, description="是否强制要求幂等键")
    compatibility_note: str | None = Field(default=None, description="受控 Schema 修正说明")


# ── 审批证据模型 ────────────────────────────────────────────────────────


class ApprovalContext(BaseModel, frozen=True):
    """可信审批证据。来源为 HUMAN_INTERRUPT 时必须包含 operator_id 和 approval_audit_id。"""

    source: ApprovalSource = Field(..., description="审批来源")
    decision: str = Field(..., description="审批决定，如 APPROVED 或 REJECTED")
    operator_id: str | None = Field(default=None, description="审批操作员标识")
    approval_audit_id: str | None = Field(default=None, description="审批审计记录 ID")

    @model_validator(mode="after")
    def _check_human_interrupt_evidence(self) -> "ApprovalContext":
        """HUMAN_INTERRUPT 来源必须有 operator_id 和 approval_audit_id。"""
        if self.source == ApprovalSource.HUMAN_INTERRUPT:
            if not self.operator_id:
                raise ValueError("HUMAN_INTERRUPT 来源必须提供 operator_id")
            if not self.approval_audit_id:
                raise ValueError("HUMAN_INTERRUPT 来源必须提供 approval_audit_id")
        return self


# ── 执行上下文 ─────────────────────────────────────────────────────────


class SkillExecutionContext(BaseModel, frozen=True):
    """可信执行上下文。由受控代码构造，业务 arguments 中不包含这些字段。"""

    room_id: str = Field(..., description="直播间 ID")
    trace_id: str = Field(..., description="追踪 ID")
    lifecycle: LifecycleStage = Field(..., description="当前生命周期")
    execution_route: SkillExecutionRoute = Field(..., description="执行路由")
    idempotency_key: str | None = Field(default=None, description="用于幂等重放的键")
    approval: ApprovalContext | None = Field(default=None, description="审批证据")


# ── 调用记录 ─────────────────────────────────────────────────────────


class SkillCall(BaseModel, frozen=True):
    """冻结的调用记录。开始后不允许修改路由或版本。"""

    skill_id: str = Field(..., description="能力 ID")
    version: str = Field(..., description="钉住的版本")
    context: SkillExecutionContext = Field(..., description="执行上下文")
    arguments: dict[str, Any] = Field(default_factory=dict, description="业务参数")


# ── 执行结果 ─────────────────────────────────────────────────────────


class SkillExecutionResult(BaseModel, frozen=True):
    """单次 Skill 执行结果，不包含调用栈或原始异常文本。"""

    skill_id: str = Field(..., description="能力 ID")
    version: str = Field(..., description="执行版本")
    status: SkillExecutionStatus = Field(..., description="执行状态")
    error_code: SkillErrorCode | None = Field(default=None, description="稳定错误码")
    output: dict[str, Any] | None = Field(default=None, description="JSON 安全的业务输出")
    summary: str = Field(default="", description="执行摘要")
    audit_id: str | None = Field(default=None, description="审计记录 ID")
