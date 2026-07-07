# Phase 3B Memory Retrieval And Revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and superpowers:executing-plans. New or modified Python/SQL files must keep UTF-8 and detailed Chinese comments.

**Goal:** Enhance LiveAgent memory quality controls with deterministic retrieval ranking, decay, conflict revision and Decision Trace feedback-to-memory.

---

### Task 1: TDD Memory Retrieval Models And Ranking

**Files:**
- Create: `tests/unit/test_memory_retrieval.py`
- Create: `tests/unit/test_decision_memory_feedback.py`
- Create: `src/memory/memory_retrieval.py`
- Modify: `src/memory/memory_aware_plan.py`

- [x] Define structured memory hit results with `effective_weight`, `relevance_score` and explanation.
- [x] Rank by confidence, evidence, freshness, layer and room match.
- [x] Keep raw memory content out of plan reasons.
- [x] Allow existing Phase 3A memory-aware plan tests to keep passing.
- [x] Filter feedback-memory values against the current product catalog before storing long-term memory.
- [x] Fail closed when feedback memory is built without a non-empty product catalog.

### Task 2: TDD Memory Decay

**Files:**
- Create: `tests/unit/test_memory_decay.py`
- Create: `src/memory/memory_decay.py`

- [x] Calculate deterministic effective weight.
- [x] Make newer memory stronger than older memory.
- [x] Make L1 decay slower than L2/L3.
- [x] Reduce suppressed memory impact without hiding it from audit.

### Task 3: TDD Belief Revision And Schema

**Files:**
- Create: `tests/unit/test_belief_revision.py`
- Modify: `docker/init_phase3_memory.sql`
- Modify: `src/memory/models.py`
- Modify: `src/memory/memory_store.py`
- Create: `src/memory/belief_revision.py`

- [x] Add memory status fields while preserving Phase 3A compatibility.
- [x] Suppress old conflicting memory instead of deleting it.
- [x] Record conflict reason in a structured and脱敏 form.
- [x] Reject revision attempts that would cross anchor or room boundaries.
- [x] Keep suppression and new-memory write in one PostgreSQL transaction.
- [x] Reject moving the same `memory_key` across rooms for the same anchor.
- [x] Treat singular and plural preference metadata fields as aliases when detecting conflicts.

### Task 4: TDD Decision Trace Feedback To Memory

**Files:**
- Create: `tests/integration/test_memory_revision_flow.py`
- Create: `src/memory/decision_memory_feedback.py`
- Modify: `src/memory/demo_memory_seed.py`

- [x] Convert accepted/rejected Decision Trace outcomes into structured L2 memories.
- [x] Use stable memory keys for demo/idempotent seed behavior.
- [x] Demonstrate next pre-live plan changes after revision.

### Task 5: CLI, README And Execution Log

**Files:**
- Create: `scripts/seed_phase3b_memory_demo_data.py`
- Create: `scripts/run_phase3b_memory_revision_demo.py`
- Modify: `README.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Add Phase 3B seed and demo commands.
- [x] Record TDD red/green, targeted tests, full tests, middleware check, CLI result, issues/fixes and next iteration direction.
- [x] Keep `.env`, caches, `docs/worklog/` and `docs/study/` out of Git.

### Acceptance Commands

```powershell
pytest tests/unit/test_memory_retrieval.py -v
pytest tests/unit/test_memory_decay.py -v
pytest tests/unit/test_belief_revision.py -v
pytest tests/integration/test_memory_revision_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/seed_phase3_memory_demo_data.py
python scripts/seed_phase3b_memory_demo_data.py
python scripts/run_phase3b_memory_revision_demo.py
git status --short --ignored
git add -n .
```
