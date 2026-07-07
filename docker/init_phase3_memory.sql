-- LiveAgent Phase 3A 记忆与信任层初始化脚本。
-- 本脚本只创建脱敏样例项目需要的记忆、信任分和 Decision Trace 表，不存储真实平台 Token、
-- 真实用户身份或真实订单数据。重复执行是安全的，适合本地测试和 CLI 演示。

SELECT pg_advisory_xact_lock(hashtext('live_agent_phase3_memory_schema'));

-- pgcrypto 用于生成审计和记忆记录 UUID；vector 用于预留 embedding 字段，Phase 3A 暂不写入向量。
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- Phase 2A 的直播间表以 room_id 为主键；Phase 3A 需要额外保证 room_id 与 anchor_id 成对一致。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'live_agent_live_rooms_room_anchor_unique'
    ) THEN
        ALTER TABLE live_agent_live_rooms
            ADD CONSTRAINT live_agent_live_rooms_room_anchor_unique UNIQUE (room_id, anchor_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS live_agent_anchor_memories (
    memory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_key TEXT UNIQUE,
    anchor_id TEXT NOT NULL REFERENCES live_agent_anchors(anchor_id),
    room_id TEXT REFERENCES live_agent_live_rooms(room_id),
    layer TEXT NOT NULL CHECK (layer IN ('L1', 'L2', 'L3')),
    content TEXT NOT NULL CHECK (length(content) > 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence NUMERIC(4, 2) NOT NULL DEFAULT 0.70 CHECK (confidence >= 0 AND confidence <= 1),
    evidence_weight NUMERIC(4, 2) NOT NULL DEFAULT 0.50 CHECK (evidence_weight >= 0 AND evidence_weight <= 1),
    source TEXT NOT NULL CHECK (source IN ('user_stated', 'system_observed', 'offline_summary', 'manual_import')),
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'live_agent_anchor_memories_room_anchor_fkey'
    ) THEN
        ALTER TABLE live_agent_anchor_memories
            ADD CONSTRAINT live_agent_anchor_memories_room_anchor_fkey
            FOREIGN KEY (room_id, anchor_id)
            REFERENCES live_agent_live_rooms(room_id, anchor_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS live_agent_anchor_trust_state (
    anchor_id TEXT PRIMARY KEY REFERENCES live_agent_anchors(anchor_id),
    trust_score NUMERIC(4, 2) NOT NULL DEFAULT 0.70 CHECK (trust_score >= 0 AND trust_score <= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_agent_decision_trace (
    decision_trace_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id TEXT NOT NULL UNIQUE,
    anchor_id TEXT NOT NULL REFERENCES live_agent_anchors(anchor_id),
    room_id TEXT NOT NULL REFERENCES live_agent_live_rooms(room_id),
    recommendation JSONB NOT NULL DEFAULT '{}'::jsonb,
    anchor_action TEXT NOT NULL CHECK (anchor_action IN ('accepted', 'rejected')),
    business_result TEXT NOT NULL CHECK (business_result IN ('good', 'bad', 'agent_right', 'anchor_right')),
    lift NUMERIC(8, 4) NOT NULL DEFAULT 0,
    trust_delta NUMERIC(5, 2) NOT NULL DEFAULT 0,
    final_trust_score NUMERIC(4, 2) NOT NULL CHECK (final_trust_score >= 0 AND final_trust_score <= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'live_agent_decision_trace_room_anchor_fkey'
    ) THEN
        ALTER TABLE live_agent_decision_trace
            ADD CONSTRAINT live_agent_decision_trace_room_anchor_fkey
            FOREIGN KEY (room_id, anchor_id)
            REFERENCES live_agent_live_rooms(room_id, anchor_id);
    END IF;
END $$;

-- anchor_id/layer 是播前读取偏好的主入口；room_id 允许读取某场直播专属记忆。
CREATE INDEX IF NOT EXISTS idx_live_agent_anchor_memories_anchor_layer
    ON live_agent_anchor_memories(anchor_id, layer, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_live_agent_anchor_memories_room
    ON live_agent_anchor_memories(room_id, created_at DESC);

-- trace_id 已由 UNIQUE 约束自动创建索引，用于和工具审计、LangGraph thread_id 做统一回放。
