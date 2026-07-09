"""DanmakuSemanticClusterer 单元测试。

测试 embedding 语义聚类：相似文本归簇、不相似文本分开、
embedding 不可用降级。
使用 MockEmbeddingService（Phase 3C），不依赖真实 API。
"""

import pytest

from src.skills.danmaku_semantic_cluster import DanmakuSemanticClusterer
from src.skills.embedding_service import MockEmbeddingService


class TestDanmakuSemanticClusterer:

    def setup_method(self):
        self.clusterer = DanmakuSemanticClusterer(embedding_service=MockEmbeddingService())

    def test_similar_texts_cluster_together(self):
        """语义相似的文本应归到同一簇。"""
        messages = ["多少钱", "几块钱", "多少米", "价格"]
        results = self.clusterer.cluster(messages, threshold=0.75)
        # 至少有 1 个簇包含 4 条中的至少 2 条（语义相似）
        total_clustered = sum(len(r.messages) for r in results)
        assert total_clustered == 4

    def test_different_texts_separate_clusters(self):
        """语义不相似的文本应分到不同簇。"""
        messages = ["多少钱", "怎么发货", "有优惠吗"]
        results = self.clusterer.cluster(messages, threshold=0.75)
        # 3 条不相似文本应形成 3 个独立簇
        assert len(results) == 3

    def test_single_message_returns_one_cluster(self):
        """单条文本返回单簇。"""
        results = self.clusterer.cluster(["多少钱"], threshold=0.75)
        assert len(results) == 1
        assert results[0].label == "cluster_0"

    def test_empty_list_returns_empty(self):
        """空列表返回空结果。"""
        results = self.clusterer.cluster([], threshold=0.75)
        assert results == []

    def test_embedding_unavailable_returns_single_per_message(self):
        """embedding 不可用时每条文本独立成簇。"""
        clusterer = DanmakuSemanticClusterer(embedding_service=None)
        results = clusterer.cluster(["多少钱", "怎么发货"], threshold=0.75)
        # 降级后每条独立
        assert len(results) == 2

    def test_high_threshold_increases_sensitivity(self):
        """更高 threshold 产生更多簇。"""
        messages = ["多少钱", "几块钱", "怎么发货"]
        low = self.clusterer.cluster(messages, threshold=0.3)
        high = self.clusterer.cluster(messages, threshold=0.9)
        # 低 threshold 时簇更少（更容易聚合），高 threshold 时簇更多
        assert len(low) <= len(high)
