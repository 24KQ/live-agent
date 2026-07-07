# Phase 2B On-Live Event Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the minimum on-live sold-out event loop with deterministic state updates, backup recommendation, prompt generation, audit, and CLI demo.

**Architecture:** Local event models enter an on-live application service. The service validates lifecycle, calls Reducer for state updates, uses deterministic skill modules for backup recommendation and prompt generation, and writes all steps to PostgreSQL audit.

**Tech Stack:** Python 3.12, Pydantic, psycopg 3, PostgreSQL, pytest.

---

### Task 1: TDD Test Coverage

**Files:**
- Create: `tests/unit/test_on_live_events.py`
- Create: `tests/unit/test_backup_product_recommender.py`
- Create: `tests/unit/test_on_live_prompt.py`
- Modify: `tests/unit/test_tool_registry.py`
- Create: `tests/integration/test_on_live_flow.py`

- [x] Write failing tests for sold-out event validation, backup recommendation, prompt generation, on-live tool registry, and full on-live flow.
- [x] Verify the new tests fail before production modules exist.

### Task 2: On-Live Skills And Registry

**Files:**
- Create: `src/skills/on_live_events.py`
- Create: `src/skills/backup_product_recommender.py`
- Create: `src/skills/on_live_prompt.py`
- Modify: `src/config/tool_registry.py`
- Modify: `src/state/models.py`

- [x] Add `InventoryEvent` and `OnLiveEventType.SOLD_OUT`.
- [x] Add deterministic backup product recommendation.
- [x] Add deterministic sold-out prompt generation.
- [x] Add on-live audit action types and register on-live tools.

### Task 3: On-Live Flow And CLI

**Files:**
- Create: `src/core/on_live_flow.py`
- Create: `scripts/run_phase2b_on_live_demo.py`
- Modify: `README.md`

- [x] Implement `OnLiveFlowService.handle_sold_out_event`.
- [x] Enforce `ON_LIVE` lifecycle boundary.
- [x] Write audit records for sold-out handling, backup recommendation, and prompt generation.
- [x] Add CLI demo for a local sold-out event.

### Task 4: Records And Verification

**Files:**
- Create: `docs/superpowers/specs/2026-07-07-phase-2b-on-live-events-design.md`
- Create: `docs/superpowers/plans/2026-07-07-phase-2b-on-live-events-plan.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Record design, implementation plan, test commands, known limits, and next iteration suggestions.
- [x] Run all required acceptance commands.
- [x] Update `phase_execution_log.md` with final test results and CLI feedback.
