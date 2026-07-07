# Phase 2A 播前业务能力设计

## 背景与目标

Phase 1 已完成播前地基层：生命周期、工具注册、安全 Hook、Reducer 和 PostgreSQL 审计。Phase 2A 在此基础上补齐主播最容易理解的播前业务能力：从数据库读取样例货盘，生成排品草案和商品手卡，并通过确认后的模拟建播动作写入审计。

本阶段仍然不接 LLM、不接真实淘宝 API、不做 Web 前端、不消费 Kafka 播中事件。目标是先把确定性业务闭环跑稳，为后续播中事件、记忆信任和前端副屏打地基。

## 架构设计

- 数据层：PostgreSQL 新增脱敏样例主播、直播场次、商品和直播间货盘关联表；seed 脚本可重复执行，使用稳定 ID 和 upsert 保证幂等。
- 货盘层：`ProductCatalogRepository` 只读查询指定直播间商品，并把数据库行转换为强校验的 `CatalogProduct`。
- 生成层：排品和手卡均使用确定性规则，避免 LLM 不稳定输出影响当前阶段测试。
- 编排层：`PreLiveBusinessFlowService` 串联查询、排品、手卡、建播确认和审计写入。
- 审计层：继续使用 Phase 1 的 `tool_call_audit` 表和 `trace_id`，完整记录 query、plan、card、setup 四类工具调用。

## 工具边界

Phase 2A 扩展工具注册表：

- `generate_live_plan`：播前生成排品方案，`soft-gate`。
- `generate_product_card`：播前生成商品手卡，`soft-gate`。
- `setup_live_session`：模拟建播写入动作，`hard-gate`，需要幂等键。

`query_products` 从 Phase 1 的内存模拟能力升级为数据库货盘查询，但仍只允许在 `PRE_LIVE` 使用。

## 数据与合规

所有样例数据均为脱敏虚构数据，不包含真实商品、真实用户、真实订单、真实账号密码或 token。公开仓库只提交 schema、seed 逻辑和 `.env.example`，真实 `.env` 继续保留在本机并被 Git 忽略。

## 验收标准

- seed 后能查询到 10 个样例商品、1 个主播和 1 个直播场次。
- 排品草案包含引流款、利润款、氛围款等可解释商品位。
- 商品手卡包含卖点、开场话术、价格提示和合规风险提示。
- 模拟建播必须经过 hard-gate 确认，确认后写入审计。
- 完整播前业务流可通过 CLI 演示，并能按 `trace_id` 回放审计链路。
