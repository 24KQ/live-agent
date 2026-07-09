"""Phase 5D LLM 复盘总结单元测试。

测试 LLM 复盘总结器：
- 生成自然语言报告
- LLM 不可用时降级到结构化报告
- 空数据返回基础报告
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from src.skills.llm_post_live_summary import (
    LLMPostLiveSummary,
    build_review_prompt,
    build_structured_fallback,
)


class TestLLMPostLiveSummary:

    def setup_method(self):
        self.summarizer = LLMPostLiveSummary()

    def test_build_review_prompt_includes_key_metrics(self):
        """复盘 prompt 应包含关键指标。"""
        attribution = {
            "total_decisions": 10,
            "adoption_rate": 0.7,
            "accuracy_rate": 0.8,
        }
        issues = ["主播拒绝了 Agent 有效建议"]
        prompt = build_review_prompt(attribution, issues)
        assert "10" in prompt
        assert "70" in prompt or "0.7" in prompt
        assert "主播拒绝了 Agent 有效建议" in prompt

    def test_build_structured_fallback_contains_metrics(self):
        """结构化降级报告应包含归因指标。"""
        attribution = {
            "total_decisions": 5,
            "adoption_rate": 0.6,
            "accuracy_rate": 0.8,
            "unattributable_count": 0,
        }
        issues = []
        report = build_structured_fallback(attribution, issues)
        assert "5" in report
        assert "60" in report

    def test_llm_unavailable_falls_back(self):
        """LLM 不可用时降级到结构化报告。"""
        with patch.object(self.summarizer, "_call_llm", side_effect=Exception("API unavailable")):
            report = self.summarizer.generate(
                attribution={"total_decisions": 3, "adoption_rate": 0.33, "accuracy_rate": 0.67},
                issues=[],
            )
        assert report is not None
        assert "3" in report

    def test_empty_data_returns_basic_report(self):
        """空数据返回基础报告。"""
        report = self.summarizer.generate(
            attribution={},
            issues=[],
        )
        assert report is not None

    def test_summary_includes_suggestion(self):
        """总结应包含后续建议。"""
        with patch.object(self.summarizer, "_call_llm", return_value="本场直播表现良好，采纳率70%。建议：继续关注价格类弹幕。"):
            report = self.summarizer.generate(
                attribution={"total_decisions": 3, "adoption_rate": 0.7, "accuracy_rate": 0.8},
                issues=[],
            )
        assert "建议" in report or "关注" in report
