# Phase 16 Controlled Multi-Agent Escalation Design

**Status:** `IMPLEMENTATION_AUTHORIZED`

**Date:** 2026-07-18

## Goal

Phase 16 adds a controlled two-Agent path only for high-conflict `LIVE`
incidents. It does not change the mutually exclusive `PREPARE | LIVE | REVIEW`
Workspace state machine or the ownership of deterministic protection and human
business recovery.

```text
governed EvidenceBundle
-> deterministic escalation selector
-> EvidenceAnalystAgent
-> immutable ConflictAnalysis
-> DecisionPlannerAgent
-> deterministic Safety Validator
-> human OperatorDecision
-> existing Compiler and controlled execution
```

Normal LIVE events retain the single-Copilot path. Inbox verification, freeze,
CAS, reconciliation, idempotency, lease, fencing, and command authorization do
not wait for and cannot be controlled by a model.

## Escalation Policy

The automatic path requires a proposal-eligible `SOLD_OUT_COMPOSITE` Bundle and
any two of these immutable facts from that exact Bundle:

1. `MULTIPLE_VALID_BACKUPS`: at least two active, positive-inventory backups.
2. `AVAILABILITY_NOISE_HIGH`: `HIGH` danmaku noise with product or backup
   availability topics.
3. `RHYTHM_PAUSE_REQUIRED`: the authority rhythm signal is `PAUSE_REQUIRED`.

An authenticated operator holding the current Workspace lease may explicitly
request escalation, but only for a proposal-eligible Bundle. Client input is
restricted to Bundle ID, expected Workspace version, and idempotency key; the
server reconstructs scope, trigger codes, Profile identities, and authorization.
Stale, reconciling, blocked, side-effect-unknown, or non-LIVE evidence never
reaches either Agent.

## Bounded Agents and Facts

`EvidenceAnalystAgent` uses `CONFLICT_ANALYSIS`; it emits only closed finding,
constraint, and risk codes, display-safe explanation text, and Bundle-owned
`EvidenceRef` values. It cannot rank products, propose an action, call a Skill,
read a Store, or write a fact directly.

`DecisionPlannerAgent` uses `LIVE_DECISION_PLANNING`; it receives the validated
Analysis plus the original Bundle, then returns the existing one-to-three
`LiveDecisionProposal` options. It cannot call Skills, select a route, query a
Store, or execute a command.

Both Profiles are exact startup-frozen identities: temperature zero, one model
call, zero Skill calls, and fixed Prompt/Schema hashes. Analyst: `2s / 1200
tokens / 0.03 CNY`; Planner: `2s / 2800 tokens / 0.07 CNY`; Coordinator:
monotonic `5s / 4000 tokens / 0.10 CNY` end-to-end ceiling.

The append-only Decision Support Store gains `EscalationRecord`,
`ConflictAnalysis`, and `MultiAgentOutcome`. A multi-Agent Proposal carries
exact escalation/analysis/Bundle ID and digest lineage. Any failed stage writes
one `DEGRADED` Outcome with a deterministic fact summary. There is no
single-Copilot fallback, partial analysis display, Agent-to-Agent free chat,
shared scratchpad, or Agent-directed business write.

## Validation, Routing, and Evaluation

The deterministic Validator rejects the entire Proposal if any option has
invalid Schema, lineage, EvidenceRef, backup, risk code, freshness, version, or
permission. It never silently filters invalid options. A `READY` Proposal is
still only an input to the existing `OperatorDecision -> Compiler ->
ExecutionCommand` boundary.

The standard runtime default remains `DETERMINISTIC_ONLY`. A distinct local
Demo/Evaluation composition can enable the controlled path; no normal startup
setting promotes it. Future production activation requires a separate Gate.

Phase 16 adds a separate immutable 48-case Manifest: 12 normal single-Copilot,
24 paired high-conflict, and 12 adversarial/degraded cases, with `12/24/12`
development/validation/holdout splits and ten smoke-eligible cases. Scripted
evaluation is mandatory. Real `deepseek-v4-flash` smoke is optional, capped at
ten cases and 1.00 CNY after endpoint, official price, usage, Prompt, Schema,
Manifest, and code-hash preflight. Insufficient external evidence is
`INCONCLUSIVE` and never opens the default route.

The local `live-session-p001-sold-out-v2` Demo shows protection first, automatic
escalation, structured analysis/proposal, and operator approval/modification/
rejection. Phase 16 ends at the Phase 17 Gate; broad documentation auditing is
not part of this implementation phase.
