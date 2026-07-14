-- Phase 12B 售罄抢占与增量 Replan 的事件权威事实表。
--
-- PostgreSQL Event Inbox 是事件权威源，Kafka 仅负责传输。首次事件事实永不覆盖，
-- 每次传输投递追加 occurrence；同一事件应用到同一 root plan 时只有一条 Application。
-- 本迁移只扩展 Phase 12A 公开表，不引用 LangGraph PostgresSaver 的任何私有表。

CREATE TABLE IF NOT EXISTS plan_event_inbox (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('SOLD_OUT')),
    room_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    observed_version INTEGER NOT NULL CHECK (observed_version >= 1),
    occurred_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    event_payload JSONB NOT NULL,
    provenance JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'RECEIVED', 'VERIFIED', 'CONFLICT', 'PROCESSING',
        'WAITING_HUMAN', 'APPLIED', 'FAILED'
    )),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    failure_fact JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT plan_event_inbox_time_order_check
        CHECK (updated_at >= created_at),
    CONSTRAINT plan_event_inbox_lease_shape_check CHECK (
        (state = 'PROCESSING'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL)
        OR
        (state <> 'PROCESSING'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL)
    )
);

-- Phase 12A 的历史行必须在不执行手工回填的情况下继续有效。CARD_BATCH/INITIAL 是
-- 旧数据的明确业务语义；紧急 child plan 在 Task 7 创建时会显式写其余 lineage。
ALTER TABLE plan_runs
    ADD COLUMN IF NOT EXISTS plan_kind TEXT NOT NULL DEFAULT 'CARD_BATCH'
        CHECK (plan_kind IN ('CARD_BATCH', 'EMERGENCY_SOLD_OUT')),
    ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0
        CHECK (priority >= 0),
    ADD COLUMN IF NOT EXISTS root_plan_run_id UUID
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS parent_plan_run_id UUID
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS trigger_event_id TEXT
        REFERENCES plan_event_inbox(event_id) ON DELETE RESTRICT;

ALTER TABLE plan_versions
    ADD COLUMN IF NOT EXISTS change_reason TEXT NOT NULL DEFAULT 'INITIAL',
    ADD COLUMN IF NOT EXISTS source_event_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];

CREATE TABLE IF NOT EXISTS plan_event_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL
        REFERENCES plan_event_inbox(event_id) ON DELETE RESTRICT,
    payload_digest TEXT NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    transport TEXT NOT NULL,
    topic TEXT NOT NULL,
    partition INTEGER CHECK (partition IS NULL OR partition >= 0),
    transport_offset BIGINT CHECK (transport_offset IS NULL OR transport_offset >= 0),
    classification TEXT NOT NULL CHECK (classification IN (
        'ACCEPTED', 'DUPLICATE', 'CONFLICT', 'REJECTED'
    )),
    received_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT plan_event_occurrences_transport_unique
        UNIQUE NULLS NOT DISTINCT (transport, topic, partition, transport_offset)
);

CREATE TABLE IF NOT EXISTS plan_event_applications (
    application_id UUID PRIMARY KEY,
    event_id TEXT NOT NULL
        REFERENCES plan_event_inbox(event_id) ON DELETE RESTRICT,
    root_plan_run_id UUID NOT NULL
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    source_plan_version INTEGER NOT NULL CHECK (source_plan_version >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'PENDING', 'FREEZING', 'EMERGENCY_RUNNING', 'WAITING_RECONCILIATION',
        'REPLAN_READY', 'APPLIED', 'FAILED'
    )),
    emergency_plan_run_id UUID
        REFERENCES plan_runs(plan_run_id) ON DELETE RESTRICT,
    applied_plan_version INTEGER CHECK (applied_plan_version >= 1),
    impact_analysis JSONB,
    failure_fact JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT plan_event_applications_event_root_unique
        UNIQUE (event_id, root_plan_run_id),
    CONSTRAINT plan_event_applications_source_version_fk
        FOREIGN KEY (root_plan_run_id, source_plan_version)
        REFERENCES plan_versions(plan_run_id, version_number) ON DELETE RESTRICT,
    CONSTRAINT plan_event_applications_applied_version_fk
        FOREIGN KEY (root_plan_run_id, applied_plan_version)
        REFERENCES plan_versions(plan_run_id, version_number) ON DELETE RESTRICT,
    CONSTRAINT plan_event_applications_time_order_check
        CHECK (updated_at >= created_at)
);

-- 对已有开发数据库重复执行迁移时，命名约束不能重复添加。该约束把紧急计划的
-- 权威 lineage 固定在关系层，防止只写 priority 却遗漏 root/parent/event 关联。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'plan_runs_kind_lineage_check'
          AND conrelid = 'plan_runs'::regclass
    ) THEN
        ALTER TABLE plan_runs
            ADD CONSTRAINT plan_runs_kind_lineage_check CHECK (
                (plan_kind = 'CARD_BATCH'
                    AND priority = 0
                    AND root_plan_run_id IS NULL
                    AND parent_plan_run_id IS NULL
                    AND trigger_event_id IS NULL)
                OR
                (plan_kind = 'EMERGENCY_SOLD_OUT'
                    AND priority = 100
                    AND root_plan_run_id IS NOT NULL
                    AND parent_plan_run_id IS NOT NULL
                    AND trigger_event_id IS NOT NULL)
            );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS plan_event_inbox_claim_idx
    ON plan_event_inbox (created_at, event_id)
    WHERE state IN ('VERIFIED', 'PROCESSING');

CREATE INDEX IF NOT EXISTS plan_event_occurrences_event_idx
    ON plan_event_occurrences (event_id, received_at, occurrence_id);

CREATE INDEX IF NOT EXISTS plan_event_applications_root_idx
    ON plan_event_applications (root_plan_run_id, state, created_at, application_id);

CREATE INDEX IF NOT EXISTS plan_runs_root_priority_idx
    ON plan_runs (root_plan_run_id, priority DESC, created_at, plan_run_id);

CREATE INDEX IF NOT EXISTS plan_runs_trigger_event_idx
    ON plan_runs (trigger_event_id)
    WHERE trigger_event_id IS NOT NULL;
