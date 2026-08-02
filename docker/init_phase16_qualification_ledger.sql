-- Phase 16 三层资格体系的独立 append-only 审计账本。
-- 本 DDL 只创建 phase16_qualification_* 表；绝不读取、更新、迁移或重签 V1–V8 历史事实。

CREATE TABLE IF NOT EXISTS phase16_qualification_policies (
    policy_digest CHAR(64) PRIMARY KEY CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    -- 预算数值的唯一权威是 HMAC 绑定的 policy 模型；这里的 CHECK 只做合理性兜底，
    -- 不再钉死精确值，避免候选调参（改预算/token）每次都要 ALTER 数据库。
    project_budget_cny NUMERIC(12,6) NOT NULL CHECK (project_budget_cny > 0),
    campaign_budget_cny NUMERIC(12,6) NOT NULL
        CHECK (campaign_budget_cny > 0 AND campaign_budget_cny <= project_budget_cny),
    holdout_batch_count INTEGER NOT NULL CHECK (holdout_batch_count = 2),
    holdout_e2e_cases_per_batch INTEGER NOT NULL CHECK (holdout_e2e_cases_per_batch = 15),
    stage_reservation_cny NUMERIC(12,6) NOT NULL
        CHECK (stage_reservation_cny > 0 AND stage_reservation_cny <= campaign_budget_cny),
    source_closure_digest CHAR(64) NOT NULL CHECK (source_closure_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    -- 不设 UNIQUE (policy_id, policy_version)：digest 才是身份（PK），重冻结时同一
    -- (id, version) 会携带新 digest 追加新行，version 只是描述性字段。
);

CREATE TABLE IF NOT EXISTS phase16_qualification_corpora (
    corpus_digest CHAR(64) PRIMARY KEY CHECK (corpus_digest ~ '^[0-9a-f]{64}$'),
    corpus_id TEXT NOT NULL,
    corpus_version TEXT NOT NULL,
    policy_digest CHAR(64) NOT NULL REFERENCES phase16_qualification_policies(policy_digest),
    holdout_release_state TEXT NOT NULL CHECK (
        holdout_release_state IN ('PENDING_INDEPENDENT_COMMITMENT', 'COMMITTED', 'RELEASED')
    ),
    holdout_commitment_digest CHAR(64) CHECK (holdout_commitment_digest ~ '^[0-9a-f]{64}$'),
    holdout_case_count INTEGER NOT NULL CHECK (holdout_case_count >= 30),
    holdout_high_conflict_e2e_case_count INTEGER NOT NULL CHECK (
        holdout_high_conflict_e2e_case_count >= 30
        AND holdout_high_conflict_e2e_case_count <= holdout_case_count
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (holdout_release_state = 'PENDING_INDEPENDENT_COMMITMENT' AND holdout_commitment_digest IS NULL)
        OR (holdout_release_state IN ('COMMITTED', 'RELEASED') AND holdout_commitment_digest IS NOT NULL)
    )
    -- 不设 UNIQUE (corpus_id, corpus_version)：理由同 policies，digest 才是身份。
);

CREATE TABLE IF NOT EXISTS phase16_qualification_candidates (
    candidate_digest CHAR(64) PRIMARY KEY CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    policy_digest CHAR(64) NOT NULL REFERENCES phase16_qualification_policies(policy_digest),
    candidate_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    endpoint_host TEXT NOT NULL,
    analyst_profile_digest CHAR(64) NOT NULL CHECK (analyst_profile_digest ~ '^[0-9a-f]{64}$'),
    planner_profile_digest CHAR(64) NOT NULL CHECK (planner_profile_digest ~ '^[0-9a-f]{64}$'),
    adapter_digest CHAR(64) NOT NULL CHECK (adapter_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    -- 不设 UNIQUE (policy_digest, candidate_id)：candidate_digest 已是 PK，重冻结同一
    -- candidate_id 会以新 digest 追加；政策行引用走 digest，不会产生混淆。
);

CREATE TABLE IF NOT EXISTS phase16_qualification_campaigns (
    campaign_id TEXT PRIMARY KEY,
    campaign_kind TEXT NOT NULL CHECK (campaign_kind IN ('DEVELOPMENT', 'VALIDATION', 'HOLDOUT')),
    batch_index INTEGER NOT NULL DEFAULT 1 CHECK (batch_index IN (1, 2)),
    policy_digest CHAR(64) NOT NULL REFERENCES phase16_qualification_policies(policy_digest),
    corpus_digest CHAR(64) NOT NULL REFERENCES phase16_qualification_corpora(corpus_digest),
    candidate_digest CHAR(64) NOT NULL REFERENCES phase16_qualification_candidates(candidate_digest),
    manifest_digest CHAR(64) NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    -- reservation 上界由 policy 的 campaign_budget 在 ledger Python 层强制（≤ project budget）；
    -- DDL 只保留一个宽松兜底，避免预算调参触发 ALTER。
    reservation_cny NUMERIC(12,6) NOT NULL CHECK (reservation_cny > 0 AND reservation_cny <= 10.000000),
    -- V9 矩阵配置：campaign 声明的运行时组合（模型 / 思考强度 / 渠道有序列表）。
    -- 白名单外值由 ledger Python 层校验器拒绝，这里只存规范化事实。
    declared_model_id TEXT NOT NULL DEFAULT 'gpt-5.6-luna',
    declared_reasoning_effort TEXT,
    declared_endpoint_hosts TEXT NOT NULL DEFAULT 'synapse-ai.uk',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 身份 = campaign_id（kind + digest[:16] + sha256(组合)[:16]）。不设
    -- UNIQUE (policy_digest, campaign_kind, candidate_digest, batch_index)：
    -- 该旧约束不含声明组合，会把同一 digest 下不同组合误判为重复（见
    -- alter_phase16_qualification_campaign_identity.sql）。
    CHECK ((campaign_kind = 'HOLDOUT') OR batch_index = 1)
);

-- 历史 campaign 只作为摘要和完整性状态的只读父证据；本表不复制旧 receipt/outcome。
CREATE TABLE IF NOT EXISTS phase16_qualification_source_evidence (
    campaign_id TEXT NOT NULL REFERENCES phase16_qualification_campaigns(campaign_id),
    source_campaign_id TEXT NOT NULL,
    source_manifest_digest CHAR(64) NOT NULL CHECK (source_manifest_digest ~ '^[0-9a-f]{64}$'),
    observation_digest CHAR(64) NOT NULL CHECK (observation_digest ~ '^[0-9a-f]{64}$'),
    integrity_status TEXT NOT NULL CHECK (
        integrity_status IN ('AUTHENTICATED', 'UNVERIFIABLE', 'INCOMPLETE')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (campaign_id, source_campaign_id)
);

-- 只记录独立 release owner 的摘要、状态及 HMAC；密文/明文/密钥均不写入账本。
CREATE TABLE IF NOT EXISTS phase16_qualification_release_events (
    corpus_digest CHAR(64) PRIMARY KEY REFERENCES phase16_qualification_corpora(corpus_digest),
    commitment_digest CHAR(64) NOT NULL CHECK (commitment_digest ~ '^[0-9a-f]{64}$'),
    plaintext_digest CHAR(64) NOT NULL CHECK (plaintext_digest ~ '^[0-9a-f]{64}$'),
    release_owner_id_digest CHAR(64) NOT NULL CHECK (release_owner_id_digest ~ '^[0-9a-f]{64}$'),
    release_auth_tag CHAR(64) NOT NULL CHECK (release_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phase16_qualification_runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES phase16_qualification_campaigns(campaign_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign_id)
);

-- case/claim/attempt/receipt/validation/outcome 均为新 campaign 的独立事实，不能借用 V5 row。
CREATE TABLE IF NOT EXISTS phase16_qualification_case_slots (
    run_id TEXT NOT NULL REFERENCES phase16_qualification_runs(run_id),
    slot_position INTEGER NOT NULL CHECK (slot_position > 0),
    case_id TEXT NOT NULL,
    case_digest CHAR(64) NOT NULL CHECK (case_digest ~ '^[0-9a-f]{64}$'),
    expected_e2e BOOLEAN NOT NULL,
    PRIMARY KEY (run_id, case_id),
    UNIQUE (run_id, slot_position)
);

CREATE TABLE IF NOT EXISTS phase16_qualification_case_claims (
    claim_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, case_id),
    FOREIGN KEY (run_id, case_id) REFERENCES phase16_qualification_case_slots(run_id, case_id)
);

CREATE TABLE IF NOT EXISTS phase16_qualification_dispatch_attempts (
    attempt_id UUID PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES phase16_qualification_runs(run_id),
    claim_id UUID NOT NULL REFERENCES phase16_qualification_case_claims(claim_id),
    stage TEXT NOT NULL CHECK (stage IN ('ANALYST', 'PLANNER')),
    profile_digest CHAR(64) NOT NULL CHECK (profile_digest ~ '^[0-9a-f]{64}$'),
    internal_request_id UUID NOT NULL,
    -- 单次 dispatch 预留上界由 policy 的 stage_reservation 在 ledger Python 层强制；
    -- DDL 只做宽松兜底。
    reservation_cny NUMERIC(12,6) NOT NULL CHECK (reservation_cny > 0 AND reservation_cny <= 1.000000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, stage),
    UNIQUE (internal_request_id)
);

CREATE TABLE IF NOT EXISTS phase16_qualification_provider_receipts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_qualification_dispatch_attempts(attempt_id),
    provider_response_id_digest CHAR(64),
    finish_reason TEXT,
    model_id TEXT NOT NULL,
    -- V9 矩阵配置：该 receipt 实际使用的思考强度（与 model_id / responded_endpoint_host
    -- 一起钉死运行时矩阵组合，受 receipt_auth_tag HMAC 覆盖）。
    reasoning_effort TEXT,
    response_digest CHAR(64) NOT NULL CHECK (response_digest ~ '^[0-9a-f]{64}$'),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens >= 0),
    latency_ms NUMERIC(14,3) NOT NULL CHECK (latency_ms >= 0),
    -- 该 receipt 对应的实际调用次数与实际响应端点（V9 传输层重试事实）。
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    responded_endpoint_host TEXT,
    actual_cost_cny NUMERIC(12,6) CHECK (actual_cost_cny >= 0),
    output_digest CHAR(64) NOT NULL CHECK (output_digest ~ '^[0-9a-f]{64}$'),
    receipt_complete BOOLEAN NOT NULL,
    receipt_auth_tag CHAR(64) NOT NULL CHECK (receipt_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (receipt_complete = false)
        OR (
            provider_response_id_digest IS NOT NULL
            AND finish_reason = 'stop'
            AND input_tokens IS NOT NULL
            AND output_tokens IS NOT NULL
            AND total_tokens = input_tokens + output_tokens
            AND actual_cost_cny IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS phase16_qualification_validation_facts (
    attempt_id UUID PRIMARY KEY REFERENCES phase16_qualification_dispatch_attempts(attempt_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    validation_digest CHAR(64) NOT NULL CHECK (validation_digest ~ '^[0-9a-f]{64}$'),
    validation_auth_tag CHAR(64) NOT NULL CHECK (validation_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phase16_qualification_pre_dispatch_blocks (
    claim_id UUID PRIMARY KEY REFERENCES phase16_qualification_case_claims(claim_id),
    run_id TEXT NOT NULL REFERENCES phase16_qualification_runs(run_id),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    block_digest CHAR(64) NOT NULL CHECK (block_digest ~ '^[0-9a-f]{64}$'),
    block_auth_tag CHAR(64) NOT NULL CHECK (block_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS phase16_qualification_case_outcomes (
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    claim_id UUID NOT NULL REFERENCES phase16_qualification_case_claims(claim_id),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    outcome_digest CHAR(64) NOT NULL CHECK (outcome_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, case_id),
    UNIQUE (claim_id),
    FOREIGN KEY (run_id, case_id) REFERENCES phase16_qualification_case_slots(run_id, case_id)
);

-- 指标事实允许按同一 run 的不同 metric 追加，但同名 metric 绝不能被重写。
CREATE TABLE IF NOT EXISTS phase16_qualification_metric_facts (
    run_id TEXT NOT NULL REFERENCES phase16_qualification_runs(run_id),
    metric_code TEXT NOT NULL CHECK (metric_code ~ '^[A-Z][A-Z0-9_]*$'),
    numerator INTEGER NOT NULL CHECK (numerator >= 0),
    denominator INTEGER NOT NULL CHECK (denominator > 0 AND numerator <= denominator),
    metric_digest CHAR(64) NOT NULL CHECK (metric_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, metric_code)
);

-- run 的结论只能插入一次；result digest/HMAC 固定 evaluator 的非敏感摘要，不接受自由文本。
CREATE TABLE IF NOT EXISTS phase16_qualification_results (
    run_id TEXT PRIMARY KEY REFERENCES phase16_qualification_runs(run_id),
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAILED', 'BLOCKED')),
    reason_code TEXT NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]*$'),
    evaluation_digest CHAR(64) NOT NULL CHECK (evaluation_digest ~ '^[0-9a-f]{64}$'),
    result_digest CHAR(64) NOT NULL CHECK (result_digest ~ '^[0-9a-f]{64}$'),
    result_auth_tag CHAR(64) NOT NULL CHECK (result_auth_tag ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION phase16_qualification_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 qualification ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_qualification_reject_truncate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase16 qualification ledger cannot be truncated';
END;
$$ LANGUAGE plpgsql;

-- HMAC 不在 SQL 内验证（密钥不进 DB）；release 是 corpus 级一次性事实，且只能引用
-- COMMITTED/RELEASED corpus。每个 holdout batch 都引用同一 release，而不是伪造两次释放。
CREATE OR REPLACE FUNCTION phase16_qualification_validate_release_event()
RETURNS trigger AS $$
DECLARE
    declared_state TEXT;
BEGIN
    SELECT holdout_release_state
      INTO declared_state
      FROM phase16_qualification_corpora
     WHERE corpus_digest = NEW.corpus_digest;
    IF declared_state NOT IN ('COMMITTED', 'RELEASED') THEN
        RAISE EXCEPTION 'phase16 qualification release requires committed corpus';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_qualification_validate_dispatch_attempt()
RETURNS trigger AS $$
DECLARE
    analyst_pass BOOLEAN;
BEGIN
    IF NEW.stage = 'PLANNER' THEN
        SELECT EXISTS (
            SELECT 1
              FROM phase16_qualification_dispatch_attempts analyst
              JOIN phase16_qualification_provider_receipts receipt ON receipt.attempt_id = analyst.attempt_id
              JOIN phase16_qualification_validation_facts validation ON validation.attempt_id = analyst.attempt_id
             WHERE analyst.claim_id = NEW.claim_id
               AND analyst.stage = 'ANALYST'
               AND receipt.receipt_complete = true
               AND validation.verdict = 'PASS'
        ) INTO analyst_pass;
        IF NOT analyst_pass THEN
            RAISE EXCEPTION 'phase16 qualification planner requires completed analyst validation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_qualification_validate_validation_fact()
RETURNS trigger AS $$
DECLARE
    receipt_complete BOOLEAN;
BEGIN
    SELECT receipt.receipt_complete INTO receipt_complete
      FROM phase16_qualification_provider_receipts receipt WHERE receipt.attempt_id = NEW.attempt_id;
    IF NEW.verdict = 'PASS' AND receipt_complete IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'phase16 qualification PASS validation requires complete receipt';
    END IF;
    IF NEW.verdict = 'BLOCKED' AND receipt_complete IS NOT NULL THEN
        RAISE EXCEPTION 'phase16 qualification BLOCKED validation cannot have receipt';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase16_qualification_validate_case_outcome()
RETURNS trigger AS $$
DECLARE
    expected_e2e BOOLEAN;
    pass_count INTEGER;
    failure_count INTEGER;
    blocked_count INTEGER;
    has_pre_dispatch_block BOOLEAN;
BEGIN
    SELECT slot.expected_e2e INTO expected_e2e
      FROM phase16_qualification_case_slots slot
     WHERE slot.run_id = NEW.run_id AND slot.case_id = NEW.case_id;
    IF expected_e2e IS NULL THEN
        RAISE EXCEPTION 'phase16 qualification outcome case is unknown';
    END IF;
    SELECT
        count(*) FILTER (WHERE validation.verdict = 'PASS' AND receipt.receipt_complete = true),
        count(*) FILTER (WHERE validation.verdict = 'FAILED'),
        count(*) FILTER (WHERE validation.verdict = 'BLOCKED')
      INTO pass_count, failure_count, blocked_count
      FROM phase16_qualification_dispatch_attempts attempt
      LEFT JOIN phase16_qualification_provider_receipts receipt ON receipt.attempt_id = attempt.attempt_id
      LEFT JOIN phase16_qualification_validation_facts validation ON validation.attempt_id = attempt.attempt_id
     WHERE attempt.claim_id = NEW.claim_id;
    SELECT EXISTS (
        SELECT 1 FROM phase16_qualification_pre_dispatch_blocks block WHERE block.claim_id = NEW.claim_id
    ) INTO has_pre_dispatch_block;
    IF NEW.status = 'PASS' AND expected_e2e AND pass_count <> 2 THEN
        RAISE EXCEPTION 'phase16 qualification E2E PASS case requires two completed stage validations';
    END IF;
    IF NEW.status = 'PASS' AND NOT expected_e2e AND pass_count <> 0 THEN
        RAISE EXCEPTION 'phase16 qualification non-E2E PASS must not dispatch model stages';
    END IF;
    IF NEW.status = 'FAILED' AND failure_count = 0 THEN
        RAISE EXCEPTION 'phase16 qualification FAILED case requires failed validation';
    END IF;
    IF NEW.status = 'BLOCKED' AND blocked_count = 0 AND NOT has_pre_dispatch_block THEN
        RAISE EXCEPTION 'phase16 qualification BLOCKED case requires blocked validation or pre-dispatch fact';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 指标只能在 run 未终态时追加；Python 的预检不是安全边界，SQL 也必须拒绝 close 之后的插入。
CREATE OR REPLACE FUNCTION phase16_qualification_validate_metric_fact()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM phase16_qualification_results WHERE run_id = NEW.run_id) THEN
        RAISE EXCEPTION 'phase16 qualification metrics cannot follow terminal result';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 高信任 PASS 必须有完整可复算 metric：E2E 的 30/30、全部硬安全、以及 holdout release。
CREATE OR REPLACE FUNCTION phase16_qualification_validate_result()
RETURNS trigger AS $$
DECLARE
    declared_kind TEXT;
    e2e_numerator INTEGER;
    e2e_denominator INTEGER;
    safety_numerator INTEGER;
    safety_denominator INTEGER;
    required_e2e_denominator INTEGER;
    corpus_identity CHAR(64);
    has_release BOOLEAN;
BEGIN
    SELECT campaign.campaign_kind, policy.holdout_e2e_cases_per_batch, campaign.corpus_digest
      INTO declared_kind, required_e2e_denominator, corpus_identity
      FROM phase16_qualification_campaigns campaign
      JOIN phase16_qualification_policies policy ON policy.policy_digest = campaign.policy_digest
      JOIN phase16_qualification_runs run ON run.campaign_id = campaign.campaign_id
     WHERE run.run_id = NEW.run_id;
    IF declared_kind IS NULL THEN
        RAISE EXCEPTION 'phase16 qualification result run is unknown';
    END IF;
    IF NEW.status = 'PASS' THEN
        SELECT numerator, denominator INTO e2e_numerator, e2e_denominator
          FROM phase16_qualification_metric_facts
         WHERE run_id = NEW.run_id AND metric_code = 'E2E_MULTI_AGENT_READY';
        SELECT numerator, denominator INTO safety_numerator, safety_denominator
          FROM phase16_qualification_metric_facts
         WHERE run_id = NEW.run_id AND metric_code = 'HARD_SAFETY_CONFORMANCE';
        IF e2e_numerator IS NULL OR safety_numerator IS NULL
           OR e2e_numerator <> e2e_denominator
           OR safety_numerator <> safety_denominator THEN
            RAISE EXCEPTION 'phase16 qualification PASS requires complete E2E and safety metrics';
        END IF;
        IF declared_kind = 'HOLDOUT' THEN
            SELECT EXISTS (
                SELECT 1 FROM phase16_qualification_release_events WHERE corpus_digest = corpus_identity
            ) INTO has_release;
            IF e2e_denominator <> required_e2e_denominator OR NOT has_release THEN
                RAISE EXCEPTION 'phase16 qualification holdout PASS requires released complete batch E2E evidence';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'phase16_qualification_policies', 'phase16_qualification_corpora',
        'phase16_qualification_candidates', 'phase16_qualification_campaigns',
        'phase16_qualification_source_evidence', 'phase16_qualification_release_events',
        'phase16_qualification_runs', 'phase16_qualification_case_slots',
        'phase16_qualification_case_claims', 'phase16_qualification_dispatch_attempts',
        'phase16_qualification_provider_receipts', 'phase16_qualification_validation_facts',
        'phase16_qualification_pre_dispatch_blocks', 'phase16_qualification_case_outcomes', 'phase16_qualification_metric_facts',
        'phase16_qualification_results'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_append_only ON %I', table_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_append_only BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION phase16_qualification_reject_mutation()',
            table_name, table_name
        );
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_no_truncate ON %I', table_name, table_name);
        EXECUTE format(
            'CREATE TRIGGER trg_%s_no_truncate BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION phase16_qualification_reject_truncate()',
            table_name, table_name
        );
    END LOOP;
END;
$$;

DROP TRIGGER IF EXISTS trg_phase16_qualification_dispatch_attempt ON phase16_qualification_dispatch_attempts;
CREATE TRIGGER trg_phase16_qualification_dispatch_attempt
    BEFORE INSERT ON phase16_qualification_dispatch_attempts
    FOR EACH ROW EXECUTE FUNCTION phase16_qualification_validate_dispatch_attempt();

DROP TRIGGER IF EXISTS trg_phase16_qualification_validation_fact ON phase16_qualification_validation_facts;
CREATE TRIGGER trg_phase16_qualification_validation_fact
    BEFORE INSERT ON phase16_qualification_validation_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_qualification_validate_validation_fact();

DROP TRIGGER IF EXISTS trg_phase16_qualification_case_outcome ON phase16_qualification_case_outcomes;
CREATE TRIGGER trg_phase16_qualification_case_outcome
    BEFORE INSERT ON phase16_qualification_case_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_qualification_validate_case_outcome();

DROP TRIGGER IF EXISTS trg_phase16_qualification_metric_fact ON phase16_qualification_metric_facts;
CREATE TRIGGER trg_phase16_qualification_metric_fact
    BEFORE INSERT ON phase16_qualification_metric_facts
    FOR EACH ROW EXECUTE FUNCTION phase16_qualification_validate_metric_fact();

DROP TRIGGER IF EXISTS trg_phase16_qualification_release_event ON phase16_qualification_release_events;
CREATE TRIGGER trg_phase16_qualification_release_event
    BEFORE INSERT ON phase16_qualification_release_events
    FOR EACH ROW EXECUTE FUNCTION phase16_qualification_validate_release_event();

DROP TRIGGER IF EXISTS trg_phase16_qualification_result ON phase16_qualification_results;
CREATE TRIGGER trg_phase16_qualification_result
    BEFORE INSERT ON phase16_qualification_results
    FOR EACH ROW EXECUTE FUNCTION phase16_qualification_validate_result();
