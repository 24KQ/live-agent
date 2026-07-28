-- Phase 16 V3 Planner 单次诊断的独立 append-only 账本。
-- V3 只记录一份新的诊断 run，绝不 ALTER、UPDATE 或 DELETE V1/V2 的任何历史事实。

CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_runs (
    run_id TEXT PRIMARY KEY CHECK (run_id='phase16-v3-planner-diagnostic-001'),
    manifest_digest CHAR(64) NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    planner_profile_digest CHAR(64) NOT NULL CHECK (planner_profile_digest ~ '^[0-9a-f]{64}$'),
    case_digest CHAR(64) NOT NULL CHECK (case_digest ~ '^[0-9a-f]{64}$'),
    total_budget_cny NUMERIC(12,6) NOT NULL CHECK (total_budget_cny=1.000000),
    planner_reservation_cny NUMERIC(12,6) NOT NULL CHECK (planner_reservation_cny=0.052000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_case_slots (
    run_id TEXT NOT NULL REFERENCES phase16_v3_planner_diagnostic_runs(run_id),
    case_id TEXT NOT NULL CHECK (case_id='phase16-high-conflict-paired-development-001'),
    case_digest CHAR(64) NOT NULL CHECK (case_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, case_id)
);

-- 每条先验风险分别留存，不能把 V2 无回执 Planner 或历史真实花费悄悄按零成本处理。
CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_prior_exposures (
    run_id TEXT NOT NULL REFERENCES phase16_v3_planner_diagnostic_runs(run_id),
    source TEXT NOT NULL CHECK (source IN (
        'PHASE16_V1_AND_HISTORICAL',
        'PHASE16_V2_ANALYST_ACTUAL',
        'PHASE16_V2_PLANNER_UNKNOWN_MAX'
    )),
    amount_cny NUMERIC(12,6) NOT NULL CHECK (amount_cny>=0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, source)
);

CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_dispatch_attempts (
    attempt_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES phase16_v3_planner_diagnostic_runs(run_id),
    case_id TEXT NOT NULL,
    planner_profile_digest CHAR(64) NOT NULL CHECK (planner_profile_digest ~ '^[0-9a-f]{64}$'),
    internal_request_id UUID NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (run_id, case_id)
);

-- 成功回执和失败事实互斥。回执仅保存摘要、usage 与稳定完成原因，不保存模型正文。
CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_provider_receipts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v3_planner_diagnostic_dispatch_attempts(attempt_id),
    provider_response_id_digest CHAR(64) NOT NULL CHECK (provider_response_id_digest ~ '^[0-9a-f]{64}$'),
    finish_reason TEXT NOT NULL CHECK (finish_reason='stop'),
    model_id TEXT NOT NULL CHECK (model_id='deepseek-v4-pro'),
    response_digest CHAR(64) NOT NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    input_tokens INTEGER NOT NULL CHECK (input_tokens>=0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens>=0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens=input_tokens+output_tokens),
    latency_ms NUMERIC(16,3) NOT NULL CHECK (latency_ms>=0),
    receipt_auth_tag CHAR(64) NOT NULL CHECK (receipt_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- V3 DDL 在本地已执行过一次但尚未记录任何 Provider receipt。升级时先只补列，若
-- 检测到旧回执则拒绝启动，而不是在没有私钥的条件下倒填或伪造 HMAC。
ALTER TABLE phase16_v3_planner_diagnostic_provider_receipts
    ADD COLUMN IF NOT EXISTS receipt_auth_tag CHAR(64);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM phase16_v3_planner_diagnostic_provider_receipts
         WHERE receipt_auth_tag IS NULL
    ) THEN
        RAISE EXCEPTION 'phase16 V3 legacy provider receipt lacks authenticity tag';
    END IF;
    ALTER TABLE phase16_v3_planner_diagnostic_provider_receipts
        ALTER COLUMN receipt_auth_tag SET NOT NULL;
    ALTER TABLE phase16_v3_planner_diagnostic_provider_receipts
        DROP CONSTRAINT IF EXISTS phase16_v3_planner_diagnostic_receipt_auth_tag_check;
    ALTER TABLE phase16_v3_planner_diagnostic_provider_receipts
        ADD CONSTRAINT phase16_v3_planner_diagnostic_receipt_auth_tag_check
        CHECK (receipt_auth_tag ~ '^[0-9a-f]{64}$');
END;
$$;

-- 失败事实故意只保存 ModelFailure 的结构化字段和 HMAC，不保存异常文本、Header 或正文。
CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_model_failure_facts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v3_planner_diagnostic_dispatch_attempts(attempt_id),
    failure_category TEXT NOT NULL CHECK (failure_category IN (
        'RATE_LIMITED', 'HTTP_ERROR', 'DEADLINE_EXCEEDED', 'TRANSPORT_ERROR',
        'INVALID_RESPONSE', 'INVALID_OUTPUT_JSON', 'FORBIDDEN_REASONING',
        'MODEL_IDENTITY_MISMATCH', 'RUNNER_OUTCOME_CONTRACT_BREACH'
    )),
    request_sent BOOLEAN NULL,
    response_digest CHAR(64) NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    http_status INTEGER NULL CHECK (http_status BETWEEN 100 AND 599),
    retry_after_seconds INTEGER NULL CHECK (retry_after_seconds>=0),
    latency_ms NUMERIC(16,3) NOT NULL CHECK (latency_ms>=0),
    fact_digest CHAR(64) NOT NULL CHECK (fact_digest ~ '^[0-9a-f]{64}$'),
    failure_auth_tag CHAR(64) NOT NULL CHECK (failure_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_validation_facts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v3_planner_diagnostic_dispatch_attempts(attempt_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS','FAILED','BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z0-9_]+$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS phase16_v3_planner_diagnostic_case_outcomes (
    run_id TEXT NOT NULL REFERENCES phase16_v3_planner_diagnostic_runs(run_id),
    case_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS','FAILED','BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z0-9_]+$'),
    outcome_digest CHAR(64) NOT NULL CHECK (outcome_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, case_id)
);

-- 所有 V3 事实表均为 append-only；报告和恢复只能读事实，不能原地修订不利结论。
CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 V3 planner diagnostic facts are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_reject_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 V3 planner diagnostic facts cannot be truncated';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_validate_attempt() RETURNS trigger AS $$
DECLARE
    run_row phase16_v3_planner_diagnostic_runs%ROWTYPE;
BEGIN
    SELECT * INTO run_row
      FROM phase16_v3_planner_diagnostic_runs
     WHERE run_id=NEW.run_id FOR UPDATE;
    IF NOT FOUND OR NEW.case_id<>'phase16-high-conflict-paired-development-001' THEN
        RAISE EXCEPTION 'phase16 V3 diagnostic attempt identity is invalid';
    END IF;
    IF NEW.planner_profile_digest<>run_row.planner_profile_digest THEN
        RAISE EXCEPTION 'phase16 V3 diagnostic planner profile conflicts';
    END IF;
    IF EXISTS (
        SELECT 1 FROM phase16_v3_planner_diagnostic_case_outcomes
         WHERE run_id=NEW.run_id AND case_id=NEW.case_id
    ) THEN
        RAISE EXCEPTION 'phase16 V3 diagnostic run is terminal';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_validate_failure() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM phase16_v3_planner_diagnostic_provider_receipts
         WHERE attempt_id=NEW.attempt_id
    ) THEN
        RAISE EXCEPTION 'phase16 V3 failure cannot coexist with provider receipt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_validate_receipt() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM phase16_v3_planner_diagnostic_model_failure_facts
         WHERE attempt_id=NEW.attempt_id
    ) THEN
        RAISE EXCEPTION 'phase16 V3 receipt cannot coexist with model failure';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_validate_validation() RETURNS trigger AS $$
