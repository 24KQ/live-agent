# Phase 2F Human Approval Interrupt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and superpowers:executing-plans or subagent-driven-development. New or modified Python code must keep UTF-8 and detailed Chinese comments.

**Goal:** Upgrade Phase 2E checkpoint recovery into a real LangGraph human-in-the-loop approval flow for high-risk pre-live setup.

**Architecture:** LangGraph owns orchestration and interrupt/resume semantics. Existing pre-live service keeps ownership of ToolRegistry, SecurityHook, setup execution and PostgreSQL audit.

---

### Task 1: TDD Human Approval Models

**Files:**
- Create: `tests/unit/test_human_approval.py`
- Create: `src/core/human_approval.py`

- [x] Write failing tests for approved/rejected decisions.
- [x] Reject blank trace, room, tool, operator and reason fields.
- [x] Reject unknown decisions.
- [x] Validate resume payload matches pending request trace, room and tool.

### Task 2: TDD LangGraph Interrupt Flow

**Files:**
- Create: `tests/unit/test_pre_live_graph_interrupt.py`
- Modify: `src/core/pre_live_graph.py`

- [x] Verify graph triggers `interrupt()` before setup when human approval is enabled.
- [x] Verify interrupt payload contains trace, room, tool, risk and action.
- [x] Verify `Command(resume=approved)` executes setup.
- [x] Verify `Command(resume=rejected)` does not execute setup.
- [x] Preserve existing `confirmed_setup` compatibility path.

### Task 3: TDD PostgreSQL Recovery Audit

**Files:**
- Create: `tests/integration/test_pre_live_graph_interrupt_flow.py`
- Create: `tests/unit/test_pre_live_business_flow_idempotency.py`
- Modify: `src/core/pre_live_business_flow.py`
- Modify: `src/state/models.py`

- [x] Initialize audit schema, Phase 2 seed data and PostgresSaver schema.
- [x] Verify approve flow persists pending approval, resumes, writes approved/resumed and setup success.
- [x] Verify reject flow persists pending approval, resumes, writes rejection and no setup success.
- [x] Verify upstream catalog, plan and card audit records are not duplicated after resume.
- [x] Verify approved setup success audit is idempotent when the same trace is replayed.

### Task 4: CLI And Records

**Files:**
- Create: `scripts/run_phase2f_pre_live_interrupt_demo.py`
- Create: `docs/superpowers/specs/2026-07-07-phase-2f-human-approval-interrupt-design.md`
- Create: `docs/superpowers/plans/2026-07-07-phase-2f-human-approval-interrupt-plan.md`
- Modify: `README.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Add CLI demo for approve and reject scenarios.
- [x] Add design and implementation plan documents.
- [x] Run all required acceptance commands.
- [x] Update phase execution log with final results, limitations and next iteration advice.

### Acceptance Commands

```powershell
pytest tests/unit/test_human_approval.py -v
pytest tests/unit/test_pre_live_graph_interrupt.py -v
pytest tests/integration/test_pre_live_graph_interrupt_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/run_phase2f_pre_live_interrupt_demo.py
git status --short --ignored
git add -n .
```
