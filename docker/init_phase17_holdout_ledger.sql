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
        'phase17_holdout_run_results', 'phase17_holdout_case_results'
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
