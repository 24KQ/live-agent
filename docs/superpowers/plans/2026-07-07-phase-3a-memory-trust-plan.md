# Phase 3A Memory And Trust Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and superpowers:executing-plans. New or modified Python/SQL files must keep UTF-8 and detailed Chinese comments.

**Goal:** Build the first memory and trust loop for LiveAgent so pre-live planning can use anchor preferences and historical performance, then update trust_score from feedback.

---

### Task 1: TDD Memory Domain Models

**Files:**
- Create: `tests/unit/test_memory_models.py`
- Create: `src/memory/models.py`

- [x] Validate L1/L2/L3 memory layers.
- [x] Reject empty anchor, empty content, unknown layer and invalid confidence.
- [x] Validate trust_score range and default value.
- [x] Validate Decision Trace fields.

### Task 2: TDD Memory Store And Schema

**Files:**
- Create: `tests/unit/test_memory_store.py`
- Create: `tests/integration/test_phase3_memory_seed_data.py`
- Create: `docker/init_phase3_memory.sql`
- Create: `src/memory/memory_store.py`
- Create: `src/memory/demo_memory_seed.py`

- [x] Create memory, trust and decision trace tables.
- [x] Reserve `embedding vector(1536)` for future pgvector retrieval.
- [x] Seed L1/L2/L3 demo memories and default trust state.
- [x] Keep seed idempotent and free of private data.

### Task 3: TDD Trust And Tool Masking

**Files:**
- Create: `tests/unit/test_trust_manager.py`
- Create: `tests/unit/test_tool_mask_policy.py`
- Create: `src/memory/trust_manager.py`
- Create: `src/memory/tool_mask_policy.py`

- [x] Implement four deterministic trust_delta rules.
- [x] Clamp trust_score into `0.00-1.00`.
- [x] Mask tools by trust_score thresholds.

### Task 4: TDD Memory-Aware Planning And Decision Trace

**Files:**
- Create: `tests/unit/test_memory_aware_plan.py`
- Create: `tests/integration/test_memory_trust_flow.py`
- Create: `src/memory/memory_aware_plan.py`
- Create: `src/memory/decision_trace_store.py`

- [x] Reuse existing deterministic live plan generator.
- [x] Boost products by preferred category, tag and product ID from memory metadata.
- [x] Add memory source to plan reasons.
- [x] Record Decision Trace and updated trust_score.

### Task 5: CLI, README And Execution Log

**Files:**
- Create: `scripts/seed_phase3_memory_demo_data.py`
- Create: `scripts/run_phase3a_memory_trust_demo.py`
- Modify: `README.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Add seed and demo commands.
- [x] Record TDD red/green, tests, CLI result, limitations and next iteration advice.

### Task 6: Code Review Hardening

**Files:**
- Modify: `docker/init_phase3_memory.sql`
- Modify: `src/memory/memory_store.py`
- Modify: `src/memory/decision_trace_store.py`
- Modify: `src/memory/memory_aware_plan.py`
- Modify: Phase 3A tests

- [x] Enforce `room_id` and `anchor_id` consistency with schema and Store validation.
- [x] Reject moving a `memory_key` across anchors.
- [x] Make Decision Trace idempotent for identical payloads and reject different payload overwrites.
- [x] Avoid echoing raw memory content in plan explanations.
- [x] Strengthen tests for whitespace IDs, repeated seed, trace overwrite, room/anchor mismatch and trust boundary behavior.

### Acceptance Commands

```powershell
pytest tests/unit/test_memory_models.py -v
pytest tests/unit/test_memory_store.py -v
pytest tests/unit/test_trust_manager.py -v
pytest tests/unit/test_tool_mask_policy.py -v
pytest tests/unit/test_memory_aware_plan.py -v
pytest tests/integration/test_phase3_memory_seed_data.py -v
pytest tests/integration/test_memory_trust_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/seed_phase3_memory_demo_data.py
python scripts/run_phase3a_memory_trust_demo.py
git status --short --ignored
git add -n .
```
