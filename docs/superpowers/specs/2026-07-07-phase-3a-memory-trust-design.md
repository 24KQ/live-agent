# Phase 3A 记忆与信任层基础闭环设计

## 背景与目标

Phase 2F 已经完成播前 Graph 的 checkpoint、interrupt 和人工审批恢复。Phase 3A 进入“越用越懂主播”的基础能力：系统需要读取主播偏好和历史表现，影响下一次播前排品，并把建议、反馈、业务结果和 trust_score 变化记录成可回放证据。

本阶段不接 LLM、不接 Kafka consumer、不做 Web、不接真实平台 API。pgvector 只预留 `embedding vector(1536)` 字段，语义检索和 embedding 写入后置。

目标链路：

```text
初始化记忆样例数据
-> 读取主播偏好与历史表现
-> 生成带记忆影响的播前排品
-> 记录 Decision Trace
-> 模拟主播反馈与业务结果
-> 更新 trust_score
-> 下一次播前建议受记忆和信任分影响
```

## 设计边界

- 记忆层只使用脱敏样例数据，不记录真实用户、真实订单、真实平台 Token。
- `MemoryStore` 只负责 PostgreSQL 读写，不包含排品规则。
- `MemoryAwarePlanService` 复用 Phase 2A 的确定性排品，再叠加结构化记忆影响。
- `TrustManager` 只实现计划中明确的四条确定性规则，不用模型预测。
- `DecisionTraceStore` 记录建议、主播反馈、业务结果、lift、trust_delta 和最终 trust_score；同一 `trace_id` 只允许相同内容幂等复用，不允许覆盖为不同反馈。
- `ToolMaskPolicy` 只决定工具可见范围，真实执行仍必须经过 ToolRegistry 与 SecurityHook。
- 排品理由只输出结构化命中摘要，例如层级、来源、类目、标签和商品 ID，不直接回显完整记忆正文。
- 记忆和 Decision Trace 必须校验 `room_id` 与 `anchor_id` 归属一致，避免跨主播串号。

## 数据模型

- `live_agent_anchor_memories`：保存 L1/L2/L3 记忆、结构化 metadata、confidence、evidence_weight、source，并预留 `embedding vector(1536)`。
- `live_agent_anchor_trust_state`：保存主播维度 trust_score，范围固定为 `0.00-1.00`，默认 `0.70`。
- `live_agent_decision_trace`：保存建议、主播动作、业务结果、lift、trust_delta 和最终 trust_score，使用 `trace_id` 关联审计回放。

## 信任分规则

- 采纳且效果好：`+0.05`
- 采纳但效果差：`-0.10`
- 拒绝且事后 Agent 对：`+0.03`
- 拒绝且事后主播对：`-0.05`
- 更新后必须钳制到 `0.00-1.00`

## 验收标准

- 能写入并查询 L1/L2/L3 记忆。
- 非法 anchor、空 content、未知 layer、非法 trust_score 会被拒绝。
- 有主播偏好时，播前排品顺序和理由能体现记忆来源。
- Decision Trace 能记录建议、反馈、结果、lift、trust_delta 和最终 trust_score。
- CLI 能展示第一次播前、反馈、trust 更新、第二次播前建议的完整闭环。
