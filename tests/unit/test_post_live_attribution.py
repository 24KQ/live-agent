"""Phase 4A 数据归因单元测试。"""

from decimal import Decimal
import pytest
from src.skills.post_live_attribution import PostLiveAttribution, AttributionResult


class TestPostLiveAttribution:
    """归因计算测试。"""

    def test_calculates_adoption_rate(self):
        """建议采纳率 = 采纳且效果好的 / 总建议数。"""
        traces = [
            {"anchor_action": "accepted", "business_result": "good"},
            {"anchor_action": "accepted", "business_result": "bad"},
            {"anchor_action": "rejected", "business_result": "agent_right"},
            {"anchor_action": "rejected", "business_result": "anchor_right"},
        ]
        result = PostLiveAttribution.calculate(traces)
        # 采纳率 = accept数 / 总数 = 2/4 = 0.5
        assert result.adoption_rate == Decimal("0.5")

    def test_calculates_accuracy_rate(self):
        """建议准确率 = (采纳且效果好 + 拒绝且agent对) / 总数。"""
        traces = [
            {"anchor_action": "accepted", "business_result": "good"},
            {"anchor_action": "accepted", "business_result": "bad"},
            {"anchor_action": "rejected", "business_result": "agent_right"},
            {"anchor_action": "rejected", "business_result": "anchor_right"},
        ]
        result = PostLiveAttribution.calculate(traces)
        # 准确率 = (1 good + 1 agent_right) / 4 = 0.5
        assert result.accuracy_rate == Decimal("0.5")

    def test_empty_traces_returns_zeros(self):
        """无决策记录时所有指标为 0。"""
        result = PostLiveAttribution.calculate([])
        assert result.adoption_rate == Decimal("0")
        assert result.accuracy_rate == Decimal("0")
        assert result.total_decisions == 0
        assert result.unattributable_count == 0
