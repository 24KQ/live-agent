"""AgentRulesPlanner 单元测试。

验证播前确定性路由选择器的行为：
- 总是返回 direct_plan
- trust_score 影响工具可见范围
- 低信任分时路由说明变化
"""

from src.core.agent_rules_planner import AgentRulesPlanner
from src.core.agent_decision import AgentReplanRoute


class TestAgentRulesPlanner:

    def setup_method(self):
        self.planner = AgentRulesPlanner()

    def test_plan_always_returns_direct_plan(self):
        """播前路由固定为 direct_plan。"""
        decision = self.planner.plan(
            room_id="room-001",
            trace_id="trace-001",
        )
        assert decision.route == AgentReplanRoute.DIRECT_PLAN
        assert decision.trace_id == "trace-001"
        assert decision.room_id == "room-001"

    def test_plan_includes_tool_calls(self):
        """播前路由应包含三个标准工具调用。"""
        decision = self.planner.plan(
            room_id="room-001",
            trace_id="trace-001",
        )
        assert len(decision.tool_calls) == 3
        tool_names = [t.tool_name for t in decision.tool_calls]
        assert "query_products" in tool_names
        assert "generate_live_plan" in tool_names
        assert "generate_product_card" in tool_names

    def test_high_trust_returns_all_tool_categories(self):
        """信任分 >= 0.7 时应返回所有非 block 工具类别。"""
        allowed = AgentRulesPlanner.allowed_tools_for_trust(0.7)
        assert "hard-gate" in allowed
        assert "soft-gate" in allowed
        assert "auto" in allowed

    def test_medium_trust_returns_auto_and_soft_gate(self):
        """信任分 0.4 ~ 0.7 时应排除 hard-gate。"""
        allowed = AgentRulesPlanner.allowed_tools_for_trust(0.5)
        assert "hard-gate" not in allowed
        assert "soft-gate" in allowed
        assert "auto" in allowed

    def test_low_trust_returns_only_auto(self):
        """信任分 < 0.4 时应只返回 auto。"""
        allowed = AgentRulesPlanner.allowed_tools_for_trust(0.3)
        assert "hard-gate" not in allowed
        assert "soft-gate" not in allowed
        assert "auto" in allowed

    def test_plan_adds_low_trust_note(self):
        """低信任分时路由说明应包含额外提示。"""
        decision = self.planner.plan(
            room_id="room-001",
            trace_id="trace-001",
            trust_score=0.3,
        )
        assert "低信任分" in decision.reason

    def test_normal_trust_no_extra_note(self):
        """正常信任分时路由说明不含低分提示。"""
        decision = self.planner.plan(
            room_id="room-001",
            trace_id="trace-001",
            trust_score=0.7,
        )
        assert "低信任分" not in decision.reason

    def test_plan_does_not_require_llm(self):
        """播前 planner 不依赖 LLM，纯规则执行。"""
        decision = self.planner.plan(
            room_id="room-001",
            trace_id="trace-001",
        )
        assert decision.fallback_reason is None
        assert decision.route is not None