BEGIN
    IF NEW.verdict='PASS' AND (
        NOT EXISTS (SELECT 1 FROM phase16_v3_planner_diagnostic_provider_receipts WHERE attempt_id=NEW.attempt_id)
        OR EXISTS (SELECT 1 FROM phase16_v3_planner_diagnostic_model_failure_facts WHERE attempt_id=NEW.attempt_id)
    ) THEN
        RAISE EXCEPTION 'phase16 V3 PASS validation requires provider receipt only';
    END IF;
    IF NEW.verdict='FAILED' AND NOT (
        EXISTS (
            SELECT 1 FROM phase16_v3_planner_diagnostic_model_failure_facts
             WHERE attempt_id=NEW.attempt_id AND request_sent IS DISTINCT FROM false
        )
        OR EXISTS (
            SELECT 1 FROM phase16_v3_planner_diagnostic_provider_receipts
             WHERE attempt_id=NEW.attempt_id
        )
    ) THEN
        -- 模型可带完整 receipt 成功返回，但随后因 AgentAction/Schema/Evidence 校验失败。
        -- 这类失败不能伪造 ModelFailure，也必须允许以 FAILED validation 如实闭合。
        RAISE EXCEPTION 'phase16 V3 FAILED validation requires sent failure or provider receipt';
    END IF;
    IF NEW.verdict='BLOCKED' AND NOT EXISTS (
        SELECT 1 FROM phase16_v3_planner_diagnostic_model_failure_facts
         WHERE attempt_id=NEW.attempt_id AND request_sent=false
    ) THEN
        RAISE EXCEPTION 'phase16 V3 BLOCKED validation requires unsent model failure';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v3_planner_diagnostic_validate_outcome() RETURNS trigger AS $$
