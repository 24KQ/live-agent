-- Phase 16 Task 10：真实双 Agent smoke 的独立 case 级预算账本。
-- 它不保存 Prompt、模型输出、经营 Proposal 或凭据；唯一职责是在两次可能的模型调用前
-- 原子预约完整 0.10 CNY，并在重启后保留保守结算事实。
CREATE TABLE IF NOT EXISTS phase16_smoke_budget_ledgers (
    scope_id TEXT PRIMARY KEY
        CHECK (scope_id = 'PHASE16_MULTI_AGENT_SMOKE'),
    limit_cny NUMERIC(12, 6) NOT NULL DEFAULT 1.000000
        CHECK (limit_cny = 1.000000 AND limit_cny <> 'NaN'::numeric),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 兼容本机早期 Task 10 草案曾允许 ``PHASE16_MULTI_AGENT_SMOKE:<suffix>`` 的空 ledger。
-- 正式语义必须只有一个全局一元池；旧自定义 scope 若仍残留，迁移会 fail-closed 而不是静默合并费用。
ALTER TABLE phase16_smoke_budget_ledgers
    DROP CONSTRAINT IF EXISTS phase16_smoke_budget_ledgers_scope_id_check;
ALTER TABLE phase16_smoke_budget_ledgers
    DROP CONSTRAINT IF EXISTS phase16_smoke_budget_scope_check;
ALTER TABLE phase16_smoke_budget_ledgers
    ADD CONSTRAINT phase16_smoke_budget_scope_check
    CHECK (scope_id = 'PHASE16_MULTI_AGENT_SMOKE');

CREATE TABLE IF NOT EXISTS phase16_smoke_budget_reservations (
    reservation_id UUID PRIMARY KEY,
    scope_id TEXT NOT NULL
        REFERENCES phase16_smoke_budget_ledgers(scope_id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    reserved_amount_cny NUMERIC(12, 6) NOT NULL
        CHECK (
            reserved_amount_cny = 0.100000
            AND reserved_amount_cny <> 'NaN'::numeric
        ),
    settled_amount_cny NUMERIC(12, 6),
    usage_known BOOLEAN,
    outcome_status TEXT,
    outcome_reason_code TEXT,
    state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SETTLED', 'RELEASED')),
    version BIGINT NOT NULL CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope_id, request_id),
    CHECK (
        (state IN ('RESERVED', 'RELEASED') AND settled_amount_cny IS NULL AND usage_known IS NULL)
        OR (
            state = 'SETTLED'
            AND settled_amount_cny IS NOT NULL
            AND settled_amount_cny >= 0
            AND settled_amount_cny <= reserved_amount_cny
            AND settled_amount_cny <> 'NaN'::numeric
            AND usage_known IS NOT NULL
        )
    )
);

-- 旧草案表可能已经存在但没有恢复所需的 case 结论列；以保守 UNKNOWN 迁移旧 settled
-- 记录，绝不让历史费用在重启后被误报为 PASS。正式 release 只来自未发送的 Analyst。
ALTER TABLE phase16_smoke_budget_reservations
    ADD COLUMN IF NOT EXISTS outcome_status TEXT;
ALTER TABLE phase16_smoke_budget_reservations
    ADD COLUMN IF NOT EXISTS outcome_reason_code TEXT;
UPDATE phase16_smoke_budget_reservations
   SET outcome_status = CASE
       WHEN state = 'SETTLED' THEN 'INCONCLUSIVE'
       WHEN state = 'RELEASED' THEN 'FAIL'
       ELSE NULL
   END,
       outcome_reason_code = CASE
       WHEN state = 'SETTLED' THEN 'LEGACY_OUTCOME_UNKNOWN'
       WHEN state = 'RELEASED' THEN 'LEGACY_REQUEST_NOT_SENT'
       ELSE NULL
   END
 WHERE outcome_status IS NULL;
ALTER TABLE phase16_smoke_budget_reservations
    DROP CONSTRAINT IF EXISTS phase16_smoke_budget_outcome_shape_check;
ALTER TABLE phase16_smoke_budget_reservations
    ADD CONSTRAINT phase16_smoke_budget_outcome_shape_check
    CHECK (
        (state = 'RESERVED' AND outcome_status IS NULL AND outcome_reason_code IS NULL)
        OR (
            state = 'SETTLED'
            AND outcome_status IN ('PASS', 'FAIL', 'INCONCLUSIVE')
            AND outcome_reason_code IS NOT NULL
        )
        OR (
            state = 'RELEASED'
            AND outcome_status = 'FAIL'
            AND outcome_reason_code IS NOT NULL
        )
    );

CREATE INDEX IF NOT EXISTS phase16_smoke_budget_scope_state_idx
    ON phase16_smoke_budget_reservations (scope_id, state, request_id);
