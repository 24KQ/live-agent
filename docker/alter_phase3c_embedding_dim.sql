-- LiveAgent Phase 3C：将 embedding 字段从 vector(1536) 改为 vector(2048)
-- 智谱（bigmodel）embedding-3 模型输出 2048 维向量。
-- 当前所有 embedding 均为 NULL，无数据损失风险。
-- 执行时机：ALTER TABLE 直接改类型，pgvector 0.8.4 支持。
SELECT pg_advisory_xact_lock(hashtext('live_agent_phase3c_embedding_dim'));
ALTER TABLE live_agent_anchor_memories
    ALTER COLUMN embedding TYPE vector(2048);
