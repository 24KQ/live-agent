-- Phase 11B 执行尝试事实表。
-- Operation 保存业务幂等身份和不可变意图；Attempt 保存该 Operation 唯一的外部
-- 尝试及终态。两张表独立于 tool_call_audit，避免改变既有审计重放唯一键语义。

CREATE TABLE IF NOT EXISTS skill_execution_operations (
    operation_id UUID PRIMARY KEY,
    skill_id TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    room_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    intent_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT skill_execution_operations_identity_unique
        UNIQUE (skill_id, skill_version, room_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS skill_execution_attempts (
    attempt_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL UNIQUE
        REFERENCES skill_execution_operations(operation_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN (
        'INTENT_RECORDED', 'SUCCEEDED', 'FAILED', 'SIDE_EFFECT_UNKNOWN'
    )),
    terminal_payload JSONB,
    failure_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT skill_execution_attempts_terminal_shape_check CHECK (
        (state = 'INTENT_RECORDED' AND terminal_payload IS NULL AND failure_payload IS NULL)
        OR (state = 'SUCCEEDED' AND terminal_payload IS NOT NULL AND failure_payload IS NULL)
        OR (state IN ('FAILED', 'SIDE_EFFECT_UNKNOWN') AND failure_payload IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS skill_execution_attempts_state_created_idx
    ON skill_execution_attempts (state, created_at);
