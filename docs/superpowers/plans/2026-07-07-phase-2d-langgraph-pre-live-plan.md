# Phase 2D LangGraph Pre-Live Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight LangGraph orchestration skeleton around the existing pre-live business flow.

**Architecture:** LangGraph is used only as workflow orchestration. Existing pre-live services keep ownership of catalog queries, plan/card generation, hard-gate behavior, and PostgreSQL audit.

**Tech Stack:** Python 3.12, LangGraph 1.2.x, Pydantic, psycopg 3, PostgreSQL, pytest.

---

### Task 1: TDD Test Coverage

**Files:**
- Create: `tests/unit/test_pre_live_graph.py`
- Create: `tests/integration/test_pre_live_graph_flow.py`

- [x] Write failing tests for LangGraph import, initial graph state, pending hard-gate, approved setup, and real PostgreSQL audit flow.
- [x] Verify the new tests fail before `src/core/pre_live_graph.py` exists.

### Task 2: LangGraph Dependency And Service Boundary

**Files:**
- Modify: `requirements.txt`
- Modify: `src/core/pre_live_business_flow.py`

- [x] Add `langgraph>=1.2,<2.0`.
- [x] Expose public pre-live service methods for graph nodes without changing existing behavior.

### Task 3: Pre-Live Graph

**Files:**
- Create: `src/core/pre_live_graph.py`

- [x] Add `PreLiveGraphState`.
- [x] Add `create_initial_pre_live_graph_state`.
- [x] Add `build_pre_live_graph`.
- [x] Implement fixed workflow nodes for catalog query, plan generation, card generation, compliance summary, and setup hard-gate.

### Task 4: CLI And Records

**Files:**
- Create: `scripts/run_phase2d_pre_live_graph_demo.py`
- Create: `docs/superpowers/specs/2026-07-07-phase-2d-langgraph-pre-live-design.md`
- Create: `docs/superpowers/plans/2026-07-07-phase-2d-langgraph-pre-live-plan.md`
- Modify: `README.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Add CLI demo.
- [x] Record design and implementation plan.
- [x] Run all required acceptance commands.
- [x] Update `phase_execution_log.md` with final results.
