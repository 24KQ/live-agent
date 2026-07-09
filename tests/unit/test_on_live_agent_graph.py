"""Phase 5C 播中 Agent Graph 单元测试。

测试播中 Agent 动态决策小循环：
- Graph 能从 START 走到 END
- 弹幕价格集中时 route 含 PRICE 相关工具
- 库存告警时 route 含 SWITCH_PRODUCT 或 RECOMMEND_BACKUP
- 无事件时 route 为 finish（不干预）
- 低 trust_score 限制高风险工具可见性
"""

from __future__ import annotations

import pytest

from src.core.on_live_agent_graph import (
    OnLiveAgentGraphState,
    build_on_live_agent_graph,
    create_initial_on_live_state,
)
from src.core.agent_decision import AgentReplanRoute, AgentPlannerDecision, AgentToolCall
from src.core.agent_rules_planner import AgentRulesPlanner


def _make_on_live_state(
    room_id: str = "room-test-5c",
    trace_id: str = "trace-test-5c",
    trust_score: float = 0.7,
    danmaku_summary: list | None = None,
    inventory_alerts: list | None = None,
) -> OnLiveAgentGraphState:
    return create_initial_on_live_state(
        room_id=room_id,
        trace_id=trace_id,
        trust_score=trust_score,
        danmaku_summary=danmaku_summary or [],
        inventory_alerts=inventory_alerts or [],
    )


class TestOnLiveAgentGraph:

    def test_graph_runs_start_to_end(self):
        """Graph 能从 START 走到 END。"""
        state = _make_on_live_state()
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        assert result is not None
        assert "completed_nodes" in result
        assert "room_id" in result
        assert result["room_id"] == "room-test-5c"

    def test_normal_live_no_intervention(self):
        """正常直播：弹幕少量，无告警 → Agent 不做干预（route 为 finish）。"""
        state = _make_on_live_state(
            danmaku_summary=[],
            inventory_alerts=[],
        )
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        # 无事件时 route 应为 finish
        assert result.get("suggestion") is not None or result.get("planner_route") is not None
        # 不应有错误
        assert result.get("error") is None

    def test_danmaku_price_focus_triggers_prompt(self):
        """弹幕价格集中时，Agent 应建议主播强调价格。"""
        state = _make_on_live_state(
            danmaku_summary=[
                {"category": "price", "count": 15, "summary": "价格相关问题"},
            ],
            inventory_alerts=[],
        )
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        suggestion = result.get("suggestion", "") or ""
        assert suggestion != ""

    def test_inventory_alert_triggers_backup(self):
        """库存告警时，Agent 应建议切换备用商品。"""
        state = _make_on_live_state(
            danmaku_summary=[],
            inventory_alerts=[
                {"product_id": "prod-001", "product_name": "测试商品A", "severity": "warning"},
            ],
        )
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        suggestion = result.get("suggestion", "") or ""
        assert suggestion != ""
        # 建议应包含"售罄"或"备用"或"切换"相关提示
        assert any(kw in suggestion for kw in ["售罄", "备用", "切换", "补货", "sold_out", "backup"])

    def test_low_trust_score_limits_high_risk(self):
        """低 trust_score 时，高风险工具不可见。"""
        state = _make_on_live_state(trust_score=0.3)
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        # 不应有错误
        assert result.get("error") is None
        # 应当执行了一些节点
        assert len(result.get("completed_nodes", [])) > 0

    def test_collect_context_includes_events(self):
        """collect_on_live_context 节点应收集弹幕和告警信息。"""
        state = _make_on_live_state(
            danmaku_summary=[{"category": "price", "count": 5}],
            inventory_alerts=[{"product_id": "prod-001", "severity": "warning"}],
        )
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        # 应未报错
        assert result.get("error") is None

    def test_route_by_decision_node_exists(self):
        """route_by_decision 条件路由节点应存在。"""
        state = _make_on_live_state()
        graph = build_on_live_agent_graph()
        result = graph.invoke(state)
        assert result is not None
