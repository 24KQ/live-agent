# Phase 7A：生产级 Agent Replay / Evaluation 实施计划

## Summary

本阶段补齐 Agent 生产评估能力：回放、规则评分、异步任务、人工复核、LLM Judge 和独立运维页面。

## Key Changes

- 新增 `src/core/agent_replay.py`：标准化回放模型和 `AgentReplayService`。
- 新增 `src/core/agent_evaluation.py`：规则评分器和维度分模型。
- 新增 `src/gateway/agent_evaluation_store.py`：内存 Store 与 PostgreSQL Store。
- 新增 `src/gateway/agent_evaluation_service.py`：API 服务和 Worker。
- 新增 `src/skills/agent_llm_judge.py`：结构化 LLM Judge。
- 新增 `docker/init_phase7a_agent_evaluations.sql`：评估相关表。
- 扩展 `src/gateway/api_server.py`：评估 REST API、WebSocket 消息和 `/evaluation` 页面。
- 新增 `front/evaluation.html`：独立运维评估页面。
- 新增 Worker 和 demo 脚本。
- 将真实 DeepSeek 集成测试标记为 `external`，默认测试不访问外部模型。

## Test Plan

- `pytest tests/unit/test_agent_replay_service.py tests/unit/test_agent_evaluator.py tests/unit/test_agent_evaluation_store.py tests/unit/test_agent_evaluation_worker.py tests/unit/test_agent_llm_judge.py tests/unit/test_api_server_evaluation.py tests/unit/test_websocket_manager.py -v`
- `pytest tests/integration/test_agent_evaluation_flow.py -v`
- `python scripts/run_phase7a_agent_evaluation_demo.py`
- `python scripts/check_doc_encoding.py`
- `git diff --check`

## 不做的事

- 不接真实淘宝/抖音平台 API。
- 不把 LLM Judge 作为安全判断依据。
- 不在本阶段完成 Golden Dataset 批量回归 UI。
- 不保存 API Key、完整 system prompt、原始弹幕或本机私密路径。
