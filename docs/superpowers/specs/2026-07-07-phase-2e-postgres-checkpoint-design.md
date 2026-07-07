# Phase 2E PostgreSQL Checkpoint 与可恢复 Graph 设计

## 目标

Phase 2E 把 Phase 2D 的播前 LangGraph 从“一次性执行”升级为“可中断、可持久化、可恢复”的工程骨架。目标链路为：

```text
初始化样例数据 -> 运行播前 Graph -> 在生成商品手卡后中断
-> PostgreSQL 保存 checkpoint -> 重新创建 graph -> 使用同一 thread_id 恢复
-> 完成合规摘要与建播 hard-gate -> 写入审计
```

本阶段仍不接 LLM、不接 Kafka consumer、不做 Web 前端、不接真实平台 API。

## 关键设计

- 采用官方 `langgraph-checkpoint-postgres==3.1.0`，不自研 checkpoint store。
- 固定 `langgraph==1.2.8`，避免依赖自动漂移导致 Graph 或 checkpoint 行为变化。
- 使用 `trace_id` 作为 LangGraph `thread_id`，让 checkpoint、审计和 CLI 输出共用同一个追踪入口。
- `PreLiveGraphState` 只保存 JSON 可序列化快照，不直接保存 `CatalogProduct`、`LivePlanDraft`、`ProductCard` 等 Pydantic 对象。
- Graph 节点继续复用 `PreLiveBusinessFlowService`，不绕过 ToolRegistry、SecurityHook 或 PostgreSQL 审计。

## 状态与恢复边界

商品、排品和手卡在进入 state 前先转换为 snapshot；恢复执行时，节点再从 snapshot 恢复成领域模型调用既有服务。这样 PostgresSaver 只需要保存普通 dict/list/string/number/bool/null。

中断点选在 `generate_product_cards` 之后，因为这时已经完成查询货盘、生成排品和 3 张手卡，恢复后只需要继续执行合规摘要和建播 hard-gate。恢复时使用同一 `thread_id` 调用 `graph.invoke(None, config=...)`，避免重复执行前半段节点和重复写审计。

## 安全与合规

- `.env` 继续 ignored，不提交真实密码。
- PostgresSaver 使用完整 conninfo 连接数据库，但日志和文档只允许展示脱敏 DSN。
- 默认设置 `LANGGRAPH_STRICT_MSGPACK=true`，降低 checkpoint 反序列化风险。
- 审计仍记录工具名、动作、风险等级、门禁策略、请求摘要、结果摘要和 `trace_id`。

## 验收标准

- 单元测试证明 snapshot 可 JSON 序列化并可往返恢复。
- 单元测试证明内存 checkpointer 可以中断并恢复，且未确认 hard-gate 不会伪装成功。
- 集成测试证明官方 PostgresSaver 可以持久化 checkpoint，重新创建 graph 后恢复到 END。
- 恢复后最终审计链路为查询货盘、排品、3 张手卡、建播确认，共 6 条记录，前半段不重复。

