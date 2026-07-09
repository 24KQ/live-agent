"""Phase 5A Agent 决策模型单元测试。

测试 Agent 决策相关 Pydantic 模型的校验逻辑：
- AgentReplanRoute 枚举只允许预定义路由
- AgentToolCall 校验工具名和参数
- AgentPlannerDecision 校验路由、trace_id、room_id
- AgentObservation 记录工具执行结果
"""

import pytest
from pydantic import ValidationError

from src.core.agent_decision import (
    AgentObservation,
    AgentPlannerDecision,
    AgentReplanRoute,
    AgentToolCall,
)


class TestAgentReplanRoute:
    """Agent 路由枚举校验。"""

    def test_valid_routes(self) -> None:
        """预定义路由都应该是合法的。"""
        for route in ["memory_first", "direct_plan", "cards_first", "risk_check", "fallback", "finish"]:
            assert AgentReplanRoute(route) is not None

    def test_unknown_route_rejected(self) -> None:
        """未知路由必须被拒绝。"""
        with pytest.raises(ValueError):
            AgentReplanRoute("unknown_route")


class TestAgentToolCall:
    """工具调用模型校验。"""

    def test_valid_tool_call(self) -> None:
        """合法工具调用应该通过校验。"""
        call = AgentToolCall(
            tool_name="query_products",
            arguments={"room_id": "room-001"},
            risk_level="LOW",
        )
        assert call.tool_name == "query_products"
        assert call.arguments == {"room_id": "room-001"}
        assert call.risk_level == "LOW"

    def test_blank_tool_name_rejected(self) -> None:
        """空工具名必须被拒绝。"""
        with pytest.raises(ValidationError):
            AgentToolCall(tool_name="  ", arguments={}, risk_level="LOW")

    def test_arguments_default_to_empty(self) -> None:
        """参数默认为空字典。"""
        call = AgentToolCall(tool_name="query_products", risk_level="LOW")
        assert call.arguments == {}


class TestAgentPlannerDecision:
    """Planner 决策模型校验。"""

    def test_valid_decision(self) -> None:
        """合法决策应该通过校验。"""
        decision = AgentPlannerDecision(
            trace_id="trace-001",
            room_id="room-001",
            goal="播前排品",
            route="memory_first",
            reason="主播有偏好记忆，先检索再排品",
            tool_calls=[
                AgentToolCall(
                    tool_name="query_products",
                    arguments={"room_id": "room-001"},
                    risk_level="LOW",
                ),
            ],
        )
        assert decision.trace_id == "trace-001"
        assert decision.route == AgentReplanRoute.MEMORY_FIRST
        assert decision.requires_human_approval is False
        assert decision.fallback_reason is None

    def test_blank_trace_id_rejected(self) -> None:
        """空 trace_id 必须被拒绝。"""
        with pytest.raises(ValidationError):
            AgentPlannerDecision(
                trace_id="  ",
                room_id="room-001",
                goal="test",
                route="finish",
                reason="test",
                tool_calls=[],
            )

    def test_blank_room_id_rejected(self) -> None:
        """空 room_id 必须被拒绝。"""
        with pytest.raises(ValidationError):
            AgentPlannerDecision(
                trace_id="trace-001",
                room_id="",
                goal="test",
                route="finish",
                reason="test",
                tool_calls=[],
            )

    def test_unknown_route_rejected(self) -> None:
        """未知路由必须被拒绝。"""
        with pytest.raises(ValidationError):
            AgentPlannerDecision(
                trace_id="trace-001",
                room_id="room-001",
                goal="test",
                route="unknown",
                reason="test",
                tool_calls=[],
            )

    def test_empty_tool_calls_allowed(self) -> None:
        """tool_calls 可以为空。"""
        decision = AgentPlannerDecision(
            trace_id="trace-001",
            room_id="room-001",
            goal="test",
            route="finish",
            reason="完成",
            tool_calls=[],
        )
        assert decision.tool_calls == []

    def test_fallback_decision_with_reason(self) -> None:
        """fallback 决策应该能携带 fallback_reason。"""
        decision = AgentPlannerDecision(
            trace_id="trace-001",
            room_id="room-001",
            goal="test",
            route="fallback",
            reason="LLM 不可用",
            tool_calls=[],
            fallback_reason="DeepSeek API 超时",
        )
        assert decision.route == AgentReplanRoute.FALLBACK
        assert decision.fallback_reason == "DeepSeek API 超时"
