-- Phase 15 Task 1：ReleaseRun 的最小事实表；后续 Task 4/7 只扩展 append-only 证据。
-- 本表不触发真实模型，也不依赖 GitHub Actions 私有存储。
CREATE TABLE IF NOT EXISTS phase15_release_runs (
    release_run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('PR', 'NIGHTLY', 'RELEASE')),
    manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PASS', 'FAIL', 'BLOCKED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS phase15_release_runs_mode_status_idx
    ON phase15_release_runs (mode, status, created_at DESC);
