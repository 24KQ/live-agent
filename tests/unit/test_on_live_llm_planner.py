"""Phase 5F 播中 LLM Planner 单元测试。

测试 OnLiveLLMPlanner：
- build_on_live_prompt 包含弹幕/告警/信任分
- LLM 返回合法 JSON 时可解析为路由
- LLM 不可用时降级到规则
- LLM 返回非法 JSON 时降级
- 无事件时 LLM 可返回 finish
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from src.skills.on_live_llm_planner import (
    OnLiveLLMPlanner,
    build_on_live_prompt,
    parse_on_live_decision,
)


class TestBuildOnLivePrompt:

    def test_prompt_contains_danmaku(self):
        """prompt 应包含弹幕信息。"""
        danmaku = [{"category": "price", "count": 15, "summary": "价格相关问题", "sample_contents": ["多少钱"]}]
        prompt = build_on_live_prompt(danmaku=danmaku, alerts=[], trust_score=0.7, memory_hints=None)
        assert "price" in prompt or "价格" in prompt
        assert "15" in prompt

    def test_prompt_contains_alerts(self):
        """prompt 应包含库存告警。"""
        alerts = [{"product_id": "p001", "product_name": "杯子", "severity": "warning"}]
        prompt = build_on_live_prompt(danmaku=[], alerts=alerts, trust_score=0.7, memory_hints=None)
        assert "p001" in prompt or "杯子" in prompt

    def test_prompt_contains_trust_score(self):
        """prompt 应包含信任分。"""
        prompt = build_on_live_prompt(danmaku=[], alerts=[], trust_score=0.35, memory_hints=None)
        assert "0.35" in prompt or "信任" in prompt

    def test_prompt_contains_memory_hints(self):
        """prompt 应包含记忆偏好。"""
        memory = [("主播偏好推高毛利商品", 0.85)]
        prompt = build_on_live_prompt(danmaku=[], alerts=[], trust_score=0.7, memory_hints=memory)
        assert "主播偏好推高毛利商品" in prompt


class TestParseOnLiveDecision:

    def test_parse_valid_json(self):
        """合法 JSON 应正确解析。"""
        json_str = '{"route": "direct_plan", "goal": "处理价格问题", "suggestion": "建议主播强调优惠"}'
        result = parse_on_live_decision(json_str)
        assert result["route"] == "direct_plan"
        assert result["goal"] == "处理价格问题"
        assert result["suggestion"] == "建议主播强调优惠"

    def test_parse_invalid_json_raises(self):
        """非法 JSON 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            parse_on_live_decision("not json at all")

    def test_parse_empty_json_raises(self):
        """空 JSON 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            parse_on_live_decision("{}")

    def test_parse_finish_route(self):
        """finish 路由应正确解析。"""
        json_str = '{"route": "finish", "goal": "无事件", "suggestion": null}'
        result = parse_on_live_decision(json_str)
        assert result["route"] == "finish"
        assert result["suggestion"] is None


class TestOnLiveLLMPlanner:

    def setup_method(self):
        self.planner = OnLiveLLMPlanner(api_key="test-key")

    def test_plan_with_no_events_finish(self):
        """无事件时返回 finish。"""
        result = self.planner.plan(danmaku_summary=[], inventory_alerts=[], trust_score=0.7)
        assert result["route"] == "finish"

    def test_plan_llm_unavailable_falls_back(self):
        """LLM 不可用时降级到规则。"""
        with patch.object(self.planner, "_call_llm", side_effect=Exception("API error")):
            result = self.planner.plan(
                danmaku_summary=[{"category": "price", "count": 15, "summary": "价格"}],
                inventory_alerts=[],
                trust_score=0.7,
            )
        # 降级后应该仍然有合理输出
        assert result["route"] in ("direct_plan", "finish")
        assert result["suggestion"] is not None

    def test_plan_llm_returns_valid_decision(self):
        """LLM 返回有效决策时使用 LLM 结果。"""
        mock_return = '{"route": "direct_plan", "goal": "处理库存告警", "suggestion": "建议立即切换备选商品"}'
        with patch.object(self.planner, "_call_llm", return_value=mock_return):
            result = self.planner.plan(
                danmaku_summary=[],
                inventory_alerts=[{"product_id": "p001", "severity": "sold_out"}],
                trust_score=0.7,
            )
        assert result["route"] == "direct_plan"
        assert "备选" in result["suggestion"]
