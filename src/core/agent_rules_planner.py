"""Phase 5A 修正：播前确定性路由选择器。

播前阶段不需要 LLM 决策路由，由 AgentRulesPlanner 根据
确定性规则选择路径。LLM Planner 保留供后续播中 Agent 使用。

路由规则：
- trust_score >= 0.7：全量非 block 工具可见
- trust_score 0.4 ~ 0.7：仅 auto + soft-gate 工具
- trust_score < 0.4：仅 auto 工具（高风险工具隐藏）
- 播前固定走 deterministic_prelive，不经过 LLM 决策
"""

from __future__ import annotations

from src.core.agent_decision import AgentPlannerDecision, AgentReplanRoute, AgentToolCall


class AgentRulesPlanner:
    """播前确定性路由选择器。

    播前不需要 LLM 决策，根据 trust_score 和阶段特征，
    按确定性规则选择路径。
    """

    DEFAULT_TRUST_SCORE = 0.7

    def plan(
        self,
        room_id: str,
        trace_id: str,
        products=None,
        memory_hits=None,
        trust_score: float = 0.7,
        available_tools=None,
    ) -> AgentPlannerDecision:
        """播前阶段路由选择。"""
        tool_calls = [
            AgentToolCall(tool_name="query_products", arguments={"room_id": room_id}, risk_level="LOW"),
            AgentToolCall(tool_name="generate_live_plan", arguments={"room_id": room_id}, risk_level="MEDIUM"),
            AgentToolCall(tool_name="generate_product_card", arguments={"room_id": room_id}, risk_level="MEDIUM"),
        ]

        route_reason = "播前固定确定性路由：查货盘 -> 排品 -> 手卡"

        if trust_score < 0.4:
            route_reason += "（低信任分：工具可见范围受限）"

        return AgentPlannerDecision(
            trace_id=trace_id,
            room_id=room_id,
            goal="播前排品：查询货盘 -> 生成排品方案 -> 生成商品手卡 -> 合规摘要 -> 建播确认",
            route=AgentReplanRoute.DIRECT_PLAN,
            reason=route_reason,
            tool_calls=tool_calls,
            requires_human_approval=False,
            fallback_reason=None,
        )

    @staticmethod
    def allowed_tools_for_trust(trust_score: float) -> list[str]:
        """根据 trust_score 返回可见工具列表。"""
        if trust_score >= 0.7:
            return ["auto", "soft-gate", "hard-gate"]
        elif trust_score >= 0.4:
            return ["auto", "soft-gate"]
        return ["auto"]
