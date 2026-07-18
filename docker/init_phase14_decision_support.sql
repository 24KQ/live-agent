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
-- Phase 16 的子外键同样依赖这些候选键；每次重放 DDL 时必须先解除新子表
-- 依赖，才能安全重建历史 Phase 14 的候选索引并再次绑定完整父链。
ALTER TABLE IF EXISTS phase16_escalations
    DROP CONSTRAINT IF EXISTS fk_phase16_escalation_bundle_scope;
ALTER TABLE IF EXISTS phase16_conflict_analyses
    DROP CONSTRAINT IF EXISTS fk_phase16_analysis_escalation_scope;
ALTER TABLE IF EXISTS phase16_multi_agent_outcomes
    DROP CONSTRAINT IF EXISTS fk_phase16_outcome_escalation_scope;
ALTER TABLE IF EXISTS phase16_multi_agent_outcomes
    DROP CONSTRAINT IF EXISTS fk_phase16_outcome_analysis_scope;
ALTER TABLE IF EXISTS phase16_multi_agent_outcomes
    DROP CONSTRAINT IF EXISTS fk_phase16_outcome_proposal_scope;
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

-- Phase 16 受控双 Agent 只在既有 EvidenceBundle 之上追加审计事实。关系列承担
-- 跨 Workspace 的父链约束，JSONB 则保留用于重放和 digest 校验的完整冻结载荷。
CREATE TABLE IF NOT EXISTS phase16_escalations (
    escalation_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    incident_id TEXT NOT NULL,
    evidence_bundle_id TEXT NOT NULL,
    evidence_bundle_digest TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('AUTOMATIC','OPERATOR_REQUESTED')),
    operator_id TEXT,
    fencing_token BIGINT CHECK (fencing_token >= 1),
    expected_workspace_version BIGINT NOT NULL CHECK (expected_workspace_version >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CHECK ((mode = 'OPERATOR_REQUESTED') = (operator_id IS NOT NULL)),
    CHECK ((mode = 'OPERATOR_REQUESTED') = (fencing_token IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS phase16_conflict_analyses (
    analysis_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    incident_id TEXT NOT NULL,
    evidence_bundle_id TEXT NOT NULL,
    evidence_bundle_digest TEXT NOT NULL,
    escalation_id TEXT NOT NULL,
    analyst_profile_id TEXT NOT NULL,
    analyst_profile_version TEXT NOT NULL,
    analyst_profile_digest TEXT NOT NULL,
    expected_workspace_version BIGINT NOT NULL CHECK (expected_workspace_version >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS phase16_multi_agent_outcomes (
    outcome_id TEXT PRIMARY KEY,
    live_session_id TEXT NOT NULL REFERENCES phase14_live_session_workspaces(live_session_id),
    incident_id TEXT NOT NULL,
    evidence_bundle_id TEXT NOT NULL,
    evidence_bundle_digest TEXT NOT NULL,
    escalation_id TEXT NOT NULL,
    escalation_digest TEXT NOT NULL,
    analysis_id TEXT,
    analysis_digest TEXT,
    proposal_id TEXT,
    proposal_digest TEXT,
    status TEXT NOT NULL CHECK (status IN ('READY','DEGRADED')),
    expected_workspace_version BIGINT NOT NULL CHECK (expected_workspace_version >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CHECK ((analysis_id IS NULL) = (analysis_digest IS NULL)),
    CHECK ((proposal_id IS NULL) = (proposal_digest IS NULL))
);

-- Phase 16 尚未有已发布事实时引入数据库 CAS 列。若未来存在未带该版本的
-- 历史行，迁移必须显式设计回放协议而不是猜测版本，因此 SET NOT NULL 应 fail-closed。
ALTER TABLE phase16_escalations
    ADD COLUMN IF NOT EXISTS expected_workspace_version BIGINT;
ALTER TABLE phase16_conflict_analyses
    ADD COLUMN IF NOT EXISTS expected_workspace_version BIGINT;
ALTER TABLE phase16_multi_agent_outcomes
    ADD COLUMN IF NOT EXISTS expected_workspace_version BIGINT;
ALTER TABLE phase16_escalations
    ALTER COLUMN expected_workspace_version SET NOT NULL;
ALTER TABLE phase16_conflict_analyses
    ALTER COLUMN expected_workspace_version SET NOT NULL;
ALTER TABLE phase16_multi_agent_outcomes
    ALTER COLUMN expected_workspace_version SET NOT NULL;

-- 同一 Bundle 只能开启一条升级；同一升级最多一条 Analyst 中间事实和一条终态。
-- 这些唯一性同时是与复合外键配套的候选键，不能只依赖应用层的先查后写。
DROP INDEX IF EXISTS uq_phase16_escalation_scope;
DROP INDEX IF EXISTS uq_phase16_escalation_bundle;
DROP INDEX IF EXISTS uq_phase16_escalation_lineage;
DROP INDEX IF EXISTS uq_phase16_analysis_scope;
DROP INDEX IF EXISTS uq_phase16_analysis_escalation;
DROP INDEX IF EXISTS uq_phase16_analysis_lineage;
DROP INDEX IF EXISTS uq_phase16_outcome_scope;
DROP INDEX IF EXISTS uq_phase16_outcome_escalation;
CREATE UNIQUE INDEX uq_phase16_escalation_scope
    ON phase16_escalations(live_session_id, escalation_id);
CREATE UNIQUE INDEX uq_phase16_escalation_bundle
    ON phase16_escalations(live_session_id, evidence_bundle_id);
CREATE UNIQUE INDEX uq_phase16_escalation_lineage
    ON phase16_escalations(live_session_id, incident_id, evidence_bundle_id, escalation_id);
CREATE UNIQUE INDEX uq_phase16_analysis_scope
    ON phase16_conflict_analyses(live_session_id, analysis_id);
CREATE UNIQUE INDEX uq_phase16_analysis_escalation
    ON phase16_conflict_analyses(live_session_id, escalation_id);
CREATE UNIQUE INDEX uq_phase16_analysis_lineage
    ON phase16_conflict_analyses(
        live_session_id, incident_id, evidence_bundle_id, escalation_id, analysis_id
    );
CREATE UNIQUE INDEX uq_phase16_outcome_scope
    ON phase16_multi_agent_outcomes(live_session_id, outcome_id);
CREATE UNIQUE INDEX uq_phase16_outcome_escalation
    ON phase16_multi_agent_outcomes(live_session_id, escalation_id);

ALTER TABLE phase16_escalations
    DROP CONSTRAINT IF EXISTS fk_phase16_escalation_bundle_scope;
ALTER TABLE phase16_conflict_analyses
    DROP CONSTRAINT IF EXISTS fk_phase16_analysis_escalation_scope;
ALTER TABLE phase16_multi_agent_outcomes
    DROP CONSTRAINT IF EXISTS fk_phase16_outcome_escalation_scope;
ALTER TABLE phase16_multi_agent_outcomes
    DROP CONSTRAINT IF EXISTS fk_phase16_outcome_analysis_scope;
ALTER TABLE phase16_multi_agent_outcomes
    DROP CONSTRAINT IF EXISTS fk_phase16_outcome_proposal_scope;

ALTER TABLE phase16_escalations
    ADD CONSTRAINT fk_phase16_escalation_bundle_scope
    FOREIGN KEY (live_session_id,incident_id,evidence_bundle_id)
    REFERENCES phase14_evidence_bundles(live_session_id,incident_id,evidence_bundle_id);
ALTER TABLE phase16_conflict_analyses
    ADD CONSTRAINT fk_phase16_analysis_escalation_scope
    FOREIGN KEY (live_session_id,incident_id,evidence_bundle_id,escalation_id)
    REFERENCES phase16_escalations(
        live_session_id,incident_id,evidence_bundle_id,escalation_id
    );
ALTER TABLE phase16_multi_agent_outcomes
    ADD CONSTRAINT fk_phase16_outcome_escalation_scope
    FOREIGN KEY (live_session_id,incident_id,evidence_bundle_id,escalation_id)
    REFERENCES phase16_escalations(
        live_session_id,incident_id,evidence_bundle_id,escalation_id
    );
ALTER TABLE phase16_multi_agent_outcomes
    ADD CONSTRAINT fk_phase16_outcome_analysis_scope
    FOREIGN KEY (live_session_id,incident_id,evidence_bundle_id,escalation_id,analysis_id)
    REFERENCES phase16_conflict_analyses(
        live_session_id,incident_id,evidence_bundle_id,escalation_id,analysis_id
    );
ALTER TABLE phase16_multi_agent_outcomes
    ADD CONSTRAINT fk_phase16_outcome_proposal_scope
    FOREIGN KEY (live_session_id,proposal_id)
    REFERENCES phase14_proposals(live_session_id,proposal_id);

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
    derived_trigger_codes JSONB;
    expected_evidence_refs JSONB;
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
        WHEN 'phase16_escalations' THEN
            IF NEW.payload->>'escalation_id' IS DISTINCT FROM NEW.escalation_id
               OR NEW.payload->>'incident_id' IS DISTINCT FROM NEW.incident_id
               OR NEW.payload->>'evidence_bundle_id' IS DISTINCT FROM NEW.evidence_bundle_id
               OR NEW.payload->>'evidence_bundle_digest' IS DISTINCT FROM NEW.evidence_bundle_digest
               OR NEW.payload->>'mode' IS DISTINCT FROM NEW.mode
               OR NEW.payload->>'operator_id' IS DISTINCT FROM NEW.operator_id THEN
                RAISE EXCEPTION 'phase16 escalation payload identity mismatch';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM phase14_evidence_bundles evidence
                 JOIN phase14_live_session_workspaces workspace
                   ON workspace.live_session_id=evidence.live_session_id
                 WHERE evidence.live_session_id=NEW.live_session_id
                   AND evidence.incident_id=NEW.incident_id
                   AND evidence.evidence_bundle_id=NEW.evidence_bundle_id
                   AND evidence.payload->'snapshot'->>'bundle_digest'=NEW.evidence_bundle_digest
                   AND (evidence.payload->'snapshot'->>'valid_until')::timestamptz
                       > clock_timestamp()
                   AND workspace.current_view='LIVE'
            ) THEN
                RAISE EXCEPTION 'phase16 escalation bundle digest mismatch';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM phase14_evidence_bundles evidence
                 WHERE evidence.live_session_id=NEW.live_session_id
                   AND evidence.evidence_bundle_id=NEW.evidence_bundle_id
                   AND evidence.payload->'snapshot'->>'proposal_eligible'='true'
            ) THEN
                RAISE EXCEPTION 'phase16 escalation requires proposal eligible bundle';
            END IF;
            IF NEW.mode = 'OPERATOR_REQUESTED' THEN
                IF NEW.payload->'trigger_codes' IS DISTINCT FROM '[]'::jsonb THEN
                    RAISE EXCEPTION 'phase16 operator escalation cannot carry trigger codes';
                END IF;
                PERFORM phase14_assert_operator_lease(
                    NEW.live_session_id, NEW.operator_id, NEW.fencing_token
                );
            ELSE
                IF NEW.operator_id IS NOT NULL OR NEW.fencing_token IS NOT NULL THEN
                    RAISE EXCEPTION 'phase16 automatic escalation cannot carry lease';
                END IF;
                -- 从同一冻结 Bundle 计算完整三选二信号。触发码必须精确相等，不能
                -- 由 HTTP/任务调用方选择性省略或伪造，且至少两个信号才进入双 Agent。
                SELECT COALESCE(jsonb_agg(signal.code ORDER BY signal.priority),'[]'::jsonb)
                  INTO derived_trigger_codes
                  FROM (
                    SELECT 1 AS priority, to_jsonb('MULTIPLE_VALID_BACKUPS'::text) AS code
                     WHERE (
                        SELECT COUNT(*)
                          FROM phase14_evidence_bundles evidence
                          CROSS JOIN LATERAL jsonb_array_elements(
                              evidence.payload->'snapshot'->'components'
                          ) AS component
                          CROSS JOIN LATERAL jsonb_array_elements(
                              component->'payload'->'backup_products'
                          ) AS backup
                         WHERE evidence.live_session_id=NEW.live_session_id
                           AND evidence.evidence_bundle_id=NEW.evidence_bundle_id
                           AND component->>'role'='PRODUCT_INVENTORY_SNAPSHOT'
                           AND COALESCE((backup->>'is_active')::boolean,FALSE)
                           AND COALESCE((backup->>'inventory')::integer,0)>0
                     ) >= 2
                    UNION ALL
                    SELECT 2, to_jsonb('AVAILABILITY_NOISE_HIGH'::text)
                     WHERE EXISTS (
                        SELECT 1
                          FROM phase14_evidence_bundles evidence
                          CROSS JOIN LATERAL jsonb_array_elements(
                              evidence.payload->'snapshot'->'components'
                          ) AS component
                          CROSS JOIN LATERAL jsonb_array_elements(
                              component->'payload'->'topics'
                          ) AS topic
                         WHERE evidence.live_session_id=NEW.live_session_id
                           AND evidence.evidence_bundle_id=NEW.evidence_bundle_id
                           AND component->>'role'='DANMAKU_AGGREGATE'
                           AND component->'payload'->>'noise_level'='HIGH'
                           AND topic->>'category' IN (
                               'PRODUCT_AVAILABILITY','BACKUP_AVAILABILITY'
                           )
                     )
                    UNION ALL
                    SELECT 3, to_jsonb('RHYTHM_PAUSE_REQUIRED'::text)
                     WHERE EXISTS (
                        SELECT 1
                          FROM phase14_evidence_bundles evidence
                          CROSS JOIN LATERAL jsonb_array_elements(
                              evidence.payload->'snapshot'->'components'
                          ) AS component
                         WHERE evidence.live_session_id=NEW.live_session_id
                           AND evidence.evidence_bundle_id=NEW.evidence_bundle_id
                           AND component->>'role'='RHYTHM_SIGNAL'
                           AND component->'payload'->>'signal_kind'='PAUSE_REQUIRED'
                     )
                  ) AS signal;
                IF jsonb_array_length(derived_trigger_codes)<2
                   OR NEW.payload->'trigger_codes' IS DISTINCT FROM derived_trigger_codes THEN
                    RAISE EXCEPTION 'phase16 automatic escalation trigger codes are invalid';
                END IF;
            END IF;
        WHEN 'phase16_conflict_analyses' THEN
            IF NEW.payload->>'analysis_id' IS DISTINCT FROM NEW.analysis_id
               OR NEW.payload->>'incident_id' IS DISTINCT FROM NEW.incident_id
               OR NEW.payload->>'evidence_bundle_id' IS DISTINCT FROM NEW.evidence_bundle_id
               OR NEW.payload->>'evidence_bundle_digest' IS DISTINCT FROM NEW.evidence_bundle_digest
               OR NEW.payload->>'escalation_id' IS DISTINCT FROM NEW.escalation_id
               OR NEW.payload->>'analyst_profile_id' IS DISTINCT FROM NEW.analyst_profile_id
               OR NEW.payload->>'analyst_profile_version' IS DISTINCT FROM NEW.analyst_profile_version
               OR NEW.payload->>'analyst_profile_digest' IS DISTINCT FROM NEW.analyst_profile_digest THEN
                RAISE EXCEPTION 'phase16 analysis payload identity mismatch';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM phase16_escalations escalation
                 WHERE escalation.live_session_id=NEW.live_session_id
                   AND escalation.incident_id=NEW.incident_id
                   AND escalation.evidence_bundle_id=NEW.evidence_bundle_id
                   AND escalation.evidence_bundle_digest=NEW.evidence_bundle_digest
                   AND escalation.escalation_id=NEW.escalation_id
            ) THEN
                RAISE EXCEPTION 'phase16 analysis parent digest mismatch';
            END IF;
            SELECT COALESCE(jsonb_agg(component.value->'reference' ORDER BY component.ordinality),'[]'::jsonb)
              INTO expected_evidence_refs
              FROM phase14_evidence_bundles evidence
              CROSS JOIN LATERAL jsonb_array_elements(
                  evidence.payload->'snapshot'->'components'
              ) WITH ORDINALITY AS component(value, ordinality)
             WHERE evidence.live_session_id=NEW.live_session_id
               AND evidence.evidence_bundle_id=NEW.evidence_bundle_id;
            IF NEW.payload->'evidence_refs' IS DISTINCT FROM expected_evidence_refs THEN
                RAISE EXCEPTION 'phase16 analysis evidence refs do not match bundle';
            END IF;
        WHEN 'phase16_multi_agent_outcomes' THEN
            IF NEW.payload->>'outcome_id' IS DISTINCT FROM NEW.outcome_id
               OR NEW.payload->>'incident_id' IS DISTINCT FROM NEW.incident_id
               OR NEW.payload->>'evidence_bundle_id' IS DISTINCT FROM NEW.evidence_bundle_id
               OR NEW.payload->>'evidence_bundle_digest' IS DISTINCT FROM NEW.evidence_bundle_digest
               OR NEW.payload->>'escalation_id' IS DISTINCT FROM NEW.escalation_id
               OR NEW.payload->>'escalation_digest' IS DISTINCT FROM NEW.escalation_digest
               OR NEW.payload->>'analysis_id' IS DISTINCT FROM NEW.analysis_id
               OR NEW.payload->>'analysis_digest' IS DISTINCT FROM NEW.analysis_digest
               OR NEW.payload->>'proposal_id' IS DISTINCT FROM NEW.proposal_id
               OR NEW.payload->>'proposal_digest' IS DISTINCT FROM NEW.proposal_digest
               OR NEW.payload->>'status' IS DISTINCT FROM NEW.status THEN
                RAISE EXCEPTION 'phase16 outcome payload identity mismatch';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM phase16_escalations escalation
                 WHERE escalation.live_session_id=NEW.live_session_id
                   AND escalation.incident_id=NEW.incident_id
                   AND escalation.evidence_bundle_id=NEW.evidence_bundle_id
                   AND escalation.evidence_bundle_digest=NEW.evidence_bundle_digest
                   AND escalation.escalation_id=NEW.escalation_id
                   AND escalation.payload->>'escalation_digest'=NEW.escalation_digest
            ) THEN
                RAISE EXCEPTION 'phase16 outcome escalation digest mismatch';
            END IF;
            IF NEW.analysis_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM phase16_conflict_analyses analysis
                 WHERE analysis.live_session_id=NEW.live_session_id
                   AND analysis.incident_id=NEW.incident_id
                   AND analysis.evidence_bundle_id=NEW.evidence_bundle_id
                   AND analysis.escalation_id=NEW.escalation_id
                   AND analysis.analysis_id=NEW.analysis_id
                   AND analysis.payload->>'analysis_digest'=NEW.analysis_digest
            ) THEN
                RAISE EXCEPTION 'phase16 outcome analysis digest mismatch';
            END IF;
            -- Proposal 的可验证快照、摘要和全链路绑定由 Task 6 一起持久化；在它
            -- 出现前数据库与 Store 一律拒绝 READY，避免把外部自报 digest 变成事实。
            IF NEW.status='READY' THEN
                RAISE EXCEPTION 'phase16 READY outcome requires Task 6 proposal persistence';
            ELSIF NEW.proposal_id IS NOT NULL
               OR NEW.proposal_digest IS NOT NULL
               OR COALESCE(length(btrim(NEW.payload->>'failure_code')),0)=0
               OR NEW.payload->>'failure_code' NOT IN (
                    'ANALYST_MODEL_ERROR','ANALYST_INVALID_OUTPUT',
                    'ANALYST_BUDGET_EXCEEDED','PLANNER_MODEL_ERROR',
                    'PLANNER_INVALID_OUTPUT','PLANNER_BUDGET_EXCEEDED',
                    'VALIDATOR_REJECTED','COORDINATOR_TIMEOUT'
               )
               OR COALESCE(length(btrim(NEW.payload->>'fact_summary')),0)=0 THEN
                -- DEGRADED 只能携带封闭失败码和可展示摘要；否则重启读取会在
                -- Pydantic 反序列化失败，且坏行会永久污染 append-only 审计链。
                RAISE EXCEPTION 'phase16 DEGRADED outcome shape is invalid';
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
        WHEN 'escalation' THEN
            SELECT payload INTO actual_payload FROM phase16_escalations
             WHERE live_session_id=NEW.live_session_id AND escalation_id=NEW.fact_id;
        WHEN 'conflict_analysis' THEN
            SELECT payload INTO actual_payload FROM phase16_conflict_analyses
             WHERE live_session_id=NEW.live_session_id AND analysis_id=NEW.fact_id;
        WHEN 'multi_agent_outcome' THEN
            SELECT payload INTO actual_payload FROM phase16_multi_agent_outcomes
             WHERE live_session_id=NEW.live_session_id AND outcome_id=NEW.fact_id;
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

CREATE OR REPLACE FUNCTION phase16_advance_workspace_cas() RETURNS trigger AS $$
DECLARE
    current_version BIGINT;
    current_view TEXT;
BEGIN
    -- 触发器在事实插入所在的同一事务锁定根 Workspace；任何直写都必须携带
    -- 当前版本，成功后由数据库而非调用方原子推进一次，避免“事实+ledger”跳过 CAS。
    SELECT workspace.version,workspace.current_view
      INTO current_version,current_view
      FROM phase14_live_session_workspaces workspace
     WHERE workspace.live_session_id=NEW.live_session_id
       FOR UPDATE;
    IF current_version IS NULL
       OR current_view IS DISTINCT FROM 'LIVE'
       OR current_version IS DISTINCT FROM NEW.expected_workspace_version THEN
        RAISE EXCEPTION 'phase16 workspace version conflict';
    END IF;
    UPDATE phase14_live_session_workspaces
       SET version=version+1,updated_at=NOW()
     WHERE live_session_id=NEW.live_session_id;
    RETURN NEW;
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
DROP TRIGGER IF EXISTS trg_phase16_escalations_payload ON phase16_escalations;
CREATE TRIGGER trg_phase16_escalations_payload BEFORE INSERT ON phase16_escalations
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase16_analyses_payload ON phase16_conflict_analyses;
CREATE TRIGGER trg_phase16_analyses_payload BEFORE INSERT ON phase16_conflict_analyses
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase16_outcomes_payload ON phase16_multi_agent_outcomes;
CREATE TRIGGER trg_phase16_outcomes_payload BEFORE INSERT ON phase16_multi_agent_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase14_validate_fact_payload();
DROP TRIGGER IF EXISTS trg_phase16_escalations_workspace_cas ON phase16_escalations;
CREATE TRIGGER trg_phase16_escalations_workspace_cas BEFORE INSERT ON phase16_escalations
    FOR EACH ROW EXECUTE FUNCTION phase16_advance_workspace_cas();
DROP TRIGGER IF EXISTS trg_phase16_analyses_workspace_cas ON phase16_conflict_analyses;
CREATE TRIGGER trg_phase16_analyses_workspace_cas BEFORE INSERT ON phase16_conflict_analyses
    FOR EACH ROW EXECUTE FUNCTION phase16_advance_workspace_cas();
DROP TRIGGER IF EXISTS trg_phase16_outcomes_workspace_cas ON phase16_multi_agent_outcomes;
CREATE TRIGGER trg_phase16_outcomes_workspace_cas BEFORE INSERT ON phase16_multi_agent_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase16_advance_workspace_cas();
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
DROP TRIGGER IF EXISTS trg_phase16_escalations_require_ledger ON phase16_escalations;
CREATE CONSTRAINT TRIGGER trg_phase16_escalations_require_ledger
    AFTER INSERT ON phase16_escalations DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('escalation','escalation_id');
DROP TRIGGER IF EXISTS trg_phase16_analyses_require_ledger ON phase16_conflict_analyses;
CREATE CONSTRAINT TRIGGER trg_phase16_analyses_require_ledger
    AFTER INSERT ON phase16_conflict_analyses DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('conflict_analysis','analysis_id');
DROP TRIGGER IF EXISTS trg_phase16_outcomes_require_ledger ON phase16_multi_agent_outcomes;
CREATE CONSTRAINT TRIGGER trg_phase16_outcomes_require_ledger
    AFTER INSERT ON phase16_multi_agent_outcomes DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION phase14_require_fact_ledger('multi_agent_outcome','outcome_id');

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
DROP TRIGGER IF EXISTS trg_phase16_escalations_append_only ON phase16_escalations;
CREATE TRIGGER trg_phase16_escalations_append_only BEFORE UPDATE OR DELETE ON phase16_escalations
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase16_analyses_append_only ON phase16_conflict_analyses;
CREATE TRIGGER trg_phase16_analyses_append_only BEFORE UPDATE OR DELETE ON phase16_conflict_analyses
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase16_outcomes_append_only ON phase16_multi_agent_outcomes;
CREATE TRIGGER trg_phase16_outcomes_append_only BEFORE UPDATE OR DELETE ON phase16_multi_agent_outcomes
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
DROP TRIGGER IF EXISTS trg_phase14_idempotency_append_only ON phase14_workspace_idempotency;
CREATE TRIGGER trg_phase14_idempotency_append_only BEFORE UPDATE OR DELETE ON phase14_workspace_idempotency
    FOR EACH ROW EXECUTE FUNCTION phase14_reject_fact_mutation();
