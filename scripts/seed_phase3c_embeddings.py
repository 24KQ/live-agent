"""Phase 3C Embedding 回填脚本。

对 live_agent_anchor_memories 表中 embedding 为 NULL 的记录，
调用智谱 embedding-3 API 生成 2048 维向量并回填到数据库。
"""

import psycopg
from src.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"[Phase 3C Embedding Backfill] 连接 PostgreSQL: {settings.postgres_safe_dsn}")

    with psycopg.connect(**settings.postgres_connection_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM live_agent_anchor_memories WHERE embedding IS NULL;"
            )
            total = cur.fetchone()[0]
            print(f"[Phase 3C Embedding Backfill] 需要回填的记忆数: {total}")

            if total == 0:
                print("[Phase 3C Embedding Backfill] 所有记忆已有 embedding，无需回填。")
                return

            cur.execute(
                "SELECT memory_id, content FROM live_agent_anchor_memories WHERE embedding IS NULL;"
            )
            from src.skills.embedding_service import EmbeddingService
            svc = EmbeddingService(settings=settings)

            updated = 0
            failed = 0
            for memory_id, content in cur.fetchall():
                try:
                    emb = svc.embed(content)
                    if not emb:
                        failed += 1
                        print(f"  [SKIP] API 返回空: {memory_id}")
                        continue
                    with conn.cursor() as update_cur:
                        update_cur.execute(
                            "UPDATE live_agent_anchor_memories SET embedding = %s WHERE memory_id = %s;",
                            (emb, memory_id),
                        )
                    conn.commit()
                    updated += 1
                    print(f"  [OK] {memory_id}: 已回填 (维度={len(emb)})")
                except Exception as exc:
                    conn.rollback()
                    failed += 1
                    print(f"  [FAIL] {memory_id}: {exc}")

            print(f"\n[Phase 3C Embedding Backfill] 完成: 成功 {updated}, 失败 {failed}")


if __name__ == "__main__":
    main()
