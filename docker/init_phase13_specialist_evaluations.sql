-- Phase 13 Specialist 模型预算、预留与调用结算事实。

CREATE TABLE IF NOT EXISTS specialist_model_budget_ledgers (
    scope_id TEXT PRIMARY KEY,
    total_limit_cny NUMERIC(12, 6) NOT NULL CHECK (total_limit_cny >= 0 AND total_limit_cny <> 'NaN'::numeric),
    phase13_limit_cny NUMERIC(12, 6) NOT NULL CHECK (phase13_limit_cny >= 0 AND phase13_limit_cny <> 'NaN'::numeric),
    phase14_reserved_cny NUMERIC(12, 6) NOT NULL CHECK (phase14_reserved_cny >= 0 AND phase14_reserved_cny <> 'NaN'::numeric),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (phase13_limit_cny + phase14_reserved_cny <= total_limit_cny)
);

CREATE TABLE IF NOT EXISTS specialist_model_budget_candidates (
    scope_id TEXT NOT NULL REFERENCES specialist_model_budget_ledgers(scope_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY', 'PHASE14_COPILOT')),
    initial_limit_cny NUMERIC(12, 6) NOT NULL CHECK (initial_limit_cny >= 0 AND initial_limit_cny <> 'NaN'::numeric),
    state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'RELEASED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS specialist_model_budget_reservations (
    reservation_id UUID PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES specialist_model_budget_ledgers(scope_id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY', 'PHASE14_COPILOT')),
    reserved_amount_cny NUMERIC(12, 6) NOT NULL CHECK (reserved_amount_cny > 0 AND reserved_amount_cny <> 'NaN'::numeric),
    settled_amount_cny NUMERIC(12, 6),
    usage_known BOOLEAN,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SETTLED', 'RELEASED')),
    version BIGINT NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_id, request_id),
    UNIQUE (scope_id, request_id, state, settled_amount_cny, usage_known),
    FOREIGN KEY (scope_id, candidate_id)
        REFERENCES specialist_model_budget_candidates(scope_id, candidate_id) ON DELETE RESTRICT,
    CHECK (
        (state = 'RESERVED' AND settled_amount_cny IS NULL AND usage_known IS NULL)
        OR (state = 'RELEASED' AND settled_amount_cny IS NULL AND usage_known IS NULL)
        OR (state = 'SETTLED' AND settled_amount_cny IS NOT NULL AND usage_known IS NOT NULL
            AND settled_amount_cny >= 0)
    )
);

CREATE TABLE IF NOT EXISTS specialist_model_calls (
    call_id UUID PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES specialist_model_budget_ledgers(scope_id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    reservation_state TEXT NOT NULL DEFAULT 'SETTLED' CHECK (reservation_state = 'SETTLED'),
    settled_amount_cny NUMERIC(12, 6) NOT NULL CHECK (settled_amount_cny >= 0 AND settled_amount_cny <> 'NaN'::numeric),
    usage_known BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_id, request_id),
    FOREIGN KEY (scope_id, request_id, reservation_state, settled_amount_cny, usage_known)
        REFERENCES specialist_model_budget_reservations(
            scope_id, request_id, state, settled_amount_cny, usage_known
        ) ON DELETE RESTRICT
);

ALTER TABLE specialist_model_calls
    ADD COLUMN IF NOT EXISTS reservation_state TEXT NOT NULL DEFAULT 'SETTLED';

DO $$
DECLARE
    constraint_record RECORD;
BEGIN
    -- 旧 Task 3 DDL 曾禁止实际费用高于预留；这会在价格表漂移时迫使应用少记
    -- 已发生费用。滚动迁移时精确删除该表达式约束，再由状态形状约束保持非负要求。
    FOR constraint_record IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'specialist_model_budget_reservations'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%settled_amount_cny <= reserved_amount_cny%'
    LOOP
        EXECUTE format(
            'ALTER TABLE specialist_model_budget_reservations DROP CONSTRAINT %I',
            constraint_record.conname
        );
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_state_shape') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_state_shape
            CHECK (
                (state = 'RESERVED' AND settled_amount_cny IS NULL AND usage_known IS NULL)
                OR (state = 'RELEASED' AND settled_amount_cny IS NULL AND usage_known IS NULL)
                OR (state = 'SETTLED' AND settled_amount_cny IS NOT NULL AND usage_known IS NOT NULL
                    AND settled_amount_cny >= 0)
            );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_ledger_amounts_finite') THEN
        ALTER TABLE specialist_model_budget_ledgers
            ADD CONSTRAINT specialist_budget_ledger_amounts_finite
            CHECK (total_limit_cny <> 'NaN'::numeric AND phase13_limit_cny <> 'NaN'::numeric AND phase14_reserved_cny <> 'NaN'::numeric);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_candidate_amount_finite') THEN
        ALTER TABLE specialist_model_budget_candidates
            ADD CONSTRAINT specialist_budget_candidate_amount_finite
            CHECK (initial_limit_cny <> 'NaN'::numeric);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_candidate_fk') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_candidate_fk
            FOREIGN KEY (scope_id, candidate_id)
            REFERENCES specialist_model_budget_candidates(scope_id, candidate_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_settlement_unique') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_settlement_unique
            UNIQUE (scope_id, request_id, state, settled_amount_cny, usage_known);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_model_calls_settlement_fk') THEN
        ALTER TABLE specialist_model_calls
            DROP CONSTRAINT IF EXISTS specialist_model_calls_scope_id_request_id_fkey;
        ALTER TABLE specialist_model_calls
            ADD CONSTRAINT specialist_model_calls_settlement_fk
            FOREIGN KEY (scope_id, request_id, reservation_state, settled_amount_cny, usage_known)
            REFERENCES specialist_model_budget_reservations(
                scope_id, request_id, state, settled_amount_cny, usage_known
            ) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_amount_finite') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_amount_finite
            CHECK (reserved_amount_cny <> 'NaN'::numeric AND (settled_amount_cny IS NULL OR settled_amount_cny <> 'NaN'::numeric));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_model_calls_amount_finite') THEN
        ALTER TABLE specialist_model_calls
            ADD CONSTRAINT specialist_model_calls_amount_finite
            CHECK (settled_amount_cny <> 'NaN'::numeric);
    END IF;
END $$;

-- D-122：升级 Phase 13 已创建但仍使用旧总账本的 scope；只匹配冻结旧值，
-- 不覆盖已经被运营或验收显式调整过的其他预算策略。
UPDATE specialist_model_budget_ledgers
SET total_limit_cny = 4.00,
    phase14_reserved_cny = 1.00
WHERE total_limit_cny = 3.00
  AND phase13_limit_cny = 2.40
  AND phase14_reserved_cny = 0.60;

CREATE INDEX IF NOT EXISTS specialist_model_budget_reservations_scope_state_idx
    ON specialist_model_budget_reservations (scope_id, state, candidate_id);

-- Task 5：正式 Specialist 配对评估事实。Attempt 永不覆盖，正式选择单独持久化。
CREATE TABLE IF NOT EXISTS specialist_evaluation_manifests (
    manifest_id TEXT PRIMARY KEY,
    manifest_version TEXT NOT NULL,
    manifest_kind TEXT NOT NULL DEFAULT 'DATASET_BASELINE'
        CHECK (manifest_kind IN ('DATASET_BASELINE', 'FORMAL_EVALUATION')),
    source_commit TEXT,
    manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    dataset_digest TEXT NOT NULL CHECK (dataset_digest ~ '^[0-9a-f]{64}$'),
    schema_digest TEXT NOT NULL CHECK (schema_digest ~ '^[0-9a-f]{64}$'),
    generator_digest TEXT NOT NULL CHECK (generator_digest ~ '^[0-9a-f]{64}$'),
    seed BIGINT NOT NULL CHECK (seed >= 0),
    development_case_ids JSONB NOT NULL,
    validation_case_ids JSONB NOT NULL,
    holdout_case_ids JSONB NOT NULL,
    case_candidate_map JSONB NOT NULL,
    profile_bundle_digest TEXT NOT NULL CHECK (profile_bundle_digest ~ '^[0-9a-f]{64}$'),
    prompt_bundle_digest TEXT NOT NULL CHECK (prompt_bundle_digest ~ '^[0-9a-f]{64}$'),
    result_schema_bundle_digest TEXT NOT NULL CHECK (result_schema_bundle_digest ~ '^[0-9a-f]{64}$'),
    pricing_source_digest TEXT NOT NULL CHECK (pricing_source_digest ~ '^[0-9a-f]{64}$'),
    temperature NUMERIC(4, 3) NOT NULL CHECK (temperature = 0 AND temperature <> 'NaN'::numeric),
    code_digest TEXT NOT NULL CHECK (code_digest ~ '^[0-9a-f]{64}$'),
    price_policy_digest TEXT NOT NULL CHECK (price_policy_digest ~ '^[0-9a-f]{64}$'),
    endpoint_host TEXT NOT NULL,
    model_id TEXT NOT NULL,
    candidate_ids JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (manifest_id, manifest_digest),
    CHECK (
        (manifest_kind = 'DATASET_BASELINE' AND source_commit IS NULL)
        OR (manifest_kind = 'FORMAL_EVALUATION' AND source_commit ~ '^[0-9a-f]{40}$')
    )
);

ALTER TABLE specialist_evaluation_manifests
    ADD COLUMN IF NOT EXISTS manifest_kind TEXT NOT NULL DEFAULT 'DATASET_BASELINE';
ALTER TABLE specialist_evaluation_manifests
    ADD COLUMN IF NOT EXISTS source_commit TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'specialist_evaluation_manifests_kind_ck'
    ) THEN
        ALTER TABLE specialist_evaluation_manifests
            ADD CONSTRAINT specialist_evaluation_manifests_kind_ck CHECK (
                (manifest_kind = 'DATASET_BASELINE' AND source_commit IS NULL)
                OR (manifest_kind = 'FORMAL_EVALUATION' AND source_commit ~ '^[0-9a-f]{40}$')
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS specialist_evaluation_runs (
    run_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    claim_version BIGINT NOT NULL DEFAULT 0 CHECK (claim_version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (manifest_id, manifest_digest)
        REFERENCES specialist_evaluation_manifests(manifest_id, manifest_digest) ON DELETE RESTRICT,
    UNIQUE (run_id, manifest_id, candidate_id),
    UNIQUE (run_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS specialist_case_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES specialist_evaluation_runs(run_id) ON DELETE RESTRICT,
    manifest_id TEXT NOT NULL REFERENCES specialist_evaluation_manifests(manifest_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    case_id TEXT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('DEVELOPMENT', 'VALIDATION', 'HOLDOUT')),
    subject TEXT NOT NULL CHECK (subject IN ('BASELINE', 'AGENT')),
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    success BOOLEAN NOT NULL,
    severe_violation BOOLEAN NOT NULL,
    infrastructure_failure BOOLEAN NOT NULL,
    latency_ms NUMERIC(12, 3) NOT NULL CHECK (latency_ms >= 0 AND latency_ms <> 'NaN'::numeric),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_cny NUMERIC(12, 6) NOT NULL CHECK (cost_cny >= 0 AND cost_cny <> 'NaN'::numeric),
    result_digest TEXT NOT NULL CHECK (result_digest ~ '^[0-9a-f]{64}$'),
    metric_outcomes JSONB NOT NULL CHECK (
        jsonb_typeof(metric_outcomes) = 'object' AND metric_outcomes <> '{}'::jsonb
    ),
    gate_results JSONB NOT NULL CHECK (jsonb_typeof(gate_results) = 'object'),
    result_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, case_id, subject, attempt_number),
    UNIQUE (attempt_id, run_id, case_id, subject),
    UNIQUE (attempt_id, manifest_id, candidate_id, case_id, subject),
    UNIQUE (attempt_id, run_id, manifest_id, candidate_id, case_id, subject),
    UNIQUE (attempt_id, run_id, manifest_id, candidate_id, case_id, subject, infrastructure_failure),
    FOREIGN KEY (run_id, manifest_id, candidate_id)
        REFERENCES specialist_evaluation_runs(run_id, manifest_id, candidate_id) ON DELETE RESTRICT,
    CHECK (NOT success OR NOT infrastructure_failure)
);

-- 开发期可能已由旧 Task 5 草案创建空表；显式补列并收紧约束，避免
-- CREATE TABLE IF NOT EXISTS 静默保留旧物理 Schema。若旧表有不完整数据，
-- SET NOT NULL 会 fail-closed，迁移不会猜测或补造评估事实。
ALTER TABLE specialist_case_attempts
    ADD COLUMN IF NOT EXISTS metric_outcomes JSONB;
ALTER TABLE specialist_case_attempts
    ADD COLUMN IF NOT EXISTS gate_results JSONB;
ALTER TABLE specialist_case_attempts
    ALTER COLUMN metric_outcomes SET NOT NULL;
ALTER TABLE specialist_case_attempts
    ALTER COLUMN gate_results SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'specialist_case_attempts_metric_outcomes_object_ck'
    ) THEN
        ALTER TABLE specialist_case_attempts
            ADD CONSTRAINT specialist_case_attempts_metric_outcomes_object_ck
            CHECK (
                jsonb_typeof(metric_outcomes) = 'object'
                AND metric_outcomes <> '{}'::jsonb
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'specialist_case_attempts_gate_results_object_ck'
    ) THEN
        ALTER TABLE specialist_case_attempts
            ADD CONSTRAINT specialist_case_attempts_gate_results_object_ck
            CHECK (jsonb_typeof(gate_results) = 'object');
    END IF;
END $$;

CREATE OR REPLACE FUNCTION reject_specialist_manifest_update()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'specialist evaluation manifest is immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS specialist_evaluation_manifest_immutable
    ON specialist_evaluation_manifests;
CREATE TRIGGER specialist_evaluation_manifest_immutable
BEFORE UPDATE ON specialist_evaluation_manifests
FOR EACH ROW EXECUTE FUNCTION reject_specialist_manifest_update();

CREATE TABLE IF NOT EXISTS specialist_selected_case_results (
    manifest_id TEXT NOT NULL REFERENCES specialist_evaluation_manifests(manifest_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    subject TEXT NOT NULL CHECK (subject IN ('BASELINE', 'AGENT')),
    attempt_id TEXT NOT NULL,
    infrastructure_failure BOOLEAN NOT NULL DEFAULT FALSE CHECK (infrastructure_failure = FALSE),
    selected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (manifest_id, candidate_id, case_id, subject),
    FOREIGN KEY (
        attempt_id, run_id, manifest_id, candidate_id, case_id, subject,
        infrastructure_failure
    )
        REFERENCES specialist_case_attempts(
            attempt_id, run_id, manifest_id, candidate_id, case_id, subject,
            infrastructure_failure
        ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS specialist_paired_metrics (
    manifest_id TEXT NOT NULL REFERENCES specialist_evaluation_manifests(manifest_id) ON DELETE RESTRICT,
    run_id TEXT NOT NULL REFERENCES specialist_evaluation_runs(run_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    split TEXT NOT NULL CHECK (split IN ('DEVELOPMENT', 'VALIDATION', 'HOLDOUT')),
    metric_id TEXT NOT NULL,
    case_ids JSONB NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count >= 1),
    baseline_success_count INTEGER NOT NULL CHECK (baseline_success_count >= 0),
    agent_success_count INTEGER NOT NULL CHECK (agent_success_count >= 0),
    baseline_rate NUMERIC(12, 6) NOT NULL CHECK (baseline_rate BETWEEN 0 AND 1 AND baseline_rate <> 'NaN'::numeric),
    agent_rate NUMERIC(12, 6) NOT NULL CHECK (agent_rate BETWEEN 0 AND 1 AND agent_rate <> 'NaN'::numeric),
    delta_percentage_points NUMERIC(12, 6) NOT NULL CHECK (delta_percentage_points <> 'NaN'::numeric),
    paired_wins INTEGER NOT NULL CHECK (paired_wins >= 0),
    paired_losses INTEGER NOT NULL CHECK (paired_losses >= 0),
    tied INTEGER NOT NULL CHECK (tied >= 0),
    severe_violation_count INTEGER NOT NULL CHECK (severe_violation_count >= 0),
    baseline_wilson_low NUMERIC(12, 6) NOT NULL CHECK (baseline_wilson_low BETWEEN 0 AND 1 AND baseline_wilson_low <> 'NaN'::numeric),
    baseline_wilson_high NUMERIC(12, 6) NOT NULL CHECK (baseline_wilson_high BETWEEN 0 AND 1 AND baseline_wilson_high <> 'NaN'::numeric),
    agent_wilson_low NUMERIC(12, 6) NOT NULL CHECK (agent_wilson_low BETWEEN 0 AND 1 AND agent_wilson_low <> 'NaN'::numeric),
    agent_wilson_high NUMERIC(12, 6) NOT NULL CHECK (agent_wilson_high BETWEEN 0 AND 1 AND agent_wilson_high <> 'NaN'::numeric),
    metric_facts_digest TEXT NOT NULL CHECK (metric_facts_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, split, metric_id),
    FOREIGN KEY (run_id, manifest_id, candidate_id)
        REFERENCES specialist_evaluation_runs(run_id, manifest_id, candidate_id) ON DELETE RESTRICT,
    CHECK (baseline_success_count <= sample_count AND agent_success_count <= sample_count),
    CHECK (paired_wins + paired_losses + tied = sample_count),
    CHECK (severe_violation_count <= sample_count),
    CHECK (baseline_wilson_low <= baseline_wilson_high),
    CHECK (agent_wilson_low <= agent_wilson_high)
);

CREATE TABLE IF NOT EXISTS specialist_retention_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES specialist_evaluation_runs(run_id) ON DELETE RESTRICT,
    manifest_id TEXT NOT NULL REFERENCES specialist_evaluation_manifests(manifest_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    decision TEXT NOT NULL CHECK (decision IN ('RETAINED', 'REJECTED', 'INCONCLUSIVE')),
    reason_code TEXT NOT NULL,
    external_evidence_sufficient BOOLEAN NOT NULL,
    severe_violation_count INTEGER NOT NULL CHECK (severe_violation_count >= 0),
    metrics_digest TEXT NOT NULL CHECK (metrics_digest ~ '^[0-9a-f]{64}$'),
    completed_validation_cases INTEGER NOT NULL CHECK (completed_validation_cases BETWEEN 0 AND 40),
    completed_holdout_cases INTEGER NOT NULL CHECK (completed_holdout_cases BETWEEN 0 AND 20),
    hard_gates_passed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (decision <> 'INCONCLUSIVE' OR external_evidence_sufficient = FALSE),
    CHECK (decision <> 'RETAINED' OR (
        severe_violation_count = 0 AND external_evidence_sufficient = TRUE
        AND completed_validation_cases = 40 AND completed_holdout_cases = 20
        AND hard_gates_passed = TRUE
    )),
    CONSTRAINT specialist_retention_decisions_manifest_candidate_uk
        UNIQUE (manifest_id, candidate_id),
    CONSTRAINT specialist_retention_decisions_run_manifest_candidate_fk
        FOREIGN KEY (run_id, manifest_id, candidate_id)
        REFERENCES specialist_evaluation_runs(run_id, manifest_id, candidate_id) ON DELETE RESTRICT
);

-- 兼容本机由旧草案创建的空表：从权威 Run 回填 manifest_id 后再收紧。
ALTER TABLE specialist_retention_decisions
    ADD COLUMN IF NOT EXISTS manifest_id TEXT;
UPDATE specialist_retention_decisions d
SET manifest_id = r.manifest_id
FROM specialist_evaluation_runs r
WHERE d.run_id = r.run_id AND d.manifest_id IS NULL;
ALTER TABLE specialist_retention_decisions
    ALTER COLUMN manifest_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'specialist_retention_decisions_manifest_candidate_uk'
    ) THEN
        ALTER TABLE specialist_retention_decisions
            ADD CONSTRAINT specialist_retention_decisions_manifest_candidate_uk
            UNIQUE (manifest_id, candidate_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'specialist_retention_decisions_run_manifest_candidate_fk'
    ) THEN
        ALTER TABLE specialist_retention_decisions
            ADD CONSTRAINT specialist_retention_decisions_run_manifest_candidate_fk
            FOREIGN KEY (run_id, manifest_id, candidate_id)
            REFERENCES specialist_evaluation_runs(run_id, manifest_id, candidate_id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION reject_specialist_immutable_fact_update()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'specialist evaluation fact is immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS specialist_case_attempt_immutable ON specialist_case_attempts;
CREATE TRIGGER specialist_case_attempt_immutable
BEFORE UPDATE ON specialist_case_attempts
FOR EACH ROW EXECUTE FUNCTION reject_specialist_immutable_fact_update();

DROP TRIGGER IF EXISTS specialist_selected_result_immutable ON specialist_selected_case_results;
CREATE TRIGGER specialist_selected_result_immutable
BEFORE UPDATE ON specialist_selected_case_results
FOR EACH ROW EXECUTE FUNCTION reject_specialist_immutable_fact_update();

DROP TRIGGER IF EXISTS specialist_paired_metric_immutable ON specialist_paired_metrics;
CREATE TRIGGER specialist_paired_metric_immutable
BEFORE UPDATE ON specialist_paired_metrics
FOR EACH ROW EXECUTE FUNCTION reject_specialist_immutable_fact_update();

DROP TRIGGER IF EXISTS specialist_retention_decision_immutable ON specialist_retention_decisions;
CREATE TRIGGER specialist_retention_decision_immutable
BEFORE UPDATE ON specialist_retention_decisions
FOR EACH ROW EXECUTE FUNCTION reject_specialist_immutable_fact_update();

-- D-122：保留 Phase 13 三候选的历史身份，同时为 Phase 14 Copilot 增加独立预算身份。
-- CREATE TABLE IF NOT EXISTS 不会升级旧 CHECK，因此显式重建两个候选约束；Phase 15
-- 的 0.60 元只保留在总账本中，当前不存在对应候选，不能被请求借用。
DO $$
BEGIN
    ALTER TABLE specialist_model_budget_candidates
        DROP CONSTRAINT IF EXISTS specialist_model_budget_candidates_candidate_id_check;
    ALTER TABLE specialist_model_budget_candidates
        ADD CONSTRAINT specialist_model_budget_candidates_candidate_id_check
        CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY', 'PHASE14_COPILOT'));

    ALTER TABLE specialist_model_budget_reservations
        DROP CONSTRAINT IF EXISTS specialist_model_budget_reservations_candidate_id_check;
    ALTER TABLE specialist_model_budget_reservations
        ADD CONSTRAINT specialist_model_budget_reservations_candidate_id_check
        CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY', 'PHASE14_COPILOT'));
END $$;
