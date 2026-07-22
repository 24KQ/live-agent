# Phase 16 Official Real-Model Smoke Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development`
> or `executing-plans` task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Close Phase 16's external real-model evidence with one auditable,
strict `10/10` DeepSeek smoke run while preserving the production default route.

**Architecture:** Build a versioned smoke-only Profile/Manifest/ledger/runtime
beside the existing LIVE runtime. It reuses `BoundedSpecialistRunner` validation
but never injects Smoke Profiles into the production coordinator, Workspace, or
operating-command path. Immutable PostgreSQL facts are the sole source for the
formal report and Acceptance update.

**Tech Stack:** Python 3.12, Pydantic, PostgreSQL, DeepSeek OpenAI-compatible
API adapter, pytest, existing Phase 16 evaluation assets.

## Global Constraints

- Formal run: `phase16-official-smoke-v1`; exactly ten fixed case slots and no
  retry after a sent call.
- Historic direct-mode spend: `0.073220 CNY`, source `HISTORICAL_DIRECT_MODE`,
  counted against the `1.000000 CNY` Phase 16 cap but never success evidence.
- Per case reservation: Analyst `0.040000 CNY` + Planner `0.052000 CNY` =
  `0.092000 CNY`; maximum exposure is `0.993220 CNY`.
- Formal Profiles: zero Skills, temperature `0`, one call, `30s`, token cap
  `4000`, output cap `2800`; they are not production LIVE Profiles.
- Missing provider response ID, finish reason, or usage after a send is
  `FAILED`; a blocked preflight before any send is `BLOCKED + INCONCLUSIVE`.
- Formal `PASS` requires 10/10 cases, 20/20 calls, all Runner and route checks,
  receipts, usage, and total cost `<= 1.000000 CNY`.
- Production remains `DETERMINISTIC_ONLY`; no real Taobao API, free A2A,
  dynamic handoff, shared scratchpad, plugins, or hot reload.
- All new or modified code has detailed UTF-8 Chinese comments. No API key,
  prompt, model body, chain-of-thought, or operating recommendation is stored.

---

### Task 0: Persist Design And Implementation Boundary

**Files:**
- Create: this plan and `docs/superpowers/specs/2026-07-22-phase16-official-smoke-evidence-design.md`
- Modify: decisions, status, roadmap, master plan, recovery prompt, and three
  worklogs.

- [x] Record D-168 through D-171 for historic spend, strict zero-retry results,
  isolated Smoke Profiles, immutable receipts, and the default route boundary.
- [x] Record the two closed read-only architecture audits and the clean
  baseline: unit `1555 passed`; integration `185 passed, 7 deselected`.
- [x] Run target-document encoding checks and `git diff --check`.
- [x] Commit only documentation as `docs: define phase16 official smoke evidence` (`a603159`).
- [x] Push the documentation commit before changing code, running migrations, or
  calling the real model.

### Task 1: Freeze Profiles, Receipts, And Offline Preflight

**Files:**
- Modify: Profile builders, model port, DeepSeek adapter, smoke Manifest helper,
  and smoke preflight.
- Test: isolated unit tests for profile identity, response receipt enforcement,
  official price, Manifest, and environment identity.

- [x] Write RED tests that prove LIVE builders reject public token/deadline
  overrides, Smoke Profiles are excluded from production registries, formal
  receipts require both provider ID and finish reason, and a mismatched price or
  Manifest blocks before network send.
- [x] Add `max_output_tokens: int | None` to `SpecialistProfile` with legacy
  behavior unchanged when omitted; have the shared runner cap request output by
  both remaining budget and this field.
- [x] Restore fixed zero-argument LIVE Profile builders. Add a smoke-only
  registry with `phase16_smoke_evidence_analyst@1.0.0` and
  `phase16_smoke_evidence_planner@1.0.0` using the fixed global values.
- [x] Extend `ModelSuccess` and parse OpenAI-compatible provider response ID and
  finish reason in the DeepSeek adapter. Do not relax general runtime behavior;
  formal smoke enforces non-empty values.
- [x] Create `phase16-official-smoke-evidence-v1` with fixed ten case
  identities, original data Manifest digest, Profile digests, price digest,
  source closure, and runner digest.
