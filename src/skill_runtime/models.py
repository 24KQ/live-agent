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
from math import isfinite
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from src.core.security_hooks import GateDecision
from src.state.models import LifecycleStage, RiskLevel


# ── 受控枚举 ──────────────────────────────────────────────────────────


class FrozenDict(dict):
    """保持 dict/JSON Schema 兼容性的只读映射。"""

    @staticmethod
    def _immutable(*args, **kwargs):
        raise TypeError("冻结映射不允许修改")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class FrozenList(list):
    """保持 list/JSON Schema 兼容性的只读序列。"""

    @staticmethod
    def _immutable(*args, **kwargs):
        raise TypeError("冻结列表不允许修改")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


def _deep_freeze(value: Any) -> Any:
    """递归冻结 JSON 风格容器，同时保留 jsonschema 识别的 dict/list 类型。"""
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON 对象 key 必须是字符串")
        return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(_deep_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and isfinite(value):
        return value
    raise ValueError(f"值不是 JSON-safe 类型: {type(value).__name__}")


_TRUSTED_COMPAT_TOKEN = object()


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


# ── 元数据模型 ─────────────────────────────────────────────────────────


class SkillManifest(BaseModel, frozen=True):
    """能力元数据事实源。

    skill_id 在 Catalog 中唯一；version 精确钉住；首版固定 1.0.0。
    该模型不可变，启动后不允许修改。
    """

    skill_id: str = Field(..., description="唯一能力 ID")
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$", description="精确保本号")
    description: str = Field(default="", description="能力说明")
    lifecycle: frozenset[LifecycleStage] = Field(..., description="允许的生命周期")
    risk_level: RiskLevel = Field(default=RiskLevel.LOW, description="风险等级")
    parameter_schema: dict[str, Any] = Field(default_factory=dict, description="Draft 2020-12 JSON Schema")
    gate_decision: GateDecision = Field(default=GateDecision.AUTO, description="门禁策略")
    requires_idempotency_key: bool = Field(default=False, description="是否强制要求幂等键")
    compatibility_note: str | None = Field(default=None, description="受控 Schema 修正说明")

    @field_validator("parameter_schema", mode="after")
    @classmethod
    def _freeze_parameter_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        """启动校验后冻结 Schema，防止运行中改写全局执行契约。"""
        return _deep_freeze(value)


# ── 审批证据模型 ────────────────────────────────────────────────────────


class ApprovalContext(BaseModel, frozen=True):
    """可信审批证据。来源为 HUMAN_INTERRUPT 时必须包含 operator_id 和 approval_audit_id。"""

    source: ApprovalSource = Field(..., description="审批来源")
    decision: Literal["APPROVED", "REJECTED"] = Field(..., description="受控审批决定")
    operator_id: str | None = Field(default=None, description="审批操作员标识")
    approval_audit_id: str | None = Field(default=None, description="审批审计记录 ID")
    _provenance_verified: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _check_human_interrupt_evidence(self, info: ValidationInfo) -> "ApprovalContext":
        """校验审批证据来源与决定之间的信任边界。

        HUMAN_INTERRUPT 必须携带操作员与审批审计 ID；TRUSTED_COMPAT
        只能表示内部兼容入口已经确认的批准，不能承载拒绝或待定状态。
        """
        if self.source == ApprovalSource.HUMAN_INTERRUPT:
            if not self.operator_id:
                raise ValueError("HUMAN_INTERRUPT 来源必须提供 operator_id")
            if not self.approval_audit_id:
                raise ValueError("HUMAN_INTERRUPT 来源必须提供 approval_audit_id")
            object.__setattr__(self, "_provenance_verified", True)
        if self.source == ApprovalSource.TRUSTED_COMPAT:
            if self.decision != "APPROVED":
                raise ValueError("TRUSTED_COMPAT 来源只能表示 APPROVED")
            context = info.context or {}
            if (
                not self._provenance_verified
                and context.get("trusted_compat_token") is not _TRUSTED_COMPAT_TOKEN
            ):
                raise ValueError("TRUSTED_COMPAT 只能由内部兼容工厂构造")
            object.__setattr__(self, "_provenance_verified", True)
        return self

    @property
    def provenance_verified(self) -> bool:
        """返回模型内部校验得到的来源可信标记。"""
        return self._provenance_verified



def _build_trusted_compat_approval(
    *,
    operator_id: str,
    approval_audit_id: str,
) -> ApprovalContext:
    """仅供内部兼容 Facade 使用，不属于 skill_runtime 公共导出面。"""
    return ApprovalContext.model_validate(
        {
            "source": ApprovalSource.TRUSTED_COMPAT,
            "decision": "APPROVED",
            "operator_id": operator_id,
            "approval_audit_id": approval_audit_id,
        },
        context={"trusted_compat_token": _TRUSTED_COMPAT_TOKEN},
    )


# ── 执行上下文 ─────────────────────────────────────────────────────────


class SkillExecutionContext(BaseModel, frozen=True):
    """可信执行上下文。由受控代码构造，业务 arguments 中不包含这些字段。"""

    room_id: str = Field(..., description="直播间 ID")
    trace_id: str = Field(..., description="追踪 ID")
    lifecycle: LifecycleStage = Field(..., description="当前生命周期")
    execution_route: SkillExecutionRoute = Field(..., description="执行路由")
    idempotency_key: str | None = Field(default=None, description="用于幂等重放的键")
    approval: ApprovalContext | None = Field(default=None, description="审批证据")
    # D-049 要求隐藏查询和旧参数补全留下可序列化证据。默认 False 表示调用方已经
    # 提供 Runtime 所需的显式快照；兼容入口发生参数搬移或快照补全时必须显式置 True。
    compatibility_enriched: bool = Field(
        default=False,
        description="是否由旧入口执行过参数搬移、隐藏读取或领域快照补全",
    )


# ── 调用记录 ─────────────────────────────────────────────────────────


class SkillCall(BaseModel, frozen=True):
    """冻结的调用记录。开始后不允许修改路由或版本。"""

    skill_id: str = Field(..., description="能力 ID")
    version: str = Field(..., description="钉住的版本")
    context: SkillExecutionContext = Field(..., description="执行上下文")
    arguments: dict[str, Any] = Field(default_factory=dict, description="业务参数")

    @field_validator("arguments", mode="after")
    @classmethod
    def _freeze_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        """冻结业务快照，保证调用开始后参数指纹不再变化。"""
        return _deep_freeze(value)


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

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_output(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """冻结执行结果，避免 checkpoint 或审计读取期间被调用方改写。"""
        return None if value is None else _deep_freeze(value)
