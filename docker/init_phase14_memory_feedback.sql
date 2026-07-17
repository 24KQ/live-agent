-- Phase 14 Task 9：资格事实与人工确认命令的 append-only 证据。
-- Candidate Store 仍保存候选当前状态；本文件保存“为什么合格”和“谁确认”的独立事实。
CREATE TABLE IF NOT EXISTS phase14_memory_eligibility (
    candidate_id TEXT PRIMARY KEY REFERENCES phase13_memory_candidates(candidate_id),
    command_id TEXT NOT NULL UNIQUE,
    candidate_version INTEGER NOT NULL CHECK (candidate_version >= 1),
    result_status TEXT NOT NULL CHECK (result_status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR')),
    reason_code TEXT NOT NULL,
    evidence_ids JSONB NOT NULL,
    product_whitelist JSONB NOT NULL,
    anchor_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    whitelist_digest TEXT NOT NULL CHECK (whitelist_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase14_memory_confirmations (
    command_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES phase13_memory_candidates(candidate_id),
    operator_id TEXT NOT NULL,
    result_status TEXT NOT NULL CHECK (result_status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR','APPROVED','REJECTED','APPLIED')),
    reason_code TEXT NOT NULL,
    result_version INTEGER NOT NULL CHECK (result_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS phase14_memory_confirmation_intents (
    command_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES phase13_memory_candidates(candidate_id),
    expected_version INTEGER NOT NULL CHECK (expected_version >= 1),
    operator_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 旧版本只有确认结果没有 intent；先按不可变结果回填最小授权事实，再安装 FK。
-- 这一步不删除、不覆盖历史结果，仅让既有事实满足新的引用完整性约束。
INSERT INTO phase14_memory_confirmation_intents(command_id,candidate_id,expected_version,operator_id)
SELECT command_id, candidate_id,
       CASE WHEN result_status = 'APPLIED' THEN GREATEST(result_version - 1, 1) ELSE result_version END,
       operator_id
FROM phase14_memory_confirmations
ON CONFLICT (command_id) DO NOTHING;

-- 重新执行 DDL 时仍需对旧表约束做显式校正，防止历史迁移遗漏新状态。
ALTER TABLE phase14_memory_eligibility DROP CONSTRAINT IF EXISTS phase14_memory_eligibility_result_status_check;
ALTER TABLE phase14_memory_eligibility ADD CONSTRAINT phase14_memory_eligibility_result_status_check CHECK (result_status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR'));
ALTER TABLE phase14_memory_confirmations DROP CONSTRAINT IF EXISTS phase14_memory_confirmations_result_status_check;
ALTER TABLE phase14_memory_confirmations ADD CONSTRAINT phase14_memory_confirmations_result_status_check CHECK (result_status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR','APPROVED','REJECTED','APPLIED'));
ALTER TABLE phase14_memory_confirmations DROP CONSTRAINT IF EXISTS phase14_memory_confirmations_intent_fk;
ALTER TABLE phase14_memory_confirmations ADD CONSTRAINT phase14_memory_confirmations_intent_fk FOREIGN KEY (command_id) REFERENCES phase14_memory_confirmation_intents(command_id);
