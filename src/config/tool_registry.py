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
    """从默认 Skill Catalog 生成兼容只读投影。

    不再维护独立元数据；名称、生命周期、风险、门禁和幂等要求
    全部从 Catalog Manifest 投影。
    """
    from src.skill_runtime.catalog import get_default_skill_catalog

    manifests = get_default_skill_catalog()
    tools: list[ToolMetadata] = []
    for m in manifests:
        tools.append(ToolMetadata(
            name=m.skill_id,
            description=m.description,
            lifecycle=m.lifecycle,
            risk_level=m.risk_level,
            parameter_schema=m.parameter_schema,
            gate_decision=m.gate_decision,
            requires_idempotency_key=m.requires_idempotency_key,
        ))
    return ToolRegistry(tools)
