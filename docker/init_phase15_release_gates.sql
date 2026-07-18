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

-- Task 4：Phase 15 自有 smoke 预算，不复用 Phase 13/14 账本额度。
CREATE TABLE IF NOT EXISTS phase15_budget_ledgers (
    scope_id TEXT PRIMARY KEY,
    limit_cny NUMERIC(12, 6) NOT NULL DEFAULT 0.60
        CHECK (limit_cny = 0.60 AND limit_cny <> 'NaN'::numeric),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase15_budget_reservations (
    reservation_id UUID PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES phase15_budget_ledgers(scope_id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    reserved_amount_cny NUMERIC(12, 6) NOT NULL
        CHECK (reserved_amount_cny > 0 AND reserved_amount_cny <= 0.60 AND reserved_amount_cny <> 'NaN'::numeric),
    settled_amount_cny NUMERIC(12, 6),
    usage_known BOOLEAN,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SETTLED', 'RELEASED')),
    version BIGINT NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope_id, request_id),
    CHECK (
        (state IN ('RESERVED', 'RELEASED') AND settled_amount_cny IS NULL AND usage_known IS NULL)
        OR (state = 'SETTLED' AND settled_amount_cny IS NOT NULL AND settled_amount_cny >= 0 AND usage_known IS NOT NULL)
    )
);

-- Task 4：ReleaseRun 绑定完整预期 case 集合，旧 Task 1 空表通过幂等扩展升级。
ALTER TABLE phase15_release_runs
    ADD COLUMN IF NOT EXISTS expected_case_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS phase15_release_case_results (
    release_run_id TEXT NOT NULL REFERENCES phase15_release_runs(release_run_id) ON DELETE RESTRICT,
    case_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    artifact_digest TEXT NOT NULL CHECK (artifact_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'BLOCKED')),
    severe_violation BOOLEAN NOT NULL,
    result_snapshot JSONB NOT NULL CHECK (jsonb_typeof(result_snapshot) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (release_run_id, case_id)
);

CREATE TABLE IF NOT EXISTS phase15_release_technical_decisions (
    release_run_id TEXT PRIMARY KEY REFERENCES phase15_release_runs(release_run_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'BLOCKED')),
    decision_snapshot JSONB NOT NULL CHECK (jsonb_typeof(decision_snapshot) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase15_release_decisions (
    release_run_id TEXT PRIMARY KEY REFERENCES phase15_release_runs(release_run_id) ON DELETE RESTRICT,
    technical_status TEXT NOT NULL CHECK (technical_status IN ('PASS', 'FAIL', 'BLOCKED')),
    promotion_status TEXT NOT NULL CHECK (promotion_status IN ('PROMOTE', 'KEEP_DISABLED', 'BLOCKED')),
    final_status TEXT NOT NULL CHECK (final_status IN (
        'RELEASED_DECISION_SUPPORT_ENABLED',
        'RELEASED_DECISION_SUPPORT_DISABLED',
        'NOT_RELEASED'
    )),
    decision_snapshot JSONB NOT NULL CHECK (jsonb_typeof(decision_snapshot) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS phase15_release_runs_mode_status_idx
    ON phase15_release_runs (mode, status, created_at DESC);

CREATE INDEX IF NOT EXISTS phase15_release_case_results_run_idx
    ON phase15_release_case_results (release_run_id, created_at, case_id);
