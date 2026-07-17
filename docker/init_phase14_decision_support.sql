-- Phase 14 三场景 Workspace 根投影、操作员租约与 append-only 事实表。
CREATE TABLE IF NOT EXISTS phase14_live_session_workspaces (
    live_session_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    root_plan_run_id TEXT NOT NULL,
    event_inbox_scope_id TEXT NOT NULL,
    decision_trace_scope_id TEXT NOT NULL,
    replay_scope_id TEXT NOT NULL,
    evaluation_scope_id TEXT NOT NULL,
    current_view TEXT NOT NULL CHECK (current_view IN ('PREPARE','LIVE','REVIEW')),
    version BIGINT NOT NULL CHECK (version >= 1),
    lock_operator_id TEXT,
    lock_lease_until TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((lock_operator_id IS NULL) = (lock_lease_until IS NULL))
);

-- 兼容本机已运行的 Task 2 草案空表/探针；正式写入后这些列均为必填身份。
ALTER TABLE phase14_live_session_workspaces ADD COLUMN IF NOT EXISTS root_plan_run_id TEXT;
ALTER TABLE phase14_live_session_workspaces ADD COLUMN IF NOT EXISTS event_inbox_scope_id TEXT;
ALTER TABLE phase14_live_session_workspaces ADD COLUMN IF NOT EXISTS decision_trace_scope_id TEXT;
ALTER TABLE phase14_live_session_workspaces ADD COLUMN IF NOT EXISTS replay_scope_id TEXT;
ALTER TABLE phase14_live_session_workspaces ADD COLUMN IF NOT EXISTS evaluation_scope_id TEXT;
UPDATE phase14_live_session_workspaces SET
    root_plan_run_id=COALESCE(root_plan_run_id,'legacy-plan:'||live_session_id),
    event_inbox_scope_id=COALESCE(event_inbox_scope_id,'legacy-event:'||live_session_id),
    decision_trace_scope_id=COALESCE(decision_trace_scope_id,'legacy-decision:'||live_session_id),
    replay_scope_id=COALESCE(replay_scope_id,'legacy-replay:'||live_session_id),
    evaluation_scope_id=COALESCE(evaluation_scope_id,'legacy-evaluation:'||live_session_id);
ALTER TABLE phase14_live_session_workspaces ALTER COLUMN root_plan_run_id SET NOT NULL;
ALTER TABLE phase14_live_session_workspaces ALTER COLUMN event_inbox_scope_id SET NOT NULL;
ALTER TABLE phase14_live_session_workspaces ALTER COLUMN decision_trace_scope_id SET NOT NULL;
ALTER TABLE phase14_live_session_workspaces ALTER COLUMN replay_scope_id SET NOT NULL;
ALTER TABLE phase14_live_session_workspaces ALTER COLUMN evaluation_scope_id SET NOT NULL;

