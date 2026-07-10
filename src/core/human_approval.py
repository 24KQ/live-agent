"""Phase 2F 人工审批模型。

本模块只负责描述 LangGraph interrupt 暂停后需要给人工看的审批请求，
以及 `Command(resume=...)` 恢复时必须携带的审批结果。这里不访问数据库，
也不执行任何业务动作；真正的建播执行仍由播前业务服务和安全 Hook 负责。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.state.models import RiskLevel


class HumanApprovalDecision(StrEnum):
    """人工审批结果枚举。

    只允许明确批准或明确拒绝，避免出现 `yes`、`ok`、`maybe` 这类无法稳定审计和
    统计的自由文本决策。未知值会被 Pydantic fail-closed 拒绝。
    """

    APPROVED = "approved"
    REJECTED = "rejected"


class HumanApprovalRequest(BaseModel):
    """发给人工审批人的 interrupt 请求。

    该对象会被序列化进 LangGraph checkpoint，因此字段全部保持 JSON 友好：
    字符串、列表和枚举值，不携带 Pydantic 领域对象或数据库连接等不可序列化内容。
    """

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    risk_level: RiskLevel
    action: str = Field(..., min_length=1)
    plan_item_ids: list[str] = Field(default_factory=list)
    message: str = Field(..., min_length=1)
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    context_summary: str | None = None

    @field_validator("trace_id", "room_id", "tool_name", "action", "message", mode="before")
    @classmethod
    def strip_and_reject_blank_text(cls, value: str) -> str:
        """去除首尾空白，并拒绝空字符串。

        仅使用 `min_length=1` 无法拒绝 `"   "` 这种输入；审批请求会进入审计和
        checkpoint，所以这里提前标准化，避免后续恢复时出现难以定位的空标识。
        """

        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field must not be blank")
        return cleaned

    @field_validator("plan_item_ids", mode="after")
    @classmethod
    def reject_blank_plan_item_ids(cls, value: list[str]) -> list[str]:
        """排品商品 ID 如果存在，就必须是非空字符串。"""

        cleaned: list[str] = []
        for item_id in value:
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError("plan_item_ids must not contain blank item id")
            cleaned.append(item_id.strip())
        return cleaned


class HumanApprovalResponse(BaseModel):
    """人工审批恢复输入。

    CLI、Web 副屏或后续审批服务最终都会把审批结果转换成这个模型，再交给
    `Command(resume=...)`。字段中不包含真实用户隐私，只保存可审计的操作员标识和理由。
    """

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    decision: HumanApprovalDecision
    operator_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)

    @field_validator("trace_id", "room_id", "tool_name", "operator_id", "reason", mode="before")
    @classmethod
    def strip_and_reject_blank_text(cls, value: str) -> str:
        """去除审批恢复输入中的空白，并拒绝空字段。"""

        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field must not be blank")
        return cleaned


def validate_human_approval_response(
    request: HumanApprovalRequest,
    response: HumanApprovalResponse,
) -> HumanApprovalResponse:
    """校验恢复输入是否匹配当前 pending 审批请求。

    LangGraph checkpoint 以 thread_id 恢复执行，但业务层仍要再次校验 trace、直播间和
    工具名，防止误把其他审批结果塞进当前高风险动作。校验失败时直接抛错，调用方不得
    继续执行建播。
    """

    if response.trace_id != request.trace_id:
        raise ValueError("human approval trace_id does not match pending request")
    if response.room_id != request.room_id:
        raise ValueError("human approval room_id does not match pending request")
    if response.tool_name != request.tool_name:
        raise ValueError("human approval tool_name does not match pending request")
    return response
