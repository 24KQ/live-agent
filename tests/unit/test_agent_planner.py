"""Phase 5A LLM Planner 单元测试。

测试 AgentPlanner 的 prompt 构建、LLM 调用解析和 fallback 逻辑。
单元测试使用 mock / fake 模式，不依赖真实 DeepSeek API。
"""

import pytest
from decimal import Decimal

from src.core.agent_decision import AgentPlannerDecision, AgentReplanRoute
from src.skills.agent_planner import AgentPlanner, build_planner_prompt
from src.skills.product_catalog import CatalogProduct


def _make_demo_product(product_id="p001", name="测试商品"):
    """生成一个简单的测试商品。"""
    return CatalogProduct(
        product_id=product_id,
        name=name,
        category="日用",
        price=Decimal("39.90"),
        inventory=100,
        conversion_rate=Decimal("0.15"),
        commission_rate=Decimal("0.05"),
        tags=["引流"],
        selling_points=["卖点一", "卖点二"],
    )


class TestBuildPlannerPrompt:
    def test_build_planner_prompt_contains_product_names(self):
        products = [_make_demo_product("p001", "保温杯"), _make_demo_product("p002", "收纳盒")]
        prompt = build_planner_prompt(
            room_id="room-001",
            products=products,
            memory_hits=[],
            trust_score=0.7,
            available_tools=["query_products", "generate_live_plan"],
        )
        assert "保温杯" in prompt
        assert "收纳盒" in prompt

    def test_build_planner_prompt_contains_trust_score(self):
        prompt = build_planner_prompt(
            room_id="room-001",
            products=[_make_demo_product()],
            memory_hits=[],
            trust_score=0.85,
            available_tools=["query_products"],
        )
        assert "0.85" in prompt

    def test_build_planner_prompt_contains_available_tools(self):
        prompt = build_planner_prompt(
            room_id="room-001",
            products=[_make_demo_product()],
            memory_hits=[],
            trust_score=0.7,
            available_tools=["query_products", "generate_live_plan"],
        )
        assert "query_products" in prompt
        assert "generate_live_plan" in prompt

    def test_build_planner_prompt_contains_memory_hints_when_present(self):
        prompt = build_planner_prompt(
            room_id="room-001",
            products=[_make_demo_product()],
            memory_hits=[("记忆一", 0.8), ("记忆二", 0.6)],
            trust_score=0.7,
            available_tools=["query_products"],
        )
        assert "记忆一" in prompt
        assert "记忆二" in prompt


class TestAgentPlannerPlan:
    def test_plan_with_mock_returns_valid_decision(self):
        planner = AgentPlanner(api_key="test-key")
        planner._call_llm = lambda sys_prompt, user_prompt: (
            '{"route": "memory_first", "goal": "播前排品", "reason": "主播有偏好", "tool_calls": []}'
        )
        decision = planner.plan(
            room_id="room-001",
            trace_id="trace-001",
            products=[_make_demo_product()],
            memory_hits=[],
            trust_score=0.7,
            available_tools=["query_products"],
        )
        assert isinstance(decision, AgentPlannerDecision)
        assert decision.route == AgentReplanRoute.MEMORY_FIRST
        assert decision.goal == "播前排品"

    def test_plan_with_invalid_json_returns_fallback(self):
        planner = AgentPlanner(api_key="test-key")
        planner._call_llm = lambda sys_prompt, user_prompt: "这不是 JSON"
        decision = planner.plan(
            room_id="room-001",
            trace_id="trace-001",
            products=[_make_demo_product()],
            memory_hits=[],
            trust_score=0.7,
            available_tools=["query_products"],
        )
        assert decision.route == AgentReplanRoute.FALLBACK
        assert decision.fallback_reason is not None

    def test_plan_with_schema_failure_returns_fallback(self):
        planner = AgentPlanner(api_key="test-key")
        planner._call_llm = lambda sys_prompt, user_prompt: '{"route": "invalid_route"}'
        decision = planner.plan(
            room_id="room-001",
            trace_id="trace-001",
            products=[_make_demo_product()],
            memory_hits=[],
            trust_score=0.7,
            available_tools=["query_products"],
        )
        assert decision.route == AgentReplanRoute.FALLBACK

    def test_plan_with_network_error_returns_fallback(self):
        planner = AgentPlanner(api_key="test-key")
        def failing_llm(sys_prompt, user_prompt):
            raise RuntimeError("API timeout")
        planner._call_llm = failing_llm
        decision = planner.plan(
            room_id="room-001",
            trace_id="trace-001",
            products=[_make_demo_product()],
            memory_hits=[],
            trust_score=0.7,
            available_tools=["query_products"],
        )
        assert decision.route == AgentReplanRoute.FALLBACK
        assert decision.fallback_reason is not None

    def test_fallback_decision_has_route_and_reason(self):
        planner = AgentPlanner(api_key="test-key")
        decision = planner._fallback_decision(
            trace_id="trace-001",
            room_id="room-001",
            reason="DeepSeek API 超时",
        )
        assert decision.route == AgentReplanRoute.FALLBACK
        assert decision.fallback_reason == "DeepSeek API 超时"
        assert decision.trace_id == "trace-001"
        assert decision.room_id == "room-001"
        assert decision.tool_calls == []
