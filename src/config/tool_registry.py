"""LiveAgent 工具注册表。

工具注册表是所有可执行能力的白名单。Phase 1 只注册播前地基层工具，
后续新增工具必须在这里声明生命周期、风险等级、参数 Schema 和门禁策略。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.security_hooks import GateDecision
from src.state.models import LifecycleStage, RiskLevel


class ToolNotFoundError(KeyError):
    """请求了未注册工具。"""


@dataclass(frozen=True)
class ToolMetadata:
    """单个工具的注册元数据。"""

    name: str
    description: str
    lifecycle: set[LifecycleStage]
    risk_level: RiskLevel
    parameter_schema: dict[str, Any]
    gate_decision: GateDecision
    requires_idempotency_key: bool


class ToolRegistry:
    """工具注册表。

    只提供查询能力，不负责执行工具。执行前调用方必须再经过 Security Hook。
    """

    def __init__(self, tools: list[ToolMetadata]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, tool_name: str) -> ToolMetadata:
        """按名称获取工具元数据，未知工具 fail-closed。"""

        try:
            return self._tools[tool_name]
        except KeyError as exc:
            raise ToolNotFoundError(tool_name) from exc

    def tool_names(self) -> list[str]:
        """返回已注册工具名称，按名称排序方便测试和审计展示。"""

        return sorted(self._tools)

    def is_available(self, tool_name: str, lifecycle: LifecycleStage) -> bool:
        """判断工具是否可在指定生命周期使用。"""

        tool = self.get(tool_name)
        return lifecycle in tool.lifecycle


def get_default_tool_registry() -> ToolRegistry:
    """构建默认工具注册表。"""

    pre_live = {LifecycleStage.PRE_LIVE}
    on_live = {LifecycleStage.ON_LIVE}
    return ToolRegistry(
        [
            ToolMetadata(
                name="query_products",
                description="查询播前模拟商品货盘",
                lifecycle=pre_live,
                risk_level=RiskLevel.LOW,
                parameter_schema={"type": "object", "properties": {"room_id": {"type": "string"}}},
                gate_decision=GateDecision.AUTO,
                requires_idempotency_key=False,
            ),
            ToolMetadata(
                name="suggest_price_change",
                description="生成播前改价建议，不直接修改状态",
                lifecycle=pre_live,
                risk_level=RiskLevel.MEDIUM,
                parameter_schema={
                    "type": "object",
                    "required": ["product_id", "suggested_price"],
                    "properties": {
                        "product_id": {"type": "string"},
                        "suggested_price": {"type": "string"},
                    },
                },
                gate_decision=GateDecision.SOFT_GATE,
                requires_idempotency_key=False,
            ),
            ToolMetadata(
                name="set_product_price",
                description="执行商品改价",
                lifecycle=pre_live,
                risk_level=RiskLevel.HIGH,
                parameter_schema={
                    "type": "object",
                    "required": ["product_id", "price"],
                    "properties": {
                        "product_id": {"type": "string"},
                        "price": {"type": "string"},
                    },
                },
                gate_decision=GateDecision.HARD_GATE,
                requires_idempotency_key=True,
            ),
            ToolMetadata(
                name="create_live_plan_draft",
                description="生成播前排品草案，不执行建播写操作",
                lifecycle=pre_live,
                risk_level=RiskLevel.MEDIUM,
                parameter_schema={"type": "object", "properties": {"room_id": {"type": "string"}}},
                gate_decision=GateDecision.SOFT_GATE,
                requires_idempotency_key=False,
            ),
            ToolMetadata(
                name="generate_live_plan",
                description="基于本地样例货盘生成确定性播前排品方案",
                lifecycle=pre_live,
                risk_level=RiskLevel.MEDIUM,
                parameter_schema={
                    "type": "object",
                    "required": ["room_id"],
                    "properties": {"room_id": {"type": "string"}},
                },
                gate_decision=GateDecision.SOFT_GATE,
                requires_idempotency_key=False,
            ),
            ToolMetadata(
                name="generate_product_card",
                description="为指定商品生成确定性主播讲解手卡",
                lifecycle=pre_live,
                risk_level=RiskLevel.MEDIUM,
                parameter_schema={
                    "type": "object",
                    "required": ["product_id"],
                    "properties": {"product_id": {"type": "string"}},
                },
                gate_decision=GateDecision.SOFT_GATE,
                requires_idempotency_key=False,
            ),
            ToolMetadata(
                name="setup_live_session",
                description="根据播前方案模拟建播配置写入",
                lifecycle=pre_live,
                risk_level=RiskLevel.HIGH,
                parameter_schema={
                    "type": "object",
                    "required": ["room_id", "plan_item_ids", "idempotency_key"],
                    "properties": {
                        "room_id": {"type": "string"},
                        "plan_item_ids": {"type": "array", "items": {"type": "string"}},
                        "idempotency_key": {"type": "string"},
                    },
                },
                gate_decision=GateDecision.HARD_GATE,
                requires_idempotency_key=True,
            ),
            ToolMetadata(
                name="handle_sold_out_event",
                description="处理播中售罄事件，调用 Reducer 下架售罄商品",
                lifecycle=on_live,
                risk_level=RiskLevel.HIGH,
                parameter_schema={
                    "type": "object",
                    "required": ["room_id", "product_id", "trace_id", "idempotency_key"],
                    "properties": {
                        "room_id": {"type": "string"},
                        "product_id": {"type": "string"},
                        "trace_id": {"type": "string"},
                        "idempotency_key": {"type": "string"},
                    },
                },
                gate_decision=GateDecision.AUTO,
                requires_idempotency_key=True,
            ),
            ToolMetadata(
                name="recommend_backup_product",
                description="播中售罄后推荐仍可讲解的备选商品",
                lifecycle=on_live,
                risk_level=RiskLevel.MEDIUM,
                parameter_schema={
                    "type": "object",
                    "required": ["room_id", "sold_out_product_id"],
                    "properties": {
                        "room_id": {"type": "string"},
                        "sold_out_product_id": {"type": "string"},
                    },
                },
                gate_decision=GateDecision.AUTO,
                requires_idempotency_key=False,
            ),
            ToolMetadata(
                name="generate_on_live_prompt",
                description="生成播中主播提示文案，不直接修改状态",
                lifecycle=on_live,
                risk_level=RiskLevel.LOW,
                parameter_schema={
                    "type": "object",
                    "required": ["room_id", "sold_out_product_id"],
                    "properties": {
                        "room_id": {"type": "string"},
                        "sold_out_product_id": {"type": "string"},
                        "backup_product_id": {"type": "string"},
                    },
                },
                gate_decision=GateDecision.AUTO,
                requires_idempotency_key=False,
            ),
        ]
    )
