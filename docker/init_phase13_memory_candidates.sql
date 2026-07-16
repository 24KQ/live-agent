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
    status TEXT NOT NULL CHECK (status IN ('STAGED','APPROVED','REJECTED','APPLIED')),
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS phase13_memory_promotion_commands (
    command_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES phase13_memory_candidates(candidate_id),
    result_status TEXT NOT NULL CHECK (result_status IN ('STAGED','APPROVED','REJECTED','APPLIED')),
    reason_code TEXT NOT NULL,
    result_version INTEGER NOT NULL CHECK (result_version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
