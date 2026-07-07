# LiveAgent 阶段执行记录

> 用途：记录各阶段任务、验收命令、测试反馈、问题修复和下一阶段建议，便于后续迭代优化。本文档只记录脱敏结论，不记录真实密码、Token 或本机私密配置。

## Phase 0: 项目脚手架与本地中间件验证

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`d969289 feat: add phase 0 project scaffold and infra check`
- 阶段状态：通过

### 结果

- Python 项目结构已建立。
- `.env.example` 保留公开模板，真实 `.env` 保留在本机并被 Git 忽略。
- PostgreSQL、pgvector、Redis、Kafka 均已在本机验证通过。
- Phase 0 全量测试通过。

### 交付物

- Python 包结构：`src/config`、`src/core`、`src/state`、`src/gateway`、`src/memory`、`src/skills`、`src/audit`。
- 配置中心：`src/config/settings.py`。
- 本地中间件检查脚本：`scripts/check_infra.py`。
- PostgreSQL 扩展初始化脚本：`docker/init_postgres.sql`。
- 依赖文件：`requirements.txt`。
- 配置模板：`.env.example`。

### 验收命令

```powershell
pytest -v
python scripts/check_infra.py
```

### 反馈

- worktree 环境需要单独复制本机 `.env`，否则中间件检查会使用公开默认密码并失败。
- PostgreSQL 首次使用 pgvector 前必须执行 `docker/init_postgres.sql`。

### 遗留限制

- Phase 0 只验证基础设施，不实现业务状态、工具门禁、审计表或 Agent 逻辑。
- `.env` 只保留在开发者本机，公开仓库只提交 `.env.example`。

## Phase 1: 播前地基层

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`786eab4 feat: add phase 1 pre-live foundation flow`
- 阶段状态：通过

### 目标

用播前场景验证最小可控闭环：

```text
查询货盘 -> 生成建议 -> 改价 hard-gate -> 人工确认 -> Reducer 更新状态 -> PostgreSQL 审计记录
```

### 计划任务

- 状态模型：生命周期、商品、直播间状态、Action、DecisionTrace。
- 生命周期：合法流转与非法跳转拒绝。
- 工具注册表：播前工具白名单、生命周期、风险等级、Schema、门禁策略。
- 安全 Hook：auto、soft-gate、hard-gate、block。
- Reducer：SET_PRICE、MARK_SOLD_OUT、SWITCH_PRODUCT。
- 审计：工具调用和状态变更写入 PostgreSQL。
- 演示：CLI 跑通播前改价审批闭环。

### 交付物

- 状态模型：`src/state/models.py`。
- 生命周期状态机：`src/core/lifecycle.py`。
- 工具注册表：`src/config/tool_registry.py`。
- 安全 Hook：`src/core/security_hooks.py`。
- 确定性 Reducer：`src/state/reducer.py`。
- 审计写入：`src/audit/tool_call_audit.py`。
- 播前闭环服务：`src/core/pre_live_flow.py`。
- 审计表初始化：`docker/init_phase1_audit.sql`。
- CLI 演示：`scripts/run_phase1_pre_live_demo.py`。
- 测试覆盖：状态模型、生命周期、工具注册、安全 Hook、Reducer、审计写入、播前闭环。

### 验收命令

```powershell
pytest tests/unit/test_state_models.py -v
pytest tests/unit/test_lifecycle.py -v
pytest tests/unit/test_tool_registry.py -v
pytest tests/unit/test_security_hooks.py -v
pytest tests/unit/test_reducer.py -v
pytest tests/integration/test_tool_call_audit.py -v
pytest tests/integration/test_pre_live_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/run_phase1_pre_live_demo.py
```

### 执行反馈

