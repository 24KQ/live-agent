"""Phase 3A 基于 trust_score 的工具可见性策略。

该策略只决定“哪些工具对当前主播可见”，不直接执行工具，也不绕过原有 SecurityHook。
真正执行时仍必须经过 ToolRegistry 和 hard-gate/soft-gate 判定。
"""

from __future__ import annotations

from decimal import Decimal

from src.config.tool_registry import ToolRegistry
from src.core.security_hooks import GateDecision
from src.state.models import LifecycleStage


class ToolMaskPolicy:
    """按信任分对工具白名单做二次裁剪。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def visible_tools(self, trust_score: Decimal, lifecycle: LifecycleStage) -> list[str]:
        """返回指定生命周期内对主播可见的工具名。

        >=0.70：展示所有非 block 工具。
        0.40-0.70：只展示 auto 和 soft-gate 工具。
        <0.40：只展示 auto 工具，避免信任不足时暴露高风险写操作。
        """

        visible: list[str] = []
        for tool_name in self.registry.tool_names():
            if not self.registry.is_available(tool_name, lifecycle):
                continue
            metadata = self.registry.get(tool_name)
            if metadata.gate_decision == GateDecision.BLOCK:
                continue
            if trust_score >= Decimal("0.70"):
                visible.append(tool_name)
                continue
            if trust_score >= Decimal("0.40") and metadata.gate_decision in {
                GateDecision.AUTO,
                GateDecision.SOFT_GATE,
            }:
                visible.append(tool_name)
                continue
            if trust_score < Decimal("0.40") and metadata.gate_decision == GateDecision.AUTO:
                visible.append(tool_name)
        return visible
