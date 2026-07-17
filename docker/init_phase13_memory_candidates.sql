CREATE TABLE IF NOT EXISTS phase13_memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    anchor_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    evidence_ids JSONB NOT NULL,
    preferred_category TEXT NOT NULL,
    preferred_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    preferred_product_ids JSONB NOT NULL,
    confidence NUMERIC(4,2) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR','APPROVED','REJECTED','APPLIED')),
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS phase13_memory_promotion_commands (
    command_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES phase13_memory_candidates(candidate_id),
    result_status TEXT NOT NULL CHECK (result_status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR','APPROVED','REJECTED','APPLIED')),
    reason_code TEXT NOT NULL,
    result_version INTEGER NOT NULL CHECK (result_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 既有本机/生产表可能由旧版本创建；显式重建状态 CHECK，保证新资格状态不会只在内存层存在。
ALTER TABLE phase13_memory_candidates DROP CONSTRAINT IF EXISTS phase13_memory_candidates_status_check;
ALTER TABLE phase13_memory_candidates ADD CONSTRAINT phase13_memory_candidates_status_check CHECK (status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR','APPROVED','REJECTED','APPLIED'));
ALTER TABLE phase13_memory_promotion_commands DROP CONSTRAINT IF EXISTS phase13_memory_promotion_commands_result_status_check;
ALTER TABLE phase13_memory_promotion_commands ADD CONSTRAINT phase13_memory_promotion_commands_result_status_check CHECK (result_status IN ('STAGED','ELIGIBLE_AWAITING_OPERATOR','APPROVED','REJECTED','APPLIED'));