- 单元测试覆盖状态模型、生命周期、工具注册、安全 Hook 和 Reducer。
- 集成测试覆盖 PostgreSQL 审计写入和播前改价审批闭环。
- 全量测试结果：`34 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- CLI 演示结果：未确认改价进入 hard-gate 且价格不变；确认后价格从 `99.00` 更新为 `89.90`，并生成审计 `audit_id`。
- 修复记录：并行执行集成测试时，审计表初始化 SQL 可能并发创建同名内部类型；已通过 PostgreSQL advisory lock 串行化 DDL 初始化。

### 遗留限制

- 不接入 LLM，改价建议和演示流程均为确定性模拟逻辑。
- 不持久化商品业务表，商品状态仍以内存模型为主。
- 不实现完整排品、商品手卡、一键建播或主播偏好记忆。
- 不接入 Kafka 播中弹幕、库存事件或抢占恢复。
- 不接入真实淘宝生产 API。

### 最终结论

Phase 1 播前地基层验收通过。系统已经具备播前最小可控闭环，可作为 Phase 2 播前业务能力的地基。

### Phase 2 输入条件

- 可复用生命周期状态机，继续约束播前、播中、播后工具边界。
- 可复用工具注册表，新增排品、手卡、建播等播前工具时必须声明生命周期、风险等级和门禁策略。
- 可复用安全 Hook，改价、发券、建播等高风险动作继续走 hard-gate。
- 可复用 Reducer，后续商品状态持久化前仍可先用确定性内存状态验证业务规则。
- 可复用审计表和 `trace_id`，Phase 2 的播前业务工具应继续写入审计。
- 可复用 CLI 演示方式，先验证业务闭环，再考虑 Web 副屏。

### 下一阶段建议

- Phase 2 再引入样例商品数据持久化、真实货盘查询、排品草案和商品手卡生成。

## Phase 2A: 播前业务能力

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 2 pre-live business flow`
- 阶段状态：通过

### 目标

基于 PostgreSQL 脱敏样例数据跑通播前业务闭环：

```text
初始化样例数据 -> 查询真实货盘 -> 生成排品草案 -> 生成商品手卡 -> 模拟建播确认 -> 写入审计 -> CLI 演示
```

### 计划任务

