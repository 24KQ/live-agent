"""Phase 4A 决策复盘单元测试。"""

from decimal import Decimal
import pytest
from src.skills.post_live_review import PostLiveReview


class TestPostLiveReview:
    """决策复盘逻辑测试。"""

    def test_generates_report_with_summary(self):
        """复盘应生成含摘要的结构化报告。"""
        traces = [
            {"anchor_action": "accepted", "business_result": "good", "trust_delta": Decimal("0.05")},
            {"anchor_action": "rejected", "business_result": "anchor_right", "trust_delta": Decimal("-0.05")},
        ]
        report = PostLiveReview.review(traces)
        assert report["total_decisions"] == 2
        assert isinstance(report["trust_delta_total"], Decimal)
        # 0.05 + (-0.05) = 0
        assert report["trust_delta_total"] == Decimal("0")

    def test_review_handles_empty_traces(self):
        """空决策列表应返回 base 报告。"""
        report = PostLiveReview.review([])
        assert report["total_decisions"] == 0
        assert report["trust_delta_total"] == Decimal("0")
