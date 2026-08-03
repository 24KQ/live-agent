-- Phase 17 holdout 独立执行账本（独立表族，不改动 v2/v3 qualification 表）。
--
-- 与 v2 表族的差异是刻意的：v2 policies/campaigns 表 CHECK 硬编码了
-- 15+15 batch 结构、reservation <= 10 等 v2 语义，phase17（10+20、15 CNY
-- 总盘封装）无法复用；独立表族同时保证 phase17 预算池永远无法借用
-- v2/v3 的历史预算池（codex 十六轮 P0 隔离要求）。
--
-- 预算池是纯事件记账（append-only 表族内没有任何 UPDATE）：
--   reserved = Σ RESERVE - Σ RELEASE        settled = Σ SETTLE
--   不变式（contract 行锁内强制）：reserved + settled <= forward_budget_remaining_cny
-- usage 未知（UNKNOWN_USAGE）按最坏情况占用：调用方以 reservation 全额
-- 入账 SETTLE 金额，不得结算为 0。
--
-- run 终态与 v2 同构：runs 表仅记录开始，终态插入
-- phase17_holdout_run_results（唯一一行）。

CREATE TABLE IF NOT EXISTS phase17_holdout_contracts (
    contract_digest CHAR(64) PRIMARY KEY CHECK (contract_digest ~ '^[0-9a-f]{64}$'),
    contract_id TEXT NOT NULL,
    project_budget_cny NUMERIC(18,6) NOT NULL CHECK (project_budget_cny > 0),
    retrospective_budget_actual_cny NUMERIC(18,6) NOT NULL CHECK (retrospective_budget_actual_cny >= 0),
    forward_budget_remaining_cny NUMERIC(18,6) NOT NULL CHECK (forward_budget_remaining_cny >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (retrospective_budget_actual_cny + forward_budget_remaining_cny <= project_budget_cny)
);

CREATE TABLE IF NOT EXISTS phase17_holdout_campaigns (
    campaign_id TEXT PRIMARY KEY,
    contract_digest CHAR(64) NOT NULL REFERENCES phase17_holdout_contracts(contract_digest),
    batch_index INTEGER NOT NULL CHECK (batch_index IN (1, 2)),
    candidate_digest CHAR(64) NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    dataset_manifest_digest CHAR(64) NOT NULL CHECK (dataset_manifest_digest ~ '^[0-9a-f]{64}$'),
    reservation_cny NUMERIC(18,6) NOT NULL CHECK (reservation_cny > 0),
    declared_model_id TEXT NOT NULL,
    declared_reasoning_effort TEXT,
    declared_endpoint_hosts TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (contract_digest, batch_index)
);

-- 预算事件 append-only 流水；RESERVE/SETTLE 每个 campaign 至多各一次。
CREATE TABLE IF NOT EXISTS phase17_holdout_budget_events (
    event_id BIGSERIAL PRIMARY KEY,
    contract_digest CHAR(64) NOT NULL REFERENCES phase17_holdout_contracts(contract_digest),
    campaign_id TEXT NOT NULL REFERENCES phase17_holdout_campaigns(campaign_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('RESERVE', 'SETTLE', 'RELEASE')),
    amount_cny NUMERIC(18,6) NOT NULL CHECK (amount_cny > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, event_type)
);

CREATE TABLE IF NOT EXISTS phase17_holdout_runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES phase17_holdout_campaigns(campaign_id),
    case_ids TEXT[] NOT NULL CHECK (cardinality(case_ids) > 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- run 终态只能插入一次；evaluation digest 固定非敏感摘要，不接受自由文本。
CREATE TABLE IF NOT EXISTS phase17_holdout_run_results (
    run_id TEXT PRIMARY KEY REFERENCES phase17_holdout_runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    evaluation_digest CHAR(64) NOT NULL CHECK (evaluation_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phase17_holdout_case_results (
    run_id TEXT NOT NULL REFERENCES phase17_holdout_runs(run_id),
    case_id TEXT NOT NULL,
    input_digest CHAR(64) NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    receipt_count INTEGER NOT NULL DEFAULT 0 CHECK (receipt_count >= 0),
    cost_cny NUMERIC(18,6) NOT NULL DEFAULT 0 CHECK (cost_cny >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, case_id)
);

-- 逐 attempt 证据（codex 第十七轮 P0-3）：每次模型调用（或失败）一行，
-- 记录请求/响应摘要、实际端点、token、成本与 receipt_hmac 完整性校验，
-- 与 v2 attempt receipt 口径对齐；case_results.receipt_count = attempts 行数。
CREATE TABLE IF NOT EXISTS phase17_holdout_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES phase17_holdout_runs(run_id),
    case_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('ANALYST', 'PLANNER')),
    attempt_index INTEGER NOT NULL CHECK (attempt_index >= 1),
    request_id TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    model_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAILED')),
    category TEXT,
    response_digest CHAR(64),
    provider_response_id TEXT,
    http_status INTEGER CHECK (http_status IS NULL OR (http_status >= 100 AND http_status <= 599)),
    latency_ms NUMERIC(20,6) NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cost_cny NUMERIC(18,6) NOT NULL CHECK (cost_cny >= 0),
    artifact_path TEXT,
    artifact_digest CHAR(64) CHECK (artifact_digest IS NULL OR artifact_digest ~ '^[0-9a-f]{64}$'),
    artifact_capture_status TEXT NOT NULL DEFAULT 'UNAVAILABLE'
        CHECK (artifact_capture_status IN ('CAPTURED', 'UNAVAILABLE', 'FAILED')),
    receipt_hmac CHAR(64) NOT NULL CHECK (receipt_hmac ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, case_id, stage, attempt_index)
);

-- 该 migration 可能在阶段②已创建的八张表上增量执行；显式 ALTER 保证
-- 新 capture 字段不会因为 IF NOT EXISTS 的旧表而静默缺失。
ALTER TABLE phase17_holdout_attempts
    ADD COLUMN IF NOT EXISTS artifact_path TEXT,
    ADD COLUMN IF NOT EXISTS artifact_digest CHAR(64),
    ADD COLUMN IF NOT EXISTS artifact_capture_status TEXT NOT NULL DEFAULT 'UNAVAILABLE';
ALTER TABLE phase17_holdout_attempts
    DROP CONSTRAINT IF EXISTS phase17_holdout_attempts_artifact_digest_check,
    ADD CONSTRAINT phase17_holdout_attempts_artifact_digest_check
        CHECK (artifact_digest IS NULL OR artifact_digest ~ '^[0-9a-f]{64}$'),
    DROP CONSTRAINT IF EXISTS phase17_holdout_attempts_artifact_capture_status_check,
    ADD CONSTRAINT phase17_holdout_attempts_artifact_capture_status_check
        CHECK (artifact_capture_status IN ('CAPTURED', 'UNAVAILABLE', 'FAILED'));

-- 27/30 聚合结论（codex 第十八轮 P1-4）：两批 run 终态后只插入一次，绑定
-- 两 run 与全链身份；evaluation digest 覆盖判定事实，不可自由文本。
CREATE TABLE IF NOT EXISTS phase17_holdout_qualifications (
    qualification_id TEXT PRIMARY KEY,
    run1_id TEXT NOT NULL REFERENCES phase17_holdout_runs(run_id),
    run2_id TEXT NOT NULL REFERENCES phase17_holdout_runs(run_id),
    contract_digest CHAR(64) NOT NULL REFERENCES phase17_holdout_contracts(contract_digest),
    candidate_digest CHAR(64) NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    dataset_manifest_digest CHAR(64) NOT NULL CHECK (dataset_manifest_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('QUALIFIED', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    total_pass INTEGER NOT NULL CHECK (total_pass >= 0),
    total_cases INTEGER NOT NULL CHECK (total_cases > 0),
    pass_min INTEGER NOT NULL CHECK (pass_min > 0),
    critical_safety_failures INTEGER NOT NULL DEFAULT 0 CHECK (critical_safety_failures >= 0),
    evaluation_digest CHAR(64) NOT NULL CHECK (evaluation_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run1_id, run2_id)
);

-- 独立第三方安全审查只追加，不把审查摘要混入模型输入或普通 run 结果。
-- artifact_digest 必须能在同一 run/case 的 CAPTURED attempt 中找到，SQL
-- 触发器负责防止“只写 verdict、不交 artifact”的伪门禁。
CREATE TABLE IF NOT EXISTS phase17_holdout_safety_reviews (
    review_id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    artifact_digest CHAR(64) NOT NULL CHECK (artifact_digest ~ '^[0-9a-f]{64}$'),
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAIL', 'INCONCLUSIVE')),
    summary TEXT NOT NULL CHECK (length(btrim(summary)) > 0),
    reviewer TEXT NOT NULL CHECK (reviewer = 'claude-independent-review'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, case_id)
        REFERENCES phase17_holdout_case_results(run_id, case_id),
    UNIQUE (run_id, case_id, reviewer)
);

CREATE OR REPLACE FUNCTION phase17_holdout_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase17 holdout ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase17_holdout_reject_truncate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase17 holdout ledger cannot be truncated';
END;
$$ LANGUAGE plpgsql;

-- case result 只能在 run 未终态时追加（run_results 行不存在）且 case 必须属于
-- 冻结的 run slot 集合；Python 的预检不是安全边界，SQL 也必须拒绝。
CREATE OR REPLACE FUNCTION phase17_holdout_validate_case_result()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM phase17_holdout_runs run
         WHERE run.run_id = NEW.run_id
           AND NEW.case_id = ANY (run.case_ids)
    ) THEN
        RAISE EXCEPTION 'phase17 holdout case result is not in the frozen run slot set';
    END IF;
    IF EXISTS (SELECT 1 FROM phase17_holdout_run_results WHERE run_id = NEW.run_id) THEN
        RAISE EXCEPTION 'phase17 holdout case results cannot follow terminal run result';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'phase17_holdout_contracts', 'phase17_holdout_campaigns',
        'phase17_holdout_budget_events', 'phase17_holdout_runs',
        'phase17_holdout_run_results', 'phase17_holdout_case_results',
        'phase17_holdout_attempts', 'phase17_holdout_qualifications',
        'phase17_holdout_safety_reviews'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_append_only ON %I', table_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_append_only BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION phase17_holdout_reject_mutation()',
            table_name, table_name
        );
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_no_truncate ON %I', table_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION phase17_holdout_reject_truncate()',
            table_name, table_name
        );
    END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS trg_phase17_holdout_case_result ON phase17_holdout_case_results;
CREATE TRIGGER trg_phase17_holdout_case_result
    BEFORE INSERT ON phase17_holdout_case_results
    FOR EACH ROW EXECUTE FUNCTION phase17_holdout_validate_case_result();

-- attempt 与 case result 同样的 slot/终态安全边界：run 未终态且 case 属于
-- 冻结 slot 集合才允许追加 attempt 证据。
CREATE OR REPLACE FUNCTION phase17_holdout_validate_attempt()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM phase17_holdout_runs run
         WHERE run.run_id = NEW.run_id
           AND NEW.case_id = ANY (run.case_ids)
    ) THEN
        RAISE EXCEPTION 'phase17 holdout attempt is not in the frozen run slot set';
    END IF;
    IF EXISTS (SELECT 1 FROM phase17_holdout_run_results WHERE run_id = NEW.run_id) THEN
        RAISE EXCEPTION 'phase17 holdout attempts cannot follow terminal run result';
    END IF;
    -- capture 的路径必须由 run/case/stage/index 四元身份唯一生成，防止把
    -- 另一个 case 的 artifact 摘要伪装成当前 attempt 的证据。
    IF NEW.artifact_capture_status = 'CAPTURED' THEN
        IF NEW.artifact_path IS NULL
           OR NEW.artifact_digest IS NULL
           OR NEW.response_digest IS NULL
           OR NEW.artifact_digest <> NEW.response_digest
           OR NEW.artifact_path <> format(
               '%s/%s/%s/attempt-%s.body',
               NEW.run_id, NEW.case_id, NEW.stage, NEW.attempt_index
           ) THEN
            RAISE EXCEPTION 'phase17 captured artifact identity is invalid';
        END IF;
    ELSIF NEW.artifact_path IS NOT NULL OR NEW.artifact_digest IS NOT NULL THEN
        RAISE EXCEPTION 'phase17 uncaptured attempt cannot carry artifact identity';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 安全审查必须绑定已经终态化的 case 与真实 capture digest，避免人工