DECLARE
    attempt_id_value UUID;
BEGIN
    SELECT attempt_id INTO attempt_id_value
      FROM phase16_v3_planner_diagnostic_dispatch_attempts
     WHERE run_id=NEW.run_id AND case_id=NEW.case_id;
    IF attempt_id_value IS NULL THEN
        RAISE EXCEPTION 'phase16 V3 outcome requires dispatch attempt';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM phase16_v3_planner_diagnostic_validation_facts
         WHERE attempt_id=attempt_id_value AND verdict=NEW.status
    ) THEN
        RAISE EXCEPTION 'phase16 V3 outcome verdict conflicts with validation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'phase16_v3_planner_diagnostic_runs',
        'phase16_v3_planner_diagnostic_case_slots',
        'phase16_v3_planner_diagnostic_prior_exposures',
        'phase16_v3_planner_diagnostic_dispatch_attempts',
        'phase16_v3_planner_diagnostic_provider_receipts',
        'phase16_v3_planner_diagnostic_model_failure_facts',
        'phase16_v3_planner_diagnostic_validation_facts',
        'phase16_v3_planner_diagnostic_case_outcomes'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_append_only ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_%s_append_only BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION phase16_v3_planner_diagnostic_reject_mutation()', table_name, table_name);
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_no_truncate ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_%s_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION phase16_v3_planner_diagnostic_reject_truncate()', table_name, table_name);
    END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS trg_phase16_v3_diagnostic_attempt ON phase16_v3_planner_diagnostic_dispatch_attempts;
CREATE TRIGGER trg_phase16_v3_diagnostic_attempt
    BEFORE INSERT ON phase16_v3_planner_diagnostic_dispatch_attempts
    FOR EACH ROW EXECUTE FUNCTION phase16_v3_planner_diagnostic_validate_attempt();
DROP TRIGGER IF EXISTS trg_phase16_v3_diagnostic_failure ON phase16_v3_planner_diagnostic_model_failure_facts;
CREATE TRIGGER trg_phase16_v3_diagnostic_failure
    BEFORE INSERT ON phase16_v3_planner_diagnostic_model_failure_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_v3_planner_diagnostic_validate_failure();
DROP TRIGGER IF EXISTS trg_phase16_v3_diagnostic_receipt ON phase16_v3_planner_diagnostic_provider_receipts;
CREATE TRIGGER trg_phase16_v3_diagnostic_receipt
    BEFORE INSERT ON phase16_v3_planner_diagnostic_provider_receipts
    FOR EACH ROW EXECUTE FUNCTION phase16_v3_planner_diagnostic_validate_receipt();
DROP TRIGGER IF EXISTS trg_phase16_v3_diagnostic_validation ON phase16_v3_planner_diagnostic_validation_facts;
CREATE TRIGGER trg_phase16_v3_diagnostic_validation
    BEFORE INSERT ON phase16_v3_planner_diagnostic_validation_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_v3_planner_diagnostic_validate_validation();
DROP TRIGGER IF EXISTS trg_phase16_v3_diagnostic_outcome ON phase16_v3_planner_diagnostic_case_outcomes;
CREATE TRIGGER trg_phase16_v3_diagnostic_outcome
    BEFORE INSERT ON phase16_v3_planner_diagnostic_case_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_v3_planner_diagnostic_validate_outcome();
