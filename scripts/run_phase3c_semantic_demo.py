"""Phase 3C 语义记忆检索 CLI 演示。

演示使用 pgvector 语义检索 + 结构化加权融合的效果。
"""

import psycopg
from src.config.settings import get_settings
from src.memory.semantic_retrieval import SemanticMemoryRetriever
from src.memory.memory_retrieval import MemoryRetriever
from src.skills.embedding_service import EmbeddingService


def main() -> None:
    settings = get_settings()
    print("=" * 60)
    print("Phase 3C 语义记忆检索演示")
    print("=" * 60)

    # 读取所有带 embedding 的记忆
    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT memory_id, memory_key, anchor_id, content, metadata::text,
                       embedding IS NOT NULL AS has_embedding
                FROM live_agent_anchor_memories
                WHERE status = 'active' AND anchor_id = 'anchor-demo-001'
                ORDER BY created_at DESC;
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "memory_id": r[0],
                    "memory_key": r[1],
                    "anchor_id": r[2],
                    "content": r[3],
                    "metadata": r[4],
                    "embedding": None,  # 语义检索自己从 DB 读
                })

    print(f"\n已加载 {len(rows)} 条主播 a001 的记忆。\n")

    # 用真实 embedding service
    svc = EmbeddingService(settings=settings)
    retriever = SemanticMemoryRetriever(embedding_service=svc)

    queries = ["利润高的产品", "主播不喜欢低价款", "售后问题怎么处理"]

    for query_text in queries:
        print(f"--- 查询: \"{query_text}\" ---")

        # 语义检索
        with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
            with conn.cursor() as cur:
                # 直接用 pgvector 做语义搜索
                query_vec = svc.embed(query_text)
                cur.execute("""
                    SELECT memory_id, content, 1 - (embedding <=> %s::vector) AS similarity
                    FROM live_agent_anchor_memories
                    WHERE status = 'active' AND anchor_id = 'anchor-demo-001' AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT 3;
                """, (query_vec, query_vec))
                for mem_id, content, sim in cur.fetchall():
                    print(f"  [语义] id={mem_id} 相似度={sim:.4f} 内容={content[:60]}")

        print()

    print("=" * 60)
    print("演示结束。新 key 可用，embedding API 正常。")
    print("=" * 60)


if __name__ == "__main__":
    main()
