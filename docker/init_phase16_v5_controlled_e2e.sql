-- Phase 16 V5 受控双 Agent E2E campaign 的独立 append-only 审计账本。
-- V1 至 V4 的任何表、触发器和历史 row 都不会被本 DDL 读取、更新或重签。

CREATE TABLE IF NOT EXISTS phase16_v5_campaigns (
    campaign_id TEXT PRIMARY KEY,
    manifest_digest CHAR(64) NOT NULL,
    total_budget_cny NUMERIC(12,6) NOT NULL CHECK (total_budget_cny = 1.000000),
    input_cny_per_million NUMERIC(12,6) NOT NULL CHECK (input_cny_per_million = 3.000000),
    output_cny_per_million NUMERIC(12,6) NOT NULL CHECK (output_cny_per_million = 6.000000),
    analyst_profile_digest CHAR(64) NOT NULL,
    planner_profile_digest CHAR(64) NOT NULL,
    model_id TEXT NOT NULL,
    thinking_mode TEXT NOT NULL CHECK (thinking_mode = 'disabled'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- run 本身永远不修改。终态另写到 phase16_v5_run_outcomes，保证运行身份与结论都是追加事实。
CREATE TABLE IF NOT EXISTS phase16_v5_runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES phase16_v5_campaigns(campaign_id),
    run_kind TEXT NOT NULL CHECK (run_kind IN ('CALIBRATION', 'FORMAL')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, run_kind)
);

-- 校准只有一条合成 slot；正式 run 必须固定十条 slot。行号和 case 摘要都属于 Manifest 身份。
CREATE TABLE IF NOT EXISTS phase16_v5_case_slots (
    run_id TEXT NOT NULL REFERENCES phase16_v5_runs(run_id),
    slot_position INTEGER NOT NULL CHECK (slot_position > 0),
    case_id TEXT NOT NULL,
    case_digest CHAR(64) NOT NULL,
    PRIMARY KEY (run_id, case_id),
    UNIQUE (run_id, slot_position)
);

CREATE TABLE IF NOT EXISTS phase16_v5_case_claims (
    claim_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, case_id),
    FOREIGN KEY (run_id, case_id) REFERENCES phase16_v5_case_slots(run_id, case_id)
);

-- attempt 写入发生在 HTTP 之前，因此它是一次外部尝试的不可重试意图，不代表 Provider 已确认收包。
CREATE TABLE IF NOT EXISTS phase16_v5_dispatch_attempts (
    attempt_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES phase16_v5_runs(run_id),
    claim_id UUID NOT NULL REFERENCES phase16_v5_case_claims(claim_id),
    stage TEXT NOT NULL CHECK (stage IN ('ANALYST', 'PLANNER')),
    profile_digest CHAR(64) NOT NULL,
    internal_request_id UUID NOT NULL,
    reservation_cny NUMERIC(12,6) NOT NULL CHECK (reservation_cny > 0 AND reservation_cny <= 0.030000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, stage),
    UNIQUE (internal_request_id)
);

