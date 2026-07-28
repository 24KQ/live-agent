-- Phase 16 V4 禁思考 JSON 协议探针的独立 append-only 账本。
-- 本表不存 Prompt、模型正文、思维链、API Key 或 Provider 原始 ID；它只记录运行身份、
-- 脱敏摘要、usage、稳定解析分类和终态。V1/V2/V3 历史表绝不在此迁移中被修改。

CREATE TABLE IF NOT EXISTS phase16_v4_json_probe_runs (
    run_id TEXT PRIMARY KEY CHECK (run_id='phase16-v4-json-probe-001'),
    protocol_digest CHAR(64) NOT NULL CHECK (protocol_digest ~ '^[0-9a-f]{64}$'),
    total_budget_cny NUMERIC(16,6) NOT NULL CHECK (total_budget_cny=1.000000),
    reservation_cny NUMERIC(16,6) NOT NULL CHECK (reservation_cny=0.010000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS phase16_v4_json_probe_prior_exposures (
    run_id TEXT NOT NULL REFERENCES phase16_v4_json_probe_runs(run_id),
    source_code TEXT NOT NULL CHECK (source_code='PHASE16_V1_V2_V3_CONSERVATIVE_EXPOSURE'),
    amount_cny NUMERIC(16,6) NOT NULL CHECK (amount_cny=0.202165),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, source_code)
);

CREATE TABLE IF NOT EXISTS phase16_v4_json_probe_dispatch_attempts (
    attempt_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES phase16_v4_json_probe_runs(run_id),
    case_id TEXT NOT NULL CHECK (case_id='phase16-v4-json-probe-minimal-json-001'),
    internal_request_id UUID NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS phase16_v4_json_probe_failure_facts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v4_json_probe_dispatch_attempts(attempt_id),
    failure_category TEXT NOT NULL CHECK (failure_category IN (
        'RATE_LIMITED', 'HTTP_ERROR', 'DEADLINE_EXCEEDED', 'TRANSPORT_ERROR',
        'INVALID_RESPONSE', 'INVALID_OUTPUT_JSON', 'MODEL_IDENTITY_MISMATCH',
        'FORBIDDEN_REASONING'
    )),
    request_sent BOOLEAN NOT NULL,
    response_digest CHAR(64) NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    http_status INTEGER NULL CHECK (http_status BETWEEN 100 AND 599),
    retry_after_seconds INTEGER NULL CHECK (retry_after_seconds>=0),
    latency_ms NUMERIC(16,3) NOT NULL CHECK (latency_ms>=0),
    parse_stage TEXT NULL CHECK (parse_stage IN (
        'CONTENT_MISSING', 'CONTENT_NON_STRING', 'OUTPUT_JSON_SYNTAX_INVALID',
        'OUTPUT_JSON_POLICY_INVALID'
    )),
    content_shape TEXT NULL CHECK (content_shape IN (
        'EMPTY', 'MARKDOWN_CODE_FENCE', 'JSON_OBJECT_LIKE', 'OTHER_TEXT'
    )),
    finish_reason TEXT NULL CHECK (finish_reason IN (
        'MISSING', 'STOP', 'LENGTH', 'TOOL_CALLS', 'CONTENT_FILTER', 'OTHER'
    )),
    reasoning_content_present BOOLEAN NULL,
    fact_digest CHAR(64) NOT NULL CHECK (fact_digest ~ '^[0-9a-f]{64}$'),
    auth_tag CHAR(64) NOT NULL CHECK (auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (parse_stage IS NULL AND content_shape IS NULL AND finish_reason IS NULL
         AND reasoning_content_present IS NULL)
        OR
        (parse_stage IS NOT NULL AND content_shape IS NOT NULL AND finish_reason IS NOT NULL
         AND reasoning_content_present IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS phase16_v4_json_probe_receipts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v4_json_probe_dispatch_attempts(attempt_id),
    provider_response_id_digest CHAR(64) NULL CHECK (provider_response_id_digest ~ '^[0-9a-f]{64}$'),
    finish_reason TEXT NOT NULL CHECK (finish_reason IN (
        'MISSING', 'STOP', 'LENGTH', 'TOOL_CALLS', 'CONTENT_FILTER', 'OTHER'
    )),
    model_id TEXT NOT NULL CHECK (model_id='deepseek-v4-pro'),
    response_digest CHAR(64) NOT NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    input_tokens INTEGER NULL CHECK (input_tokens>=0),
    output_tokens INTEGER NULL CHECK (output_tokens>=0),
    total_tokens INTEGER NULL,
    latency_ms NUMERIC(16,3) NOT NULL CHECK (latency_ms>=0),
    output_digest CHAR(64) NOT NULL CHECK (output_digest ~ '^[0-9a-f]{64}$'),
    receipt_complete BOOLEAN NOT NULL,
    auth_tag CHAR(64) NOT NULL CHECK (auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL)
        OR
        (input_tokens IS NOT NULL AND output_tokens IS NOT NULL
         AND total_tokens=input_tokens+output_tokens)
    ),
    CHECK (
        NOT receipt_complete OR (
            provider_response_id_digest IS NOT NULL
            AND finish_reason='STOP'
            AND input_tokens IS NOT NULL
            AND output_tokens IS NOT NULL
            AND total_tokens IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS phase16_v4_json_probe_outcomes (
    run_id TEXT PRIMARY KEY REFERENCES phase16_v4_json_probe_runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z0-9_]+$'),
    outcome_digest CHAR(64) NOT NULL CHECK (outcome_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- 所有 V4 事实均不可 UPDATE/DELETE/TRUNCATE。重新诊断必须创建新的设计和独立 run，
-- 不能通过修改旧记录把失败伪装成成功。
CREATE OR REPLACE FUNCTION phase16_v4_json_probe_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 V4 JSON probe facts are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v4_json_probe_reject_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 V4 JSON probe facts cannot be truncated';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v4_json_probe_validate_attempt() RETURNS trigger AS $$
BEGIN
    IF NEW.run_id<>'phase16-v4-json-probe-001'
       OR NEW.case_id<>'phase16-v4-json-probe-minimal-json-001' THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe attempt identity is invalid';
    END IF;
    IF EXISTS (SELECT 1 FROM phase16_v4_json_probe_outcomes WHERE run_id=NEW.run_id) THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe run is terminal';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v4_json_probe_validate_failure() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM phase16_v4_json_probe_receipts WHERE attempt_id=NEW.attempt_id) THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe failure cannot coexist with receipt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v4_json_probe_validate_receipt() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM phase16_v4_json_probe_failure_facts WHERE attempt_id=NEW.attempt_id) THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe receipt cannot coexist with failure';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v4_json_probe_validate_outcome() RETURNS trigger AS $$
DECLARE
    attempt_value UUID;
BEGIN
    SELECT attempt_id INTO attempt_value
      FROM phase16_v4_json_probe_dispatch_attempts WHERE run_id=NEW.run_id;
    IF attempt_value IS NULL THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe outcome requires attempt';
    END IF;
    IF NEW.status='PASS' AND NOT EXISTS (
        SELECT 1 FROM phase16_v4_json_probe_receipts
         WHERE attempt_id=attempt_value AND receipt_complete=true
    ) THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe PASS requires complete receipt';
    END IF;
    IF NEW.status='FAILED' AND NOT (
        EXISTS (SELECT 1 FROM phase16_v4_json_probe_failure_facts
                 WHERE attempt_id=attempt_value AND request_sent=true)
        OR EXISTS (SELECT 1 FROM phase16_v4_json_probe_receipts WHERE attempt_id=attempt_value)
    ) THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe FAILED requires sent fact';
    END IF;
    IF NEW.status='BLOCKED' AND NOT EXISTS (
        SELECT 1 FROM phase16_v4_json_probe_failure_facts
         WHERE attempt_id=attempt_value AND request_sent=false
    ) THEN
        RAISE EXCEPTION 'phase16 V4 JSON probe BLOCKED requires unsent failure';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'phase16_v4_json_probe_runs',
        'phase16_v4_json_probe_prior_exposures',
        'phase16_v4_json_probe_dispatch_attempts',
        'phase16_v4_json_probe_failure_facts',
        'phase16_v4_json_probe_receipts',
        'phase16_v4_json_probe_outcomes'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_append_only ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_%s_append_only BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION phase16_v4_json_probe_reject_mutation()', table_name, table_name);
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_no_truncate ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_%s_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION phase16_v4_json_probe_reject_truncate()', table_name, table_name);
    END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS trg_phase16_v4_json_probe_attempt ON phase16_v4_json_probe_dispatch_attempts;
CREATE TRIGGER trg_phase16_v4_json_probe_attempt
    BEFORE INSERT ON phase16_v4_json_probe_dispatch_attempts
    FOR EACH ROW EXECUTE FUNCTION phase16_v4_json_probe_validate_attempt();
DROP TRIGGER IF EXISTS trg_phase16_v4_json_probe_failure ON phase16_v4_json_probe_failure_facts;
CREATE TRIGGER trg_phase16_v4_json_probe_failure
    BEFORE INSERT ON phase16_v4_json_probe_failure_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_v4_json_probe_validate_failure();
DROP TRIGGER IF EXISTS trg_phase16_v4_json_probe_receipt ON phase16_v4_json_probe_receipts;
CREATE TRIGGER trg_phase16_v4_json_probe_receipt
    BEFORE INSERT ON phase16_v4_json_probe_receipts
    FOR EACH ROW EXECUTE FUNCTION phase16_v4_json_probe_validate_receipt();
DROP TRIGGER IF EXISTS trg_phase16_v4_json_probe_outcome ON phase16_v4_json_probe_outcomes;
CREATE TRIGGER trg_phase16_v4_json_probe_outcome
    BEFORE INSERT ON phase16_v4_json_probe_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_v4_json_probe_validate_outcome();
