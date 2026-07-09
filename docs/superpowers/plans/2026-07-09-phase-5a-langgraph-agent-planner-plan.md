# Phase 5A LangGraph Agent Planner 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement each task.

**Goal:** 把播前链路从线性 workflow 升级为 LangGraph Agent 编排，体现 LLM 决策 + conditional edges + tool calling + observe/replan。

**Architecture:** 新增 Agent 决策模型、LLM Planner、Tool Executor 和 Agent Graph 四个模块，通过 LangGraph StateGraph + add_conditional_edges 连接。保留原有 pre_live_graph.py 不破坏。

**Tech Stack:** LangGraph 1.2.8, Pydantic, DeepSeek API (deepseek-v4-flash), PostgreSQL checkpoint

---

## File Structure

| File | Operation | Responsibility |
|------|-----------|----------------|
| src/core/agent_decision.py | Create | Agent 决策 Pydantic 模型 |
| src/skills/agent_planner.py | Create | LLM Planner (DeepSeek 封装) |
| src/core/agent_tool_executor.py | Create | 白名单工具执行器 |
| src/core/pre_live_agent_graph.py | Create | LangGraph Agent 播前图 |
| scripts/run_phase5a_pre_live_agent_demo.py | Create | CLI 演示 |
| tests/unit/test_agent_decision.py | Create | 决策模型单元测试 |
| tests/unit/test_agent_planner.py | Create | Planner 单元测试 |
| tests/unit/test_agent_tool_executor.py | Create | 工具执行器单元测试 |
| tests/unit/test_pre_live_agent_graph.py | Create | Agent 图单元测试 |
| tests/integration/test_pre_live_agent_graph_flow.py | Create | 集成测试 |
| docs/project_guidance/phase_execution_log.md | Update | Phase 5A 留迹 |
| README.md | Update | Phase 5A 演示命令 |

---

## Task 1: Agent 决策模型

**Files:**
- Create: src/core/agent_decision.py
- Test: tests/unit/test_agent_decision.py

Steps: Write failing tests -> verify fail -> implement model -> verify pass -> commit

---

## Task 2: LLM Planner

**Files:**
- Create: src/skills/agent_planner.py
- Test: tests/unit/test_agent_planner.py

Steps: Write failing tests -> verify fail -> implement planner -> verify pass -> commit

---

## Task 3: Tool Executor

**Files:**
- Create: src/core/agent_tool_executor.py
- Test: tests/unit/test_agent_tool_executor.py

Steps: Write failing tests -> verify fail -> implement executor -> verify pass -> commit

---

## Task 4: Agent Graph

**Files:**
- Create: src/core/pre_live_agent_graph.py
- Test: tests/unit/test_pre_live_agent_graph.py

Steps: Write failing tests -> verify fail -> implement graph -> verify pass -> commit

---

## Task 5: CLI Demo + Integration Test + Documentation

**Files:**
- Create: scripts/run_phase5a_pre_live_agent_demo.py
- Create: tests/integration/test_pre_live_agent_graph_flow.py
- Update: docs/project_guidance/phase_execution_log.md
- Update: README.md

Steps: Write demo -> verify runs -> write integration test -> verify pass -> update docs -> commit