-- 原始 Provider ID 和模型正文永不入库；只存其不可逆摘要及可审计 usage/成本。
CREATE TABLE IF NOT EXISTS phase16_v5_provider_receipts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v5_dispatch_attempts(attempt_id),
    provider_response_id_digest CHAR(64),
    finish_reason TEXT,
    model_id TEXT NOT NULL,
    response_digest CHAR(64) NOT NULL,
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens >= 0),
    latency_ms NUMERIC(14,3) NOT NULL CHECK (latency_ms >= 0),
    actual_cost_cny NUMERIC(12,6) CHECK (actual_cost_cny >= 0),
    output_digest CHAR(64) NOT NULL,
    receipt_complete BOOLEAN NOT NULL,
    receipt_auth_tag CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (receipt_complete = false)
        OR (
            provider_response_id_digest IS NOT NULL
            AND finish_reason = 'stop'
            AND model_id IS NOT NULL
            AND input_tokens IS NOT NULL
            AND output_tokens IS NOT NULL
            AND total_tokens = input_tokens + output_tokens
            AND actual_cost_cny IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS phase16_v5_validation_facts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_v5_dispatch_attempts(attempt_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    validation_digest CHAR(64) NOT NULL,
    validation_auth_tag CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- recovery fact 只表达“进程重启后此 claim 已不能继续”的审计结论。它不替代任何模型
-- validation，也不保存 Prompt、模型正文、异常文本或 Provider 原始标识；用于让已通过
-- 单阶段校验但未写 case/run outcome 的崩溃状态也能 append-only 地终结，且绝不重发。
CREATE TABLE IF NOT EXISTS phase16_v5_recovery_facts (
    claim_id UUID PRIMARY KEY REFERENCES phase16_v5_case_claims(claim_id),
    run_id TEXT NOT NULL REFERENCES phase16_v5_runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    recovery_digest CHAR(64) NOT NULL,
    recovery_auth_tag CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phase16_v5_case_outcomes (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    claim_id UUID NOT NULL REFERENCES phase16_v5_case_claims(claim_id),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    outcome_digest CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, case_id),
    UNIQUE (claim_id),
    FOREIGN KEY (run_id, case_id) REFERENCES phase16_v5_case_slots(run_id, case_id)
);

CREATE TABLE IF NOT EXISTS phase16_v5_run_outcomes (
    run_id TEXT PRIMARY KEY REFERENCES phase16_v5_runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    outcome_digest CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 所有 V5 表均拒绝更新、删除和 truncate。终态的状态变化以独立 outcome row 表达，
-- 避免“把失败改成通过”的数据库旁路。
CREATE OR REPLACE FUNCTION phase16_v5_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 V5 ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_v5_reject_truncate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 V5 ledger cannot be truncated';
END;
$$ LANGUAGE plpgsql;

-- Planner intent 只能出现在 Analyst 的完整 receipt 和 PASS validation 之后，数据库层也
-- 独立于 Python Runner 强制串行双 Agent 顺序。
CREATE OR REPLACE FUNCTION phase16_v5_validate_dispatch_attempt()
RETURNS trigger AS $$
DECLARE
    analyst_pass BOOLEAN;
BEGIN
    IF NEW.stage = 'PLANNER' THEN
        SELECT EXISTS (
            SELECT 1
              FROM phase16_v5_dispatch_attempts analyst
              JOIN phase16_v5_provider_receipts receipt ON receipt.attempt_id = analyst.attempt_id
              JOIN phase16_v5_validation_facts validation ON validation.attempt_id = analyst.attempt_id
             WHERE analyst.claim_id = NEW.claim_id
               AND analyst.stage = 'ANALYST'
               AND receipt.receipt_complete = true
               AND validation.verdict = 'PASS'
        ) INTO analyst_pass;
        IF NOT analyst_pass THEN
            RAISE EXCEPTION 'phase16 V5 planner requires completed analyst validation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- PASS validation 不能没有完整 receipt；BLOCKED validation 不能伪装成一条已完成 receipt。
CREATE OR REPLACE FUNCTION phase16_v5_validate_validation_fact()
RETURNS trigger AS $$
DECLARE
    receipt_complete BOOLEAN;
BEGIN
    SELECT receipt.receipt_complete INTO receipt_complete
      FROM phase16_v5_provider_receipts receipt WHERE receipt.attempt_id = NEW.attempt_id;
    IF NEW.verdict = 'PASS' AND receipt_complete IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'phase16 V5 PASS validation requires complete receipt';
    END IF;
    IF NEW.verdict = 'BLOCKED' AND receipt_complete IS NOT NULL THEN
        RAISE EXCEPTION 'phase16 V5 BLOCKED validation cannot have receipt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- case PASS 必须恰有两个通过 stage，且收据完整。失败/阻断也必须有相应 validation，
-- 或唯一的 recovery fact；后者只在进程崩溃后封闭未完成 claim，防止客户端直接写一个
-- 没有底层审计事实的终态，也防止已发送 case 被误解释为可重试。
CREATE OR REPLACE FUNCTION phase16_v5_validate_case_outcome()
RETURNS trigger AS $$
DECLARE
    pass_count INTEGER;
    failure_count INTEGER;
    blocked_count INTEGER;
    recovery_status TEXT;
BEGIN
    SELECT
        count(*) FILTER (WHERE validation.verdict = 'PASS' AND receipt.receipt_complete = true),
        count(*) FILTER (WHERE validation.verdict = 'FAILED'),
        count(*) FILTER (WHERE validation.verdict = 'BLOCKED')
      INTO pass_count, failure_count, blocked_count
      FROM phase16_v5_dispatch_attempts attempt
      LEFT JOIN phase16_v5_provider_receipts receipt ON receipt.attempt_id = attempt.attempt_id
     LEFT JOIN phase16_v5_validation_facts validation ON validation.attempt_id = attempt.attempt_id
     WHERE attempt.claim_id = NEW.claim_id;
    SELECT status INTO recovery_status
      FROM phase16_v5_recovery_facts recovery WHERE recovery.claim_id = NEW.claim_id;
    IF NEW.status = 'PASS' AND pass_count <> 2 THEN
        RAISE EXCEPTION 'phase16 V5 PASS case requires two completed stage validations';
    END IF;
    IF NEW.status = 'FAILED' AND failure_count = 0 AND recovery_status IS DISTINCT FROM 'FAILED' THEN
        RAISE EXCEPTION 'phase16 V5 FAILED case requires failed validation';
    END IF;
    IF NEW.status = 'BLOCKED' AND blocked_count = 0 AND recovery_status IS DISTINCT FROM 'BLOCKED' THEN
        RAISE EXCEPTION 'phase16 V5 BLOCKED case requires blocked validation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- run PASS 的 slot 数由 run_kind 固定：校准必须 1/1，正式 run 必须严格 10/10；
-- 这条触发器不会让 V4 的最小 JSON PASS 变成双 Agent E2E PASS。
CREATE OR REPLACE FUNCTION phase16_v5_validate_run_outcome()
RETURNS trigger AS $$
DECLARE
    declared_kind TEXT;
    expected_slots INTEGER;
    slot_count INTEGER;
    pass_count INTEGER;
    failure_count INTEGER;
    blocked_count INTEGER;
BEGIN
    SELECT run_kind INTO declared_kind FROM phase16_v5_runs WHERE run_id = NEW.run_id;
    IF declared_kind IS NULL THEN
        RAISE EXCEPTION 'phase16 V5 outcome run is unknown';
    END IF;
    expected_slots := CASE WHEN declared_kind = 'CALIBRATION' THEN 1 ELSE 10 END;
    SELECT count(*), count(*) FILTER (WHERE status = 'PASS'),
           count(*) FILTER (WHERE status = 'FAILED'), count(*) FILTER (WHERE status = 'BLOCKED')
      INTO slot_count, pass_count, failure_count, blocked_count
      FROM phase16_v5_case_outcomes WHERE run_id = NEW.run_id;
    IF NEW.status = 'PASS' AND (slot_count <> expected_slots OR pass_count <> expected_slots) THEN
        RAISE EXCEPTION 'phase16 V5 PASS run has incomplete case outcomes';
    END IF;
    IF NEW.status = 'FAILED' AND failure_count = 0 THEN
        RAISE EXCEPTION 'phase16 V5 FAILED run requires failed case';
    END IF;
    IF NEW.status = 'BLOCKED' AND blocked_count = 0 THEN
        RAISE EXCEPTION 'phase16 V5 BLOCKED run requires blocked case';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'phase16_v5_campaigns', 'phase16_v5_runs', 'phase16_v5_case_slots',
        'phase16_v5_case_claims', 'phase16_v5_dispatch_attempts',
        'phase16_v5_provider_receipts', 'phase16_v5_validation_facts', 'phase16_v5_recovery_facts',
        'phase16_v5_case_outcomes', 'phase16_v5_run_outcomes'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_append_only ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_%s_append_only BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION phase16_v5_reject_mutation()', table_name, table_name);
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_no_truncate ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER trg_%s_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION phase16_v5_reject_truncate()', table_name, table_name);
    END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS trg_phase16_v5_dispatch_attempt ON phase16_v5_dispatch_attempts;
CREATE TRIGGER trg_phase16_v5_dispatch_attempt
    BEFORE INSERT ON phase16_v5_dispatch_attempts
    FOR EACH ROW EXECUTE FUNCTION phase16_v5_validate_dispatch_attempt();
DROP TRIGGER IF EXISTS trg_phase16_v5_validation_fact ON phase16_v5_validation_facts;
CREATE TRIGGER trg_phase16_v5_validation_fact
    BEFORE INSERT ON phase16_v5_validation_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_v5_validate_validation_fact();
DROP TRIGGER IF EXISTS trg_phase16_v5_case_outcome ON phase16_v5_case_outcomes;
CREATE TRIGGER trg_phase16_v5_case_outcome
    BEFORE INSERT ON phase16_v5_case_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_v5_validate_case_outcome();
DROP TRIGGER IF EXISTS trg_phase16_v5_run_outcome ON phase16_v5_run_outcomes;
CREATE TRIGGER trg_phase16_v5_run_outcome
    BEFORE INSERT ON phase16_v5_run_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_v5_validate_run_outcome();