- 新增 Phase 2A 设计文档和实施计划，继续保留阶段留迹。
- 新增 PostgreSQL 播前样例表：主播、直播场次、商品、直播间货盘关联。
- 新增可重复执行的 seed 脚本，写入 10 个脱敏样例商品、1 个主播、1 个直播场次。
- 新增数据库货盘查询服务，替代 Phase 1 的纯内存模拟查询。
- 新增确定性排品生成逻辑，输出引流款、利润款、氛围款和常规款。
- 新增确定性商品手卡生成逻辑，输出卖点、开场话术、价格提示和合规风险提示。
- 扩展工具注册表，注册 `generate_live_plan`、`generate_product_card`、`setup_live_session`。
- 新增播前业务流服务和 CLI 演示，串联查询、排品、手卡、建播确认和审计。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-2-pre-live-business-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-2-pre-live-business-plan.md`。
- Phase 2A 表初始化：`docker/init_phase2_pre_live.sql`。
- 样例数据 seed：`src/skills/demo_data_seed.py`、`scripts/seed_phase2_demo_data.py`。
- 货盘查询：`src/skills/product_catalog.py`。
- 排品生成：`src/skills/live_plan_generator.py`。
- 手卡生成：`src/skills/product_card_generator.py`。
- 播前业务流：`src/core/pre_live_business_flow.py`。
- CLI 演示：`scripts/run_phase2_pre_live_demo.py`。
- 审计扩展：`src/audit/tool_call_audit.py` 支持按 `trace_id` 读取完整链路。

### 验收命令

```powershell
pytest tests/unit/test_product_catalog.py -v
pytest tests/unit/test_live_plan_generator.py -v
pytest tests/unit/test_product_card_generator.py -v
pytest tests/unit/test_tool_registry.py -v
pytest tests/integration/test_phase2_seed_data.py -v
pytest tests/integration/test_pre_live_business_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/run_phase2_pre_live_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：新增测试初次运行失败，原因是 `product_catalog`、`live_plan_generator`、`product_card_generator`、`demo_data_seed`、`pre_live_business_flow` 等 Phase 2A 模块尚未实现。
- 单元测试覆盖货盘模型校验、可用商品筛选、确定性排品、确定性手卡和工具注册表扩展。
- 集成测试覆盖 PostgreSQL schema 初始化、seed 数据写入、数据库货盘查询、完整播前业务流和审计链路。
- 修复记录：并行执行 Phase 2A 集成测试时，schema 初始化曾出现 PostgreSQL 内部类型唯一冲突；根因是 DDL 事务提交前释放 advisory lock，已改为 `pg_advisory_xact_lock`，让锁随事务提交自动释放。
- 修复记录：直接执行 `python scripts/seed_phase2_demo_data.py` 和 `python scripts/run_phase2_pre_live_demo.py` 时曾找不到 `src` 包；已在 CLI 入口显式加入仓库根目录到 `sys.path`，并让 Phase 2A 演示显式初始化审计表。
- 优化记录：排品算法初版可能让前三个商品位连续出现同类引流款；已新增 TDD 测试并调整为先覆盖引流款、利润款、氛围款，再填充剩余商品。
- 指定单元测试结果：`test_product_catalog`、`test_live_plan_generator`、`test_product_card_generator`、`test_tool_registry` 全部通过。
- 指定集成测试结果：`test_phase2_seed_data`、`test_pre_live_business_flow` 全部通过。
- 全量测试结果：`44 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- seed 脚本结果：写入 1 个主播、1 个直播场次、10 个脱敏样例商品。
- CLI 演示结果：查询到 10 个商品；排品前 3 位覆盖引流款、利润款、氛围款；生成 3 张商品手卡；模拟建播通过 hard-gate 并写入审计 `audit_id`。

### 最终结论

Phase 2A 播前业务能力验收通过。系统已经从 Phase 1 的内存模拟货盘推进到 PostgreSQL 样例货盘，并能稳定演示查询货盘、生成排品、生成手卡、确认建播和审计留痕。

### 遗留限制

- 不接入 LLM，排品和手卡均为确定性规则生成。
- 不接入真实淘宝生产 API，不处理真实用户隐私数据。
- 不实现 Web 前端，仍使用 CLI 演示闭环。
- 不接入 Kafka 播中弹幕、库存事件或抢占恢复。
- 不实现长期记忆、trust_score 或播后复盘。

### 播前增强能力 Backlog

> 用途：记录 Phase 2A 之后仍需补强的播前能力，避免后续误以为“播前业务已全量完成”。以下条目只作为迭代指导，不代表 Phase 2A 验收范围。

| 建议阶段 | 能力项 | 目标 | 验收方向 |
| :--- | :--- | :--- | :--- |
| Phase 2B / Phase 3 | 复杂排品策略 | 在当前“引流款、利润款、氛围款、常规款”规则上，加入活动目标、利润率、库存压力、直播节奏和商品冷启动权重。 | 给定不同活动目标时，排品顺序应发生可解释变化，并保留 `trace_id` 审计。 |
| Phase 3 | 主播偏好与历史表现记忆 | 记录主播偏好、历史采纳/拒绝、商品表现和 trust_score，让后续排品与建议能随场次迭代。 | 下一场直播启动时能加载上一场确认过的偏好，并影响建议强度或工具可见性。 |
| Phase 3 / Phase 4 | LLM 手卡话术生成 | 在确定性手卡基础上接入 LLM，生成更自然的讲解话术，但必须保留 Schema 校验、禁用词检查和审计。 | LLM 输出不合 Schema 时自动失败或降级为模板手卡，高风险内容不得进入最终手卡。 |
| Phase 4 | 完整一键建播配置 | 将当前模拟建播扩展为更完整的直播间配置草案，例如标题、商品顺序、价格口径、优惠提醒和开场脚本。 | 建播配置必须经过 hard-gate 或明确确认，不允许自动写入真实平台。 |
| Phase 5 | Web 副屏界面 | 在 CLI 闭环稳定后，提供主播可视化副屏，用于查看排品、手卡、审批状态、审计结果和播中提示。 | Web 页面能展示播前方案和手卡，并能触发确认/拒绝类人工决策。 |
| 长期预留 | 真实平台 API 适配层 | 将本地模拟网关替换或扩展为真实平台适配器，但只在合规授权和安全评审通过后启用。 | 所有真实 API 写操作必须有显式授权、幂等键、审批记录和回滚/降级策略。 |

### 下一阶段建议

- Phase 2B 或后续阶段再引入基础播中事件：售罄、弹幕聚合、切品建议和应急提示。
- 前端副屏建议在播前、播中、播后 CLI 闭环都稳定后再做，避免 UI 先行掩盖核心业务链路问题。

## Phase 2B: 基础播中事件闭环

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 2b on-live event flow`
- 阶段状态：通过

### 目标

用本地模拟售罄事件跑通最小播中闭环：

