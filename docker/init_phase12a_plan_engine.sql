-- Phase 12A DAG PlanEngine 权威事实表。
--
-- PlanStore 与 LangGraph PostgresSaver 只共享 PostgreSQL 实例，不共享事务、外键或
-- 私有表。关系列承载并发身份、状态、lease 与 fencing；JSONB 承载不可变计划、
-- 输入输出和审计扩展事实。所有时间使用 TIMESTAMPTZ，由应用统一写入 UTC。

CREATE TABLE IF NOT EXISTS plan_runs (
    plan_run_id UUID PRIMARY KEY,
    room_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    run_key TEXT NOT NULL UNIQUE,
    plan_digest TEXT NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    execution_route TEXT NOT NULL DEFAULT 'PLAN_ENGINE'
        CHECK (execution_route IN ('PLAN_ENGINE')),
    state TEXT NOT NULL CHECK (state IN (
        'ACTIVE', 'FROZEN', 'SUCCEEDED', 'FAILED'
    )),
    planning_input JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_version_id UUID PRIMARY KEY,
    plan_run_id UUID NOT NULL
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    parent_plan_version_id UUID
        REFERENCES plan_versions(plan_version_id) ON DELETE RESTRICT,
    provider_id TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    proposal JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT plan_versions_run_version_unique
        UNIQUE (plan_run_id, version_number),
    CONSTRAINT plan_versions_identity_scope_unique
        UNIQUE (plan_version_id, plan_run_id, version_number)
);

CREATE TABLE IF NOT EXISTS plan_nodes (
    node_id UUID PRIMARY KEY,
    plan_version_id UUID NOT NULL,
    plan_run_id UUID NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number >= 1),
    node_order INTEGER NOT NULL CHECK (node_order >= 0),
    logical_key TEXT NOT NULL,
    node_kind TEXT NOT NULL CHECK (node_kind IN ('CONTROL', 'SKILL')),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'READY', 'RUNNING', 'WAITING_APPROVAL',
        'WAITING_RECONCILIATION', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED',
        'FROZEN', 'INVALIDATED', 'SKIPPED'
    )),
    skill_id TEXT,
    skill_version TEXT,
    input_bindings JSONB NOT NULL,
    capability JSONB NOT NULL,
    resource_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    retry_at TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ,
    reused_from_node_id UUID REFERENCES plan_nodes(node_id) ON DELETE RESTRICT,
    invalidated_from_node_id UUID REFERENCES plan_nodes(node_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT plan_nodes_version_fk
        FOREIGN KEY (plan_version_id, plan_run_id, version_number)
        REFERENCES plan_versions(plan_version_id, plan_run_id, version_number)
        ON DELETE RESTRICT,
    CONSTRAINT plan_nodes_skill_shape_check CHECK (
        (node_kind = 'CONTROL' AND skill_id IS NULL AND skill_version IS NULL)
        OR (node_kind = 'SKILL' AND skill_id IS NOT NULL AND skill_version IS NOT NULL)
    ),
    CONSTRAINT plan_nodes_version_logical_key_unique
        UNIQUE (plan_version_id, logical_key),
    CONSTRAINT plan_nodes_version_order_unique
        UNIQUE (plan_version_id, node_order),
    CONSTRAINT plan_nodes_version_node_identity_unique
        UNIQUE (plan_version_id, node_id)
);

CREATE TABLE IF NOT EXISTS plan_node_dependencies (
    plan_version_id UUID NOT NULL,
    plan_run_id UUID NOT NULL,
    node_id UUID NOT NULL,
    dependency_node_id UUID NOT NULL,
    dependency_order INTEGER NOT NULL CHECK (dependency_order >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_version_id, node_id, dependency_node_id),
    CONSTRAINT plan_node_dependencies_order_unique
        UNIQUE (plan_version_id, node_id, dependency_order),
    CONSTRAINT plan_node_dependencies_node_fk
        FOREIGN KEY (plan_version_id, node_id)
        REFERENCES plan_nodes(plan_version_id, node_id) ON DELETE RESTRICT,
    CONSTRAINT plan_node_dependencies_dependency_fk
        FOREIGN KEY (plan_version_id, dependency_node_id)
        REFERENCES plan_nodes(plan_version_id, node_id) ON DELETE RESTRICT,
    CONSTRAINT plan_node_dependencies_run_fk
        FOREIGN KEY (plan_run_id)
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    CONSTRAINT plan_node_dependencies_not_self_check
        CHECK (node_id <> dependency_node_id)
);

CREATE TABLE IF NOT EXISTS node_runs (
    node_run_id UUID PRIMARY KEY,
    plan_run_id UUID NOT NULL
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    node_id UUID NOT NULL
        REFERENCES plan_nodes(node_id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    claim_version BIGINT NOT NULL CHECK (claim_version >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'READY', 'RUNNING', 'WAITING_APPROVAL',
        'WAITING_RECONCILIATION', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED',
        'FROZEN', 'INVALIDATED', 'SKIPPED'
    )),
    lease_owner TEXT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    input_snapshot JSONB NOT NULL,
    input_fingerprint TEXT CHECK (
        input_fingerprint IS NULL OR input_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    output JSONB,
    failure_fact JSONB,
    resource_keys TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    node_type TEXT NOT NULL,
    skill_id TEXT,
    skill_version TEXT,
    skill_attempt_id UUID,
    deadline_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT node_runs_node_attempt_unique
        UNIQUE (node_id, attempt_number),
    CONSTRAINT node_runs_node_claim_version_unique
        UNIQUE (node_id, claim_version)
);

CREATE TABLE IF NOT EXISTS plan_commands (
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL CHECK (command_type IN (
        'APPROVE', 'REJECT', 'RECONCILE', 'RESUME'
    )),
    plan_run_id UUID NOT NULL
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    expected_plan_version INTEGER NOT NULL CHECK (expected_plan_version >= 1),
    node_id UUID REFERENCES plan_nodes(node_id) ON DELETE RESTRICT,
    expected_node_status TEXT CHECK (
        expected_node_status IS NULL OR expected_node_status IN (
            'PENDING', 'READY', 'RUNNING', 'WAITING_APPROVAL',
            'WAITING_RECONCILIATION', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED',
            'FROZEN', 'INVALIDATED', 'SKIPPED'
        )
    ),
    payload JSONB NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    resulting_node_status TEXT CHECK (
        resulting_node_status IS NULL OR resulting_node_status IN (
            'PENDING', 'READY', 'RUNNING', 'WAITING_APPROVAL',
            'WAITING_RECONCILIATION', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED',
            'FROZEN', 'INVALIDATED', 'SKIPPED'
        )
    ),
    completed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS plan_nodes_ready_retry_idx
    ON plan_nodes (plan_run_id, version_number, state, retry_at, node_id)
    WHERE state IN ('READY', 'RETRY_WAIT');

CREATE INDEX IF NOT EXISTS plan_node_dependencies_dependency_idx
    ON plan_node_dependencies (plan_version_id, dependency_node_id, node_id);

CREATE INDEX IF NOT EXISTS node_runs_active_lease_idx
    ON node_runs (state, lease_until, node_id, claim_version DESC)
    WHERE state = 'RUNNING';

CREATE INDEX IF NOT EXISTS node_runs_resource_keys_gin_idx
    ON node_runs USING GIN (resource_keys);

CREATE INDEX IF NOT EXISTS node_runs_plan_history_idx
    ON node_runs (plan_run_id, node_id, attempt_number);

CREATE INDEX IF NOT EXISTS plan_commands_plan_created_idx
    ON plan_commands (plan_run_id, created_at);
