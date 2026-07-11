# Phase 7A：生产级 Agent Replay / Evaluation 设计

## 目标

Phase 7A 把播中 Harness Agent 从“可运行、可审批、可审计”推进到“可回放、可评分、可回归验证”。它面向生产排障和版本回归，而不是演示用的简单日志。

## 核心链路

```text
Harness Session / Checkpoint / Audit / DecisionTrace
-> AgentReplayService
-> AgentRuleEvaluator
-> Evaluation Store / Worker
-> API / Evaluation Page / Human Review
```

## 设计取舍

- 回放优先使用 LangGraph checkpoint，checkpoint 不可用时从 `live_agent_harness_sessions`、`tool_call_audit` 和 DecisionTrace 降级重建。
- 规则评分不依赖 LLM，覆盖状态完整性、工具选择、安全策略、人审合规、执行效率、业务效果。
- LLM Judge 只影响“建议语义质量”低权重维度，不能修改安全违规结论。
- PostgreSQL 同时作为评估事实源和轻量任务队列，Worker 使用 `FOR UPDATE SKIP LOCKED` 抢占任务。
- 人工复核以 overlay 方式新增记录，不覆盖原始机器评分。

## 数据表

- `live_agent_evaluation_runs`：评估任务、状态、回放快照、总分、覆盖率、违规项、租约和错误。
- `live_agent_evaluation_dimension_scores`：维度分、权重、证据和评估器版本。
- `live_agent_evaluation_reviews`：人工复核结论、操作员和原因。
- `live_agent_evaluation_datasets`、`live_agent_evaluation_cases`、`live_agent_evaluation_batches`：Golden Dataset 和批量回归预留表。

## API

- `POST /api/agent/evaluations`
- `GET /api/agent/evaluations/{evaluation_id}`
- `GET /api/agent/replays/{trace_id}`
- `POST /api/agent/evaluations/{evaluation_id}/reviews`
- `GET /evaluation`

## 当前边界

- 已实现规则评估、PostgreSQL 任务队列、Worker、LLM Judge 结构化接口、API 和运维页面。
- Golden Dataset 批量回归表已预留，批量 API 和 case 管理留到后续迭代。
- checkpoint 精确回放接口已预留，当前生产默认优先使用 Harness session 降级回放。
