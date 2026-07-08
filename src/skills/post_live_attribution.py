"""Phase 4A 数据归因计算。

从 DecisionTrace 列表计算播后核心归因指标。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class AttributionResult:
    """播后归因结果。"""

    total_decisions: int = 0
    adoption_rate: Decimal = Decimal("0")
    accuracy_rate: Decimal = Decimal("0")
    unattributable_count: int = 0
    notes: list[str] = field(default_factory=list)


class PostLiveAttribution:
    """播后数据归因计算器。

    从审计/DecisionTrace 记录中提取采纳率、准确率等指标。
    缺失数据时标注为"不可归因"，不伪造。
    """

    @staticmethod
    def calculate(traces: list[dict[str, Any]]) -> AttributionResult:
        """从决策记录列表计算归因指标。"""
        total = len(traces)
        if total == 0:
            return AttributionResult()

        adopted = 0
        accurate = 0
        unattributable = 0

        for trace in traces:
            action = trace.get("anchor_action", "")
            result = trace.get("business_result", "")

            if not action or not result:
                unattributable += 1
                continue

            if action == "accepted":
                adopted += 1
                if result == "good":
                    accurate += 1
            elif action == "rejected":
                if result == "agent_right":
                    accurate += 1

        adoption_rate = Decimal(str(adopted)) / Decimal(str(total)) if total > 0 else Decimal("0")
        accuracy_rate = Decimal(str(accurate)) / Decimal(str(total)) if total > 0 else Decimal("0")

        return AttributionResult(
            total_decisions=total,
            adoption_rate=adoption_rate,
            accuracy_rate=accuracy_rate,
            unattributable_count=unattributable,
            notes=[] if unattributable == 0 else [f"{unattributable} 条记录缺少 action 或 result，标注为不可归因"],
        )
