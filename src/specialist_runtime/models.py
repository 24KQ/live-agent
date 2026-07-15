"""Phase 13 Specialist Agent 的版本化公共协议。

本模块只定义冻结、严格 JSON 的任务、动作、结果和证据引用，不包含模型调用、
Skill 执行或候选业务逻辑。这样 Profile Registry 可以在进程启动时先完成身份与
Schema 校验，再由后续 Task 注入执行组件。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
import math
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class FrozenDict(Mapping[str, Any]):
    """以不可变键值 tuple 保存 JSON 对象，避免 ``dict`` 基类写入绕过。"""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_items", tuple(values.items()))

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("frozen JSON object cannot be mutated")


class StrictFrozenModel(BaseModel):
    """禁止 Pydantic 的免校验 update 复制，封闭冻结模型的公共绕过入口。"""

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "StrictFrozenModel":
        if update is not None:
            raise TypeError("frozen protocol does not allow model_copy(update=...)")
        # 模型及全部嵌套 JSON 都已深度冻结，安全复制与原对象等价。
        return self


def _freeze_json(value: Any) -> Any:
    """递归校验严格 JSON 并冻结容器，拒绝 NaN、tuple 和非字符串 key。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _plain_json(value: Any) -> Any:
    """把冻结容器还原为规范 JSON 可编码结构，供稳定摘要使用。"""

    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    if isinstance(value, BaseModel):
        return _plain_json(value.model_dump(mode="json"))
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_sha256(value: Any) -> str:
    """使用排序 key、紧凑分隔符和 UTF-8 生成跨进程稳定摘要。"""

    encoded = json.dumps(
        _plain_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class SpecialistTaskKind(StrEnum):
    """确定性 Orchestrator 支持的三个生命周期任务。"""

    LIVE_OPS_ADVICE = "LIVE_OPS_ADVICE"
    PLAN_PROPOSAL = "PLAN_PROPOSAL"
    POST_LIVE_REVIEW = "POST_LIVE_REVIEW"


class AgentActionKind(StrEnum):
    """模型单轮只能选择 Skill、结束或主动放弃。"""

    CALL_SKILL = "CALL_SKILL"
    FINAL = "FINAL"
    ABSTAIN = "ABSTAIN"


class AgentResultStatus(StrEnum):
    """Runner 对外暴露的封闭终态。"""

    SUCCEEDED = "SUCCEEDED"
    ABSTAINED = "ABSTAINED"
    FALLBACK = "FALLBACK"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MODEL_ERROR = "MODEL_ERROR"
    POLICY_DENIED = "POLICY_DENIED"
    INVALID_OUTPUT = "INVALID_OUTPUT"


class EvidenceKind(StrEnum):
    """Resolver Registry 可以验证的权威证据来源。"""

    EVENT = "EVENT"
    PLAN = "PLAN"
    PLAN_NODE = "PLAN_NODE"
    SKILL_ATTEMPT = "SKILL_ATTEMPT"
    AUDIT = "AUDIT"
    REPLAY = "REPLAY"
    MEMORY = "MEMORY"
    EVALUATION = "EVALUATION"


class EvidenceRef(StrictFrozenModel):
    """不携带业务正文的证据身份、版本和摘要引用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    evidence_id: str = Field(..., min_length=1)
    source_version: str = Field(..., min_length=1)
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    anchor_id: str | None = Field(default=None, min_length=1)
    room_id: str | None = Field(default=None, min_length=1)


class AgentTask(StrictFrozenModel):
    """调用方提交给某个精确 Profile 的冻结任务快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(..., min_length=1)
    task_kind: SpecialistTaskKind
    profile_id: str = Field(..., min_length=1)
    profile_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    room_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    objective: str = Field(..., min_length=1)
    input_snapshot: Any
    initial_evidence_refs: tuple[EvidenceRef, ...] = ()
    evaluation_case_id: str | None = Field(default=None, min_length=1)
    task_digest: str = ""

    @field_validator("input_snapshot", mode="after")
    @classmethod
    def _freeze_input_snapshot(cls, value: Any) -> Any:
        return _freeze_json(value)

    @field_serializer("input_snapshot", when_used="json")
    def _serialize_input_snapshot(self, value: Any) -> Any:
        return _plain_json(value)

    @model_validator(mode="after")
    def _verify_task_digest(self) -> "AgentTask":
        payload = self.model_dump(mode="json", exclude={"task_digest"})
        calculated = canonical_json_sha256(payload)
        if self.task_digest and self.task_digest != calculated:
            raise ValueError("task_digest does not match task facts")
        object.__setattr__(self, "task_digest", calculated)
        return self


class AgentAction(StrictFrozenModel):
    """单轮结构化动作；字段互斥防止 FINAL 或 ABSTAIN 夹带 Skill 调用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AgentActionKind
    skill_id: str | None = Field(default=None, min_length=1)
    arguments: Any = Field(default_factory=dict)
    final_output: Any | None = None
    reason_code: str | None = Field(default=None, min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    reason_summary: str = Field(default="", max_length=500)

    @field_validator("arguments", "final_output", mode="after")
    @classmethod
    def _freeze_action_json(cls, value: Any) -> Any:
        return None if value is None else _freeze_json(value)

    @field_serializer("arguments", "final_output", when_used="json")
    def _serialize_action_json(self, value: Any) -> Any:
        return None if value is None else _plain_json(value)

    @model_validator(mode="after")
    def _validate_exclusive_shape(self) -> "AgentAction":
        if self.kind is AgentActionKind.CALL_SKILL:
            if self.skill_id is None:
                raise ValueError("CALL_SKILL requires skill_id")
            if self.final_output is not None or self.reason_code is not None:
                raise ValueError("CALL_SKILL cannot carry FINAL or ABSTAIN fields")
        elif self.kind is AgentActionKind.FINAL:
            if self.final_output is None:
                raise ValueError("FINAL requires final_output")
            if self.skill_id is not None or self.arguments or self.reason_code is not None:
                raise ValueError("FINAL cannot carry Skill or ABSTAIN fields")
        else:
            if not self.reason_code:
                raise ValueError("ABSTAIN requires reason_code")
            if self.skill_id is not None or self.arguments or self.final_output is not None:
                raise ValueError("ABSTAIN cannot carry Skill or FINAL fields")
        return self


class AgentFailure(StrictFrozenModel):
    """Runner 终止执行时返回的稳定错误事实，不承载模型思维链。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(..., min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    retryable: bool = False
    details: Any = Field(default_factory=dict)

    @field_validator("details", mode="after")
    @classmethod
    def _freeze_failure_details(cls, value: Any) -> Any:
        # 失败详情只保存可审计的结构化事实；调用栈和模型推理不得混入协议。
        return _freeze_json(value)

    @field_serializer("details", when_used="json")
    def _serialize_failure_details(self, value: Any) -> Any:
        return _plain_json(value)


class AgentResult(StrictFrozenModel):
    """一次受限执行的可审计终态与成本汇总。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(..., min_length=1)
    profile_id: str = Field(..., min_length=1)
    profile_version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    status: AgentResultStatus
    output: Any | None = None
    failure: AgentFailure | None = None
    actions: tuple[AgentAction, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    summary: str = Field(..., min_length=1, max_length=1000)
    model_calls: int = Field(default=0, ge=0, strict=True)
    skill_calls: int = Field(default=0, ge=0, strict=True)
    input_tokens: int = Field(default=0, ge=0, strict=True)
    output_tokens: int = Field(default=0, ge=0, strict=True)
    total_tokens: int = Field(default=0, ge=0, strict=True)
    latency_ms: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    cost_cny: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))

    @field_validator("output", mode="after")
    @classmethod
    def _freeze_result_output(cls, value: Any) -> Any:
        return None if value is None else _freeze_json(value)

    @field_serializer("output", when_used="json")
    def _serialize_result_output(self, value: Any) -> Any:
        return None if value is None else _plain_json(value)

    @model_validator(mode="after")
    def _validate_result_totals(self) -> "AgentResult":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        is_success_shape = self.status in {
            AgentResultStatus.SUCCEEDED,
            AgentResultStatus.FALLBACK,
        }
        if is_success_shape:
            if self.output is None:
                raise ValueError("successful or fallback result requires output")
            if self.failure is not None:
                raise ValueError("successful or fallback result cannot carry failure")
        else:
            if self.output is not None:
                raise ValueError("non-success result cannot carry output")
            if self.failure is None:
                raise ValueError("non-success result requires structured failure")
        return self
