"""Phase 5G-B 播中 Harness Planner 单元测试。

这些测试约束 Planner 的结构化输出协议：
- LLM 只能返回固定 action。
- 工具名必须来自 ToolRegistry。
- 无事件时不浪费 LLM 调用。
- LLM 失败时降级到 Phase 5F 的播中 planner。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.core.agent_harness_context import AgentContextResult
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.policy_view import SkillPolicyView
from src.skills.on_live_harness_planner import (
    OnLiveHarnessPlanner,
    build_harness_prompt,
    parse_harness_decision,
)


def _context() -> AgentContextResult:
    return AgentContextResult(
        system_context="信任分: 0.7\n弹幕摘要: 价格问题 15 次\n库存告警: 无",
        should_degrade=False,
        summary="trust=0.7 | danmaku=15",
    )


def test_build_harness_prompt_contains_context_tools_and_risk_rules() -> None:
    """prompt 应包含上下文、工具列表和风险约束。"""
    prompt = build_harness_prompt(
        context=_context(),
        available_tools=["generate_on_live_prompt", "recommend_backup_product"],
        observations=[],
    )
    assert "价格问题" in prompt
    assert "generate_on_live_prompt" in prompt
    assert "recommend_backup_product" in prompt
    assert "HIGH" in prompt or "高风险" in prompt


def test_parse_valid_final_answer_json() -> None:
    """合法 final_answer JSON 应解析为 decision。"""
    raw = """
    {
      "thought": "价格问题集中，直接给主播建议",
      "action": "final_answer",
      "tool_name": null,
      "arguments": {},
      "final_suggestion": "建议主播解释优惠机制",
      "risk_level": "LOW"
    }
    """
    decision = parse_harness_decision(raw)
    assert decision.action == "final_answer"
    assert decision.final_suggestion == "建议主播解释优惠机制"


def test_parse_valid_call_tool_json() -> None:
    """合法 call_tool JSON 应保留工具名和参数。"""
    raw = """
    {
      "thought": "库存告警，需要推荐备选",
      "action": "call_tool",
      "tool_name": "recommend_backup_product",
      "arguments": {"sold_out_product_id": "p001"},
      "final_suggestion": null,
      "risk_level": "MEDIUM"
    }
    """
    decision = parse_harness_decision(raw)
    assert decision.action == "call_tool"
    assert decision.tool_name == "recommend_backup_product"
    assert decision.arguments["sold_out_product_id"] == "p001"


def test_parse_invalid_json_raises() -> None:
    """非 JSON 输出应拒绝。"""
    with pytest.raises(ValueError):
        parse_harness_decision("not json")


def test_parse_unknown_action_raises() -> None:
    """未知 action 应拒绝，避免 fail-open。"""
    with pytest.raises(ValueError):
        parse_harness_decision('{"thought": "x", "action": "delete_all"}')


def test_parse_unknown_tool_raises() -> None:
    """未知工具应拒绝。"""
    raw = """
    {
      "thought": "x",
      "action": "call_tool",
      "tool_name": "unknown_tool",
      "arguments": {},
      "risk_level": "LOW"
    }
    """
    with pytest.raises(ValueError):
        parse_harness_decision(raw)


def test_no_event_returns_no_action_without_llm_call() -> None:
    """无弹幕、无告警时不调用 LLM，直接 no_action。"""
    planner = OnLiveHarnessPlanner(api_key="test-key")
    with patch.object(planner, "_call_llm", side_effect=AssertionError("should not call llm")):
        decision = planner.plan_next_step(
            context=_context(),
            danmaku_summary=[],
            inventory_alerts=[],
            observations=[],
        )
    assert decision.action == "no_action"


def test_llm_valid_decision_is_used() -> None:
    """LLM 返回合法 JSON 时使用 LLM 决策。"""
    planner = OnLiveHarnessPlanner(api_key="test-key")
    llm_json = """
    {
      "thought": "价格问题集中",
      "action": "final_answer",
      "tool_name": null,
      "arguments": {},
      "final_suggestion": "建议主播强调券后价",
      "risk_level": "LOW"
    }
    """
    from src.skills.llm_client import LLMResponse
    with patch.object(planner._llm_client, "call", return_value=LLMResponse(content=llm_json)):
        decision = planner.plan_next_step(
            context=_context(),
            danmaku_summary=[{"category": "price", "count": 15}],
            inventory_alerts=[],
            observations=[],
        )
    assert decision.action == "final_answer"
    assert "券后价" in decision.final_suggestion


def test_llm_cannot_select_skill_excluded_by_injected_policy_view() -> None:
    """Prompt 约束失效时，Planner 仍须按自身冻结快照拒绝已移除能力。"""

    policy_view = SkillPolicyView(
        [
            manifest
            for manifest in get_default_skill_catalog()
            if manifest.skill_id != "aggregate_danmaku_questions"
        ]
    )
    planner = OnLiveHarnessPlanner(api_key="test-key", policy_view=policy_view)
    llm_json = """
    {
      "thought": "尝试调用已移除能力",
      "action": "call_tool",
      "tool_name": "aggregate_danmaku_questions",
      "arguments": {},
      "final_suggestion": null,
      "risk_level": "LOW"
    }
    """
    from src.skills.llm_client import LLMResponse

    with patch.object(planner._llm_client, "call", return_value=LLMResponse(content=llm_json)):
        decision = planner.plan_next_step(
            context=_context(),
            danmaku_summary=[{"category": "price", "count": 15}],
            inventory_alerts=[],
            observations=[],
        )

    assert not (
        decision.action == "call_tool"
        and decision.tool_name == "aggregate_danmaku_questions"
    )
    assert decision.fallback_reason is not None


def test_llm_failure_falls_back_to_phase5f_planner() -> None:
    """LLM 失败时降级到 Phase 5F planner，并返回 fallback decision。"""
    planner = OnLiveHarnessPlanner(api_key="test-key")
    from src.skills.llm_client import LLMResponse
    with patch.object(planner._llm_client, "call", return_value=LLMResponse(content="", fallback_triggered=True)):
        decision = planner.plan_next_step(
            context=_context(),
            danmaku_summary=[{"category": "price", "count": 15, "summary": "价格"}],
            inventory_alerts=[],
            observations=[],
        )
    assert decision.action in {"final_answer", "fallback"}
    assert decision.fallback_reason is not None


def test_decision_support_mode_disables_phase5f_fallback() -> None:
    """显式新路由的模型失败必须上抛，由 Graph 标记 DEGRADED。"""

    planner = OnLiveHarnessPlanner(api_key="test-key", fallback_enabled=False)
    from src.skills.llm_client import LLMResponse

    with patch.object(
        planner._llm_client,
        "call",
        return_value=LLMResponse(content="", fallback_triggered=True),
    ):
        with pytest.raises(RuntimeError, match="fallback"):
            planner.plan_next_step(
                context=_context(),
                danmaku_summary=[{"category": "price", "count": 15}],
                inventory_alerts=[],
                observations=[],
            )
