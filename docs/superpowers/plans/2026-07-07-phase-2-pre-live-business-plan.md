# Phase 2A Pre-Live Business Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 2A deterministic pre-live business loop on PostgreSQL demo data.

**Architecture:** PostgreSQL stores anonymized demo catalog data; Python services query products, generate deterministic plans/cards, run safety gates, and write audit records. CLI scripts seed and demonstrate the flow.

**Tech Stack:** Python 3.12, Pydantic, psycopg 3, PostgreSQL, pytest.

---

### Task 1: TDD Test Coverage

**Files:**
- Create: `tests/unit/test_product_catalog.py`
- Create: `tests/unit/test_live_plan_generator.py`
- Create: `tests/unit/test_product_card_generator.py`
- Modify: `tests/unit/test_tool_registry.py`
- Create: `tests/integration/test_phase2_seed_data.py`
- Create: `tests/integration/test_pre_live_business_flow.py`

- [x] Write failing tests for catalog validation, filtering, plan generation, card generation, registry metadata, seed data, and full business flow.
- [x] Run the new test set and confirm it fails because Phase 2A modules do not exist.

### Task 2: Data Schema And Seed

**Files:**
- Create: `docker/init_phase2_pre_live.sql`
- Create: `src/skills/demo_data_seed.py`
- Create: `scripts/seed_phase2_demo_data.py`

- [x] Add anonymized PostgreSQL schema for anchors, live rooms, products, and room-product mapping.
- [x] Add idempotent seed logic for 10 products, 1 anchor, and 1 live room.
- [x] Use transaction-level advisory lock to avoid concurrent DDL races.

### Task 3: Pre-Live Business Services

**Files:**
- Create: `src/skills/product_catalog.py`
- Create: `src/skills/live_plan_generator.py`
- Create: `src/skills/product_card_generator.py`
- Modify: `src/config/tool_registry.py`
- Modify: `src/state/models.py`

- [x] Add `CatalogProduct` and PostgreSQL catalog repository.
- [x] Add deterministic plan generation for traffic, profit, atmosphere, and regular products.
- [x] Add deterministic product card generation with compliance risk tips.
- [x] Register `generate_live_plan`, `generate_product_card`, and `setup_live_session`.

### Task 4: Flow And Demo

**Files:**
- Create: `src/core/pre_live_business_flow.py`
- Modify: `src/audit/tool_call_audit.py`
- Create: `scripts/run_phase2_pre_live_demo.py`

- [x] Add `PreLiveBusinessFlowService` to orchestrate query, plan, cards, setup gate, and audit.
- [x] Add audit list query by `trace_id`.
- [x] Add CLI demo showing catalog count, plan, cards, setup gate, and audit ID.

### Task 5: Documentation And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/project_guidance/phase_execution_log.md`

- [x] Add README commands for Phase 2A seed and demo.
- [x] Record Phase 2A objectives, deliverables, commands, test feedback, bug fix, and limitations.
- [x] Run the full requested acceptance command set before final commit.
