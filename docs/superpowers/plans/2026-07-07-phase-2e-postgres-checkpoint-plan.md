# Phase 2E PostgreSQL Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official PostgreSQL checkpoint persistence and recovery to the pre-live LangGraph flow.

**Architecture:** Keep business logic in the existing pre-live service. LangGraph remains the orchestration layer, while official PostgresSaver stores resumable checkpoints keyed by `trace_id` as `thread_id`.

**Tech Stack:** Python, LangGraph 1.2.8, langgraph-checkpoint-postgres 3.1.0, PostgreSQL, pytest.

---

### Task 1: Dependency And Settings

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`
- Modify: `src/config/settings.py`
- Test: `tests/unit/test_settings.py`

- [x] Write failing tests for checkpoint conninfo and strict msgpack defaults.
- [x] Pin `langgraph==1.2.8` and add `langgraph-checkpoint-postgres==3.1.0`.
- [x] Add `LANGGRAPH_STRICT_MSGPACK=true` to `.env.example`.
- [x] Add `Settings.postgres_checkpoint_conninfo` and `Settings.langgraph_strict_msgpack`.
- [x] Verify with `pytest tests/unit/test_settings.py -v`.

### Task 2: Serializable Graph State

**Files:**
- Modify: `src/core/pre_live_graph.py`
- Test: `tests/unit/test_pre_live_graph_serialization.py`

- [x] Write failing tests for product, plan and card snapshot round trips.
- [x] Replace Pydantic objects in `PreLiveGraphState` with JSON snapshot fields.
- [x] Add snapshot conversion helpers.
- [x] Update graph nodes to restore models internally and return snapshots externally.
- [x] Verify with `pytest tests/unit/test_pre_live_graph_serialization.py -v`.

### Task 3: Checkpoint Resume Semantics

**Files:**
- Modify: `src/core/pre_live_graph.py`
- Create: `src/core/langgraph_checkpoint.py`
- Test: `tests/unit/test_pre_live_graph_checkpoint.py`
- Test: `tests/integration/test_pre_live_graph_checkpoint_flow.py`

- [x] Write failing tests for `thread_id`, interruption and resume.
- [x] Add `create_pre_live_graph_config(trace_id)`.
- [x] Let `build_pre_live_graph` accept `checkpointer` and `interrupt_after`.
- [x] Wrap official `PostgresSaver.from_conn_string()` and `.setup()`.
- [x] Verify with unit and integration checkpoint tests.

### Task 4: CLI And Documentation

**Files:**
- Create: `scripts/run_phase2e_pre_live_checkpoint_demo.py`
- Create: `docs/superpowers/specs/2026-07-07-phase-2e-postgres-checkpoint-design.md`
- Create: `docs/superpowers/plans/2026-07-07-phase-2e-postgres-checkpoint-plan.md`
- Modify: `README.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Add CLI demo that interrupts after product cards and resumes with the same `thread_id`.
- [x] Document Phase 2E command in README.
- [x] Record design, implementation plan, test feedback, CLI output and next-stage suggestions.
- [x] Verify with `python scripts/run_phase2e_pre_live_checkpoint_demo.py`.

### Task 5: Final Verification

**Commands:**

```powershell
pytest tests/unit/test_pre_live_graph_serialization.py -v
pytest tests/unit/test_pre_live_graph_checkpoint.py -v
pytest tests/unit/test_settings.py -v
pytest tests/integration/test_pre_live_graph_checkpoint_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/run_phase2e_pre_live_checkpoint_demo.py
git status --short --ignored
git add -n .
```

Expected final result: all tests pass, middleware check passes, CLI shows checkpoint interruption and recovery, and ignored files such as `.env` are not included in dry-run staging.
