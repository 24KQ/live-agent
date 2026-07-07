"""LiveAgent Phase 1 领域状态模型。

本模块只定义内存中的领域对象，不访问数据库或外部服务。这样做的目的
是让生命周期、工具门禁、Reducer 和审计都能基于同一套强类型数据工作，
并且可以用单元测试快速验证非法输入会被提前拒绝。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LifecycleStage(StrEnum):
    """直播生命周期阶段。

    Phase 1 主要服务 PRE_LIVE，但提前声明 ON_LIVE 和 POST_LIVE，
    方便生命周期状态机和工具注册表从第一版就具备完整边界。
    """

    PRE_LIVE = "PRE_LIVE"
    ON_LIVE = "ON_LIVE"
    POST_LIVE = "POST_LIVE"


class ActionType(StrEnum):
    """Reducer 支持的确定性动作类型。"""

    SET_PRICE = "SET_PRICE"
    MARK_SOLD_OUT = "MARK_SOLD_OUT"
    SWITCH_PRODUCT = "SWITCH_PRODUCT"


class RiskLevel(StrEnum):
    """工具风险等级。

    风险等级用于安全 Hook 决策和审计记录，不直接代表是否允许执行；
    是否允许执行由工具注册表中的 gate_decision 决定。
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Product(BaseModel):
    """播前货盘中的单个商品。"""

    model_config = ConfigDict(frozen=True)

    product_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    price: Decimal = Field(..., ge=Decimal("0"))
    inventory: int = Field(..., ge=0)
    is_active: bool = True
    conversion_rate: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    tags: list[str] = Field(default_factory=list)


class LiveRoomState(BaseModel):
    """直播间的内存状态快照。

    Phase 1 先以内存状态作为业务状态源，PostgreSQL 只负责写审计。
    商品真实持久化会在后续 Phase 继续推进。
    """

    model_config = ConfigDict(frozen=True)

    room_id: str = Field(..., min_length=1)
    lifecycle: LifecycleStage = LifecycleStage.PRE_LIVE
    products: list[Product] = Field(default_factory=list)
    current_product_id: str | None = None

    @model_validator(mode="after")
    def validate_current_product(self) -> "LiveRoomState":
        """确认当前商品如果存在，就必须属于货盘。"""

        if self.current_product_id is None:
            return self
        product_ids = {product.product_id for product in self.products}
        if self.current_product_id not in product_ids:
            raise ValueError("current_product_id must exist in products")
        return self

    def get_product(self, product_id: str) -> Product:
        """按商品 ID 获取商品，不存在时抛出 KeyError。

        Reducer 会把 KeyError 转换成领域错误，测试中也可以直接使用这个
        方法断言状态更新结果。
        """

        for product in self.products:
            if product.product_id == product_id:
                return product
        raise KeyError(product_id)

    def replace_product(self, updated_product: Product) -> "LiveRoomState":
        """返回替换单个商品后的新状态。

        Pydantic 模型设置为 frozen，避免调用方在不经过 Reducer 的情况下
        原地修改状态；所有状态变更都通过复制产生新快照。
        """

        replaced = False
        products: list[Product] = []
        for product in self.products:
            if product.product_id == updated_product.product_id:
                products.append(updated_product)
                replaced = True
            else:
                products.append(product)
        if not replaced:
            raise KeyError(updated_product.product_id)
        return self.model_copy(update={"products": products})


class Action(BaseModel):
    """Reducer 可执行动作。

    Action 由工具调用、安全 Hook 或演示脚本生成。Phase 1 只支持三个动作，
    未知动作类型会被 Pydantic 枚举校验直接拒绝。
    """

    action_type: ActionType
    product_id: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = Field(..., min_length=1)


class DecisionTrace(BaseModel):
    """一次播前建议或执行的决策轨迹摘要。"""

    trace_id: str = Field(..., min_length=1)
    room_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    action_type: ActionType
    recommendation: str
    operator_decision: str | None = None
    audit_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("operator_decision")
    @classmethod
    def validate_operator_decision(cls, value: str | None) -> str | None:
        """限制主播决策字段，避免审计里出现难以统计的自由文本。"""

        if value is None:
            return value
        allowed = {"approved", "rejected", "pending"}
        if value not in allowed:
            raise ValueError(f"operator_decision must be one of {sorted(allowed)}")
        return value
