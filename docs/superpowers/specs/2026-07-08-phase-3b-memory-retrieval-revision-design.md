# Phase 3B 记忆检索、衰减与冲突修正设计

## 背景与目标

Phase 3A 已经完成记忆与信任层基础闭环：系统可以读取主播偏好和历史表现，影响播前排品，并把主播反馈写入 Decision Trace。Phase 3B 在此基础上增强“记忆质量控制”：同一主播的记忆需要按相关性、证据权重、新鲜度和层级进行排序；旧记忆需要随时间衰减；当新反馈与旧偏好冲突时，系统不能直接删除旧记忆，而应降低旧记忆影响并留下可复盘理由。

本阶段仍不接 LLM、不接 embedding、不接 Kafka consumer、不做 Web、不接真实平台 API。所有规则保持确定性，保证测试稳定。

目标链路：

```text
增强记忆检索 -> 记忆衰减 -> 冲突修正 -> Decision Trace 反哺记忆 -> 下一轮排品变化
```

## 设计边界

- 记忆检索只使用 PostgreSQL 中的脱敏样例数据，不记录真实用户身份、真实订单、平台 Token 或本机私密路径。
- `MemoryStore` 继续承担数据库读写，新增的检索和修正策略放在独立模块，避免把排品逻辑塞进 Store。
- `MemoryRetriever` 根据主播、直播间、层级、`confidence`、`evidence_weight`、新鲜度和记忆状态生成结构化命中结果。
- `MemoryDecayPolicy` 计算 `effective_weight`，L1 衰减最慢，L2/L3 按时间和证据权重衰减；被 suppressed 的旧记忆影响力显著降低。
- `BeliefRevisionService` 处理新偏好与旧偏好的冲突：写入新记忆，保留旧记忆，把旧记忆标记为 `suppressed` 并记录冲突原因。
- `BeliefRevisionService` 在真实 `MemoryStore` 上必须使用单事务完成旧记忆 suppress 和新记忆写入，避免新证据写入失败时旧记忆已被压低。
- `DecisionTraceMemoryFeedbackService` 把主播反馈和业务结果归纳成结构化 L2 记忆，供后续播前检索使用；写入前必须按当前货盘过滤类目、标签和商品 ID，缺少货盘时必须 fail-closed。
- `memory_key` 不能跨主播或跨直播间移动；反馈记忆 key 必须包含主播、直播间和 trace 维度，降低重复运行或跨房间碰撞风险。
- `MemoryAwarePlanService` 可以继续兼容普通记忆列表，也可以消费增强检索结果；排品理由只展示结构化命中摘要，不直接回显完整记忆正文。

## 数据模型调整

`live_agent_anchor_memories` 在 Phase 3A 基础上增加记忆状态字段：

- `status`：`active` 或 `suppressed`，默认 `active`。
- `suppressed_reason`：旧记忆被冲突修正压低影响力时的脱敏原因。
- `updated_at`：记录记忆被写入或修正的时间。

这些字段不改变既有 Phase 3A 数据含义。旧数据通过 schema 初始化脚本自动补列，默认仍为 active。

## 检索与衰减规则

- 基础强度：`confidence * evidence_weight`。
- 新鲜度：按 `created_at` 到参考时间的天数衰减。
- L1 半衰期最长，L2 次之，L3 最短，避免长期抽象总结压过近期明确反馈。
- `suppressed` 记忆只保留少量影响力，用于审计和解释“历史上曾经有过该偏好，但后来被修正”。
- 排序使用 `relevance_score = effective_weight + layer_bonus + room_bonus`，再按时间和 ID 稳定排序。

## 冲突修正规则

- 当新记忆的 `metadata.conflict_group` 与旧记忆相同，但偏好类目、标签或商品 ID 不一致时，判定为同组偏好冲突；单数字段和复数字段按同一偏好维度归一化比较。
- 新记忆写入为 active。
- 旧 active 记忆不删除，改为 suppressed，并写入 `suppressed_reason`。
- 修正结果返回被压低的旧记忆 key、写入的新记忆 key 和冲突字段，便于 CLI 与阶段日志复盘。
- suppress 旧记忆和写入新记忆必须在同一 PostgreSQL 事务内完成。

## Decision Trace 反哺记忆

- 主播采纳且效果好：生成正向 L2 记忆，提升相关偏好。
- 主播采纳但效果差：生成谨慎 L2 记忆，提醒下次降低相同偏好影响。
- 主播拒绝且事后主播对：生成修正 L2 记忆，优先尊重主播判断。
- 主播拒绝且事后 Agent 对：生成观察 L2 记忆，但不直接覆盖主播显式偏好。
- 反哺记忆只允许保存货盘中存在的类目、标签和商品 ID；不保存完整话术、主播原话、订单信息、平台字段或本机路径。

## 验收标准

- 能按主播、直播间和层级检索记忆，并返回结构化命中解释。
- 新记忆比旧记忆有效权重更高；L1 衰减慢于 L2/L3。
- suppressed 旧记忆仍可查询但排序和排品影响降低。
- 冲突修正不会删除旧记忆，并能记录修正原因。
- Decision Trace 能生成脱敏 L2 记忆。
- CLI 能展示旧偏好、新冲突反馈、旧记忆被 suppress、下一轮排品变化。