- [x] Run targeted unit tests, the source/Manifest validation, encoding checks,
  and `git diff --check`; commit the isolated contract change.

### Task 2: Add Formal PostgreSQL Append-Only Ledger

**Files:**
- Create: versioned DDL and formal-ledger module.
- Modify: migration registry and smoke interfaces.
- Test: dedicated unit and PostgreSQL integration tests.

- [x] Write PostgreSQL RED tests for one-time historic import, ten immutable
  slots, eleventh-slot rejection, `.073220 + 10 * .092000 = .993220` exposure,
  concurrent CAS single winner, duplicate receipt rejection, sensitive-field
  exclusion, restart recovery, and no resend of open attempts.
- [x] Add separate versioned tables for run, historical spend, fixed case slots,
  claims, attempts, provider receipts, validation facts, and terminal outcomes.
  Do not mutate the legacy `0.100000` smoke ledger.
- [x] Implement only narrowly named append operations: ensure/import, claim,
  begin dispatch, append receipt, append validation, close case, and recover
  open attempts. Use run-row locks and unique keys for fail-closed CAS.
- [x] Enforce that Planner starts only after the same case's Analyst validation
  is fully `PASS`; recovery appends a stable unknown failure rather than retrying.
- [x] Run migration dry-run and dedicated PostgreSQL recovery/concurrency tests;
  review and commit the ledger implementation.

### Task 3: Connect Formal Runner And Sole CLI Entry

**Files:**
- Modify: `Phase16SmokeRunner`, a smoke-only coordinator/validator, command
  script, and their tests.

- [x] Write RED tests that prove the runner calls `BoundedSpecialistRunner`,
  Analyst failure prevents Planner dispatch, labels/splits/expected routes stay
  out of model input, and invalid AgentAction/Schema/EvidenceRef cannot become
  a validation `PASS`.
- [x] Introduce a smoke-only budget port and evidence projection. Extract only
  pure result-validation functions from the production coordinator; retain
  separate smoke facts rather than production Analysis/Proposal/Outcome writes.
- [x] Make the existing script default to dry-run and require `--execute` for
  networking. Replace the old direct adapter path with a hard failure notice so
  it cannot bypass the formal ledger or runner.
- [x] Build each case from its frozen six-role Evidence and synthetic Workspace;
  permit Planner only after successful Analyst validation and require
  `MULTI_AGENT_READY`.
- [x] Run targeted unit/PostgreSQL tests, review, and commit the formal entry.

### Task 4: Execute One Formal Smoke And Render Evidence

**Files:**
- Modify: report renderer, Phase 16 Acceptance, and formal smoke evidence
  artifact only after the single authorized execution.

- [ ] Run unit, integration, PostgreSQL, migration, compile, sensitive-payload,
  documentation encoding, and diff gates before `--execute`.
- [ ] Run the sole explicit `--execute` invocation once after preflight passes.
  Stop immediately at the first sent-call failure and do not rerun it.
- [ ] Render a sanitized report from PostgreSQL receipts. It records provider
  receipt digests, model IDs, usage, latency, cost, status, and script-baseline
  comparison without Prompt, model body, or recommendation text.
- [ ] Update Acceptance to `PASS` only on strict `10/10` / `20/20`; otherwise
  retain the precise `BLOCKED + INCONCLUSIVE` or `FAILED` conclusion.
- [ ] Verify formal cost stays within `1.000000 CNY`; review and commit evidence.

### Task 5: Final Review, PR, And Merge Commit

**Files:**
- Modify: final decisions, Acceptance, status, roadmap, master plan, recovery
  prompt, and worklogs.

- [ ] Re-run full unit/integration, PostgreSQL recovery, compileall, migration
  dry-run, sensitive-payload scan, document encoding scan, and `git diff --check`.
- [ ] Conduct independent specification and code-quality/security reviews; fix
  all Critical and Important findings with RED/GREEN evidence.
- [ ] Commit and push all remaining changes with scoped messages.
- [ ] Create a PR from `codex/phase16-official-smoke-evidence`; merge with a
  merge commit only after all required Gates are green.
- [ ] Verify `origin/main` contains the merge, retain
  `AWAITING_PHASE_17_GATE`, and do not begin Phase 17.
