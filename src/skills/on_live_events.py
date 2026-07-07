"""Phase 2B 播中事件模型。

本模块只定义播中事件的结构化输入，不消费 Kafka，也不访问数据库。Phase 2B
先用本地确定性事件模拟售罄场景，后续接入 Kafka consumer 时可以复用这里的
校验模型作为事件入站边界。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OnLiveEventType(StrEnum):
    """Phase 2B 支持的播中事件类型。"""

    SOLD_OUT = "sold_out"


class InventoryEvent(BaseModel):
    """播中库存事件。

    `trace_id` 是串联事件、工具调用、Reducer 结果和审计记录的关键字段。
    当前阶段只支持 sold_out；未知事件类型会被 Pydantic 枚举校验拒绝。
    """

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    product_id: str = Field(..., min_length=1)
    event_type: OnLiveEventType
    trace_id: str = Field(..., min_length=1)
