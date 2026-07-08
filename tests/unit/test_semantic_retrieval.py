"""Phase 3C SemanticMemoryRetriever 单元测试。

使用 MockEmbeddingService 做确定性验证，不依赖真实 API。
测试覆盖：语义检索 Top-K、空查询拒绝、NULL embedding 跳过、
混合加权融合（0.6 x semantic + 0.4 x structural）。
"""

from __future__ import annotations

import pytest
from src.memory.semantic_retrieval import SemanticMemoryRetriever, SemanticResult
from src.skills.embedding_service import MockEmbeddingService


class TestSemanticMemoryRetriever:
    """基于 MockEmbeddingService 的纯逻辑测试。"""

    @staticmethod
    def _fake_rows() -> list[dict]:
        """构造三条带 embedding 的模拟记忆行。"""
        mock = MockEmbeddingService()
        return [
            {
                "memory_id": "m1",
                "memory_key": "key_high_margin",
                "anchor_id": "a001",
                "content": "主播偏好高利润产品",
                "metadata": {"tag": "preference"},
                "embedding": mock.embed("主播偏好高利润产品"),
            },
            {
                "memory_id": "m2",
                "memory_key": "key_low_price",
                "anchor_id": "a001",
                "content": "主播不喜欢低价引流款",
                "metadata": {"tag": "aversion"},
                "embedding": mock.embed("主播不喜欢低价引流款"),
            },
            {
                "memory_id": "m3",
                "memory_key": "key_after_sale",
                "anchor_id": "a001",
                "content": "售后响应要快才留客",
                "metadata": {"tag": "rule"},
                "embedding": mock.embed("售后响应要快才留客"),
            },
        ]

    def test_retrieve_returns_top_k_sorted(self) -> None:
        """语义检索返回 Top-K，且按相似度降序排列。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        rows = self._fake_rows()
        results = retriever._semantic_top_k(
            query_text="利润高的爆款产品",
            rows=rows,
            top_k=2,
        )
        assert len(results) == 2
        # 按相似度降序，第一条应更相关
        assert results[0].similarity_score >= results[1].similarity_score

    def test_empty_query_raises_value_error(self) -> None:
        """空 query_text 抛出 ValueError。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        with pytest.raises(ValueError, match="empty"):
            retriever._semantic_top_k("", self._fake_rows(), top_k=3)

    def test_rows_with_null_embedding_are_skipped(self) -> None:
        """embedding 为 None 的记忆不参与排序。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        rows = [
            {
                "memory_id": "m_good",
                "memory_key": "k1",
                "anchor_id": "a001",
                "content": "有效记忆",
                "metadata": {},
                "embedding": MockEmbeddingService().embed("有效记忆"),
            },
            {
                "memory_id": "m_null",
                "memory_key": "k2",
                "anchor_id": "a001",
                "content": "无嵌入记忆",
                "metadata": {},
                "embedding": None,
            },
        ]
        results = retriever._semantic_top_k("有效记忆", rows, top_k=2)
        assert len(results) == 1
        assert results[0].memory_id == "m_good"

    def test_mixed_retrieve_weighted_fusion(self) -> None:
        """混合检索使用加权融合 0.6*semantic + 0.4*structural。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        rows = self._fake_rows()
        # 结构化结果：m3 排第1（分高），m2 排第2
        structural = {  # memory_id -> score
            "m1": 0.5,
            "m2": 0.7,
            "m3": 0.9,
        }
        results = retriever.mixed_retrieve(
            query_text="售后问题怎么处理",
            rows=rows,
            structural_scores=structural,
            top_k=3,
        )
        assert len(results) == 3
        # m3 在两边都命中，分应最高
        scores_by_id = {r.memory_id: r.similarity_score for r in results}
        assert scores_by_id["m3"] >= scores_by_id["m2"]

    def test_mixed_retrieve_no_structural(self) -> None:
        """结构化分为空时，纯用语义分。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        rows = self._fake_rows()
        results = retriever.mixed_retrieve(
            query_text="利润高的产品",
            rows=rows,
            structural_scores={},
            top_k=2,
        )
        assert len(results) == 2
        # 纯语义，按相似度降序
        assert results[0].similarity_score >= results[1].similarity_score

    def test_mixed_retrieve_filters_by_anchor(self) -> None:
        """混合检索只返回 anchor_id 匹配的结果。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        rows = self._fake_rows() + [
            {
                "memory_id": "m_other",
                "memory_key": "key_other",
                "anchor_id": "a999",
                "content": "其他主播的记忆",
                "metadata": {},
                "embedding": MockEmbeddingService().embed("其他主播的记忆"),
            },
        ]
        results = retriever.mixed_retrieve(
            query_text="利润高的产品",
            rows=rows,
            structural_scores={},
            top_k=3,
        )
        # m_other 不在 a001，不应出现
        ids = {r.memory_id for r in results}
        assert "m_other" not in ids

    def test_similar_contents_score_higher(self) -> None:
        """同语义的内容应该比不同语义的得分更高。"""
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())
        rows = self._fake_rows()
        results = retriever._semantic_top_k(
            query_text="高毛利爆款商品",
            rows=rows,
            top_k=3,
        )
        # m1 "偏好高利润产品" 应与 query 最接近（hash 相近）
        # m3 "售后响应" 应最远
        scores = {r.memory_id: r.similarity_score for r in results}
        assert scores["m1"] > scores["m3"]
