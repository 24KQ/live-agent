-- LiveAgent Phase 4D 弹幕聚合结果持久化建表脚本。
-- 不存原始弹幕，只存 5s 窗口聚合结果。
SELECT pg_advisory_xact_lock(hashtext('live_agent_phase4_danmaku_aggregates'));
CREATE TABLE IF NOT EXISTS live_agent_danmaku_aggregates (
    id SERIAL PRIMARY KEY,
    room_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count > 0),
    sample_contents JSONB NOT NULL DEFAULT '[]'::jsonb,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_danmaku_aggr_room_time
    ON live_agent_danmaku_aggregates(room_id, window_start DESC);
