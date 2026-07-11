-- Phase 6C Harness Agent Web 会话表。
-- 该表保存副屏可查询的审批会话状态；LangGraph checkpoint 仍由官方 PostgresSaver 表管理。

CREATE TABLE IF NOT EXISTS live_agent_harness_sessions (
    trace_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    anchor_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending_human', 'approved', 'rejected', 'completed', 'error')),
    approval_request JSONB NOT NULL DEFAULT '{}'::jsonb,
    interrupt_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    latest_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    approval_decision TEXT,
    operator_id TEXT,
    reason TEXT,
    audit_status TEXT,
    audit_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision_trace_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_harness_sessions_room_updated
    ON live_agent_harness_sessions(room_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_harness_sessions_status_updated
    ON live_agent_harness_sessions(status, updated_at DESC);
