# Phase 2C Danmaku Aggregation Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the minimum on-live danmaku aggregation and reference-reply loop with deterministic rules, audit, and CLI demo.

**Architecture:** Local danmaku events enter an on-live application service. The service validates lifecycle, aggregates events into 5-second question groups, generates deterministic reference replies, and writes all steps to PostgreSQL audit.

**Tech Stack:** Python 3.12, Pydantic, psycopg 3, PostgreSQL, pytest.

---

### Task 1: TDD Test Coverage

**Files:**
- Create: `tests/unit/test_danmaku_events.py`
- Create: `tests/unit/test_danmaku_aggregator.py`
- Create: `tests/unit/test_danmaku_reply_generator.py`
- Modify: `tests/unit/test_tool_registry.py`
- Create: `tests/integration/test_danmaku_flow.py`

- [x] Write failing tests for event validation, 5-second aggregation, deterministic replies, on-live tool registry, and full danmaku flow.
- [x] Verify the new tests fail before production modules exist.

### Task 2: Danmaku Skills And Registry

**Files:**
- Create: `src/skills/danmaku_events.py`
- Create: `src/skills/danmaku_aggregator.py`
- Create: `src/skills/danmaku_reply_generator.py`
- Modify: `src/config/tool_registry.py`
- Modify: `src/state/models.py`

- [x] Add `DanmakuEvent`.
- [x] Add deterministic 5-second question aggregation.
- [x] Add deterministic reference reply generation.
- [x] Add danmaku audit action types and register on-live tools.

### Task 3: Danmaku Flow And CLI

**Files:**
- Create: `src/core/danmaku_flow.py`
- Create: `scripts/run_phase2c_danmaku_demo.py`
- Modify: `README.md`

- [x] Implement `DanmakuFlowService.handle_danmaku_batch`.
- [x] Enforce `ON_LIVE` lifecycle boundary.
- [x] Write audit records for aggregation and reply generation.
- [x] Add CLI demo for a local danmaku batch.

### Task 4: Records And Verification

**Files:**
- Create: `docs/superpowers/specs/2026-07-07-phase-2c-danmaku-aggregation-design.md`
- Create: `docs/superpowers/plans/2026-07-07-phase-2c-danmaku-aggregation-plan.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Record design, implementation plan, test commands, known limits, and next iteration suggestions.
- [x] Run all required acceptance commands.
- [x] Update `phase_execution_log.md` with final test results and CLI feedback.
