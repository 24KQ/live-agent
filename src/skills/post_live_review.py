"""Phase 4A 决策复盘与信任度更新。

对照每条决策的 Agent 建议 vs 主播动作 vs 业务结果，输出复盘报告。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


class PostLiveReview:
    """播后决策复盘器。

    复盘每条决策记录，生成结构化报告。
    trust_score 更新由 TrustManager 负责，这里只计算 delta。
    """

    @staticmethod
    def review(traces: list[dict[str, Any]]) -> dict[str, Any]:
        """生成播后复盘报告。

        返回 dict:
        - total_decisions: 总决策数
        - trust_delta_total: 本次复盘 trust 累计变化
        - decision_summaries: 每条决策的简要分析
        - issues: 发现的问题
        """
        total = len(traces)
        delta_total = Decimal("0")
        summaries: list[dict] = []
        issues: list[str] = []

        for trace in traces:
            action = trace.get("anchor_action", "unknown")
            result = trace.get("business_result", "unknown")
            delta_val = trace.get("trust_delta", 0.0)
            delta = Decimal(str(delta_val))
            delta_total += delta

            summary = {
                "action": action,
                "result": result,
                "trust_delta": str(delta),
            }
            summaries.append(summary)

            # 标记问题
            if action == "rejected" and result == "good":
                issues.append("主播拒绝了 Agent 有效建议，存在信任偏移")
            elif action == "accepted" and result == "bad":
                issues.append("Agent 建议被采纳但效果不佳，需优化建议模型")

        return {
            "total_decisions": total,
            "trust_delta_total": delta_total,
            "decision_summaries": summaries,
            "issues": issues,
        }