```text
ON_LIVE -> 售罄事件 -> Reducer 下架 -> 推荐备选商品 -> 生成主播提示 -> 写审计 -> CLI 演示
```

### 计划任务

- 新增 Phase 2B 设计文档和实施计划，继续保留阶段留迹。
- 新增播中库存事件模型，Phase 2B 只支持 `sold_out`。
- 新增备选商品推荐逻辑，只推荐仍上架且有库存的商品。
- 新增播中主播提示模板，明确售罄商品、备选商品和人工确认口径。
- 扩展工具注册表，注册 `handle_sold_out_event`、`recommend_backup_product`、`generate_on_live_prompt`。
- 新增播中业务流服务，串联事件校验、Reducer、推荐、提示和审计。
- 新增 CLI 演示脚本，展示售罄处理、备选切换、提示和审计结果。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-2b-on-live-events-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-2b-on-live-events-plan.md`。
- 播中事件模型：`src/skills/on_live_events.py`。
- 备选推荐：`src/skills/backup_product_recommender.py`。
- 主播提示：`src/skills/on_live_prompt.py`。
- 播中闭环服务：`src/core/on_live_flow.py`。
- CLI 演示：`scripts/run_phase2b_on_live_demo.py`。
- 测试覆盖：播中事件、备选推荐、提示生成、工具注册、播中闭环集成测试。

### 验收命令

```powershell
pytest tests/unit/test_on_live_events.py -v
pytest tests/unit/test_backup_product_recommender.py -v
pytest tests/unit/test_on_live_prompt.py -v
pytest tests/unit/test_tool_registry.py -v
pytest tests/integration/test_on_live_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/run_phase2b_on_live_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：新增测试初次运行失败，原因是 `on_live_events`、`backup_product_recommender`、`on_live_prompt`、`on_live_flow` 等 Phase 2B 模块尚未实现。
- 单元测试覆盖事件模型校验、备选商品推荐、播中提示生成和工具注册表扩展。
- 集成测试覆盖非 `ON_LIVE` 拒绝、售罄下架、备选切换、无备选人工接管和审计链路。
- 指定单元测试结果：`test_on_live_events`、`test_backup_product_recommender`、`test_on_live_prompt`、`test_tool_registry` 全部通过。
- 指定集成测试结果：`test_on_live_flow` 通过。
- 全量测试结果：`57 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- CLI 演示结果：`p001` 售罄后库存变为 0 并下架，推荐 `p007`，当前商品切换到 `p007`，生成 warning 提示，并写入 3 条审计记录。
- 问题与修复记录：本阶段未出现阻塞性实现缺陷；TDD 红灯均为预期的缺失模块/缺失注册失败，绿灯实现后通过。

### 最终结论

Phase 2B 基础播中事件闭环验收通过。系统已经具备最小播中售罄处理能力，可以在 `ON_LIVE` 阶段处理本地模拟售罄事件，并完成状态下架、备选推荐、主播提示和审计留痕。

### 遗留限制

- 不接入 LLM，主播提示为确定性模板。
- 不接入真实淘宝生产 API，不处理真实用户隐私数据。
- 不实现 Web 前端，仍使用 CLI 演示闭环。
- 不启动长期 Kafka consumer，本阶段只使用本地模拟售罄事件。
- 不实现弹幕聚合、流量事件、PlanEngine 抢占恢复或播后复盘。

### 下一阶段建议

- 下一步可进入基础弹幕聚合：先用本地事件模拟 5 秒窗口聚合同类问题，再生成参考回复。
- Kafka consumer 建议在本地事件模型稳定后接入，先消费库存和弹幕 topic，不直接触发真实平台写操作。
- PlanEngine / 抢占恢复建议放到售罄与弹幕两个播中事件都稳定后再做，避免调度层过早复杂化。
- 继续保持 CLI 优先、Web 后置；等播前和基础播中 CLI 都稳定后，再设计 Web 副屏。

## Phase 2C: 基础弹幕聚合与参考回复

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 2c danmaku aggregation flow`
- 阶段状态：通过

### 目标

用本地脱敏弹幕批次跑通最小播中弹幕闭环：

```text
ON_LIVE -> 本地模拟弹幕批次 -> 5 秒窗口聚合 -> 同类问题合并 -> 确定性参考回复 -> 写审计 -> CLI 演示
```

### 计划任务

