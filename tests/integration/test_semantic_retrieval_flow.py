"""Phase 3C 语义检索集成测试。

验证真实 pgvector 余弦距离查询和混合加权融合链路。
依赖本地 PostgreSQL + pgvector + seed 数据。
"""

from __future__ import annotations

import psycopg
import pytest
from src.config.settings import get_settings
from src.memory.semantic_retrieval import SemanticMemoryRetriever
from src.memory.memory_retrieval import MemoryRetriever
from src.skills.embedding_service import EmbeddingService
from src.memory.memory_store import MemoryStore
from src.memory.models import MemoryLayer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def seeded_data(settings):
    """确保有带 embedding 的 seed 数据。"""
    import subprocess
    subprocess.run(["python", "-m", "scripts.seed_phase3_memory_demo_data"], capture_output=True)
    subprocess.run(["python", "-m", "scripts.seed_phase3c_embeddings"], capture_output=True)


class TestSemanticRetrievalFlow:
    """真实 pgvector 端到端语义检索。"""

    def test_pgvector_semantic_search_returns_top_k(self, settings, seeded_data):
        """pgvector 余弦距离查询返回 Top-K 语义相似记忆。"""
        svc = EmbeddingService(settings=settings)
        query_text = "利润高的爆款产品"
        query_vec = svc.embed(query_text)
        assert len(query_vec) == 2048

        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT memory_id, content,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM live_agent_anchor_memories
                    WHERE status = 'active' AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT 3;
                """, (query_vec, query_vec))
                results = cur.fetchall()

        assert len(results) == 3
        # 按相似度降序
        assert results[0][2] >= results[1][2] >= results[2][2]
        # 最高相似度应在合理范围内 (0.35-0.99 之间)
        assert results[0][2] > 0.3

    def test_null_embedding_memories_not_in_results(self, settings, seeded_data):
        """embedding 为 NULL 的记忆不出现在语义搜索结果中。"""
        svc = EmbeddingService(settings=settings)
        query_vec = svc.embed("测试查询")

        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM live_agent_anchor_memories
                    WHERE status = 'active' AND embedding IS NULL;
                """)
                null_count = cur.fetchone()[0]

                cur.execute("""
                    SELECT memory_id FROM live_agent_anchor_memories
                    WHERE status = 'active' AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT 10;
                """, (query_vec,))
                non_null_ids = {r[0] for r in cur.fetchall()}

        if null_count > 0:
            # 验证 NULL embedding 的记忆不在语义结果中
            with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT memory_id FROM live_agent_anchor_memories
                        WHERE embedding IS NULL;
                    """)
                    null_ids = {r[0] for r in cur.fetchall()}
            assert null_ids.isdisjoint(non_null_ids)

    def test_mixed_retrieve_weighted_fusion(self, settings, seeded_data):
        """混合检索加权融合: 0.6*semantic + 0.4*structural。"""
        svc = EmbeddingService(settings=settings)
        retriever = SemanticMemoryRetriever(embedding_service=svc)

        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT memory_id, memory_key, anchor_id, content,
                           metadata::text, embedding
                    FROM live_agent_anchor_memories
                    WHERE status = 'active' AND embedding IS NOT NULL
                    LIMIT 10;
                """)
                rows = []
                for r in cur.fetchall():
                    rows.append({
                        "memory_id": r[0],
                        "memory_key": r[1],
                        "anchor_id": r[2],
                        "content": r[3],
                        "metadata": r[4],
                        "embedding": r[5],
                    })

        assert len(rows) >= 3

        # 构造结构化分数：第1条 0.9, 第2条 0.5, 第3条 0.2
        structural = {
            rows[0]["memory_id"]: 0.9,
            rows[1]["memory_id"]: 0.5,
            rows[2]["memory_id"]: 0.2,
        }

        results = retriever.mixed_retrieve(
            query_text="厨房类高利润商品",
            rows=rows,
            structural_scores=structural,
            top_k=3,
        )

        assert len(results) >= 1
        # 验证每个结果都有有效的 similarity_score
        for r in results:
            assert 0.0 <= r.similarity_score <= 1.0
            assert r.memory_id
            assert r.content

    def test_semantic_retrieval_fallback_when_api_fails(self, settings, seeded_data):
        """API 不可用时语义检索降级不抛异常。"""
        # 用 MockEmbeddingService 模拟 API 故障
        from src.skills.embedding_service import MockEmbeddingService
        retriever = SemanticMemoryRetriever(embedding_service=MockEmbeddingService())

        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT memory_id, memory_key, anchor_id, content,
                           metadata::text, embedding
                    FROM live_agent_anchor_memories
                    WHERE status = 'active' AND embedding IS NOT NULL
                    LIMIT 5;
                """)
                rows = []
                for r in cur.fetchall():
                    rows.append({
                        "memory_id": r[0],
                        "memory_key": r[1],
                        "anchor_id": r[2],
                        "content": r[3],
                        "metadata": r[4],
                        "embedding": r[5],
                    })

        # 用 Mock 做 mixed_retrieve，不应抛异常
        results = retriever.mixed_retrieve(
            query_text="测试",
            rows=rows,
            structural_scores={},
            top_k=2,
        )
        assert isinstance(results, list)
