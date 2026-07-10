# Phase 5H Harness Agent 审计与 DecisionTrace 闭环设计

## 背景

Phase 5G-B 已经把播中 Agent 改造成 LangGraph Harness Loop，但 `write_audit` 节点仍是占位。这样 Agent 虽然能推理、路由、调工具和观察结果，却缺少可回放证据，后续播后复盘无法回答“Agent 为什么给出这个建议”“工具策略是否被 block”“最终建议有没有进入 DecisionTrace”。

Phase 5H 的目标是补齐 Evaluation Interface：每轮 Harness Agent 的工具策略、工具结果、最终建议和异常状态都要进入结构化审计。

## 核心设计

### OnLiveHarnessAuditWriter

新增 `src/core/on_live_harness_audit.py`，提供 `OnLiveHarnessAuditWriter`：

- 接收 LangGraph 最终 state。
- 生成 ToolCallAuditStore 兼容的 `AuditEvent`。
- 生成 DecisionTraceStore 兼容的 `DecisionTraceRecord`。
- 支持无 store 的 dry-run 模式。
- 审计 payload 递归脱敏，不记录 API key、token、password、`.env` 路径、本机私密路径。

### Graph 接入

`build_on_live_harness_agent_graph()` 新增 `audit_writer` 可选参数。默认使用 dry-run writer，保证 CLI 和测试不依赖数据库。

`write_audit` 节点行为：

```text
state -> audit_writer.write(state)
  -> 成功：回填 audit_status / audit_ids / decision_trace_ids / audit_payload
  -> 失败：audit_status=error，保留原 agent_status 和 final_suggestion
```

### DecisionTrace 策略

本阶段只记录 Agent 建议证据：

- `anchor_action=REJECTED`
- `business_result=AGENT_RIGHT`
- `lift=0.00`
- `trust_delta=0.00`
- `final_trust_score=trust_score`

主播是否采纳、真实业务结果和 trust_delta 在播后复盘阶段再更新。

## 边界

- 不接真实平台 API。
- 不改 WebSocket 推送链路。
- 不引入新数据库表。
- 不强制 CLI 写数据库。
- 高风险工具仍只 pending，不自动执行；人审恢复留到 Phase 5I。

## 后续方向

1. Phase 5I：把 `pending_human` 接入 LangGraph `interrupt()`，实现人审恢复。
2. Phase 6C：把 Harness 节点路径、审计状态和最终建议推送到 Web 副屏。
3. 播后复盘：用 DecisionTrace 反馈更新真实 `anchor_action/business_result/trust_delta`。
