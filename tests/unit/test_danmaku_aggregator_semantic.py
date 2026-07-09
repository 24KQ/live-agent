"""DanmakuAggregator 语义增强测试。

测试 aggregate_with_semantic_fallback 的语义聚类和 LLM 兜底逻辑。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.skills.danmaku_aggregator import (
    DanmakuQuestionCategory,
    DanmakuQuestionGroup,
    aggregate_danmaku_questions,
    aggregate_with_semantic_fallback,
)
from src.skills.danmaku_events import DanmakuEvent
from src.skills.danmaku_semantic_cluster import ClusterResult, DanmakuSemanticClusterer
from src.skills.embedding_service import MockEmbeddingService


BASE_TIME = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def make_event(content: str, offset_seconds: int = 0, room_id: str = "room-demo-001", trace_id: str = "trace-semantic-agg") -> DanmakuEvent:
    return DanmakuEvent(
        room_id=room_id,
        viewer_id=f"viewer_{offset_seconds:03d}",
        content=content,
        event_time=BASE_TIME + timedelta(seconds=offset_seconds),
        trace_id=trace_id,
    )


class TestAggregateWithSemanticFallback:

    def test_fallback_to_keyword_when_clusterer_none(self):
        """clusterer 和 llm_fallback 都为 None 时回退到关键词聚合。"""
        events = [make_event("多少钱", 0)]
        result = aggregate_with_semantic_fallback(events, clusterer=None, llm_fallback=None)
        assert len(result) >= 1
        assert result[0].category == DanmakuQuestionCategory.PRICE

    def test_semantic_cluster_used_for_general_messages(self):
        """GENERAL 弹幕 >= 5 条时使用语义聚类。"""
        events = [
            make_event("这个怎么弄", i)
            for i in range(5)
        ]
        clusterer = DanmakuSemanticClusterer(embedding_service=MockEmbeddingService())
        result = aggregate_with_semantic_fallback(events, clusterer=clusterer)
        # 5 条同类 GENERAL 应聚合为 1 簇或较少簇
        general_groups = [g for g in result if g.category == DanmakuQuestionCategory.GENERAL]
        assert len(general_groups) >= 1

    def test_llm_fallback_reclassifies_general(self):
        """LLM 兜底重新分类 GENERAL 弹幕。"""
        events = [
            make_event("这个怎么操作", i)
            for i in range(5)
        ]
        mock_llm = MagicMock()
        mock_llm.classify_unclassified.return_value = [
            {"content": f"这个怎么操作", "category": DanmakuQuestionCategory.USAGE, "reason": "操作相关问题"},
        ] * 5

        clusterer = DanmakuSemanticClusterer(embedding_service=MockEmbeddingService())
        result = aggregate_with_semantic_fallback(events, clusterer=clusterer, llm_fallback=mock_llm)

        usage_groups = [g for g in result if g.category == DanmakuQuestionCategory.USAGE]
        assert len(usage_groups) >= 1

    def test_single_window_preserved(self):
        """语义聚合不改变时间窗口聚合行为。"""
        events = [make_event("多少钱", 0), make_event("价格是多少", 2)]
        keyword = aggregate_danmaku_questions(events, window_seconds=5)
        semantic = aggregate_with_semantic_fallback(events, window_seconds=5)
        # 关键词可分类的弹幕应保持一致
        assert len(keyword) == len(semantic)