-- verdict 脱离实际网络响应单独进入 QUALIFIED 计算。
CREATE OR REPLACE FUNCTION phase17_holdout_validate_safety_review()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM phase17_holdout_run_results result
         WHERE result.run_id = NEW.run_id
    ) THEN
        RAISE EXCEPTION 'phase17 safety review requires a terminal run';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM phase17_holdout_attempts attempt
         WHERE attempt.run_id = NEW.run_id
           AND attempt.case_id = NEW.case_id
           AND attempt.response_digest = NEW.artifact_digest
           AND attempt.artifact_digest = NEW.artifact_digest
           AND attempt.artifact_capture_status = 'CAPTURED'
    ) THEN
        RAISE EXCEPTION 'phase17 safety review artifact digest is not captured';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_phase17_holdout_safety_review ON phase17_holdout_safety_reviews;
CREATE TRIGGER trg_phase17_holdout_safety_review
    BEFORE INSERT ON phase17_holdout_safety_reviews
    FOR EACH ROW EXECUTE FUNCTION phase17_holdout_validate_safety_review();

DROP TRIGGER IF EXISTS trg_phase17_holdout_attempt ON phase17_holdout_attempts;
CREATE TRIGGER trg_phase17_holdout_attempt
    BEFORE INSERT ON phase17_holdout_attempts
    FOR EACH ROW EXECUTE FUNCTION phase17_holdout_validate_attempt();
