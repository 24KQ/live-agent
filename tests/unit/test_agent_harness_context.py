"""Phase 5G Harness Agent 上下文单元测试。

测试 AgentHarnessContext：
- 上下文包含弹幕、库存、商品、trust_score、记忆摘要。
- 大字段被摘要不直接注入完整大 JSON。
- 超预算时返回降级标记。
"""

from __future__ import annotations

import pytest
from typing import Any

from src.core.agent_harness_context import (
    build_agent_context,
    AgentContextBudget,
    AgentContextResult,
)


def test_context_contains_danmaku() -> None:
    """上下文应包含弹幕摘要信息。"""
    result = build_agent_context(
        danmaku_summary=[
            {"category": "price", "count": 15, "summary": "价格问题"},
        ],
        inventory_alerts=[],
        current_product=None,
        trust_score=0.7,
        memory_summary=None,
        budget=AgentContextBudget(),
    )
    assert result is not None
    assert "price" in result.system_context or "价格" in result.system_context
    assert result.should_degrade is False


def test_context_contains_alerts() -> None:
    """上下文应包含库存告警信息。"""
    result = build_agent_context(
        danmaku_summary=[],
        inventory_alerts=[
            {"product_id": "p001", "product_name": "杯子", "severity": "warning"},
        ],
        current_product=None,
        trust_score=0.7,
        memory_summary=None,
        budget=AgentContextBudget(),
    )
    assert "p001" in result.system_context or "杯子" in result.system_context
    assert result.should_degrade is False


def test_context_contains_trust_score() -> None:
    """上下文应包含信任分。"""
    result = build_agent_context(
        danmaku_summary=[],
        inventory_alerts=[],
        current_product=None,
        trust_score=0.35,
        memory_summary=None,
        budget=AgentContextBudget(),
    )
    assert "0.35" in result.system_context or "信任" in result.system_context
    assert result.should_degrade is False


def test_context_contains_memory_summary() -> None:
    """上下文应包含记忆摘要。"""
    result = build_agent_context(
        danmaku_summary=[],
        inventory_alerts=[],
        current_product=None,
        trust_score=0.7,
        memory_summary="主播偏好推荐高毛利商品",
        budget=AgentContextBudget(),
    )
    assert "高毛利" in result.system_context


def test_context_summarizes_large_danmaku() -> None:
    """弹幕数据过多时自动摘要，不过量占用上下文。"""
    large = [{"category": f"cat{i}", "count": i, "summary": f"问题{i}"} for i in range(100)]
    result = build_agent_context(
        danmaku_summary=large,
        inventory_alerts=[],
        current_product=None,
        trust_score=0.7,
        memory_summary=None,
        budget=AgentContextBudget(max_danmaku_items=10),
    )
    # 超过预算的弹幕内容不应全部注入
    context_len = len(result.system_context)
    # 100 条全文注入应远大于摘要结果
    full_inject_estimate = sum(len(f"cat{i}") + len(f"问题{i}") for i in range(100))
    assert context_len < full_inject_estimate, f"context length {context_len} exceeds full inject estimate {full_inject_estimate}"
    assert result.should_degrade is True


def test_context_exceeds_budget_returns_degrade() -> None:
    """超预算时返回降解标记。"""
    result = build_agent_context(
        danmaku_summary=[{"category": "price", "count": 999}],
        inventory_alerts=[],
        current_product=None,
        trust_score=0.7,
        memory_summary=None,
        budget=AgentContextBudget(max_total_chars=50),
    )
    assert result.should_degrade is True


def test_context_empty_data_returns_valid() -> None:
    """无事件时仍应返回有效上下文。"""
    result = build_agent_context(
        danmaku_summary=[],
        inventory_alerts=[],
        current_product=None,
        trust_score=0.7,
        memory_summary=None,
        budget=AgentContextBudget(),
    )
    assert result is not None
    assert result.system_context != ""
