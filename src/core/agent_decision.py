"""Phase 5A Agent 决策模型。

定义 Agent 在播前编排中使用的结构化决策:

- AgentReplanRoute: 路由枚举, 决定 graph 走哪个分支
- AgentToolCall: LLM planner 选择的工具调用
- AgentPlannerDecision: planner 输出的完整决策(路由 + 工具 + 理由)
- AgentObservation: 工具执行后的观察结果

所有模型都是 Pydantic BaseModel, 保证 JSON 可序列化,
不携带 Pydantic 领域对象或数据库连接。空字段一律拒绝。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentReplanRoute(StrEnum):
    """Agent planner 可选的路由方向。

    每个值对应 graph 中一条 conditional edge:
    - memory_first:   先检索主播记忆, 再排品
    - direct_plan:    直接生成排品(跳过记忆检索)
    - cards_first:    先生成商品手卡
    - risk_check:     先做合规/风险检查
    - fallback:       LLM 不可用, 走确定性链路
    - finish:         所有工具执行完, 进入建播环节
    """

    MEMORY_FIRST = "memory_first"
    DIRECT_PLAN = "direct_plan"
    CARDS_FIRST = "cards_first"
    RISK_CHECK = "risk_check"
    FALLBACK = "fallback"
    FINISH = "finish"


class AgentToolCall(BaseModel):
    """LLM planner 选择的单个工具调用。

    tool_name 必须在 ToolRegistry 白名单中, 由 ToolExecutor 校验。
    arguments 是工具参数的泛型字典, 不做业务校验。
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = Field(default="MEDIUM")

    @field_validator("tool_name", mode="before")
    @classmethod
    def strip_and_reject_blank(cls, value: str) -> str:
        """去空格并拒绝空白工具名, 避免 fail-open。"""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("tool_name must not be blank")
        return cleaned


class AgentObservation(BaseModel):
    """工具执行后的观察结果。

    status 只有 success / error 两种, 不允许自由文本。
    summary 是给 planner 看的简要结果, 不含敏感信息。
    audit_id 为 None 时表示工具未写入审计。
    """

    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    summary: str = Field(default="")
    audit_id: str | None = None

    @field_validator("tool_name", mode="before")
    @classmethod
    def strip_tool_name(cls, value: str) -> str:
        """去空格并拒绝空白工具名。"""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("tool_name must not be blank")
        return cleaned

    @field_validator("status", mode="after")
    @classmethod
    def validate_status(cls, value: str) -> str:
        """status 只允许 success / error / pending。"""
        allowed = {"success", "error", "pending"}
        if value not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}, got {value!r}")
        return value


class AgentPlannerDecision(BaseModel):
    """LLM planner 输出的完整决策。

    route 决定 graph 走哪条 conditional edge。
    tool_calls 是 planner 选择执行的工具列表, 可为空。
    requires_human_approval 为 True 时, graph 在执行前触发 interrupt。
    fallback_reason 非 None 时表示这条决策是 fallback 生成的。
    """

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    goal: str = Field(default="")
    route: AgentReplanRoute
    reason: str = Field(default="")
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    requires_human_approval: bool = False
    fallback_reason: str | None = None

    @field_validator("trace_id", "room_id", mode="before")
    @classmethod
    def strip_and_reject_blank(cls, value: str) -> str:
        """去空格并拒绝空白字符串, 避免 checkpoint 写入空标识。"""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field must not be blank")
        return cleaned

    def model_dump_safe(self) -> dict[str, Any]:
        """返回 JSON 安全的 dict, 供 LangGraph checkpoint 序列化使用。"""
        return self.model_dump(mode="json")