- 新增 Phase 2C 设计文档和实施计划，继续保留阶段留迹。
- 新增弹幕事件模型，要求 `room_id`、脱敏 `viewer_id`、`content`、`event_time`、`trace_id` 完整。
- 新增 5 秒窗口弹幕聚合器，按价格、库存、优惠、物流、使用方法、售后、通用问题分类。
- 新增确定性参考回复生成器，输出回复文本、风险提示、置信度和是否需要人工复核。
- 扩展工具注册表，注册 `aggregate_danmaku_questions` 和 `generate_danmaku_reply`。
- 新增播中弹幕流服务，串联生命周期校验、聚合、回复和审计。
- 新增 CLI 演示脚本，展示脱敏弹幕聚合、参考回复和审计结果。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-2c-danmaku-aggregation-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-2c-danmaku-aggregation-plan.md`。
- 弹幕事件模型：`src/skills/danmaku_events.py`。
- 弹幕聚合器：`src/skills/danmaku_aggregator.py`。
- 参考回复生成器：`src/skills/danmaku_reply_generator.py`。
- 播中弹幕流服务：`src/core/danmaku_flow.py`。
- CLI 演示：`scripts/run_phase2c_danmaku_demo.py`。
- 测试覆盖：弹幕事件、聚合器、回复生成、工具注册和弹幕流集成测试。

### 验收命令

```powershell
pytest tests/unit/test_danmaku_events.py -v
pytest tests/unit/test_danmaku_aggregator.py -v
pytest tests/unit/test_danmaku_reply_generator.py -v
pytest tests/unit/test_tool_registry.py -v
pytest tests/integration/test_danmaku_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/run_phase2c_danmaku_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：新增测试初次运行失败，原因是 `danmaku_events`、`danmaku_aggregator`、`danmaku_reply_generator`、`danmaku_flow` 模块尚未实现，工具注册表也未注册 Phase 2C 工具。
- 单元测试覆盖弹幕事件字段校验、空白内容拒绝、5 秒窗口聚合、同类问题合并、跨房间或跨 trace 拒绝、确定性参考回复和工具注册表扩展。
- 集成测试覆盖非 `ON_LIVE` 拒绝、弹幕批次聚合、参考回复生成、状态不变和 PostgreSQL 审计链路。
- 指定单元测试结果：`test_danmaku_events`、`test_danmaku_aggregator`、`test_danmaku_reply_generator`、`test_tool_registry` 全部通过。
- 指定集成测试结果：`test_danmaku_flow` 通过。
- 全量测试结果：`70 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- CLI 演示结果：输入 6 条脱敏弹幕，聚合出 5 类问题；价格类问题合并计数为 2；生成价格、优惠、库存、使用方法、物流 5 条主播参考回复；写入 6 条审计记录。
- 问题与修复记录：本阶段未出现阻塞性实现缺陷；TDD 红灯均为预期的缺失模块/缺失注册失败，绿灯实现后通过。

### 最终结论

Phase 2C 基础弹幕聚合与参考回复验收通过。系统已经具备最小播中弹幕处理能力，可以在 `ON_LIVE` 阶段处理本地脱敏弹幕批次，并完成同类问题聚合、主播参考回复和审计留痕。

### 遗留限制

- 不接入 LLM，参考回复为确定性模板。
- 不接入 Kafka consumer，本阶段只使用本地模拟弹幕批次。
- 不自动发送回复给观众，输出只供主播参考。
- 不接入真实淘宝生产 API，不处理真实用户隐私数据。
- 不实现 Web 前端，仍使用 CLI 演示闭环。
- 不实现 PlanEngine、抢占恢复、长期记忆、trust_score 或播后复盘。

### 下一阶段建议

- Phase 2D 或 Phase 3 可接入 Kafka consumer，先消费本地弹幕和库存 topic，并继续只触发审计和参考建议。
- PlanEngine / 抢占恢复建议在售罄事件和弹幕聚合都稳定后启动，用来处理“正在生成内容时被售罄或高频弹幕打断”的场景。
- LLM 话术增强建议放在规则模板稳定后接入，必须保留 Schema 校验、禁用词检查、人工复核和审计。
- Web 副屏继续后置；等播前和基础播中 CLI 闭环更稳定后，再展示排品、手卡、售罄提示和弹幕参考回复。
