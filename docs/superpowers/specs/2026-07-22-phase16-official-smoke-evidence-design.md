# Phase 16 Official Real-Model Smoke Evidence Design

## Status

`FORMAL_RUN_EXECUTED_FAILED_AWAITING_CLOSEOUT` as of 2026-07-22. This document
defines a bounded Phase 16 evidence-closure slice. The sole formal run was sent
once and stopped after the first Analyst validation failure; it does not start
Phase 17, change the production route, or authorize an automatic operating
action.

## Goal

Phase 16 already proves its deterministic protection, controlled dual-Agent
orchestration, human authorization boundary, restart recovery, and scripted
evaluation. This slice adds the missing external evidence for the final
controlled dual-Agent path: an auditable DeepSeek smoke run that is strict
enough to be honestly reported as `PASS` only when all ten frozen cases and all
twenty model calls succeed.

## Fixed Scope And Non-Goals

- The sole formal run identity is `phase16-official-smoke-v1`.
- The sole dataset identity is a new immutable
  `phase16-official-smoke-evidence-v1` Manifest. It binds exactly ten existing
  smoke-eligible high-conflict cases, their six-role Evidence identities, the
  two Smoke Profiles, prices, an eight-file execution-identity subset, and a
  runner summary. It is historical evidence and is never rewritten after send.
- `phase16-official-smoke-historical-closure-audit-v1.json` separately binds
  the original Manifest digest, execution commit, and the complete first-party
  dependency closure by exact Git blob SHA-256 values. It corrects the old
  naming without changing the executed v1 Manifest or pretending current
  remediation source is what sent the request.
- The production default remains `DETERMINISTIC_ONLY`. A smoke `PASS` is
  integration evidence only; it never enables the LIVE route or submits an
  operating command.
- This slice does not call a real Taobao API, introduce free A2A, dynamic
  handoff, shared scratchpad, plugin loading, hot reload, or a new production
  agent.
- The legacy `PHASE16_MULTI_AGENT_SMOKE` tables and their `0.100000` per-case
  semantics are historical Task 10 evidence. They are not modified or reused
  as the formal ledger.

## Budget And Result Semantics

The formal Phase 16 budget is `1.000000 CNY`. The previous direct-mode
experiment consumed `0.073220 CNY`; it is imported once as immutable
`HISTORICAL_DIRECT_MODE` spending. It is not evidence of the formal run.

Each of the ten fixed slots reserves `0.092000 CNY`: `0.040000` for Analyst and
`0.052000` for Planner. Historical spending plus all ten reservations is
`0.993220 CNY`, leaving no path to an eleventh case or a budget overrun.

The result state machine is deliberately asymmetric:

- A preflight block before the first network send is `BLOCKED + INCONCLUSIVE`.
- Once any call has been sent, a missing receipt, missing usage, validation
  failure, malformed structured response, wrong route, timeout, transport
  error, or budget inconsistency is `FAILED`.
- A sent failure stops the run immediately. There is no retry, no text repair,
  and no substitution of scripted output.
- Formal `PASS` requires exactly `10/10` cases, `20/20` calls, non-empty
  provider IDs and finish reasons, complete usage, successful AgentAction /
  Schema / EvidenceRef validation, expected `MULTI_AGENT_READY` routes, and
  settled total cost at or below `1.000000 CNY`.

## Isolated Smoke Runtime

The smoke runtime uses two profile identities that are never registered in the
production LIVE coordinator:

- `phase16_smoke_evidence_analyst@1.0.0`
- `phase16_smoke_evidence_planner@1.0.0`

Both have zero Skills, temperature `0`, one call, a `30s` deadline, total token
cap `4000`, and `max_output_tokens=2800`. Existing LIVE Profile builders return
their historical fixed identities and do not expose public token or deadline
overrides. The smoke-only profile registry, budget adapter, evidence projection,
and coordinator must be separate from production Workspace, Store, and LIVE
routing.

The new runner must reuse `BoundedSpecialistRunner` for its normal AgentAction,
JSON Schema, EvidenceRef, deadline, and model-contract validation. It must not
copy validation logic or directly call an `AgentModelPort`. A narrow pure result
validator may be extracted from the production coordinator, but it must produce
only smoke validation facts rather than production Analysis, Proposal, or
Outcome domain facts.

## Receipt And Ledger Model

The formal PostgreSQL ledger is append-only and versioned separately from the
legacy smoke ledger. It stores a single run, fixed slots, immutable historical
spend, one reservation per slot, dispatch attempts, provider receipts,
validation facts, and one terminal outcome per slot.

