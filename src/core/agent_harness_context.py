"""Phase 5G Harness Agent 上下文构建模块。

根据弹幕聚合、库存告警、当前商品、信任分和记忆摘要，
统一构造 Agent 可见上下文，控制上下文预算，超出时返回降解标记。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentContextBudget:
    """上下文预算控制。"""
    max_danmaku_items: int = 10
    max_total_chars: int = 2000


@dataclass
class AgentContextResult:
    """上下文构建结果。"""
    system_context: str = ""
    should_degrade: bool = False
    summary: str = ""


def build_agent_context(
    danmaku_summary: list[dict[str, Any]],
    inventory_alerts: list[dict[str, Any]],
    current_product: dict[str, Any] | None,
    trust_score: float,
    memory_summary: str | None,
    budget: AgentContextBudget | None = None,
) -> AgentContextResult:
    """构建 Agent 决策上下文。"""
    if budget is None:
        budget = AgentContextBudget()

    lines = []
    summary_parts = []
    degraded = False

    lines.append("信任分: " + str(trust_score))
    summary_parts.append("trust=" + str(trust_score))

    if current_product:
        pid = current_product.get("product_id", "?")
        name = current_product.get("name", current_product.get("product_name", "?"))
        lines.append("当前商品: " + str(name) + " (" + str(pid) + ")")
        summary_parts.append("product=" + str(pid))

    total_danmaku = len(danmaku_summary)
    if total_danmaku > 0:
        total_count = sum(d.get("count", 0) for d in danmaku_summary)
        lines.append("弹幕摘要 (共 " + str(total_danmaku) + " 类, " + str(total_count) + " 条):")
        if total_danmaku <= budget.max_danmaku_items:
            for d in danmaku_summary:
                cat = d.get("summary", d.get("category", "未知"))
                cnt = d.get("count", 0)
                samples = d.get("sample_contents", [])
                sample_str = ""
                if samples:
                    sample_str = " 例如: " + "、".join(str(s) for s in samples[:3])
                lines.append("  - " + str(cat) + " (" + str(cnt) + " 次)" + sample_str)
        else:
            degraded = True
            top = sorted(danmaku_summary, key=lambda d: d.get("count", 0), reverse=True)
            lines.append("  (仅展示前 " + str(budget.max_danmaku_items) + "/" + str(total_danmaku) + " 类)")
            for d in top[:budget.max_danmaku_items]:
                cat = d.get("summary", d.get("category", "未知"))
                cnt = d.get("count", 0)
                lines.append("  - " + str(cat) + " (" + str(cnt) + " 次)")
        summary_parts.append("danmaku=" + str(total_count))
    else:
        lines.append("弹幕: 无")

    total_alerts = len(inventory_alerts)
    if total_alerts > 0:
        lines.append("库存告警 (共 " + str(total_alerts) + " 个):")
        for a in inventory_alerts:
            pid = a.get("product_id", "?")
            pname = a.get("product_name", "")
            sev = a.get("severity", "warning")
            lines.append("  - [" + str(sev) + "] " + str(pname) + "(" + str(pid) + ")")
        summary_parts.append("alerts=" + str(total_alerts))
    else:
        lines.append("库存告警: 无")

    if memory_summary:
        mem = memory_summary[:200] if len(memory_summary) > 200 else memory_summary
        lines.append("记忆摘要: " + str(mem))
        summary_parts.append("memory=yes")

    context = "\n".join(lines)

    if len(context) > budget.max_total_chars:
        context = context[:budget.max_total_chars] + "...(超预算截断)"
        degraded = True

    return AgentContextResult(
        system_context=context,
        should_degrade=degraded,
        summary=" | ".join(summary_parts),
    )