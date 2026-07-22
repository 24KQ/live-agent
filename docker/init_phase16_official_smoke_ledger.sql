-- Phase 16 正式真实模型 smoke 的独立 append-only 账本。
-- 本文件不能复用旧 phase16_smoke_* 表：旧表的 0.100000 reservation 和历史直接模式
-- 语义不满足正式 10/10、0.073220 历史支出以及每例 0.092000 的冻结边界。

CREATE TABLE IF NOT EXISTS phase16_official_smoke_runs (
    run_id TEXT PRIMARY KEY
        CHECK (run_id = 'phase16-official-smoke-v1'),
    manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    analyst_profile_digest TEXT NOT NULL CHECK (analyst_profile_digest ~ '^[0-9a-f]{64}$'),
    planner_profile_digest TEXT NOT NULL CHECK (planner_profile_digest ~ '^[0-9a-f]{64}$'),
    total_budget_cny NUMERIC(12, 6) NOT NULL
        CHECK (total_budget_cny = 1.000000 AND total_budget_cny <> 'NaN'::numeric),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_historical_spend (
    run_id TEXT NOT NULL REFERENCES phase16_official_smoke_runs(run_id) ON DELETE RESTRICT,
    source TEXT NOT NULL CHECK (source = 'HISTORICAL_DIRECT_MODE'),
    amount_cny NUMERIC(12, 6) NOT NULL
        CHECK (amount_cny = 0.073220 AND amount_cny <> 'NaN'::numeric),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, source)
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_case_slots (
    run_id TEXT NOT NULL REFERENCES phase16_official_smoke_runs(run_id) ON DELETE RESTRICT,
    slot_position SMALLINT NOT NULL CHECK (slot_position BETWEEN 1 AND 10),
    case_id TEXT NOT NULL,
    case_digest TEXT NOT NULL CHECK (case_digest ~ '^[0-9a-f]{64}$'),
    analyst_reservation_cny NUMERIC(12, 6) NOT NULL
        CHECK (analyst_reservation_cny = 0.040000 AND analyst_reservation_cny <> 'NaN'::numeric),
    planner_reservation_cny NUMERIC(12, 6) NOT NULL
        CHECK (planner_reservation_cny = 0.052000 AND planner_reservation_cny <> 'NaN'::numeric),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, case_id),
    UNIQUE (run_id, slot_position)
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_case_claims (
    claim_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    reserved_amount_cny NUMERIC(12, 6) NOT NULL
        CHECK (reserved_amount_cny = 0.092000 AND reserved_amount_cny <> 'NaN'::numeric),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, case_id),
    UNIQUE (run_id, claim_id),
    FOREIGN KEY (run_id, case_id)
        REFERENCES phase16_official_smoke_case_slots(run_id, case_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_dispatch_attempts (
    attempt_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    claim_id UUID NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('ANALYST', 'PLANNER')),
    profile_digest TEXT NOT NULL CHECK (profile_digest ~ '^[0-9a-f]{64}$'),
    internal_request_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (claim_id, stage),
    UNIQUE (run_id, internal_request_id),
    FOREIGN KEY (run_id, case_id)
        REFERENCES phase16_official_smoke_case_slots(run_id, case_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (run_id, claim_id)
        REFERENCES phase16_official_smoke_case_claims(run_id, claim_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_provider_receipts (
    attempt_id UUID PRIMARY KEY
        REFERENCES phase16_official_smoke_dispatch_attempts(attempt_id) ON DELETE RESTRICT,
    provider_response_id_digest TEXT NOT NULL
        CHECK (provider_response_id_digest ~ '^[0-9a-f]{64}$'),
    finish_reason TEXT NOT NULL CHECK (finish_reason IN ('stop', 'length', 'content_filter', 'tool_calls')),
    model_id TEXT NOT NULL CHECK (model_id = 'deepseek-v4-flash'),
    response_digest TEXT NOT NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    input_tokens BIGINT NOT NULL CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL CHECK (output_tokens >= 0),
    total_tokens BIGINT NOT NULL CHECK (total_tokens = input_tokens + output_tokens),
    latency_ms NUMERIC(14, 3) NOT NULL CHECK (latency_ms >= 0 AND latency_ms <> 'NaN'::numeric),
    input_cost_cny NUMERIC(12, 6) NOT NULL CHECK (input_cost_cny >= 0 AND input_cost_cny <> 'NaN'::numeric),
    output_cost_cny NUMERIC(12, 6) NOT NULL CHECK (output_cost_cny >= 0 AND output_cost_cny <> 'NaN'::numeric),
    total_cost_cny NUMERIC(12, 6) NOT NULL
        CHECK (total_cost_cny = input_cost_cny + output_cost_cny AND total_cost_cny <> 'NaN'::numeric),
    receipt_auth_tag TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_validation_facts (
    attempt_id UUID PRIMARY KEY
        REFERENCES phase16_official_smoke_dispatch_attempts(attempt_id) ON DELETE RESTRICT,
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAILED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    validation_digest TEXT NOT NULL CHECK (validation_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase16_official_smoke_case_outcomes (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    claim_id UUID NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    outcome_digest TEXT NOT NULL CHECK (outcome_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (run_id, case_id),
    FOREIGN KEY (run_id, case_id)
        REFERENCES phase16_official_smoke_case_slots(run_id, case_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (run_id, claim_id)
        REFERENCES phase16_official_smoke_case_claims(run_id, claim_id)
        ON DELETE RESTRICT
);

-- 旧开发数据库可能已由早期 DDL 创建 receipt 表。CREATE TABLE IF NOT EXISTS 不会补齐
-- 新约束，因此迁移重复执行时显式检查并添加全局 Provider receipt 唯一性，防止一个
-- response ID 被伪造成多次独立模型调用。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'phase16_official_smoke_provider_receipts'::regclass
           AND conname = 'phase16_official_smoke_provider_response_id_digest_unique'
    ) THEN
        ALTER TABLE phase16_official_smoke_provider_receipts
            ADD CONSTRAINT phase16_official_smoke_provider_response_id_digest_unique
            UNIQUE (provider_response_id_digest);
    END IF;
END;
$$;

-- 早期 Task 2 草案尚未写入数据库外 HMAC 标签。若旧表已有任何 receipt，迁移不能在
-- 缺少原始签名 key 的情况下补造可信标签，必须 fail-closed；空表则安全补齐列、NOT NULL
-- 和格式约束。这样旧事实不会被重新解释为正式真实模型证据。
ALTER TABLE phase16_official_smoke_provider_receipts
    ADD COLUMN IF NOT EXISTS receipt_auth_tag TEXT;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM phase16_official_smoke_provider_receipts
         WHERE receipt_auth_tag IS NULL
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke legacy receipt lacks external authenticity tag';
    END IF;
    ALTER TABLE phase16_official_smoke_provider_receipts
        ALTER COLUMN receipt_auth_tag SET NOT NULL;
    ALTER TABLE phase16_official_smoke_provider_receipts
        DROP CONSTRAINT IF EXISTS phase16_official_smoke_receipt_auth_tag_check;
    ALTER TABLE phase16_official_smoke_provider_receipts
        ADD CONSTRAINT phase16_official_smoke_receipt_auth_tag_check
        CHECK (receipt_auth_tag ~ '^[0-9a-f]{64}$');
END;
$$;

-- CREATE TABLE IF NOT EXISTS 不会修改已有列类型。以下版本化 schema 断言使旧草案在
-- 执行正式 smoke 前明确失败，而不是继续运行在 TEXT request ID、可空 HMAC 标签等弱
-- 约束上。升级策略只允许安全的空表补列；携带旧 receipt 的数据库必须人工隔离。
DO $$
BEGIN
    IF NOT (
        EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema=current_schema()
               AND table_name='phase16_official_smoke_dispatch_attempts'
               AND column_name='internal_request_id'
               AND data_type='uuid'
               AND is_nullable='NO'
        )
        AND EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema=current_schema()
               AND table_name='phase16_official_smoke_provider_receipts'
               AND column_name='provider_response_id_digest'
               AND data_type='text'
               AND is_nullable='NO'
        )
        AND EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema=current_schema()
               AND table_name='phase16_official_smoke_provider_receipts'
               AND column_name='receipt_auth_tag'
               AND data_type='text'
               AND is_nullable='NO'
        )
        AND EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conrelid='phase16_official_smoke_provider_receipts'::regclass
               AND conname='phase16_official_smoke_provider_response_id_digest_unique'
        )
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke schema contract is incompatible; migration refuses weak legacy evidence';
    END IF;
END;
$$;

-- D-170 的正式账本不是通用 run 容器。以下常量来自版本化
-- phase16-official-smoke-evidence-v1 Manifest：run 只能绑定同一 Manifest、两份
-- Smoke Profile 和十个有序 case/digest。应用层 ensure_run 会再次重验磁盘 Manifest；
-- 触发器则阻止绕开 Python 的直接 SQL 用任意合法哈希组装伪造 PASS 事实链。
CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_frozen_run() RETURNS trigger AS $$
BEGIN
    IF NEW.run_id <> 'phase16-official-smoke-v1'
       OR NEW.manifest_digest <> 'd490b0868413323e4956b16b86f9f195abdd99f546057bc1221d44181ba7b3ff'
       OR NEW.analyst_profile_digest <> '415b331477a55c58bd61e0d632ec3b74aa3137a5c30f8fd1344ab19fb2875bee'
       OR NEW.planner_profile_digest <> '40423dd6f8d7a1618ff65623940fc417ce54771fa48391338ab34bf5f8dc34c0' THEN
        RAISE EXCEPTION 'phase16 official smoke frozen manifest identity conflicts with formal evidence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_frozen_case_slot() RETURNS trigger AS $$
BEGIN
    IF NEW.run_id <> 'phase16-official-smoke-v1'
       OR NOT EXISTS (
           SELECT 1
             FROM (VALUES
                 (1, 'phase16-high-conflict-paired-development-001', '979d90b04ca16b1450b887d83740c506dbfa0172a3b861ca923077f1adbf3a1a'),
                 (2, 'phase16-high-conflict-paired-development-002', 'eaab3b3c50b9c1d37c224680c49572fca9be2c851cd922ad2bc47d5b154189c2'),
                 (3, 'phase16-high-conflict-paired-development-003', 'cb6372dd8b2f0b57f748332a8d6bf03dfaaff2661821c23572a6243621015bca'),
                 (4, 'phase16-high-conflict-paired-development-004', '8365cbab982492bc73158f157c79c37bef7018a6cb26d91efef7680a268e811f'),
                 (5, 'phase16-high-conflict-paired-development-005', '2c34b5acebb8f1e0bdbf3523919a155e0ade6486afa59c3a1455728af202cb5a'),
                 (6, 'phase16-high-conflict-paired-development-006', 'baae0713ea18aa9cc87abca8f765ae112b5a67d1caca9466895aba0a1fea9c89'),
                 (7, 'phase16-high-conflict-paired-validation-007', 'f739622375fdb1103a8341ce4fa038acefd22fdb27339cba461439892d2d37bd'),
                 (8, 'phase16-high-conflict-paired-validation-008', 'd0a1a805be6a613b0f978b06d51dbebab185e1bbd6886baf8f5f7ddb48f2a45e'),
                 (9, 'phase16-high-conflict-paired-validation-009', '458baf11bae10c3b98fd497096b4a4ca3743847fda8afd955e232e37e8ced9a7'),
                 (10, 'phase16-high-conflict-paired-validation-010', '7a33a8bd78fc681e0699c7499c6cce61e3b177f8ab8b6d85558fa7956c76caeb')
             ) AS frozen_slot(slot_position, case_id, case_digest)
            WHERE frozen_slot.slot_position = NEW.slot_position
              AND frozen_slot.case_id = NEW.case_id
              AND frozen_slot.case_digest = NEW.case_digest
       ) THEN
        RAISE EXCEPTION 'phase16 official smoke frozen case slot conflicts with formal evidence';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Planner 直接 SQL 插入也必须等待同 claim 的 Analyst PASS，且 attempt 的 run/case
-- 组合必须准确属于 claim。应用层同样做检查，数据库触发器承担最终一致性防线。
CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_claim() RETURNS trigger AS $$
DECLARE
    expected_manifest_digest TEXT;
    slot_count INTEGER;
    slot_reservation_total NUMERIC(12, 6);
    historical_count INTEGER;
    historical_total NUMERIC(12, 6);
BEGIN
    SELECT run.manifest_digest
      INTO expected_manifest_digest
      FROM phase16_official_smoke_runs run
     WHERE run.run_id=NEW.run_id;
    IF expected_manifest_digest IS NULL OR NEW.manifest_digest IS DISTINCT FROM expected_manifest_digest THEN
        RAISE EXCEPTION 'phase16 official smoke claim manifest digest conflicts with run';
    END IF;
    -- claim 是正式 run 的第一条可发送事实。必须在这里验证历史直接模式支出和完整十
    -- slot 已经同一事务封印，否则直接 SQL 可先造一个半初始化 run，再伪造 PASS 链。
    SELECT count(*), COALESCE(sum(analyst_reservation_cny + planner_reservation_cny), 0)
      INTO slot_count, slot_reservation_total
      FROM phase16_official_smoke_case_slots
     WHERE run_id=NEW.run_id;
    SELECT count(*), COALESCE(sum(amount_cny), 0)
      INTO historical_count, historical_total
      FROM phase16_official_smoke_historical_spend
     WHERE run_id=NEW.run_id
       AND source='HISTORICAL_DIRECT_MODE';
    IF slot_count <> 10
       OR slot_reservation_total <> 0.920000
       OR historical_count <> 1
       OR historical_total <> 0.073220 THEN
        RAISE EXCEPTION 'phase16 official smoke run initialization is incomplete';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_attempt() RETURNS trigger AS $$
DECLARE
    expected_profile_digest TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM phase16_official_smoke_case_claims claim
         WHERE claim.claim_id=NEW.claim_id
           AND claim.run_id=NEW.run_id
           AND claim.case_id=NEW.case_id
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke attempt claim lineage is invalid';
    END IF;
    SELECT CASE WHEN NEW.stage='ANALYST' THEN run.analyst_profile_digest
                ELSE run.planner_profile_digest END
      INTO expected_profile_digest
      FROM phase16_official_smoke_runs run
     WHERE run.run_id=NEW.run_id;
    IF expected_profile_digest IS NULL OR NEW.profile_digest IS DISTINCT FROM expected_profile_digest THEN
        RAISE EXCEPTION 'phase16 official smoke attempt profile digest conflicts with run';
    END IF;
    IF NEW.stage='PLANNER' AND NOT EXISTS (
        SELECT 1
          FROM phase16_official_smoke_dispatch_attempts analyst
          JOIN phase16_official_smoke_validation_facts validation
            ON validation.attempt_id=analyst.attempt_id
         WHERE analyst.claim_id=NEW.claim_id
           AND analyst.stage='ANALYST'
           AND validation.verdict='PASS'
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke planner requires analyst validation pass';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_receipt() RETURNS trigger AS $$
DECLARE
    stage_limit NUMERIC(12, 6);
    expected_input_cost NUMERIC(12, 6);
    expected_output_cost NUMERIC(12, 6);
BEGIN
    SELECT CASE WHEN attempt.stage='ANALYST' THEN slot.analyst_reservation_cny
                ELSE slot.planner_reservation_cny END
      INTO stage_limit
      FROM phase16_official_smoke_dispatch_attempts attempt
      JOIN phase16_official_smoke_case_slots slot
        ON slot.run_id=attempt.run_id AND slot.case_id=attempt.case_id
     WHERE attempt.attempt_id=NEW.attempt_id;
    -- D-168 固定 DeepSeek V4 Flash 的 cache-miss 输入 1.0、输出 2.0 CNY / 百万
    -- token。两个价格均为整数元，因此 input/output token 均按百万分之一精确落在
    -- NUMERIC(12,6) 网格；直接 SQL 不能低报成本或替换供应商计价事实。
    expected_input_cost := NEW.input_tokens::NUMERIC / 1000000;
    expected_output_cost := NEW.output_tokens::NUMERIC * 2 / 1000000;
    IF NEW.input_cost_cny <> expected_input_cost
       OR NEW.output_cost_cny <> expected_output_cost
       OR NEW.total_cost_cny <> expected_input_cost + expected_output_cost THEN
        RAISE EXCEPTION 'phase16 official smoke receipt cost conflicts with frozen price';
    END IF;
    IF stage_limit IS NULL OR NEW.total_cost_cny > stage_limit THEN
        RAISE EXCEPTION 'phase16 official smoke receipt exceeds frozen stage reservation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_validation() RETURNS trigger AS $$
BEGIN
    IF NEW.verdict='PASS' AND NOT EXISTS (
        SELECT 1 FROM phase16_official_smoke_provider_receipts
         WHERE attempt_id=NEW.attempt_id
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke PASS validation requires provider receipt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- outcome 是正式报告的唯一 case 终态。即使某个同进程调用绕过 Python，也不能写入
-- 没有完整 Analyst/Planner receipt 与 PASS validation 的“成功”，或没有失败验证的
-- “失败”。同时校验 claim 与 case 的联合 lineage，避免把一个合法 claim 挂到别的 slot。
CREATE OR REPLACE FUNCTION phase16_official_smoke_validate_outcome() RETURNS trigger AS $$
DECLARE
    passed_stage_count INTEGER;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM phase16_official_smoke_case_claims claim
         WHERE claim.claim_id=NEW.claim_id
           AND claim.run_id=NEW.run_id
           AND claim.case_id=NEW.case_id
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke outcome claim lineage is invalid';
    END IF;
    IF NEW.status='PASS' THEN
        SELECT count(DISTINCT attempt.stage)
          INTO passed_stage_count
          FROM phase16_official_smoke_dispatch_attempts attempt
          JOIN phase16_official_smoke_provider_receipts receipt
            ON receipt.attempt_id=attempt.attempt_id
          JOIN phase16_official_smoke_validation_facts validation
            ON validation.attempt_id=attempt.attempt_id
         WHERE attempt.claim_id=NEW.claim_id
           AND validation.verdict='PASS'
           AND attempt.stage IN ('ANALYST', 'PLANNER');
        IF passed_stage_count <> 2 THEN
            RAISE EXCEPTION 'phase16 official smoke PASS requires two validated provider receipts';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1
          FROM phase16_official_smoke_dispatch_attempts attempt
          JOIN phase16_official_smoke_validation_facts validation
            ON validation.attempt_id=attempt.attempt_id
         WHERE attempt.claim_id=NEW.claim_id
           AND validation.verdict='FAILED'
    ) THEN
        RAISE EXCEPTION 'phase16 official smoke FAILED outcome requires failed validation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- PostgreSQL 触发器把三张初始化事实表冻结为 append-only。后续 Task 2 的 claim、attempt、
-- receipt、validation 和 outcome 表将复用同一函数，避免应用层遗漏 UPDATE/DELETE 防线。
CREATE OR REPLACE FUNCTION phase16_official_smoke_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 official smoke facts are append-only';
END;
$$ LANGUAGE plpgsql;

-- 行级 append-only 触发器不会收到 TRUNCATE。正式账本的恢复与“绝不重发”证据依赖
-- 历史事实仍然存在，因此额外用 statement-level 触发器拒绝 TRUNCATE 及其 CASCADE。
CREATE OR REPLACE FUNCTION phase16_official_smoke_reject_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 official smoke facts cannot be truncated';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_phase16_official_smoke_runs_append_only ON phase16_official_smoke_runs;
CREATE TRIGGER trg_phase16_official_smoke_runs_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_runs
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_runs_frozen_identity ON phase16_official_smoke_runs;
CREATE TRIGGER trg_phase16_official_smoke_runs_frozen_identity
    BEFORE INSERT ON phase16_official_smoke_runs
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_frozen_run();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_historical_spend_append_only
    ON phase16_official_smoke_historical_spend;
CREATE TRIGGER trg_phase16_official_smoke_historical_spend_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_historical_spend
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_slots_append_only
    ON phase16_official_smoke_case_slots;
CREATE TRIGGER trg_phase16_official_smoke_case_slots_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_case_slots
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_slots_frozen_identity
    ON phase16_official_smoke_case_slots;
CREATE TRIGGER trg_phase16_official_smoke_case_slots_frozen_identity
    BEFORE INSERT ON phase16_official_smoke_case_slots
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_frozen_case_slot();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_claims_append_only
    ON phase16_official_smoke_case_claims;
CREATE TRIGGER trg_phase16_official_smoke_case_claims_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_case_claims
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_claims_validate
    ON phase16_official_smoke_case_claims;
CREATE TRIGGER trg_phase16_official_smoke_case_claims_validate
    BEFORE INSERT ON phase16_official_smoke_case_claims
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_claim();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_dispatch_attempts_validate
    ON phase16_official_smoke_dispatch_attempts;
CREATE TRIGGER trg_phase16_official_smoke_dispatch_attempts_validate
    BEFORE INSERT ON phase16_official_smoke_dispatch_attempts
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_attempt();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_provider_receipts_validate
    ON phase16_official_smoke_provider_receipts;
CREATE TRIGGER trg_phase16_official_smoke_provider_receipts_validate
    BEFORE INSERT ON phase16_official_smoke_provider_receipts
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_receipt();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_validation_facts_validate
    ON phase16_official_smoke_validation_facts;
CREATE TRIGGER trg_phase16_official_smoke_validation_facts_validate
    BEFORE INSERT ON phase16_official_smoke_validation_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_validation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_outcomes_validate
    ON phase16_official_smoke_case_outcomes;
CREATE TRIGGER trg_phase16_official_smoke_case_outcomes_validate
    BEFORE INSERT ON phase16_official_smoke_case_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_validate_outcome();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_dispatch_attempts_append_only
    ON phase16_official_smoke_dispatch_attempts;
CREATE TRIGGER trg_phase16_official_smoke_dispatch_attempts_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_dispatch_attempts
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_provider_receipts_append_only
    ON phase16_official_smoke_provider_receipts;
CREATE TRIGGER trg_phase16_official_smoke_provider_receipts_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_provider_receipts
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_validation_facts_append_only
    ON phase16_official_smoke_validation_facts;
CREATE TRIGGER trg_phase16_official_smoke_validation_facts_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_validation_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_outcomes_append_only
    ON phase16_official_smoke_case_outcomes;
CREATE TRIGGER trg_phase16_official_smoke_case_outcomes_append_only
    BEFORE UPDATE OR DELETE ON phase16_official_smoke_case_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_official_smoke_reject_mutation();

DROP TRIGGER IF EXISTS trg_phase16_official_smoke_runs_no_truncate ON phase16_official_smoke_runs;
CREATE TRIGGER trg_phase16_official_smoke_runs_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_runs
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_historical_spend_no_truncate
    ON phase16_official_smoke_historical_spend;
CREATE TRIGGER trg_phase16_official_smoke_historical_spend_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_historical_spend
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_slots_no_truncate
    ON phase16_official_smoke_case_slots;
CREATE TRIGGER trg_phase16_official_smoke_case_slots_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_case_slots
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_claims_no_truncate
    ON phase16_official_smoke_case_claims;
CREATE TRIGGER trg_phase16_official_smoke_case_claims_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_case_claims
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_dispatch_attempts_no_truncate
    ON phase16_official_smoke_dispatch_attempts;
CREATE TRIGGER trg_phase16_official_smoke_dispatch_attempts_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_dispatch_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_provider_receipts_no_truncate
    ON phase16_official_smoke_provider_receipts;
CREATE TRIGGER trg_phase16_official_smoke_provider_receipts_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_provider_receipts
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_validation_facts_no_truncate
    ON phase16_official_smoke_validation_facts;
CREATE TRIGGER trg_phase16_official_smoke_validation_facts_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_validation_facts
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();
DROP TRIGGER IF EXISTS trg_phase16_official_smoke_case_outcomes_no_truncate
    ON phase16_official_smoke_case_outcomes;
CREATE TRIGGER trg_phase16_official_smoke_case_outcomes_no_truncate
    BEFORE TRUNCATE ON phase16_official_smoke_case_outcomes
    FOR EACH STATEMENT EXECUTE FUNCTION phase16_official_smoke_reject_truncate();

-- 正式账本的 schema 本身也是证据边界。该摘要覆盖八张表的每个列契约、全部主键/
-- 唯一/外键/CHECK、所有非内部 trigger 和所有正式 trigger function 的函数体。任何旧
-- migration 遗留的弱列、被删除的 lineage FK、被移除的 append-only trigger，都会改变摘要。
CREATE OR REPLACE FUNCTION phase16_official_smoke_schema_contract_digest() RETURNS TEXT AS $$
DECLARE
    contract_digest TEXT;
BEGIN
    WITH formal_tables(table_name) AS (
        VALUES
            ('phase16_official_smoke_runs'),
            ('phase16_official_smoke_historical_spend'),
            ('phase16_official_smoke_case_slots'),
            ('phase16_official_smoke_case_claims'),
            ('phase16_official_smoke_dispatch_attempts'),
            ('phase16_official_smoke_provider_receipts'),
            ('phase16_official_smoke_validation_facts'),
            ('phase16_official_smoke_case_outcomes')
    ), facts AS (
        SELECT format(
            'column:%s:%s:%s:%s:%s',
            column_meta.table_name,
            column_meta.ordinal_position,
            column_meta.column_name,
            column_meta.data_type,
            column_meta.is_nullable
        ) AS fact
          FROM information_schema.columns AS column_meta
          JOIN formal_tables ON formal_tables.table_name=column_meta.table_name
         WHERE column_meta.table_schema=current_schema()
        UNION ALL
        SELECT format(
            'constraint:%s:%s:%s',
            relation.relname,
            constraint_meta.contype,
            pg_get_constraintdef(constraint_meta.oid, true)
        ) AS fact
          FROM pg_constraint AS constraint_meta
          JOIN pg_class AS relation ON relation.oid=constraint_meta.conrelid
          JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
          JOIN formal_tables ON formal_tables.table_name=relation.relname
         WHERE namespace.nspname=current_schema()
        UNION ALL
        SELECT format(
            'trigger:%s:%s:%s',
            relation.relname,
            trigger_meta.tgname,
            pg_get_triggerdef(trigger_meta.oid, true)
        ) AS fact
          FROM pg_trigger AS trigger_meta
          JOIN pg_class AS relation ON relation.oid=trigger_meta.tgrelid
          JOIN pg_namespace AS namespace ON namespace.oid=relation.relnamespace
          JOIN formal_tables ON formal_tables.table_name=relation.relname
         WHERE namespace.nspname=current_schema()
           AND NOT trigger_meta.tgisinternal
        UNION ALL
        SELECT format('function:%s:%s', procedure.proname, procedure.prosrc) AS fact
          FROM pg_proc AS procedure
          JOIN pg_namespace AS namespace ON namespace.oid=procedure.pronamespace
         WHERE namespace.nspname=current_schema()
           AND procedure.proname = ANY (ARRAY[
               'phase16_official_smoke_validate_claim',
               'phase16_official_smoke_validate_attempt',
               'phase16_official_smoke_validate_receipt',
               'phase16_official_smoke_validate_validation',
               'phase16_official_smoke_validate_outcome',
               'phase16_official_smoke_reject_mutation',
               'phase16_official_smoke_reject_truncate',
               'phase16_official_smoke_validate_frozen_run',
               'phase16_official_smoke_validate_frozen_case_slot'
           ])
    )
    SELECT md5(COALESCE(string_agg(fact, E'\n' ORDER BY fact), ''))
      INTO contract_digest
      FROM facts;
    RETURN contract_digest;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION phase16_official_smoke_assert_schema_contract() RETURNS VOID AS $$
DECLARE
    -- 此值由全新隔离 schema 执行本 DDL 后的完整列/约束/触发器/函数契约计算得到。
    -- 它不包含本断言函数自身，故替换期望值不会改变被核验的 schema 投影；任何现有
    -- 数据库移除了 CHECK、lineage FK 或 append-only trigger，都会产生不同摘要并 fail-closed。
    expected_contract_digest TEXT := 'e9f9f0671d54f9906d3414c70507411c';
    actual_contract_digest TEXT;
BEGIN
    actual_contract_digest := phase16_official_smoke_schema_contract_digest();
    IF actual_contract_digest <> expected_contract_digest THEN
        RAISE EXCEPTION 'phase16 official smoke schema contract is incompatible; migration refuses weak evidence';
    END IF;
END;
$$ LANGUAGE plpgsql;

SELECT phase16_official_smoke_assert_schema_contract();