CREATE TABLE IF NOT EXISTS phase14_workspace_idempotency (
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    idempotency_key TEXT NOT NULL,
    fact_kind TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    fact_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (live_session_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS phase14_incidents (
    incident_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS phase14_evidence_bundles (
    evidence_bundle_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    incident_id TEXT NOT NULL REFERENCES phase14_incidents(incident_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS phase14_proposals (
    proposal_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    incident_id TEXT NOT NULL REFERENCES phase14_incidents(incident_id),
    evidence_bundle_id TEXT NOT NULL REFERENCES phase14_evidence_bundles(evidence_bundle_id),
    proposal_key TEXT NOT NULL,
    proposal_version BIGINT NOT NULL CHECK (proposal_version >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- 本地恢复可能已运行过 Task 2 草案 DDL；以 proposal_id 回填旧探针后补正式 lineage 约束。
ALTER TABLE phase14_proposals ADD COLUMN IF NOT EXISTS proposal_key TEXT;
UPDATE phase14_proposals SET proposal_key=proposal_id WHERE proposal_key IS NULL;
ALTER TABLE phase14_proposals ALTER COLUMN proposal_key SET NOT NULL;
DROP INDEX IF EXISTS uq_phase14_proposal_lineage_version;
CREATE UNIQUE INDEX uq_phase14_proposal_lineage_version
    ON phase14_proposals(live_session_id, proposal_key, proposal_version);

CREATE TABLE IF NOT EXISTS phase14_operator_decisions (
    decision_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    proposal_id TEXT NOT NULL REFERENCES phase14_proposals(proposal_id),
    operator_id TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS phase14_execution_commands (
    command_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    decision_id TEXT NOT NULL REFERENCES phase14_operator_decisions(decision_id),
    operator_id TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- 复合唯一键与外键把 live_session_id 纳入每一条父子关系；即使绕过 Store，
-- 数据库也不能把另一个直播会话的事故、证据、方案、决定或命令串进当前 Workspace。
ALTER TABLE phase14_evidence_bundles
    DROP CONSTRAINT IF EXISTS fk_phase14_evidence_incident_scope;
ALTER TABLE phase14_proposals
    DROP CONSTRAINT IF EXISTS fk_phase14_proposal_evidence_scope;
ALTER TABLE phase14_operator_decisions
    DROP CONSTRAINT IF EXISTS fk_phase14_decision_proposal_scope;
ALTER TABLE phase14_execution_commands
    DROP CONSTRAINT IF EXISTS fk_phase14_command_decision_scope;

DROP INDEX IF EXISTS uq_phase14_incident_scope;
DROP INDEX IF EXISTS uq_phase14_evidence_scope;
DROP INDEX IF EXISTS uq_phase14_proposal_scope;
DROP INDEX IF EXISTS uq_phase14_decision_scope;
CREATE UNIQUE INDEX uq_phase14_incident_scope
    ON phase14_incidents(live_session_id, incident_id);
CREATE UNIQUE INDEX uq_phase14_evidence_scope
    ON phase14_evidence_bundles(live_session_id, incident_id, evidence_bundle_id);
CREATE UNIQUE INDEX uq_phase14_proposal_scope
    ON phase14_proposals(live_session_id, proposal_id);
CREATE UNIQUE INDEX uq_phase14_decision_scope
    ON phase14_operator_decisions(live_session_id, decision_id);

ALTER TABLE phase14_evidence_bundles
    ADD CONSTRAINT fk_phase14_evidence_incident_scope
    FOREIGN KEY (live_session_id,incident_id)
    REFERENCES phase14_incidents(live_session_id,incident_id);
ALTER TABLE phase14_proposals
    ADD CONSTRAINT fk_phase14_proposal_evidence_scope
    FOREIGN KEY (live_session_id,incident_id,evidence_bundle_id)
    REFERENCES phase14_evidence_bundles(live_session_id,incident_id,evidence_bundle_id);
ALTER TABLE phase14_operator_decisions
    ADD CONSTRAINT fk_phase14_decision_proposal_scope
    FOREIGN KEY (live_session_id,proposal_id)
    REFERENCES phase14_proposals(live_session_id,proposal_id);
ALTER TABLE phase14_execution_commands
    ADD CONSTRAINT fk_phase14_command_decision_scope
    FOREIGN KEY (live_session_id,decision_id)
    REFERENCES phase14_operator_decisions(live_session_id,decision_id);

-- 每个 Proposal 只能形成一个人工终态决定；修改必须先形成新 Proposal 版本，
-- 不能靠读取新的 Workspace 版本后继续向同一 Proposal 追加矛盾事实。
DROP INDEX IF EXISTS uq_phase14_one_decision_per_proposal;
CREATE UNIQUE INDEX uq_phase14_one_decision_per_proposal
    ON phase14_operator_decisions(proposal_id);

-- 关系身份用于并发和外键，JSONB 用于审计重放；两份表示必须逐行同构。
CREATE OR REPLACE FUNCTION phase14_validate_fact_payload() RETURNS trigger AS $$
DECLARE
    parent_proposal_version BIGINT;
    parent_proposal_key TEXT;
    latest_proposal_version BIGINT;
BEGIN
    IF NEW.payload->>'live_session_id' IS DISTINCT FROM NEW.live_session_id
       OR (NEW.payload->>'created_at')::timestamptz IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'phase14 fact payload identity mismatch';
    END IF;

    CASE TG_TABLE_NAME
        WHEN 'phase14_incidents' THEN
            IF NEW.payload->>'incident_id' IS DISTINCT FROM NEW.incident_id THEN
                RAISE EXCEPTION 'phase14 incident payload identity mismatch';
            END IF;
        WHEN 'phase14_evidence_bundles' THEN
            IF NEW.payload->>'evidence_bundle_id' IS DISTINCT FROM NEW.evidence_bundle_id
               OR NEW.payload->>'incident_id' IS DISTINCT FROM NEW.incident_id THEN
                RAISE EXCEPTION 'phase14 evidence payload identity mismatch';
            END IF;
        WHEN 'phase14_proposals' THEN
            IF NEW.payload->>'proposal_id' IS DISTINCT FROM NEW.proposal_id
               OR NEW.payload->>'incident_id' IS DISTINCT FROM NEW.incident_id
               OR NEW.payload->>'evidence_bundle_id' IS DISTINCT FROM NEW.evidence_bundle_id
               OR NEW.payload->>'proposal_key' IS DISTINCT FROM NEW.proposal_key
               OR (NEW.payload->>'proposal_version')::bigint IS DISTINCT FROM NEW.proposal_version THEN
                RAISE EXCEPTION 'phase14 proposal payload identity mismatch';
            END IF;
        WHEN 'phase14_operator_decisions' THEN
            IF NEW.payload->>'decision_id' IS DISTINCT FROM NEW.decision_id
               OR NEW.payload->>'proposal_id' IS DISTINCT FROM NEW.proposal_id
               OR NEW.payload->>'operator_id' IS DISTINCT FROM NEW.operator_id THEN
                RAISE EXCEPTION 'phase14 decision payload identity mismatch';
            END IF;
            PERFORM phase14_assert_operator_lease(
                NEW.live_session_id, NEW.operator_id, NEW.fencing_token
            );
            SELECT proposal_version, proposal_key
              INTO parent_proposal_version, parent_proposal_key
              FROM phase14_proposals
             WHERE live_session_id=NEW.live_session_id
               AND proposal_id=NEW.proposal_id;
            IF parent_proposal_version IS NULL
               OR (NEW.payload->>'expected_proposal_version')::bigint
                  IS DISTINCT FROM parent_proposal_version THEN
                RAISE EXCEPTION 'phase14 decision proposal version mismatch';
            END IF;
            SELECT MAX(proposal_version) INTO latest_proposal_version
              FROM phase14_proposals
             WHERE live_session_id=NEW.live_session_id
               AND proposal_key=parent_proposal_key;
            IF latest_proposal_version IS DISTINCT FROM parent_proposal_version THEN
                RAISE EXCEPTION 'phase14 latest proposal is required';
            END IF;
        WHEN 'phase14_execution_commands' THEN
            IF NEW.payload->>'command_id' IS DISTINCT FROM NEW.command_id
               OR NEW.payload->>'decision_id' IS DISTINCT FROM NEW.decision_id THEN
                RAISE EXCEPTION 'phase14 command payload identity mismatch';
            END IF;
            PERFORM phase14_assert_operator_lease(
                NEW.live_session_id, NEW.operator_id, NEW.fencing_token
            );
            IF NOT EXISTS (
                SELECT 1 FROM phase14_operator_decisions decision
                 WHERE decision.live_session_id=NEW.live_session_id
                   AND decision.decision_id=NEW.decision_id
                   AND decision.operator_id=NEW.operator_id
                   AND decision.fencing_token=NEW.fencing_token
            ) THEN
                RAISE EXCEPTION 'phase14 command decision fencing mismatch';
            END IF;
        ELSE
            RAISE EXCEPTION 'phase14 unsupported fact table';
    END CASE;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase14_validate_idempotency_ledger() RETURNS trigger AS $$
DECLARE
    actual_payload JSONB;
BEGIN
    CASE NEW.fact_kind
        WHEN 'incident' THEN
            SELECT payload INTO actual_payload FROM phase14_incidents
             WHERE live_session_id=NEW.live_session_id AND incident_id=NEW.fact_id;
        WHEN 'evidence_bundle' THEN
            SELECT payload INTO actual_payload FROM phase14_evidence_bundles
             WHERE live_session_id=NEW.live_session_id AND evidence_bundle_id=NEW.fact_id;
        WHEN 'proposal' THEN
            SELECT payload INTO actual_payload FROM phase14_proposals
             WHERE live_session_id=NEW.live_session_id AND proposal_id=NEW.fact_id;
        WHEN 'operator_decision' THEN
            SELECT payload INTO actual_payload FROM phase14_operator_decisions
             WHERE live_session_id=NEW.live_session_id AND decision_id=NEW.fact_id;
        WHEN 'execution_command' THEN
            SELECT payload INTO actual_payload FROM phase14_execution_commands
             WHERE live_session_id=NEW.live_session_id AND command_id=NEW.fact_id;
        ELSE
            RAISE EXCEPTION 'phase14 idempotency ledger fact kind is invalid';
    END CASE;
    IF actual_payload IS NULL
       OR actual_payload IS DISTINCT FROM NEW.fact_payload
       OR NEW.fact_payload->>'idempotency_key' IS DISTINCT FROM NEW.idempotency_key THEN
        RAISE EXCEPTION 'phase14 idempotency ledger payload mismatch';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase14_assert_operator_lease(
    target_live_session_id TEXT,
    target_operator_id TEXT,
    target_fencing_token BIGINT
) RETURNS void AS $$
DECLARE
    workspace_row phase14_live_session_workspaces%ROWTYPE;
BEGIN
    SELECT * INTO workspace_row FROM phase14_live_session_workspaces
     WHERE live_session_id=target_live_session_id FOR UPDATE;
    IF workspace_row.live_session_id IS NULL
       OR workspace_row.lock_operator_id IS DISTINCT FROM target_operator_id
       OR workspace_row.fencing_token IS DISTINCT FROM target_fencing_token
       OR workspace_row.lock_lease_until IS NULL
       OR clock_timestamp() >= workspace_row.lock_lease_until THEN
        RAISE EXCEPTION 'phase14 operator lease is invalid or expired';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION phase14_require_fact_ledger() RETURNS trigger AS $$
DECLARE
    stable_fact_id TEXT;
BEGIN
    stable_fact_id := to_jsonb(NEW)->>TG_ARGV[1];
    IF NOT EXISTS (
        SELECT 1 FROM phase14_workspace_idempotency ledger
         WHERE ledger.live_session_id=NEW.live_session_id
           AND ledger.idempotency_key=NEW.payload->>'idempotency_key'
           AND ledger.fact_kind=TG_ARGV[0]
           AND ledger.fact_id=stable_fact_id
           AND ledger.fact_payload=NEW.payload
    ) THEN
        RAISE EXCEPTION 'phase14 fact is missing idempotency ledger';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_phase14_incidents_payload ON phase14_incidents;
CREATE TRIGGER trg_phase14_incidents_payload BEFORE INSERT ON phase14_incidents
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase14_evidence_payload ON phase14_evidence_bundles;
CREATE TRIGGER trg_phase14_evidence_payload BEFORE INSERT ON phase14_evidence_bundles
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase14_proposals_payload ON phase14_proposals;
CREATE TRIGGER trg_phase14_proposals_payload BEFORE INSERT ON phase14_proposals
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase14_decisions_payload ON phase14_operator_decisions;
CREATE TRIGGER trg_phase14_decisions_payload BEFORE INSERT ON phase14_operator_decisions
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase14_commands_payload ON phase14_execution_commands;
CREATE TRIGGER trg_phase14_commands_payload BEFORE INSERT ON phase14_execution_commands
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase14_idempotency_payload ON phase14_workspace_idempotency;
CREATE TRIGGER trg_phase14_idempotency_payload BEFORE INSERT ON phase14_workspace_idempotency
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_idempotency_ledger();

DROP TRIGGER IF EXISTS trg_phase14_incidents_require_ledger ON phase14_incidents;
CREATE CONSTRAINT TRIGGER trg_phase14_incidents_require_ledger
    AFTER INSERT ON phase14_incidents DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('incident','incident_id');
DROP TRIGGER IF EXISTS trg_phase14_evidence_require_ledger ON phase14_evidence_bundles;
CREATE CONSTRAINT TRIGGER trg_phase14_evidence_require_ledger
    AFTER INSERT ON phase14_evidence_bundles DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('evidence_bundle','evidence_bundle_id');
DROP TRIGGER IF EXISTS trg_phase14_proposals_require_ledger ON phase14_proposals;
CREATE CONSTRAINT TRIGGER trg_phase14_proposals_require_ledger
    AFTER INSERT ON phase14_proposals DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('proposal','proposal_id');
DROP TRIGGER IF EXISTS trg_phase14_decisions_require_ledger ON phase14_operator_decisions;
CREATE CONSTRAINT TRIGGER trg_phase14_decisions_require_ledger
    AFTER INSERT ON phase14_operator_decisions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('operator_decision','decision_id');
DROP TRIGGER IF EXISTS trg_phase14_commands_require_ledger ON phase14_execution_commands;
CREATE CONSTRAINT TRIGGER trg_phase14_commands_require_ledger
    AFTER INSERT ON phase14_execution_commands DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('execution_command','command_id');

-- 事实表和幂等账本只允许追加。Workspace 根投影是唯一可更新的关系行。
CREATE OR REPLACE FUNCTION phase14_reject_fact_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'phase14 decision support facts are append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_phase14_incidents_append_only ON phase14_incidents;
CREATE TRIGGER trg_phase14_incidents_append_only BEFORE UPDATE OR DELETE ON phase14_incidents
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase14_evidence_append_only ON phase14_evidence_bundles;
CREATE TRIGGER trg_phase14_evidence_append_only BEFORE UPDATE OR DELETE ON phase14_evidence_bundles
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase14_proposals_append_only ON phase14_proposals;
CREATE TRIGGER trg_phase14_proposals_append_only BEFORE UPDATE OR DELETE ON phase14_proposals
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase14_decisions_append_only ON phase14_operator_decisions;
CREATE TRIGGER trg_phase14_decisions_append_only BEFORE UPDATE OR DELETE ON phase14_operator_decisions
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase14_commands_append_only ON phase14_execution_commands;
CREATE TRIGGER trg_phase14_commands_append_only BEFORE UPDATE OR DELETE ON phase14_execution_commands
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase14_idempotency_append_only ON phase14_workspace_idempotency;
CREATE TRIGGER trg_phase14_idempotency_append_only BEFORE UPDATE OR DELETE ON phase14_workspace_idempotency
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
