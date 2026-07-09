"""DanmakuLLMFallback 单元测试。

测试 LLM 兜底分类：未分类弹幕 >= 5 条时调用 LLM，
LLM 不可用时降级为 general。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.skills.danmaku_llm_fallback import DanmakuLLMFallback
from src.skills.danmaku_aggregator import DanmakuQuestionCategory


class TestDanmakuLLMFallback:

    def setup_method(self):
        self.fallback = DanmakuLLMFallback()

    def test_insufficient_unclassified_returns_empty(self):
        """未分类弹幕 < 5 条时返回空列表。"""
        result = self.fallback.classify_unclassified(
            ["多少钱", "怎么用"],
        )
        assert result == []

    def test_sufficient_unclassified_calls_llm(self):
        """未分类弹幕 >= 5 条时调用 LLM 分类。"""
        unclassified = [
            "这个怎么操作",
            "怎么弄",
            "如何操作",
            "操作步骤是什么",
            "具体怎么搞",
        ]
        with patch.object(self.fallback, "_call_llm", return_value=[
                {"content": "这个怎么操作", "category": DanmakuQuestionCategory.USAGE, "reason": "操作相关"},
                {"content": "怎么弄", "category": DanmakuQuestionCategory.USAGE, "reason": "操作相关"},
                {"content": "如何操作", "category": DanmakuQuestionCategory.USAGE, "reason": "操作相关"},
                {"content": "操作步骤是什么", "category": DanmakuQuestionCategory.USAGE, "reason": "操作相关"},
                {"content": "具体怎么搞", "category": DanmakuQuestionCategory.USAGE, "reason": "操作相关"},
            ]) as mock_call:
            result = self.fallback.classify_unclassified(unclassified)
            mock_call.assert_called_once()
            assert len(result) == 5
            for item in result:
                assert item["category"] == DanmakuQuestionCategory.USAGE

    def test_llm_unavailable_falls_back_to_general(self):
        """LLM 不可用时所有未分类弹幕降级为 general。"""
        unclassified = [
            "这个怎么操作",
            "怎么弄",
            "如何操作",
            "操作步骤是什么",
            "具体怎么搞",
        ]
        with patch.object(self.fallback, "_call_llm", side_effect=Exception("API unavailable")):
            result = self.fallback.classify_unclassified(unclassified)
            assert len(result) == 5
            for item in result:
                assert item["category"] == DanmakuQuestionCategory.GENERAL

    def test_empty_unclassified_returns_empty(self):
        """空列表返回空结果。"""
        result = self.fallback.classify_unclassified([])
        assert result == []

    def test_batch_size_respected(self):
        """超过 batch_size 的弹幕分批调用 LLM。"""
        unclassified = [f"question_{i}" for i in range(25)]
        call_count = 0

        def mock_llm(messages):
            nonlocal call_count
            call_count += 1
            return {msg: "general" for msg in messages}

        with patch.object(self.fallback, "_call_llm", side_effect=mock_llm):
            result = self.fallback.classify_unclassified(unclassified, batch_size=10)
            # 25 条，batch_size=10，需要 3 次调用
            assert call_count == 3
            assert len(result) == 25