Each write is protected by a run-row lock plus unique constraints. The only
permitted operations are run initialization / historic import, slot claim,
attempt creation before dispatch, receipt append, validation append, case close,
and recovery of open attempts. Recovery turns an unclosed sent attempt into a
stable unknown failure and never re-sends it. Planner dispatch is permitted only
after every Analyst validation fact for the same slot is `PASS`.

Once any case reaches `BLOCKED` or `FAILED`, both the Python ledger API and
PostgreSQL triggers reject later claims or dispatch attempts for the formal run.
This run-level terminal rule is intentionally stronger than a duplicate-stage
constraint: it prevents a caller from using an unclaimed slot to evade the
strict first-send zero-retry experiment.

The database may record only non-sensitive audit data: run/case/stage/Profile
digests, internal request ID, provider response ID, finish reason, model ID,
response digest, usage, latency, cost, and validation result. It must never
store API keys, prompts, model body text, chain-of-thought, or operating
recommendation text.

## Provider Contract

`ModelSuccess` gains optional `provider_response_id` and `finish_reason` fields.
The DeepSeek OpenAI-compatible adapter parses both from the provider response.
The general runtime stays backward compatible when a non-smoke port omits them;
the formal smoke validation makes either omission a sent-call failure.

The official evidence binds `deepseek-v4-flash` at `api.deepseek.com` to the
user-supplied official cache-miss price of `1.0 CNY` per million input tokens
and `2.0 CNY` per million output tokens. Price evidence is digested into the
Manifest and rechecked during preflight.

## Command Entry And Reporting

`scripts/run_phase16_real_smoke.py` is the sole formal command. It defaults to
dry-run and performs no network I/O without `--execute`. It invokes the formal
runner, not the previous direct adapter path. The previous direct mode remains
at the same path only as a hard-failing compatibility notice; it cannot send
requests.

The report renderer reads immutable PostgreSQL facts and exposes only receipt
digests and aggregate statistics. It compares formal outcomes to the scripted
baseline without treating that baseline as external proof. Acceptance changes
from `INCONCLUSIVE` only after the strict formal criteria pass; otherwise it
records the exact `BLOCKED`, `INCONCLUSIVE`, or `FAILED` state.

## Executed Formal Run

The sole `--execute` invocation passed all local gates and sent the Analyst
request for `phase16-high-conflict-paired-development-001`. PostgreSQL recorded
one complete receipt with model `deepseek-v4-flash`, finish reason `stop`, usage
`2610 / 1848 / 4458`, latency `14138.545 ms`, and cost `0.006306 CNY`.
The following validation and case outcome are both
`FAILED / ANALYST_VALIDATION_FAILED`. Therefore the Planner and remaining nine
slots were not sent, no retry or text repair is allowed, and the formal external
evidence is `FAILED`. The sanitized evidence Addendum is
`phase-16-official-smoke-evidence.md`; the production default remains
`DETERMINISTIC_ONLY`.

## Verification

Every implementation change follows `RED -> GREEN -> REFACTOR -> REVIEW ->
VERIFY -> DOCS -> COMMIT -> PUSH`. Unit tests exercise contracts without
network access. PostgreSQL tests use an isolated test database and cover CAS,
idempotent historic import, duplicate rejection, crash recovery, and sensitive
field exclusion. Only after all local gates pass can one `--execute` run occur.
The final merge requires full unit/integration, PostgreSQL recovery, migration
dry-run, compile, sensitive-payload scan, document encoding scan, and
`git diff --check` evidence.

### Task 5 Closeout Verification

The final fresh evidence is unit `1596 passed, 1 warning`, integration
`214 passed, 7 deselected, 5 warnings`, the full Phase 16 escalation PostgreSQL
file `31 passed`, and formal ledger/runner PostgreSQL `29 passed`. Both the
idempotent migration application and its dry-run recognized all 19 steps without
failure. The final review also rechecked run-level terminal locking, the historical
Git-blob audit, authenticated read-only reporting, sensitive-payload exclusion, and
the fixed two-second Coordinator safety test.

Two additional independent read-only review attempts failed before reading files
because the local review proxy returned `502` and `503`. They produced no findings
and are not represented as approvals. The main model performed the same scoped
review; no unresolved Critical or Important finding remained. This does not alter
the immutable formal `FAILED` result or permit another execution.
