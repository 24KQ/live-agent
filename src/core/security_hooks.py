"""工具调用安全 Hook。

安全 Hook 在工具真正执行前给出 allow/block/needs-confirmation 的判断。
这里不做业务状态更新，只返回结构化决策，方便调用方决定是否进入 Reducer。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.tool_registry import ToolMetadata


class GateDecision(StrEnum):
    """工具门禁策略。"""

    AUTO = "auto"
    SOFT_GATE = "soft-gate"
    HARD_GATE = "hard-gate"
    BLOCK = "block"


@dataclass(frozen=True)
class GateResult:
    """安全 Hook 的结构化返回值。"""

    allowed: bool
    decision: GateDecision
    requires_confirmation: bool
    reason: str


def evaluate_tool_gate(tool: "ToolMetadata", confirmed: bool) -> GateResult:
    """评估工具是否允许继续执行。

    confirmed 只对 hard-gate 有意义。auto 和 soft-gate 不强制确认；
    block 则无论是否确认都拒绝执行。
    """

    if tool.gate_decision == GateDecision.AUTO:
        return GateResult(True, GateDecision.AUTO, False, "低风险只读工具，允许自动执行")

    if tool.gate_decision == GateDecision.SOFT_GATE:
        return GateResult(True, GateDecision.SOFT_GATE, False, "提示主播注意该建议，但允许继续执行")

    if tool.gate_decision == GateDecision.HARD_GATE:
        if confirmed:
            return GateResult(True, GateDecision.HARD_GATE, False, "主播已确认，允许执行高风险工具")
        return GateResult(False, GateDecision.HARD_GATE, True, "高风险工具需要主播确认")

    return GateResult(False, GateDecision.BLOCK, False, "工具被安全策略阻断")
