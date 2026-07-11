-- Phase 7B 生产级 Agent 运行硬化：Harness Session 扩展 + 运维告警表。
-- 本脚本幂等，支持重复执行。
-- 使用 pg_advisory_xact_lock 保证并发安全。

BEGIN;

-- 并发锁：同一时间只有一个进程执行此 DDL
SELECT pg_advisory_xact_lock(hashtext('live_agent_phase7b_hardening_schema'));

-- ============================================================
-- 1. 扩展 live_agent_harness_sessions：添加生产级字段
-- ============================================================

-- 添加新字段（幂等：IF NOT EXISTS 在 Postgres 的 ALTER TABLE 中不支持列级别，
-- 改用 DO $$ 块捕获 duplicate column 异常）
DO $$
BEGIN
    BEGIN
        ALTER TABLE live_agent_harness_sessions
            ADD COLUMN approval_expires_at TIMESTAMPTZ;
    EXCEPTION
        WHEN duplicate_column THEN NULL;
    END;

    BEGIN
        ALTER TABLE live_agent_harness_sessions
            ADD COLUMN locked_by TEXT;
    EXCEPTION
        WHEN duplicate_column THEN NULL;
    END;

    BEGIN
        ALTER TABLE live_agent_harness_sessions
            ADD COLUMN lock_until TIMESTAMPTZ;
    EXCEPTION
        WHEN duplicate_column THEN NULL;
    END;

    BEGIN
        ALTER TABLE live_agent_harness_sessions
            ADD COLUMN idempotency_key TEXT;
    EXCEPTION
        WHEN duplicate_column THEN NULL;
    END;

    BEGIN
        ALTER TABLE live_agent_harness_sessions
            ADD COLUMN approval_attempts INTEGER NOT NULL DEFAULT 0;
    EXCEPTION
        WHEN duplicate_column THEN NULL;
    END;

    BEGIN
        ALTER TABLE live_agent_harness_sessions
            ADD COLUMN expired_reason TEXT;
    EXCEPTION
        WHEN duplicate_column THEN NULL;
    END;
END $$;

-- 重建 status CHECK 约束：删除旧的，添加包含 ''expired'' 和 ''locked'' 的新约束
DO $$
DECLARE
    old_conname TEXT;
BEGIN
    -- 查找旧约束名
    SELECT conname INTO old_conname
    FROM pg_constraint
    WHERE conrelid = ''live_agent_harness_sessions''::regclass
      AND contype = ''c''
      AND pg_get_constraintdef(oid) LIKE ''%pending_human%'';

    -- 先删除旧约束（如果存在）
    IF old_conname IS NOT NULL THEN
        EXECUTE format(''ALTER TABLE live_agent_harness_sessions DROP CONSTRAINT %I'', old_conname);
    END IF;

    -- 添加新约束（幂等：检查是否已存在同义约束）
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = ''live_agent_harness_sessions''::regclass
          AND contype = ''c''
          AND pg_get_constraintdef(oid) LIKE ''%expired%''
    ) THEN
        ALTER TABLE live_agent_harness_sessions
            ADD CONSTRAINT ck_harness_sessions_status_v2
            CHECK (status IN (''pending_human'', ''approved'', ''rejected'', ''completed'', ''error'', ''expired'', ''locked''));
    END IF;
END $$;

-- 创建索引用于查询 expired/locked 会话
CREATE INDEX IF NOT EXISTS idx_harness_sessions_expiry
    ON live_agent_harness_sessions(status, approval_expires_at)
    WHERE status = ''pending_human'';

CREATE INDEX IF NOT EXISTS idx_harness_sessions_idempotency
    ON live_agent_harness_sessions(trace_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- ============================================================
-- 2. 新增 live_agent_operational_alerts 表
-- ============================================================

CREATE TABLE IF NOT EXISTS live_agent_operational_alerts (
    alert_id TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL CHECK (alert_type IN (
        ''approval_expired'',
        ''duplicate_approval'',
        ''evaluation_retry_exhausted'',
        ''audit_write_failure'',
        ''replay_fidelity_degraded''
    )),
    severity TEXT NOT NULL CHECK (severity IN (''info'', ''warning'', ''error'', ''critical'')),
    source TEXT NOT NULL,
    trace_id TEXT,
    evaluation_id TEXT,
    message TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT ''{}''::jsonb,
    status TEXT NOT NULL DEFAULT ''open'' CHECK (status IN (''open'', ''acknowledged'', ''resolved'')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_operational_alerts_open
    ON live_agent_operational_alerts(status, created_at DESC)
    WHERE status = ''open'';

CREATE INDEX IF NOT EXISTS idx_operational_alerts_type
    ON live_agent_operational_alerts(alert_type, created_at DESC);

COMMIT;

