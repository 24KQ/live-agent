"""Phase 2C 弹幕事件模型。

本模块只负责定义弹幕入站事件的结构化边界，不消费 Kafka、不访问数据库。
当前阶段的事件来自本地 CLI 模拟；后续接入 Kafka consumer 时，仍可以复用
这里的 Pydantic 校验，保证空房间、空内容和缺失 trace_id 的数据不会进入系统。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DanmakuEvent(BaseModel):
    """单条播中弹幕事件。

    `viewer_id` 必须是脱敏后的观众标识，例如 hash 或本地模拟 ID；系统不应在
    公开仓库、审计表或日志里保存真实平台账号。`trace_id` 用于串联同一批弹幕
    的聚合、参考回复和审计记录。
    """

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    viewer_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    event_time: datetime
    trace_id: str = Field(..., min_length=1)

    @field_validator("room_id", "viewer_id", "content", "trace_id")
    @classmethod
    def strip_and_reject_blank(cls, value: str) -> str:
        """去掉首尾空白后再次校验空字符串。

        Pydantic 的 `min_length=1` 可以挡住空字符串，但挡不住 `"   "` 这种
        只有空白字符的输入；这里统一做 strip，避免空弹幕进入聚合器。
        """

        stripped = value.strip()
        if not stripped:
            raise ValueError("field must not be blank")
        return stripped
