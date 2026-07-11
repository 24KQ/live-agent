-- Phase 7A Agent Replay / Evaluation tables.
-- 评估任务是生产排障和版本回归的事实源；这里使用 advisory lock 避免并行
-- 集成测试或多进程初始化时出现 DDL race。
SELECT pg_advisory_xact_lock(hashtext('live_agent_phase7a_agent_evaluations_schema'));

CREATE TABLE IF NOT EXISTS live_agent_evaluation_runs (
    evaluation_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'production_hybrid',
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'partial', 'failed')),
    replay_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    overall_score NUMERIC(6, 2),
    coverage_percent NUMERIC(6, 2),
    verdict TEXT,
    violations JSONB NOT NULL DEFAULT '[]'::jsonb,
    dimension_scores JSONB NOT NULL DEFAULT '[]'::jsonb,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(trace_id, evaluator_version, input_fingerprint, profile)
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_status_lease
    ON live_agent_evaluation_runs(status, lease_until, created_at);

CREATE INDEX IF NOT EXISTS idx_eval_runs_trace_updated
    ON live_agent_evaluation_runs(trace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS live_agent_evaluation_dimension_scores (
    id BIGSERIAL PRIMARY KEY,
    evaluation_id TEXT NOT NULL REFERENCES live_agent_evaluation_runs(evaluation_id) ON DELETE CASCADE,
    dimension TEXT NOT NULL,
    score NUMERIC(6, 2),
    weight NUMERIC(6, 2) NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    evaluator_type TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eval_dimension_scores_eval
    ON live_agent_evaluation_dimension_scores(evaluation_id);

CREATE TABLE IF NOT EXISTS live_agent_evaluation_reviews (
    review_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL REFERENCES live_agent_evaluation_runs(evaluation_id) ON DELETE CASCADE,
    operator_id TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_eval_reviews_eval
    ON live_agent_evaluation_reviews(evaluation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS live_agent_evaluation_datasets (
    dataset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_agent_evaluation_cases (
    case_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES live_agent_evaluation_datasets(dataset_id) ON DELETE CASCADE,
    input_snapshot JSONB NOT NULL,
    expected_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    forbidden_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    expected_gate TEXT,
    expected_terminal_status TEXT,
    rubric JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS live_agent_evaluation_batches (
    batch_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES live_agent_evaluation_datasets(dataset_id) ON DELETE CASCADE,
    candidate_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
