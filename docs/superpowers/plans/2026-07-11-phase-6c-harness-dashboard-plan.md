# Phase 6C Harness Dashboard Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the LangGraph Harness Agent interrupt and human approval loop in the Web dashboard with PostgreSQL-backed session state.

**Architecture:** Keep LangGraph checkpoint persistence in official PostgresSaver, and store only Web-facing session snapshots in `live_agent_harness_sessions`. FastAPI calls `HarnessDashboardService`, while the frontend consumes REST and `agent_harness_update` WebSocket events.

**Tech Stack:** Python, FastAPI, LangGraph, langgraph-checkpoint-postgres, PostgreSQL, Vanilla JS, pytest.

---

### Task 1: Session Store

- [x] Write tests for saving pending sessions, loading by trace, idempotent final updates, and latest-room lookup.
- [x] Implement `HarnessSessionRecord`, `InMemoryHarnessSessionStore`, `PostgresHarnessSessionStore`, and schema initialization.
- [x] Verify with `pytest tests/unit/test_harness_session_store.py -v`.

### Task 2: Dashboard Service

- [x] Write tests for start, approve, reject, and mismatch fail-closed behavior.
- [x] Implement `HarnessDashboardService` around `build_on_live_harness_agent_graph()`.
- [x] Use PostgresSaver for integration and InMemorySaver for unit tests.
- [x] Verify PostgreSQL approve/reject flow with `pytest tests/integration/test_harness_dashboard_flow.py -v`.

### Task 3: FastAPI and WebSocket

- [x] Add `POST /api/agent/harness/start`.
- [x] Add `GET /api/agent/harness/status`.
- [x] Add `POST /api/agent/harness/approval`.
- [x] Add `/ws` route and `agent_harness_update` broadcast support.
- [x] Verify with `pytest tests/unit/test_api_server_harness.py -v`.

### Task 4: Frontend and Demo

- [x] Add Harness Agent panel with node path, pending approval card, approve/reject buttons, observations, final suggestion, and audit status.
- [x] Keep dashboard dark, compact, and scan-friendly.
- [x] Add CLI demo for approve and reject paths.

### Task 5: Trace Records

- [x] Add design document and implementation plan.
- [x] Update project guidance with completion status, test feedback, limitations, and next direction.
