# Phase 16 Controlled Multi-Agent Escalation Implementation Plan

**Goal:** Add bounded, auditable dual-Agent escalation for high-conflict LIVE
sold-out incidents without weakening deterministic protection or human authority.

**Architecture:** A startup-frozen Coordinator records escalation facts, runs
the Analyst then Planner once each, validates the complete result, and hands
only `READY` Proposals to the existing OperatorDecision/Compiler path.

**Tech Stack:** Python 3.12, Pydantic frozen models, PostgreSQL, FastAPI,
WebSocket, BoundedSpecialistRunner, ScriptedModel, pytest.

## Execution Rules

- Every Task follows `RED -> GREEN -> REFACTOR -> REVIEW -> VERIFY -> DOCS ->
  COMMIT -> PUSH` and produces one independent ASCII commit.
- New or changed code contains detailed UTF-8 Chinese comments. Existing user
  dirty files from the primary workspace are never touched or staged.
- The main agent owns state machines, migrations, integration, verification,
  commits, and pushes. Read-only sub-agent analysis/review is allowed only with
  continuous-state monitoring and no running sub-agent at commit time.
- No real model is called before Task 10 preflight. Security, budget, required
  infrastructure, or test hard-gate failure stops the current Task.

## Tasks

### Task 1: Persist the Approved Baseline

Create this Design/Plan, D-134 through D-140, Phase 16 recovery entry, and
state/worklog/master/roadmap facts. Preserve Phase 15 history and defer the
broad Markdown audit to Phase 17. Verify decision numbering, document
consistency, UTF-8/LF, and `git diff --check`; commit
`docs: freeze phase 16 controlled multi-agent design`.

### Task 2: Stabilize Root Test Collection

RED: capture root pytest's three Phase 14 unit/integration module collisions.
GREEN: rename only integration `test_phase14_memory_confirmation.py`,
`test_phase14_operator_decision.py`, and `test_phase14_sold_out_flow.py` to
unique `*_postgres.py` names, preserving behavior. Commit
`test: stabilize phase 14 postgres collection`.

### Task 3: Add Runtime and Domain Contracts

RED tests define `CONFLICT_ANALYSIS`, `LIVE_DECISION_PLANNING`, exact Profiles,
`EscalationRecord`, `ConflictAnalysis`, `MultiAgentOutcome`, and Proposal
lineage. GREEN implements strict frozen/digest/closed-code contracts in
`src/specialist_runtime/models.py`, `src/decision_support/models.py`, and
`src/decision_support/proposal.py`. Commit
`feat: add controlled multi-agent contracts`.

### Task 4: Persist Escalation Facts

RED requires in-memory/PostgreSQL append, foreign-key parent validation,
idempotent replay, Workspace CAS, unique Bundle/Profile escalation, fencing,
and restart recovery. GREEN extends `docker/init_phase14_decision_support.sql`
and `src/decision_support/store.py` with append-only escalation/analysis/outcome
facts. Commit `feat: persist multi-agent escalation facts`.

### Task 5: Select and Analyze High Conflict

RED fixes proposal eligibility, the three frozen signals, three-select-two
logic, lease-bound manual escalation, no model for normal/adversarial Bundles,
and retry identity. GREEN adds `src/decision_support/multi_agent.py` with the
selector and Analyst Coordinator segment; failures persist one `DEGRADED`
Outcome. Commit `feat: analyze high-conflict live evidence`.

### Task 6: Plan and Validate Whole Proposals

RED makes Planner input the exact Bundle plus validated Analysis and rejects
wrong identity, incomplete references, unavailable backups, missing risk codes,
or an invalid option. GREEN implements the Planner segment, exact
`2s/2800/0.07` limit, `5s/4000/0.10` ceiling, whole-Proposal rejection, and
unchanged OperatorDecision authority. Commit
`feat: validate multi-agent live proposals`.

### Task 7: Governed API and WebSocket Projection

RED API tests require authentication, current lease, Bundle identity, Workspace
CAS, idempotency, and fail-closed errors. GREEN extends
`src/gateway/decision_support_service.py` and `src/gateway/api_server.py` with
the narrow escalation endpoint and stable Workspace/WebSocket facts. Commit
`feat: expose governed multi-agent escalation`.

### Task 8: Local Operations Workspace

RED dashboard tests require route/trigger/analysis/outcome labels, a safe
`DEGRADED` view, and disabled execution without a READY Proposal. GREEN extends
`front/index.html` to consume only server facts and send only the narrow
escalation request. Commit `feat: show controlled multi-agent live replay`.

### Task 9: Frozen Pairwise Evaluation

RED requires byte-stable separate 48-case data, exact splits, labels,
smoke eligibility, and immutable identity hashes. GREEN adds Phase 16 assets
under `evaluation/phase16_controlled_multi_agent` and runs actual Coordinator
paths with ScriptedModel, reporting pairing, route correctness, budgets,
failure semantics, and replay identity. Commit
`test: add controlled multi-agent evaluation`.

### Task 10: Formal Smoke Preflight

RED blocks model sends without exact identity, official price, usage, Prompt,
Schema, Manifest, code hash, endpoint, and available reservation. GREEN adds a
separate `PHASE16_MULTI_AGENT_SMOKE` ledger capped at ten cases / 1.00 CNY;
default tests run only scripted rehearsal. Commit
`feat: gate controlled multi-agent smoke`.

### Task 11: Demo and Acceptance

RED fixes the deterministic `live-session-p001-sold-out-v2` replay contract:
protection first, dual-Agent route, operator decision, compiled but never
auto-submitted recovery, and stable restart audit. GREEN generates Phase 16
Acceptance, runs root pytest, unit/integration, PostgreSQL, compileall,
migration dry-run, strict encoding, and diff verification. Commit
`docs: accept phase 16 controlled multi-agent`, set
`AWAITING_PHASE_17_GATE`, and stop.
