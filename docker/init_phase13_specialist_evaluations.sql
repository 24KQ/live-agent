-- Phase 13 Specialist 模型预算、预留与调用结算事实。

CREATE TABLE IF NOT EXISTS specialist_model_budget_ledgers (
    scope_id TEXT PRIMARY KEY,
    total_limit_cny NUMERIC(12, 6) NOT NULL CHECK (total_limit_cny >= 0 AND total_limit_cny <> 'NaN'::numeric),
    phase13_limit_cny NUMERIC(12, 6) NOT NULL CHECK (phase13_limit_cny >= 0 AND phase13_limit_cny <> 'NaN'::numeric),
    phase14_reserved_cny NUMERIC(12, 6) NOT NULL CHECK (phase14_reserved_cny >= 0 AND phase14_reserved_cny <> 'NaN'::numeric),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (phase13_limit_cny + phase14_reserved_cny <= total_limit_cny)
);

CREATE TABLE IF NOT EXISTS specialist_model_budget_candidates (
    scope_id TEXT NOT NULL REFERENCES specialist_model_budget_ledgers(scope_id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    initial_limit_cny NUMERIC(12, 6) NOT NULL CHECK (initial_limit_cny >= 0 AND initial_limit_cny <> 'NaN'::numeric),
    state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'RELEASED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS specialist_model_budget_reservations (
    reservation_id UUID PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES specialist_model_budget_ledgers(scope_id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL CHECK (candidate_id IN ('LIVE_OPS', 'PLANNER', 'REVIEW_MEMORY')),
    reserved_amount_cny NUMERIC(12, 6) NOT NULL CHECK (reserved_amount_cny > 0 AND reserved_amount_cny <> 'NaN'::numeric),
    settled_amount_cny NUMERIC(12, 6),
    usage_known BOOLEAN,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SETTLED', 'RELEASED')),
    version BIGINT NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_id, request_id),
    UNIQUE (scope_id, request_id, state, settled_amount_cny, usage_known),
    FOREIGN KEY (scope_id, candidate_id)
        REFERENCES specialist_model_budget_candidates(scope_id, candidate_id) ON DELETE RESTRICT,
    CHECK (
        (state = 'RESERVED' AND settled_amount_cny IS NULL AND usage_known IS NULL)
        OR (state = 'RELEASED' AND settled_amount_cny IS NULL AND usage_known IS NULL)
        OR (state = 'SETTLED' AND settled_amount_cny IS NOT NULL AND usage_known IS NOT NULL
            AND settled_amount_cny >= 0 AND settled_amount_cny <= reserved_amount_cny)
    )
);

CREATE TABLE IF NOT EXISTS specialist_model_calls (
    call_id UUID PRIMARY KEY,
    scope_id TEXT NOT NULL REFERENCES specialist_model_budget_ledgers(scope_id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    reservation_state TEXT NOT NULL DEFAULT 'SETTLED' CHECK (reservation_state = 'SETTLED'),
    settled_amount_cny NUMERIC(12, 6) NOT NULL CHECK (settled_amount_cny >= 0 AND settled_amount_cny <> 'NaN'::numeric),
    usage_known BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (scope_id, request_id),
    FOREIGN KEY (scope_id, request_id, reservation_state, settled_amount_cny, usage_known)
        REFERENCES specialist_model_budget_reservations(
            scope_id, request_id, state, settled_amount_cny, usage_known
        ) ON DELETE RESTRICT
);

ALTER TABLE specialist_model_calls
    ADD COLUMN IF NOT EXISTS reservation_state TEXT NOT NULL DEFAULT 'SETTLED';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_ledger_amounts_finite') THEN
        ALTER TABLE specialist_model_budget_ledgers
            ADD CONSTRAINT specialist_budget_ledger_amounts_finite
            CHECK (total_limit_cny <> 'NaN'::numeric AND phase13_limit_cny <> 'NaN'::numeric AND phase14_reserved_cny <> 'NaN'::numeric);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_candidate_amount_finite') THEN
        ALTER TABLE specialist_model_budget_candidates
            ADD CONSTRAINT specialist_budget_candidate_amount_finite
            CHECK (initial_limit_cny <> 'NaN'::numeric);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_candidate_fk') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_candidate_fk
            FOREIGN KEY (scope_id, candidate_id)
            REFERENCES specialist_model_budget_candidates(scope_id, candidate_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_settlement_unique') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_settlement_unique
            UNIQUE (scope_id, request_id, state, settled_amount_cny, usage_known);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_model_calls_settlement_fk') THEN
        ALTER TABLE specialist_model_calls
            DROP CONSTRAINT IF EXISTS specialist_model_calls_scope_id_request_id_fkey;
        ALTER TABLE specialist_model_calls
            ADD CONSTRAINT specialist_model_calls_settlement_fk
            FOREIGN KEY (scope_id, request_id, reservation_state, settled_amount_cny, usage_known)
            REFERENCES specialist_model_budget_reservations(
                scope_id, request_id, state, settled_amount_cny, usage_known
            ) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_budget_reservation_amount_finite') THEN
        ALTER TABLE specialist_model_budget_reservations
            ADD CONSTRAINT specialist_budget_reservation_amount_finite
            CHECK (reserved_amount_cny <> 'NaN'::numeric AND (settled_amount_cny IS NULL OR settled_amount_cny <> 'NaN'::numeric));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='specialist_model_calls_amount_finite') THEN
        ALTER TABLE specialist_model_calls
            ADD CONSTRAINT specialist_model_calls_amount_finite
            CHECK (settled_amount_cny <> 'NaN'::numeric);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS specialist_model_budget_reservations_scope_state_idx
    ON specialist_model_budget_reservations (scope_id, state, candidate_id);
