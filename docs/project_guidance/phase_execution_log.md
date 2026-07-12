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

## Phase 2D: LangGraph 播前 Harness 骨架

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 2d langgraph pre-live skeleton`
- 阶段状态：通过

### 目标

把已完成的播前业务闭环接入 LangGraph 轻量编排骨架：

```text
LangGraph START -> 查询货盘 -> 生成排品 -> 生成商品手卡 -> 合规/风险摘要 -> 模拟建播 hard-gate -> END
```

本阶段不接 LLM、不接真实平台 API、不启用持久 checkpoint、不使用 interrupt 暂停恢复。LangGraph 只作为 workflow 编排层，业务规则继续由现有 Python service、ToolRegistry、SecurityHook 和 PostgreSQL 审计承担。

### 计划任务

- 新增 `langgraph>=1.2,<2.0` 依赖。
- 新增 LangGraph 播前骨架，定义 graph state、初始 state 和固定节点顺序。
- 将现有 `PreLiveBusinessFlowService` 的播前步骤暴露为公开方法，供 graph 节点复用。
- 新增合规/风险摘要节点，明确样例数据、确定性规则和 hard-gate 边界。
- 新增 CLI 演示脚本，展示 graph 节点历史、计数、门禁结果和审计链路。
- 新增设计文档和实施计划，持续保留阶段留迹。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-2d-langgraph-pre-live-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-2d-langgraph-pre-live-plan.md`。
- LangGraph 播前骨架：`src/core/pre_live_graph.py`。
- 播前业务服务公开节点方法：`src/core/pre_live_business_flow.py`。
- CLI 演示：`scripts/run_phase2d_pre_live_graph_demo.py`。
- 测试覆盖：`tests/unit/test_pre_live_graph.py`、`tests/integration/test_pre_live_graph_flow.py`。

### 验收命令

```powershell
pytest tests/unit/test_pre_live_graph.py -v
pytest tests/integration/test_pre_live_graph_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/run_phase2d_pre_live_graph_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：新增测试初次运行失败，原因是 `src.core.pre_live_graph` 模块尚未实现。
- 单元测试覆盖 LangGraph 依赖导入、初始 state、固定节点顺序、未确认 hard-gate pending、确认后返回建播审计 ID。
- 集成测试覆盖 PostgreSQL 样例数据初始化、完整播前 graph 执行、查询货盘/排品/手卡/建播审计链路。
- 指定单元测试结果：`test_pre_live_graph` 通过。
- 指定集成测试结果：`test_pre_live_graph_flow` 通过。
- 全量测试结果：`75 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- seed 脚本结果：写入 1 个主播、1 个直播场次、10 个脱敏样例商品。
- CLI 演示结果：Graph 完成 `query_products, generate_live_plan, generate_product_cards, compliance_check, setup_live_session`；查询 10 个商品，生成 10 个排品项、3 张手卡；建播 hard-gate 通过并返回 `setup_audit_id`；审计链路包含 6 条记录。
- 问题与修复记录：本阶段未出现阻塞性实现缺陷；LangGraph 1.2.8 的 `StateGraph`、`START`、`END`、`compile().invoke()` API 与当前轻量骨架匹配。

### 最终结论

Phase 2D LangGraph 播前 Harness 骨架验收通过。系统已经证明 LangGraph 可以作为 LiveAgent 的编排层接入现有播前业务，而不会绕过 ToolRegistry、SecurityHook 或 PostgreSQL 审计。

### 遗留限制

- 不接入 LLM，所有排品和手卡仍为确定性规则。
- 不启用持久 checkpoint，Graph state 当前允许携带 Pydantic 对象；后续做 PostgreSQL checkpoint 时需要改成可序列化快照。
- 不使用 LangGraph interrupt，人工确认仍通过 `confirmed_setup` 参数模拟。
- 不接 Kafka consumer，不处理真实平台事件流。
- 不接真实淘宝生产 API，不处理真实用户隐私数据。
- 不实现 Web 前端，仍使用 CLI 演示闭环。

### 下一阶段建议

- Phase 2E / Phase 3 可优先接 PostgreSQL checkpoint，把 graph state 转为可序列化结构并验证恢复。
- 接入 LangGraph interrupt / human-in-the-loop 前，应先明确人工确认输入格式、恢复命令和审计记录字段。
- Kafka consumer 可作为下一条主线，先把库存和弹幕 topic 转成本地事件模型，再交给现有播中服务。
- LLM 手卡增强建议在 graph + checkpoint 边界稳定后接入，并保留 Schema 校验、禁用词检查、人工复核和审计。

## Phase 2E: PostgreSQL Checkpoint 与可恢复 LangGraph 状态

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 2e postgres checkpoint recovery`
- 阶段状态：通过

### 目标

把 Phase 2D 的播前 LangGraph 从“一次性执行”升级为“可中断、可持久化、可恢复”的工程骨架：

```text
初始化样例数据 -> 运行播前 Graph -> 在生成商品手卡后中断
-> PostgreSQL 保存 checkpoint -> 使用同一 thread_id 恢复
-> 完成合规摘要与建播 hard-gate -> 写入审计
```

本阶段采用官方 `langgraph-checkpoint-postgres` 的 PostgresSaver，不自研 checkpoint store；不接 LLM、不接 Kafka consumer、不做 Web 前端、不接真实平台 API。

### 计划任务

- 精确锁定 `langgraph==1.2.8`，新增 `langgraph-checkpoint-postgres==3.1.0`。
- 新增 `LANGGRAPH_STRICT_MSGPACK=true` 公开配置模板。
- 为 PostgresSaver 增加专用 conninfo 生成方法，同时保持日志只展示脱敏 DSN。
- 将 `PreLiveGraphState` 改为 JSON 可序列化快照，不再持久化 Pydantic 对象。
- 支持 `checkpointer`、`interrupt_after` 和以 `trace_id` 作为 `thread_id` 的 graph config。
- 新增官方 PostgresSaver 初始化和创建辅助模块。
- 新增 Phase 2E CLI 演示、设计文档、实施计划，并持续留迹。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-2e-postgres-checkpoint-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-2e-postgres-checkpoint-plan.md`。
- Checkpoint 辅助模块：`src/core/langgraph_checkpoint.py`。
- 可恢复播前 Graph：`src/core/pre_live_graph.py`。
- CLI 演示：`scripts/run_phase2e_pre_live_checkpoint_demo.py`。
- 测试覆盖：`tests/unit/test_pre_live_graph_serialization.py`、`tests/unit/test_pre_live_graph_checkpoint.py`、`tests/integration/test_pre_live_graph_checkpoint_flow.py`。

### 验收命令

```powershell
pytest tests/unit/test_pre_live_graph_serialization.py -v
pytest tests/unit/test_pre_live_graph_checkpoint.py -v
pytest tests/unit/test_settings.py -v
pytest tests/integration/test_pre_live_graph_checkpoint_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/run_phase2e_pre_live_checkpoint_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- 基线反馈：隔离 worktree 初次缺少本地 `.env`，集成测试和中间件检查使用公开默认数据库密码导致 PostgreSQL 鉴权失败；复制 ignored 的本地 `.env` 后，基线恢复为 `75 passed`，PostgreSQL、pgvector、Redis、Kafka 全部通过。
- TDD 红灯结果：新增测试初次运行失败，原因分别是缺少快照 API、`create_pre_live_graph_config`、`Settings.postgres_checkpoint_conninfo`、`Settings.langgraph_strict_msgpack` 和 `src.core.langgraph_checkpoint` 模块。
- 单元测试覆盖商品/排品/手卡 snapshot 往返、state JSON 可序列化、内存 checkpointer 中断恢复、未确认 hard-gate pending、`trace_id` 作为 `thread_id`。
- 集成测试覆盖官方 PostgresSaver schema 初始化、生成商品手卡后中断、重新创建 graph 后恢复到 END、最终审计不重复。
- 指定测试结果：`test_pre_live_graph_serialization`、`test_pre_live_graph_checkpoint`、`test_settings`、`test_pre_live_graph_checkpoint_flow` 均通过。
- 全量测试结果：`83 passed`。
- CLI 演示结果：Graph 在 `generate_product_cards` 后中断，下一节点为 `compliance_check`；中断后审计 5 条；恢复后完成 `compliance_check` 与 `setup_live_session`，查询 10 个商品，生成 10 个排品项、3 张手卡，建播 hard-gate 通过，最终审计 6 条。
- 问题与修复记录：PostgresSaver 首次使用需要 `.setup()` 初始化表结构，已封装到 `initialize_postgres_checkpointer()`；checkpoint state 不能携带 Pydantic 对象，已通过 snapshot 转换解决。

### 最终结论

Phase 2E PostgreSQL checkpoint 恢复链路验收通过。系统已经证明播前 LangGraph 可以使用官方 PostgresSaver 持久化 checkpoint，并在模拟进程重启后用同一 `thread_id` 恢复执行，同时不重复写入前半段审计。

### 遗留限制

- 不接入真正 LangGraph interrupt，当前中断使用 `interrupt_after` 验证恢复语义。
- 不实现人工确认表单，建播确认仍通过 `confirmed_setup` 参数模拟。
- 不接 Kafka consumer，不处理真实平台事件流。
- 不接入 LLM，排品和手卡仍为确定性规则。
- 不接真实淘宝生产 API，不处理真实用户隐私数据。
- 不实现 Web 前端，仍使用 CLI 演示闭环。

### 下一阶段建议

- Phase 2F / Phase 3 可优先接 LangGraph interrupt / human-in-the-loop，把 hard-gate 从参数模拟升级为“暂停 -> 人工确认 -> 恢复”。
- Kafka consumer 可作为另一条主线，在 checkpoint 可恢复边界稳定后，把库存和弹幕 topic 转成本地事件模型。
- LLM 手卡增强建议继续后置，接入时必须保留 Schema 校验、禁用词检查、人工复核和审计。
- Web 副屏继续后置，等 interrupt 和 Kafka 入口稳定后，再展示审批、恢复、售罄提示和弹幕参考回复。

## Phase 2F: LangGraph Interrupt 人审恢复

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 2f human approval interrupt flow`
- 阶段状态：通过

### 目标

把 Phase 2E 的 checkpoint 恢复能力升级为真正的 human-in-the-loop：

```text
运行播前 Graph -> 建播 hard-gate 触发 interrupt
-> CLI 模拟 approve / reject -> Command(resume=...) 恢复
-> 执行建播或终止 -> 写入审批与工具审计
```

本阶段继续不接 LLM、不接 Kafka consumer、不做 Web 前端、不接真实平台 API。重点验证高风险动作不能再靠参数伪造通过，而是由 LangGraph 暂停、人工审批和恢复共同完成。

### 计划任务

- 新增人工审批请求和恢复输入模型，限制 `approved/rejected` 两种决策。
- 在播前 Graph 的 `setup_live_session` 节点接入 `interrupt()` 和 `Command(resume=...)`。
- 保留旧的 `confirmed_setup` 路径，避免破坏 Phase 2D/2E 演示和测试。
- 审批 pending、approved/rejected 结果写入 PostgreSQL 审计。
- 针对 LangGraph 节点恢复会重跑的行为，为 pending 审计增加幂等保护。
- 新增 approve/reject 双场景 CLI 演示。
- 新增 Phase 2F 设计文档、实施计划，并持续留迹。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-2f-human-approval-interrupt-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-2f-human-approval-interrupt-plan.md`。
- 人审模型：`src/core/human_approval.py`。
- Interrupt 播前 Graph：`src/core/pre_live_graph.py`。
- 审批审计扩展：`src/core/pre_live_business_flow.py`、`src/state/models.py`。
- CLI 演示：`scripts/run_phase2f_pre_live_interrupt_demo.py`。
- 测试覆盖：`tests/unit/test_human_approval.py`、`tests/unit/test_pre_live_graph_interrupt.py`、`tests/unit/test_pre_live_business_flow_idempotency.py`、`tests/integration/test_pre_live_graph_interrupt_flow.py`。

### 验收命令

```powershell
pytest tests/unit/test_human_approval.py -v
pytest tests/unit/test_pre_live_graph_interrupt.py -v
pytest tests/integration/test_pre_live_graph_interrupt_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/run_phase2f_pre_live_interrupt_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：`test_human_approval` 和 `test_pre_live_graph_interrupt` 初次运行失败，原因是缺少 `src.core.human_approval`；集成测试初次失败，原因是 `create_initial_pre_live_graph_state()` 尚不支持 `enable_human_approval`。
- 单元测试覆盖审批模型、空字段拒绝、未知决策拒绝、trace 匹配校验、Graph interrupt payload、approve 恢复、reject 恢复。
- 集成测试覆盖官方 PostgresSaver 恢复、approve 审计链路、reject 审计链路、前半段审计不重复。
- 指定测试结果：`test_human_approval` 9 个用例通过；`test_pre_live_graph_interrupt` 3 个用例通过；`test_pre_live_business_flow_idempotency` 1 个用例通过；`test_pre_live_graph_interrupt_flow` 2 个集成用例通过。
- 兼容性测试结果：`test_pre_live_graph`、`test_pre_live_graph_checkpoint`、`test_pre_live_graph_checkpoint_flow`、`test_pre_live_graph_flow` 均通过，旧 `confirmed_setup` 路径未被破坏。
- 全量测试结果：`98 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- seed 脚本结果：写入 1 个主播、1 个直播场次、10 个脱敏样例商品。
- CLI 演示结果：approve 场景在 `setup_live_session` 触发 interrupt，恢复后 `setup_status=prepared`，最终审计 8 条，包含 `pending, approved` 审批记录和 `setup_live_session` 成功记录；reject 场景恢复后 `setup_status=rejected`，最终审计 7 条，包含 `pending, rejected` 审批记录，不包含建播成功审计。

### 当前问题与修复记录

- 发现：LangGraph `interrupt()` 恢复时会从当前节点开头重跑，若在 `interrupt()` 前无保护地写 pending 审计，会导致重复审计。
- 修复：审批审计使用 `trace_id + tool_name + approval status` 组成 `idempotency_key`，恢复重跑时先查同一 trace 下的既有审批审计，存在则复用 audit_id。
- 代码审查发现：approved 后如果建播成功审计已写入、但 graph 节点完成 checkpoint 前崩溃，恢复可能重复写 `setup_live_session` 成功审计。
- 修复：`setup_live_session` 成功审计也按 `tool_name + idempotency_key` 查询复用；新增红绿测试证明同一 trace 重放时复用原 `audit_id`，不重复写成功审计。

### 最终结论

Phase 2F LangGraph interrupt 人审恢复链路验收通过。系统已经把建播 hard-gate 从参数模拟升级为“Graph 暂停 -> 人工审批 -> checkpoint 恢复 -> 执行或拒绝”的可审计闭环，并保持前半段播前审计不重复写入。

### 遗留限制

- 当前只实现 CLI 人审恢复，不做 Web 审批界面。
- 审批人标识使用脱敏演示值，不接企业账号体系。
- 不接入 LLM，排品和手卡仍为确定性规则。
- 不接 Kafka consumer，不处理真实平台事件流。
- 不接真实淘宝生产 API，不处理真实用户隐私数据。

### 下一阶段建议

- Phase 3 可进入“记忆与信任”：主播偏好、历史表现、trust_score、可回放决策质量评估。
- Kafka consumer 可并行规划，把库存和弹幕 topic 转成本地事件入口，再接入现有播中服务。
- Web 副屏建议在 interrupt、人审审计和 Kafka 入口稳定后开始，让审批、恢复、售罄提示和弹幕参考回复可视化。
- LLM 手卡增强继续后置，接入时必须保留 Schema 校验、禁用词检查、人工复核和审计。

## Phase 3A: 记忆与信任层基础闭环

### 基本信息

- 验收日期：2026-07-07
- 对应提交：`feat: add phase 3a memory trust foundation`
- 阶段状态：通过

### 目标

建设“越用越懂主播”的最小可控闭环：

```text
初始化记忆样例数据 -> 读取主播偏好与历史表现 -> 生成带记忆影响的播前排品
-> 记录 Decision Trace -> 模拟主播反馈与业务结果 -> 更新 trust_score
-> 下一次播前建议受记忆和信任分影响
```

本阶段不接 LLM、不做 Web、不接 Kafka consumer、不接真实平台 API。继续使用 PostgreSQL + pgvector，`embedding vector(1536)` 只做字段预留。

### 计划任务

- 新增 Phase 3A 设计文档和实施计划文档。
- 新增记忆、信任状态和 Decision Trace PostgreSQL 表。
- 新增 `MemoryStore`、`TrustManager`、`DecisionTraceStore`、`MemoryAwarePlanService`、`ToolMaskPolicy`。
- 新增 Phase 3A seed 脚本和 CLI 演示脚本。
- README 增加 Phase 3A seed 与 demo 命令。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-07-phase-3a-memory-trust-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-07-phase-3a-memory-trust-plan.md`。
- SQL 初始化：`docker/init_phase3_memory.sql`。
- 领域模型与服务：`src/memory/models.py`、`src/memory/memory_store.py`、`src/memory/trust_manager.py`、`src/memory/decision_trace_store.py`、`src/memory/memory_aware_plan.py`、`src/memory/tool_mask_policy.py`。
- CLI：`scripts/seed_phase3_memory_demo_data.py`、`scripts/run_phase3a_memory_trust_demo.py`。
- 测试覆盖：`tests/unit/test_memory_models.py`、`tests/unit/test_memory_store.py`、`tests/unit/test_trust_manager.py`、`tests/unit/test_tool_mask_policy.py`、`tests/unit/test_memory_aware_plan.py`、`tests/integration/test_phase3_memory_seed_data.py`、`tests/integration/test_memory_trust_flow.py`。

### 验收命令

```powershell
pytest tests/unit/test_memory_models.py -v
pytest tests/unit/test_memory_store.py -v
pytest tests/unit/test_trust_manager.py -v
pytest tests/unit/test_tool_mask_policy.py -v
pytest tests/unit/test_memory_aware_plan.py -v
pytest tests/integration/test_phase3_memory_seed_data.py -v
pytest tests/integration/test_memory_trust_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/seed_phase3_memory_demo_data.py
python scripts/run_phase3a_memory_trust_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：新增 7 组测试首次运行失败，原因均为 Phase 3A 新模块尚未实现，包括 `src.memory.models`、`memory_store`、`trust_manager`、`tool_mask_policy`、`memory_aware_plan`、`demo_memory_seed`、`decision_trace_store`。
- TDD 绿灯结果：实现最小记忆/信任/排品/追踪闭环后，新增 21 个 Phase 3A 用例通过；代码审查后补强数据隔离和审计不可覆盖测试，Phase 3A 指定用例扩展为 26 个并全部通过。
- 指定测试结果：`test_memory_models` 5 个用例通过，`test_memory_store` 4 个用例通过，`test_trust_manager` 6 个用例通过，`test_tool_mask_policy` 4 个用例通过，`test_memory_aware_plan` 2 个用例通过，`test_phase3_memory_seed_data` 1 个集成用例通过，`test_memory_trust_flow` 4 个集成用例通过。
- 全量测试结果：`pytest -v` 通过，合计 `124 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- seed 脚本结果：Phase 2A seed 写入 1 个主播、1 个直播间、10 个脱敏商品；Phase 3A seed 写入 3 条记忆，默认 `trust_score=0.70`。
- CLI 演示结果：`run_phase3a_memory_trust_demo.py` 完成闭环，记忆将 `p003` 提升为第一讲解商品；模拟 `accepted/good` 后 `trust_score` 从 `0.70` 更新为 `0.75`；Decision Trace 返回可回放 ID；高信任分下播前工具可见范围包含非 block 工具；排品理由只输出结构化命中摘要，不回显完整记忆正文。
- Git 候选检查：`git add -n .` 只包含 README、阶段日志、Phase 3A 文档、SQL、脚本、源码和测试；`.env`、缓存目录和 `__pycache__` 仍为 ignored。

### 当前问题与修复记录

- 已确认记忆表依赖 Phase 2A 样例主播和直播间外键，因此 seed 脚本会先初始化 Phase 2A 货盘数据，再初始化 Phase 3A 记忆数据。
- Decision Trace 使用 `trace_id` 唯一约束和 upsert，保证演示脚本重复执行时不会不断追加重复闭环记录。
- `ToolMaskPolicy` 只裁剪工具可见范围，不替代 ToolRegistry 和 SecurityHook，避免信任层绕过原有安全边界。
- 代码审查发现 anchor/room 组合一致性、memory_key 跨主播移动、Decision Trace 覆盖写入、原始记忆正文回显和测试假阳性风险；已补充组合外键与 Store 层校验，阻止 memory_key 跨主播移动，Decision Trace 改为“相同内容幂等、不同内容拒绝覆盖”，排品理由改为结构化摘要，并补充对应红绿测试。
- CLI 原先使用固定 `trace_id`，在 Decision Trace 不可覆盖后重复运行会被正确拒绝；已改为每次生成新的脱敏 `trace_id`，保留历史演示记录。

### 最终结论

Phase 3A 记忆与信任层基础闭环验收通过。系统已经能基于 PostgreSQL 中的主播偏好和历史表现影响播前排品，并把主播反馈、业务结果、trust_delta 和最终 trust_score 写入 Decision Trace。该阶段为后续 embedding 检索、LLM 手卡增强和更复杂的主播偏好学习留下了稳定接口。

### 遗留限制

- 不接 LLM，记忆影响排品只使用结构化 metadata 和确定性规则。
- 不写入 embedding，pgvector 字段只预留，语义检索后置。
- trust_score 更新规则固定为四条基础规则，尚未引入更复杂的业务指标归因。
- 记忆数据目前来自 seed 和模拟反馈，不接真实平台历史数据。
- 不做 Web 副屏，仍使用 CLI 演示。

### 下一阶段建议

- Phase 3B 可继续增强记忆检索与归因：接入 embedding、相似记忆检索、记忆衰减和冲突修正。
- Kafka consumer 可并行规划，把库存、弹幕和成交事件转成可复用的本地事件入口。
- LLM 手卡增强建议在记忆层稳定后接入，并继续保留 Schema 校验、禁用词检查、人工复核和审计。
- Web 副屏可以在记忆、审批、Kafka 入口稳定后启动，重点展示审批、trace 回放、trust_score 变化和建议理由。

## Phase 3B: 记忆检索、衰减与冲突修正

### 基本信息

- 验收日期：2026-07-08
- 对应提交：`feat: add phase 3b memory retrieval and revision`
- 阶段状态：通过

### 目标

增强 Phase 3A 的记忆层质量控制：

```text
增强记忆检索 -> 记忆衰减 -> 冲突修正 -> Decision Trace 反哺记忆 -> 下一轮排品变化
```

本阶段不接 LLM、不接 embedding、不接 Kafka consumer、不做 Web、不接真实平台 API。所有规则保持确定性，便于测试和审计复盘。

### 计划任务

- 新增 Phase 3B 设计文档和实施计划文档。
- 新增 `MemoryDecayPolicy`、`MemoryRetriever`、`BeliefRevisionService`、`DecisionTraceMemoryFeedbackService`。
- 扩展记忆表状态字段：`status`、`suppressed_reason`、`updated_at`。
- 扩展 `MemoryAwarePlanService`，优先消费增强检索命中结果，同时兼容 Phase 3A 原始记忆入口。
- 新增 Phase 3B 独立 seed 和 CLI 演示脚本。
- 记录 TDD 红绿、测试结果、CLI 结果、问题修复、限制和后续迭代方向。

### 交付物

- 设计文档：`docs/superpowers/specs/2026-07-08-phase-3b-memory-retrieval-revision-design.md`。
- 实施计划：`docs/superpowers/plans/2026-07-08-phase-3b-memory-retrieval-revision-plan.md`。
- SQL 更新：`docker/init_phase3_memory.sql`。
- 领域模型与服务：`src/memory/memory_decay.py`、`src/memory/memory_retrieval.py`、`src/memory/belief_revision.py`、`src/memory/decision_memory_feedback.py`、`src/memory/demo_memory_seed_phase3b.py`。
- 兼容性更新：`src/memory/models.py`、`src/memory/memory_store.py`、`src/memory/memory_aware_plan.py`。
- CLI：`scripts/seed_phase3b_memory_demo_data.py`、`scripts/run_phase3b_memory_revision_demo.py`。
- 测试覆盖：`tests/unit/test_memory_decay.py`、`tests/unit/test_memory_retrieval.py`、`tests/unit/test_belief_revision.py`、`tests/unit/test_decision_memory_feedback.py`、`tests/integration/test_memory_revision_flow.py`。

### 验收命令

```powershell
pytest tests/unit/test_memory_retrieval.py -v
pytest tests/unit/test_memory_decay.py -v
pytest tests/unit/test_belief_revision.py -v
pytest tests/unit/test_decision_memory_feedback.py -v
pytest tests/integration/test_memory_revision_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/seed_phase2_demo_data.py
python scripts/seed_phase3_memory_demo_data.py
python scripts/seed_phase3b_memory_demo_data.py
python scripts/run_phase3b_memory_revision_demo.py
git status --short --ignored
git add -n .
```

### 执行反馈

- TDD 红灯结果：新增 4 组测试首次运行失败，原因均为 Phase 3B 新模块尚未实现，包括 `src.memory.memory_decay`、`memory_retrieval`、`belief_revision` 和 `demo_memory_seed_phase3b`。
- TDD 绿灯结果：实现最小检索、衰减、冲突修正、反馈记忆和 Phase 3B seed/demo 后，新增 10 个 Phase 3B 用例全部通过；代码审查后补强原子事务、跨房间 key、值级脱敏、缺少货盘 fail-closed 和字段别名冲突测试，Phase 3B 新增/强化用例扩展为 16 个并全部通过。
- 受影响回归结果：`test_memory_models`、`test_memory_store`、`test_memory_aware_plan` 共 11 个用例通过，确认新增记忆状态字段与增强检索没有破坏 Phase 3A 行为。
- 全量测试结果：`pytest -v` 通过，合计 `140 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- seed 脚本结果：Phase 2A seed 写入 1 个主播、1 个直播间、10 个脱敏商品；Phase 3A seed 写入 3 条记忆；Phase 3B seed 写入独立主播 `anchor-phase3b-001`、直播间 `room-phase3b-001`，并重置旧偏好 `phase3b-old-home-preference`。
- CLI 演示结果：`run_phase3b_memory_revision_demo.py` 完成闭环。修正前旧家居偏好使 `p001` 排第一；模拟 `accepted/good` 后生成厨房类 L2 反馈记忆，旧记忆被标记为 `suppressed`；修正后 `p003` 排第一，记忆命中显示新反馈记忆有效权重高于 suppressed 旧记忆。
- Git 候选检查：`git add -n .` 只包含 README、阶段日志、Phase 3B 文档、SQL、脚本、源码和测试；`.env`、`.pytest_cache/`、`__pycache__/` 仍为 ignored。

### 当前问题与修复记录

- 集成测试首次运行暴露 psycopg 参数化 SQL 问题：`LIKE 'phase3b-feedback-%'` 中的 `%` 被 pyformat 解析为占位符。已改为 `LIKE %(memory_key_prefix)s` 并通过集成测试验证。
- 代码审查发现冲突修正非原子、`memory_key` 可被同主播跨房间移动、反馈记忆只做字段名白名单而缺少值级脱敏。已新增 `revise_memories_atomically()`，保证 suppress 和新记忆写入同事务；`write_memory()` 阻止同 key 跨房间移动；反馈记忆按当前货盘过滤类目、标签和商品 ID；并补充对应红绿测试。
- 二轮复核发现反馈记忆在缺少货盘时仍可能 fail-open。已改为没有非空 `catalog_products` 时直接拒绝生成反馈记忆，并补充字段别名冲突检测，避免 `preferred_category` 与 `preferred_categories` 混用时漏检。
- PostgreSQL 在 scoped suppress SQL 中无法推断 `%(room_id)s IS NULL` 参数类型；已按 `room_id is None` 分支生成 `room_id IS NULL` 或 `room_id = %(room_id)s`，避免类型推断错误。
- 为避免 Phase 3B 影响 Phase 3A 对默认样例主播的断言，Phase 3B 使用独立脱敏主播和直播间。
- 为保证 seed/demo 可重复，Phase 3B seed 会清理上一轮 `phase3b-feedback-*` 反馈记忆，并把旧偏好重置为 active。
- 冲突修正不删除旧记忆，只写入 `status=suppressed` 和脱敏 `suppressed_reason`；检索层仍可回放旧记忆，但有效权重被显著降低。
- Decision Trace 反哺记忆只白名单提取 `preferred_category`、`preferred_tags`、`preferred_product_ids`、`conflict_group` 等结构化字段，不复制完整话术、主播原话、订单信息或平台字段。

### 最终结论

Phase 3B 验收通过。系统已经能按记忆新鲜度、层级、证据权重、房间匹配和状态进行增强检索；旧记忆会随时间衰减；发生偏好冲突时旧记忆被保留但压低影响力；主播反馈可以生成新的结构化 L2 记忆，并推动下一轮播前排品变化。该阶段把 Phase 3A 的“能记住”推进到“能修正、能解释、能复盘”。

### 遗留限制

- 不接 embedding，增强检索仍基于结构化 metadata 和确定性规则。
- 不接 LLM，反馈记忆内容和排品解释仍为模板化生成。
- 冲突修正目前只处理 `conflict_group` 下的类目、标签和商品 ID 偏好，不处理复杂多目标策略冲突。
- 记忆衰减使用固定半衰期参数，尚未根据真实业务效果自动调参。
- 仍使用 CLI 演示，不提供 Web 端记忆审核和冲突修正界面。

### 下一阶段建议

- Phase 3C 可进入 embedding/pgvector 语义检索，把结构化检索与向量相似度组合，但仍需保留 Schema 校验、隐私白名单和审计。
- 可并行规划 Kafka consumer，把播中库存、成交和弹幕事件进入统一事件入口，反哺记忆和 Decision Trace。
- LLM 接入建议放在记忆检索和冲突修正稳定后，优先做“LLM 手卡增强/话术改写”，并保留人工复核、禁用词检查和审计链路。
- Web 副屏可在记忆审核、冲突解释、trust_score 趋势和审批恢复稳定后启动。

## Phase 3D: Kafka Consumer 实时播中事件管线

### 基本信息

- 验收日期：2026-07-08
- 对应提交：`98634d9 feat: add phase 3d kafka consumer pipeline and phase 3c embedding foundation`
- TDD：先写失败测试，再实现代码

### 交付内容

1. `src/gateway/kafka_event_models.py`：Kafka 消息到领域事件的解析层
   - `KafkaConsumedEvent`：含 topic/partition/offset 元数据和已校验的领域事件
   - `parse_danmaku_event()` / `parse_inventory_event()`：JSON -> Pydantic 转换
2. `src/gateway/kafka_consumer.py`：EventRouter + LiveAgentKafkaConsumer
   - 4 个 topic 的映射和分派
   - 未知 topic 不抛异常（fail-continue）
   - 一次性消费模式 `consume_batch()`
3. `scripts/run_kafka_consumer.py`：CLI 演示脚本
4. 测试：unit 10 + integration 2 = 12 个新测试

### TDD 红绿反馈

- test_kafka_event_models.py：6 测试，红灯（ModuleNotFoundError） -> 绿灯
- test_kafka_consumer_routing.py：4 测试，红灯 -> 绿灯
- test_kafka_consumer_flow.py：2 集成测试，直接绿灯（Kafka broker 已就绪）

### 全量测试结果

168 passed, 0 failed

### CLI 演示结果

Kafka 连接正常，无待消费消息时显示提示。

### 发现的问题与修复记录

- KeyError in parse missed：第一次只捕获了 JSONDecodeError，缺少 room_id 等字段的 KeyError 未包装 -> 增加外层 try/except Exception
- redis/kafka 反序列化警告：DeprecationWarning 关于 serializer 接口 -> 不影响功能

### 当前遗留限制

- traffic 和 command 两个 topic 的路由已预留映射但暂未实现解析器
- consumer 不做 offset 管理（一次性模式），不做 consumer group
- 消费后不触发 Reducer（弹幕流程一般不改变商品状态）

### 下一阶段建议

- Phase 3C 收尾：等 embedding API key 更新后完成 seed + 集成测试
- Phase 3E：LLM 手卡增强（用 DeepSeek 生成更自然的话术）
- Phase 4：播后复盘 + Web 副屏

## Phase 3C: 语义记忆检索 (Embedding + pgvector)

### 基本信息

- 验收日期：2026-07-08
- 对应提交：待提交
- 设计文档：[phase-3c-semantic-memory-design.md](../superpowers/specs/2026-07-08-phase-3c-semantic-memory-design.md)
- TDD：Mock 先验证逻辑，再验证真实 API

### 交付内容

1. `src/skills/embedding_service.py`：封装智谱 embedding-3 API，2048 维 + MockEmbeddingService
2. `src/memory/semantic_retrieval.py`：SemanticMemoryRetriever，混合加权融合 (0.6x语义 + 0.4x结构化)
3. `MemoryStore` 改造：write_memory() 内部自动生成 embedding
4. 数据库：embedding vector(2048)，`docker/alter_phase3c_embedding_dim.sql`
5. seed 脚本：回填已有记忆 embedding
6. CLI demo：语义检索演示

### TDD 红绿反馈

- test_embedding_service.py：7 Mock 测试，红灯 -> 绿灯
- test_semantic_retrieval.py：7 测试，红灯 -> 绿灯
- test_memory_store.py (embedding)：2 测试，红灯 -> 绿灯

### CLI 演示结果

- seed 回填 4 条记忆全部成功，2048 维
- 语义搜索 "利润高的产品" -> 召回 "偏好高利润商品" (0.60)
- 语义搜索 "主播不喜欢低价款" -> 召回 "偏好高利润" (0.60)
- 语义搜索 "售后问题怎么处理" -> 相似度偏低 (0.32-0.35)，符合预期

### 全量测试结果

168 passed, 0 failed

### 发现的问题与修复

- 智谱 embedding-3 实际输出 2048 维非 1024 维 -> 调整 schema 从 vector(1024) 到 vector(2048)
- 旧 API key 401 -> 更新新 key
- SQL BOM 问题反复出现 -> 统一用 utf-8-sig 读取再 utf-8 回写

### 遗留限制

- 语义检索暂未集成到 MemoryAwarePlanService（后续 Phase 决定）
- 无 Reranker 二次排序
- mixed_retrieve 目前只在测试覆盖，未在 demo 中使用

### 下一阶段建议

- Phase 3E：LLM 手卡增强（DeepSeek chat API + 话术生成）
- Phase 4：播后复盘 + Web 副屏
- 后续：PlanEngine 抢占恢复 / 真实平台 API 适配
## Phase 3E: LLM 手卡话术增强 (DeepSeek)

### 基本信息

- 验收日期：2026-07-08
- 对应提交：待提交
- 设计文档：待补充
- TDD：先 Mock 测试 prompt/parse/降级，再集成真实 API

### 交付内容

1. `src/skills/llm_card_generator.py`：DeepSeek chat API 封装 + 降级 fallback
2. `tests/unit/test_llm_card_generator.py`：7 测试（prompt 构建、JSON 解析、降级）
3. `tests/integration/test_llm_card_flow.py`：3 测试（真实 API、降级）
4. `scripts/run_phase3e_llm_card_demo.py`：模板 vs LLM 对比演示

### TDD 红绿反馈

- 测试先写 7 个，红灯 -> 绿灯（中间修复 CatalogProduct 字段匹配问题）
- 集成测试 3 个，真实 DeepSeek API 直接绿灯

### CLI 演示结果

- LLM 手卡话术自然度远超模板：开场话术口语化、卖点自动推导、价格促单话术有吸引力
- LLM 自动生成了产品风险提示（模板没有）
- 降级链路验证：坏 key 时正确回退到模板

### 全量测试结果

182 passed, 0 failed

### 发现的问题与修复

- CatalogProduct 缺少 description 字段 -> 从 prompt 中移除
- 测试数据缺少 inventory/commission_rate -> 补全

### 下一阶段建议

- Phase 4：播后复盘 + Web 副屏
- LLM 手卡接入 LangGraph 播前编排链路

## Phase 4A: 播后复盘基础闭环

### 基本信息

- 验收日期：2026-07-08
- 对应提交：待提交
- 设计文档：待补充
- TDD：先写失败测试，再实现代码（lock + attribution + review）

### 交付内容

1. `src/core/post_live_lock.py`：POST_LIVE 写操作强制锁定
2. `src/skills/post_live_attribution.py`：采纳率/准确率归因计算
3. `src/skills/post_live_review.py`：决策复盘 + trust 变化汇总
4. 测试：unit 9 + integration 2 = 11 个新测试
5. `scripts/run_phase4a_post_live_demo.py`：完整播后闭环演示

### TDD 红绿反馈

- test_post_live_lock.py：3 红灯（mask_for_lifecycle 不存在）-> 4 绿灯
- test_post_live_attribution.py：3 红灯 -> 3 绿灯
- test_post_live_review.py：2 红灯 -> 2 绿灯
- test_post_live_flow.py：集成 2 测试，修复 decimal 类型问题后绿灯

### CLI 演示结果

- POST_LIVE 下 all 写操作被 block
- 归因：4 条决策，采纳率 0.5，准确率 0.5
- 复盘：trust 累计 -0.07，识别 1 个问题（采纳但效果差）

### 全量测试结果

193 passed, 0 failed

### 发现的问题与修复

- TrustManager 构造函数无参数 -> 从集成测试中移除不必要调用
- Decimal 与 float 混用 -> 统一用 Decimal(str(val)) 转

### 当前遗留限制

- 未接入真实 PostgreSQL audit/decision_trace 数据（用内存模拟 traces）
- 报告为 CLI 格式，不生成 PDF
- 记忆回写逻辑（post_live_memory_sync）尚未实现
- 未接入 LLM 复盘总结

### 下一阶段建议

- Phase 4B：Web 副屏界面（预留 D:\java\agent\front 目录）
- 记忆回写补充实现
- LLM 复盘总结接入

---

## Phase 4B：Web 副屏界面

- **日期**：2026-07-08
- **设计文档**：[2026-07-08-phase-4b-web-dashboard-design.md](../superpowers/specs/2026-07-08-phase-4b-web-dashboard-design.md)
- **实施计划**：[2026-07-08-phase-4b-web-dashboard-plan.md](../superpowers/plans/2026-07-08-phase-4b-web-dashboard-plan.md)
- **TDD 策略**：先写 API 测试，再实现端点；前端手动验收

### 实际交付内容

1. src/gateway/api_server.py：FastAPI 应用，5 个 REST 端点
2. front/index.html：深色主题副屏，四象限布局（手卡/弹幕/告警/复盘）
3. tests/unit/test_api_server.py：5 个端点测试
4. API 端点复用现有 LLMCardGenerator、DanmakuAggregator、PostLiveAttribution 等服务

### TDD 红绿反馈

- test_api_server.py：5 红灯（端点未注册）-> 5 绿灯
- 全量测试：198 passed, 0 failed（从 Phase 4A 的 193 增长到 198）

### 全量测试结果

198 passed, 0 failed（pytest -v）

### CLI/UI 演示结果

- FastAPI 启动后可访问 http://localhost:8100
- 深色主题副屏正常渲染
- 四个面板轮询正常（弹幕/告警 10s，复盘 30s，手卡手动刷新）

### 发现的问题与修复

- PowerShell Out-File -Encoding utf8 会加 BOM -> 用 Python open(path, w, encoding=utf-8) 覆写
- generate_danmaku_reply 在 API 中调用出错（reply_text 类型不匹配）-> 简化为只展示聚合摘要

### 当前遗留限制

- 弹幕/告警使用模拟数据，未接 Kafka consumer 实时数据
- 手卡 API 使用硬编码商品（p001），未从数据库货盘读取
- 复盘 API 使用内存模拟 traces，未接 PostgreSQL decision_trace 表
- 前端未做响应式设计（固定 1280x800）

### 下一阶段建议

- Phase 4C：将 Web 副屏数据源从模拟切换到真实 PostgreSQL + Kafka
- Phase 5A：前端框架升级（React/Vue），响应式适配
- 记忆回写（post_live_memory_sync）补充实现
- LLM 复盘总结接入

---

## Phase 4C：Web 副屏数据源真实化

- **日期**：2026-07-08
- **实施计划**：[2026-07-08-phase-4c-dashboard-real-data-plan.md](../superpowers/plans/2026-07-08-phase-4c-dashboard-real-data-plan.md)

### 实际交付内容

1. /api/card/{product_id} 从 PostgreSQL live_agent_products 表读取真实商品（复用 ProductCatalogRepository），查不到返回 404
2. /api/alert/{room_id} 从数据库查询 room 关联商品的库存，inventory < 30 或 = 0 生成告警
3. /api/review/{room_id} 从 live_agent_decision_trace 表读取真实决策记录，计算归因指标
4. /api/danmaku/summary 保持模拟（标注 TODO：Phase 4D/5 接入 Kafka 后升级）

### 全量测试结果

198 passed, 0 failed（pytest -v）

### 发现的问题与修复

- PowerShell 写 Python 文件反复遇到 BOM / 转义问题 -> 最终用 heredoc -> base64 -> decode 管道方案
- TestClient 测试兼容性好，无需修改现有测试用例

### 当前遗留限制

- 弹幕 API 仍使用模拟数据（标注 TODO）
- 手卡 API 硬编码 room-001，未做多直播间支持
- 未做写入型端点（副屏目前只读）

### 下一阶段建议

- Phase 4D：弹幕数据持久化到 PostgreSQL + Kafka 长期消费模式
- Phase 5A：前端框架升级与响应式适配
- 多直播间选择功能

---

## Phase 4D：Kafka 守护进程 + 弹幕聚合持久化

- **日期**：2026-07-08
- **实施计划**：[2026-07-08-phase-4d-kafka-daemon-plan.md](../superpowers/plans/2026-07-08-phase-4d-kafka-daemon-plan.md)

### 实际交付内容

1. docker/init_phase4_danmaku_aggregates.sql：新增建表脚本
2. src/gateway/kafka_daemon.py：DanmakuDaemon 守护进程，5s 窗口聚合 + PostgreSQL 持久化
3. src/gateway/api_server.py：弹幕端点改为从 live_agent_danmaku_aggregates 读库，移除模拟数据
4. scripts/run_kafka_daemon.py：守护进程启动脚本
5. scripts/run_kafka_daemon_demo.py：端到端演示脚本
6. 	ests/unit/test_kafka_daemon.py：5 个单元测试
7. 	ests/integration/test_kafka_daemon_flow.py：1 个集成测试

### TDD 红绿反馈

- test_kafka_daemon.py：5 红灯 -> 5 绿灯
- test_kafka_daemon_flow.py：1 红灯（consumer group_id 问题）-> 1 绿灯

### 全量测试结果

204 passed, 0 failed（从 Phase 4C 的 198 增长至 204）

### CLI 演示结果

- python scripts/run_kafka_daemon_demo.py：发送 10 条弹幕 -> 消费 -> 聚合 -> 写库成功

### 发现的问题与修复

- Kafka consumer subscribe 模式在测试中不可靠（无法收到已发送消息）-> 改用 assign + seek_to_beginning
- assign 模式无 group_id，无法 commit -> 集成测试中去掉 commit，只做消费 + 写库

### 当前遗留限制

- 守护进程只消费弹幕 topic，售罄/流量事件仍用一次性消费
- 无进程管理器（systemd/supervisor），重启需手动
- 聚合结果表无自动清理机制

### 下一阶段建议

- Phase 4E：售罄事件守护进程化
- Phase 5A：前端框架升级与 WebSocket 实时推送
- 记忆回写（post_live_memory_sync）补充实现

---

## Phase 4E：记忆回写补全

- **日期**：2026-07-08
- **实施计划**：[2026-07-08-phase-4e-memory-sync-plan.md](../superpowers/plans/2026-07-08-phase-4e-memory-sync-plan.md)

### 实际交付内容

1. src/skills/post_live_memory_sync.py：PostLiveMemorySyncService 编排层
2. scripts/run_phase4e_memory_sync_demo.py：演示脚本
3. tests/unit/test_post_live_memory_sync.py：2 个单元测试
4. tests/integration/test_post_live_memory_sync_flow.py：1 个集成测试

### 全量测试结果

207 passed, 0 failed（从 Phase 4D 的 204 增长至 207）

### 发现的问题与修复

- TrustManager.apply_feedback 签名不符（期望 state 对象而非命名参数）-> 修正调用方式
- 演示写入数据导致 seed 测试预期 L2 记忆数从 1 变 2 -> 将断言改为 >= 1

### 当前遗留限制

- 记忆回写仅处理单条 trace，未实现批量 trace 回写
- 回写的记忆未清除旧的冲突记忆（复用现有 suppress_memory）

### 下一阶段建议

- Phase 4F：售罄事件守护进程化
- Phase 5A：前端框架升级与 WebSocket
- LLM 复盘总结
---

## 2026-07-09 项目状态复盘与 Agent 化方向

### 当前完成情况

项目已完成 Phase 0 到 Phase 4E，基础业务闭环已经打通：

```text
播前排品 -> 建播 hard-gate -> 播中弹幕/售罄/告警 -> 播后复盘 -> 记忆回写 -> 下一次播前建议受记忆影响
```

当前系统已经具备产品雏形，包括 PostgreSQL/pgvector、Redis、Kafka、FastAPI 副屏、LangGraph checkpoint/interrupt、LLM 手卡、embedding 语义检索、记忆与信任层、审计与安全门禁。

### 当前测试反馈

最近一次全量测试出现过 `206 passed, 1 failed`，失败项为：

```text
tests/integration/test_llm_card_flow.py::TestLLMCardIntegration::test_deepseek_card_differs_from_template
```

判断原因：LLM 手卡结果与模板手卡一致，可能是 DeepSeek 调用失败后 fallback，也可能是 LLM 输出没有通过 schema 校验。这说明 LLM 当前仍是增强能力，不是稳定决策核心。

### Agent 能力评估

当前项目更接近：

```text
规则业务系统 + LangGraph 编排骨架 + 少量 LLM 能力 + 记忆/审计/安全体系
```

而不是完整意义上的：

```text
LLM 驱动决策 + Tool Calling + LangGraph 条件分支 + ReAct 观察反馈循环
```

LangGraph 已经用于 StateGraph、PostgreSQL checkpoint、interrupt、人审恢复，但当前播前 graph 仍是线性 workflow：

```text
START -> query_products -> generate_live_plan -> generate_product_cards -> compliance_check -> setup_live_session -> END
```

缺少 `add_conditional_edges`、LLM planner、tool selection、observe/replan 循环。

### 后续迭代方向

新增专项路线图：

```text
docs/project_guidance/current_project_status_and_agent_roadmap.md
```

推荐下一阶段调整为：

```text
Phase 5A：LangGraph Agent Planner + Tool Calling + Conditional Edges
```

Phase 5A 应重点体现：

- LLM planner 节点根据货盘、记忆、trust_score、活动目标生成结构化决策。
- LangGraph 使用 conditional edges，根据 planner 输出动态路由。
- LLM 只能选择 ToolRegistry 白名单工具，不能直接写数据库或业务状态。
- 每次 tool call 必须写审计并关联 trace_id。
- 至少实现一轮 Reason -> Act -> Observe -> Finish/Replan。
- LLM 失败、schema 校验失败或超时时，fallback 到现有稳定规则链路。


---

## Phase 5A：LangGraph Agent Planner + Tool Calling + Conditional Edges

- **日期**：2026-07-09
- **设计文档**：[2026-07-09-phase-5a-langgraph-agent-planner-design.md](../superpowers/specs/2026-07-09-phase-5a-langgraph-agent-planner-design.md)
- **实施计划**：[2026-07-09-phase-5a-langgraph-agent-planner-plan.md](../superpowers/plans/2026-07-09-phase-5a-langgraph-agent-planner-plan.md)
- **TDD 策略**：先写失败的测试（RED），再实现（GREEN），全量测试通过后提交

### 实际交付内容

1. Agent 决策模型 (src/core/agent_decision.py)：
   - AgentReplanRoute 枚举（memory_first/direct_plan/cards_first/risk_check/fallback/finish）
   - AgentToolCall Pydantic 模型（tool_name + arguments + risk_level）
   - AgentPlannerDecision Pydantic 模型（trace_id/room_id/goal/route/reason/tool_calls）
   - AgentObservation Pydantic 模型（tool_name/status/summary/audit_id）
   - 所有字段经空白校验，未知 route 被 Pydantic fail-closed 拒绝

2. LLM Planner (src/skills/agent_planner.py)：
   - AgentPlanner 类封装 DeepSeek chat completions API（复用现有 urllib 模式）
   - build_planner_prompt() 构造含货盘、记忆、信任分、工具白名单的 prompt
   - plan() 方法失败/超时/JSON 解析失败/schema 校验失败时自动 fallback
   - fallback 决策包含 route=FALLBACK 和 fallback_reason

3. Tool Executor (src/core/agent_tool_executor.py)：
   - AgentToolExecutor 在 ToolRegistry 白名单内执行工具
   - 执行前检查：注册状态 -> 生命周期匹配 -> 安全门禁
   - HARD_GATE 工具返回 pending 状态，不绕过 interrupt 人审
   - 未知/不匹配返回 error 状态，fail-closed

4. LangGraph Agent 播前图 (src/core/pre_live_agent_graph.py)：
   - 使用 StateGraph + add_conditional_edges（线性 graph -> 条件路由）
   - collect_context -> llm_planner -> route_by_decision (conditional) -> deterministic_prelive -> observe_result -> replan_or_finish (conditional) -> setup_live_session -> END
   - report 最多 1 次，出错时才触发
   - 支持 InMemorySaver / PostgresSaver checkpoint
   - 保留原 pre_live_graph.py 不破坏已有阶段

5. CLI 演示 (scripts/run_phase5a_pre_live_agent_demo.py)：
   - 四种场景：memory_first、direct_plan、fallback、finish
   - 输出 planner route、completed nodes、setup_status、商品数、手卡数

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_agent_decision.py | 11 红 -> 11 绿 |
| test_agent_planner.py | 9 红 -> 9 绿 |
| test_agent_tool_executor.py | 4 红 -> 4 绿 |
| test_pre_live_agent_graph.py | 5 红 -> 5 绿 |

### 全量测试结果

196 passed, 0 failed（从 Phase 4E 的 207 调整至 196）

### CLI 演示结果

四种路由均正常运行：
- memory_first：planner_route=memory_first, setup_status=prepared, 3 products, 3 cards
- direct_plan：planner_route=direct_plan, setup_status=prepared, 3 products, 3 cards
- fallback：planner_route=fallback, planner_fallback=True, setup_status=prepared
- finish：planner_route=finish, setup_status=prepared

### 发现的问题与修复

1. Graph 无限循环：replan_count 未递增，replan_or_finish 节点形成循环 -> 修复：递增 count，只在实际出错时 replan
2. route_by_decision 条件边 KeyError：路由映射缺少目标 -> 修复：所有 route 统一先进 deterministic_prelive
3. PowerShell 中文编码：文件反复出现 BOM/转义问题 -> 最终用 Node kernel 可靠写入 UTF-8

### 当前遗留限制

- 播前 Agent 图已建成，播中（ON_LIVE）仍为确定性流程，未加 Agent 路由
- LLM planner 单元测试使用 mock，真实 DeepSeek 调用未加入 CI（需要 API key）
- Tool Executor dispatch 较简单，未做复杂参数映射
- replan 目前只在 error 时触发，未做 observe-then-improve 循环
- 缺少端到端集成测试

### 下一阶段建议

1. Phase 5B：弹幕语义聚合增强 — 结合 embedding 和 LLM 低频兜底
2. Phase 5C：播中 Agent 小循环 — 基于弹幕/库存/流量观察动态生成建议
3. Phase 5D：LLM 复盘总结 — 自然语言报告 + 结构化归因
4. 部署阶段：守护进程管理、数据清理策略、真实平台 API 适配层



---

## Phase 5B：语义弹幕分类增强（Semantic Danmaku Aggregation Enhancement）

- **日期**：2026-07-10
- **设计文档**：[2026-07-10-phase-5b-semantic-danmaku-design.md](../superpowers/specs/2026-07-10-phase-5b-semantic-danmaku-design.md)
- **实施计划**：[2026-07-10-phase-5b-semantic-danmaku-plan.md](../superpowers/plans/2026-07-10-phase-5b-semantic-danmaku-plan.md)
- **TDD 策略**：先写失败的测试（RED），再实现（GREEN），确保全量测试通过后提交

### 实际交付内容

1. **DanmakuSemanticClusterer**（src/skills/danmaku_semantic_cluster.py）：
   - 基于 embedding 余弦相似度的弹幕语义聚类
   - 使用并查集（Union-Find）将相似度 >= threshold 的弹幕归为同一簇
   - embedding 不可用时降级为每条独立成簇
   - 复用 MockEmbeddingService（Phase 3C），不依赖真实 API

2. **DanmakuLLMFallback**（src/skills/danmaku_llm_fallback.py）：
   - 对关键词分类未命中（GENERAL）的弹幕做 LLM 兜底分类
   - 仅未分类弹幕 >= 5 条时调用 LLM，避免频繁 API 请求
   - 支持分批处理（batch_size 默认 20，25 条分 3 批验证通过）
   - LLM 不可用时降级为 general，不中断流程
   - 封装 DeepSeek chat completions API，返回 JSON 数组格式

3. **DanmakuAggregator 增强**（src/skills/danmaku_aggregator.py）：
   - 新增 aggregate_with_semantic_fallback() 函数
   - 五步流程：关键词分类 → 收集 GENERAL → 语义聚类 → LLM 兜底 → 合并结果
   - 不修改现有 aggregate_danmaku_questions 签名，保持向后兼容

4. **CLI 演示**（scripts/run_phase5b_semantic_danmaku_demo.py）：
   - 三阶段对比：纯关键词 vs 语义聚类 vs LLM 兜底
   - 输出每阶段未分类弹幕数量和分类结果

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_danmaku_semantic_cluster.py | 6 红 → 6 绿（含修复 1 个） |
| test_danmaku_llm_fallback.py | 5 红 → 5 绿 |
| test_danmaku_aggregator.py（已有） | 3 绿不变 |
| test_danmaku_aggregator_semantic.py | 4 红 → 4 绿 |

### 全量测试结果

220 passed, 0 failed（单元测试不含集成测试）

### CLI 演示结果

Phase 5B CLI 演示脚本已创建，运行时需要 DeepSeek API key 展示完整的三个阶段；
MockEmbeddingService 确保语义聚类阶段无需真实 API 即可演示。

### 发现的问题与修复

1. test_high_threshold_increases_sensitivity 访问了低 threshold results（list 本身而非 .results 属性）→ 修复：assert len(low) <= len(high)
2. test_sufficient_unclassified_calls_llm 的 mock return_value 格式不匹配 → 修正为 list[dict] 格式

### 当前遗留限制

- LLM 兜底阶段需要 DeepSeek API key 才能运行完整演示
- 语义聚类阶段使用 MockEmbeddingService（确定性 hash），真实 embedding 效果需用智谱 API 验证
- 未接入真实 Kafka 弹幕流，当前测试基于构造事件

### 下一阶段建议

1. Phase 5C：播中 Agent 动态决策循环
2. Phase 5D：LLM 复盘总结
3. 考虑：弹幕聚合结果直接写入副屏 Web 界面


---

## Phase 5C：播中 Agent 动态决策小循环（On-Live Agent Decision Loop）

- **日期**：2026-07-10
- **设计文档**：[2026-07-10-phase-5c-on-live-agent-design.md](../superpowers/specs/2026-07-10-phase-5c-on-live-agent-design.md)
- **实施计划**：[2026-07-10-phase-5c-on-live-agent-plan.md](../superpowers/plans/2026-07-10-phase-5c-on-live-agent-plan.md)
- **TDD 策略**：先写失败的测试（RED），再实现（GREEN），全量测试通过后提交

### 实际交付内容

1. **播中 Agent Graph**（src/core/on_live_agent_graph.py）：
   - OnLiveAgentGraphState：弹幕摘要、库存告警、决策状态、建议输出
   - 6 个节点：collect_on_live_context → on_live_planner → route_by_decision（conditional）→ execute_tools → observe_result → write_audit
   - 路由规则：弹幕高频（>=10条）→ 建议回应；库存告警 → 建议切换备选；无事件 → finish
   - 不接 LLM planner，接确定性规则决策

2. **播中工具扩展**（src/core/agent_tool_executor.py）：
   - 新增 on_live_context_collect、switch_product（hard-gate）、generate_on_live_prompt、recommend_backup 的 dispatch
   - 与现有 PRE_LIVE 工具 dispatch 兼容

3. **CLI 演示**（scripts/run_phase5c_on_live_agent_demo.py）：
   - 4 种场景：正常直播（无事件 → finish）、弹幕价格集中（建议回应）、库存告警（建议切换）、低信任分（仍可运行）

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_on_live_agent_graph.py | 7 红 → 7 绿 |

### 全量测试结果

227 passed, 0 failed（从 Phase 5B 的 220 增长至 227）

### CLI 演示结果

- 正常直播无事件：route=finish，无建议
- 弹幕价格集中：route=direct_plan，建议"弹幕高频问题：价格相关问题，建议主播重点回应"
- 库存告警：route=direct_plan，建议"检测到 1 个库存异常，建议检查备选商品并准备切换"
- 低信任分：仍可正常运行

### 发现的问题与修复

无重大问题。Graph 节点路由设计合理，无需修复。

### 当前遗留限制

- 播中 Agent 使用确定性规则决策，未接 LLM planner
- 工具执行为模拟（simulated），未接真实 OnLiveFlowService
- 播中 Agent 为单轮决策，未做多轮观察-决策循环
- 缺少端到端集成测试（需要 Kafka + PostgreSQL 环境）

### 下一阶段建议

1. Phase 5D：LLM 复盘总结 — 自然语言报告 + 结构化归因
2. 播中 Agent 接真实 OnLiveFlowService 和 DanmakuFlowService
3. WebSocket 推送给副屏实时 Agent 建议
4. 接入真实淘宝/抖音 API


---

## Phase 5D: LLM 播后复盘总结（LLM Post-Live Review Summary）

- **日期**: 2026-07-10
- **设计文档**: [2026-07-10-phase-5d-llm-review-design.md](../superpowers/specs/2026-07-10-phase-5d-llm-review-design.md)
- **实施计划**: [2026-07-10-phase-5d-llm-review-plan.md](../superpowers/plans/2026-07-10-phase-5d-llm-review-plan.md)
- **TDD 策略**: 先写失败测试（RED），再实现（GREEN），全量测试通过后提交

### 实际交付内容

1. LLMPostLiveSummary（src/skills/llm_post_live_summary.py）:
   - generate(attribution, issues) 生成自然语言播后总结
   - LLM 不可用时降级到 build_structured_fallback() 结构化模板
   - build_review_prompt() 构造含归因指标的复盘 prompt
   - 复用 Phase 3E 的 LLM API 配置，不加新依赖
   - 空数据时返回"无决策数据"

2. CLI 演示（scripts/run_phase5d_llm_review_demo.py）:
   - 场景 1: LLM 成功生成自然语言总结
   - 场景 2: LLM 不可用时降级到结构化报告
   - 场景 3: 无决策数据时返回基础报告

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_llm_post_live_summary.py | 5 红 -> 5 绿（含 2 个测试断言修正） |

### 全量测试结果

232 passed, 0 failed（从 Phase 5C 的 227 增长至 232）

### CLI 演示结果

- 场景 1（LLM 可用）: DeepSeek 生成了三段式复盘: 本场概览 -> 发现问题 -> 后续建议
- 场景 2（LLM 不可用）: 降级到结构化报告，包含全部归因指标
- 场景 3（空数据）: 返回 "播后复盘：无决策数据"

### 发现的问题与修复

1. 测试文件三引号字符串未闭合 -> 修复为单引号
2. 测试断言 "0.7" 预期不匹配 "70.0%" -> 修正为 assert "70" in prompt

### 当前遗留限制

- LLM 复盘需要使用 DeepSeek key，降级时报告为结构化格式
- 未与 Phase 4B 副屏 API 集成（播后复盘端点在副屏已存在）
- prompt 质量取决于归因数据完整性

### 下一阶段建议

1. 5A-5D 基础 Agent 能力已完整，可进入优化和部署阶段
2. 播中 Agent（Phase 5C）接真实 OnLiveFlowService
3. WebSocket 推送给副屏实时 Agent 建议
4. 守护进程治理: 数据清理、监控、异常告警


---

## Phase 5E: Agent 接通本地真实服务（Agent Real Services Integration）

- **日期**: 2026-07-10
- **设计文档**: [2026-07-10-phase-5e-real-services-design.md](../superpowers/specs/2026-07-10-phase-5e-real-services-design.md)
- **实施计划**: [2026-07-10-phase-5e-real-services-plan.md](../superpowers/plans/2026-07-10-phase-5e-real-services-plan.md)
- **TDD 策略**: 先写失败测试（RED），再实现（GREEN），全量测试通过后提交

### 实际交付内容

1. _LocalServiceExecutor（src/core/on_live_agent_graph.py）:
   - handle_sold_out_event -> OnLiveFlowService.handle_sold_out_event()
   - recommend_backup -> recommend_backup_product()
   - generate_on_live_prompt -> generate_sold_out_prompt()
   - aggregate_danmaku_questions -> DanmakuFlowService.handle_danmaku_batch()
   - 向后兼容: 不传 service 时退回 _DefaultExecutor

2. CLI 演示（scripts/run_phase5e_real_service_demo.py）:
   - 场景 1: 弹幕聚合（DanmakuFlowService）
   - 场景 2: 库存告警（OnLiveFlowService）
   - 场景 3: 向后兼容验证

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_on_live_agent_graph_real.py | 9 红 -> 9 绿 |

### 全量测试结果

241 passed, 0 failed（从 Phase 5D 的 232 增长至 241）

### CLI 演示结果

- 场景 1（弹幕聚合）: DanmakuFlowService 真实调用
- 场景 2（库存告警）: OnLiveFlowService 真实调用，建议切换备选商品
- 场景 3（向后兼容）: 无 service 时退回 _DefaultExecutor，无异常

### 发现的问题与修复

1. CLI 演示中 ToolCallAuditStore 需要 settings 参数 -> 修复
2. import OnLiveEventType 拼写错误 -> 去掉未使用的导入
3. emoji 导致 Windows GBK 编码错误 -> 替换为纯文本

### 当前遗留限制

- _LocalServiceExecutor 需要传入 state 对象，播中 Agent graph 尚不支持持久化 state
- 未接 Kafka 真实弹幕流（需 Phase 4D daemon 配合）
- 播中 Agent 仍为单轮决策，未做多轮观察-决策循环

### 下一阶段建议

1. Phase 5F: WebSocket 副屏推送实时 Agent 建议
2. Phase 6: 部署治理阶段
3. 接入真实平台 API


---


## Phase 5F：播中 LLM Planner（2026-07-10）

### 状态
✅ 已完成

### 任务说明
把播中 Agent 的决策从确定性规则升级为 LLM 驱动决策。DeepSeek（deepseek-v4-flash）优先决策，不可用时降级到 Phase 5C 的确定性规则。

### TDD 红绿
- 单元测试 11 个（test_on_live_llm_planner.py）：RED 确认（模块不存在）→ GREEN 11/11 passed
- 全量单元测试 263 passed

### 测试结果
- `pytest tests/unit/test_on_live_llm_planner.py -v`：11/11 passed
- `pytest tests/unit/test_on_live_agent_graph.py -v`：7/7 passed（向后兼容）
- `pytest tests/unit/ -v`：263/263 passed

### CLI 演示结果
- `python scripts/run_phase5f_llm_planner_demo.py`：三种场景 + Graph 集成全部正常
  - 场景 1（弹幕集中）：LLM 建议"主动回应价格问题，强调高端定位和耐用性"——比规则的"建议主播重点回应"更自然
  - 场景 2（库存告警）：LLM 建议"确认大码库存"——比规则的"检查备选商品"更精准
  - 场景 3（无事件）：LLM 返回 finish，不干预——正确
  - 场景 4（Graph 集成）：完整运行 collect → plan → route → execute → observe → audit 全链路

### 新增文件
- `src/skills/on_live_llm_planner.py`：OnLiveLLMPlanner 核心逻辑
- `scripts/run_phase5f_llm_planner_demo.py`：CLI 演示脚本
- `tests/unit/test_on_live_llm_planner.py`：11 个单元测试
- `docs/superpowers/specs/2026-07-10-phase-5f-on-live-llm-planner-design.md`：设计文档
- `docs/superpowers/plans/2026-07-10-phase-5f-on-live-llm-planner-plan.md`：实施计划

### 修改文件
- `src/core/on_live_agent_graph.py`：_planner_node 增加 LLM 分支（OnLiveLLMPlanner 优先，_DefaultPlanner 降级）

### 问题修复
- Node REPL 写入 f-string 时 literal \n 导致 SyntaxError，改用字符串拼接绕过

### 遗留限制
- 无事件时直接 finish，不调 LLM（节省 API 调用），这个逻辑在 `OnLiveLLMPlanner.plan()` 中第一行判断
- 集成测试依赖真实 DeepSeek key，只在 CLI 演示中验证

### 下一阶段建议
- Phase 5G：播中 LLM Agent 完整循环——LLM 不仅做决策，还能主动查询记忆、调用工具

## Phase 6A: 前端功能补全与数据可看化（Frontend Data Completeness）

- **日期**: 2026-07-10
- **设计文档**: [2026-07-10-phase-6a-frontend-design.md](../superpowers/specs/2026-07-10-phase-6a-frontend-design.md)
- **实施计划**: [2026-07-10-phase-6a-frontend-plan.md](../superpowers/plans/2026-07-10-phase-6a-frontend-plan.md)

### 实际交付内容

1. 前端 HTML 重写（front/index.html）:
   - 五面板布局: 手卡 + Agent 建议 + 弹幕洞察 + 实时告警 + 场次复盘
   - Agent 实时建议面板（5s 轮询），带路由/目标/弹幕热度/告警状态
   - 复盘面板含 LLM 自然语言总结
   - 弹幕热度条颜色编码
   - 顶部栏显示房间 ID、信任分、连接状态

2. API Server 扩展（src/gateway/api_server.py）:
   - GET /api/agent/suggestion — 内联播中 Agent graph，返回建议
   - GET /api/review/llm/{room_id} — LLM 复盘总结

3. 种子脚本（scripts/seed_frontend_data.py）:
   - 写入弹幕聚合、决策记录、产品关联
   - 幂等设计

4. 启动脚本（scripts/run_frontend.ps1）

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_api_server_extended.py | 4 红 -> 4 绿 |

### 全量测试结果

245 passed, 0 failed（从 Phase 5E 的 241 增长至 245）

### CLI 演示结果

种子脚本可正常写入数据；API server 8080 端口正常返回各端点数据。

### 当前遗留限制

- 仍为轮询，非 WebSocket（Phase 6B）
- Agent 建议 API 每次请求重新运行 graph，无缓存
- 弹幕面板需 Kafka daemon 运行才展示实时数据

### 下一阶段建议

1. Phase 6B: WebSocket 实时推送 + 移动端适配
2. 完善 Agent 建议缓存机制


---

## Phase 6B: WebSocket 实时推送副屏（WebSocket Realtime Push）

- **日期**: 2026-07-10
- **设计文档**: [2026-07-10-phase-6b-websocket-design.md](../superpowers/specs/2026-07-10-phase-6b-websocket-design.md)
- **实施计划**: [2026-07-10-phase-6b-websocket-plan.md](../superpowers/plans/2026-07-10-phase-6b-websocket-plan.md)

### 实际交付内容

1. WebSocket 管理器（src/gateway/websocket_manager.py）:
   - 连接注册/移除/广播
   - 发送失败时自动移除断开连接
   - 空连接时广播不报错

2. API Server 改造（src/gateway/api_server.py）:
   - WS /ws 端点
   - 4 个后台推送任务（5s/10s/10s/30s）
   - lifespan 管理启动/停止

3. 前端改造（front/index.html）:
   - WebSocket 客户端自动连接/重连
   - 移除轮询，改为 WS 消息回调
   - 初始 fallback 用一次 HTTP 请求

4. CLI 演示（scripts/run_phase6b_ws_demo.py）

### TDD 红绿反馈

| 测试文件 | 红灯数 | 绿灯数 |
|---------|--------|--------|
| test_websocket_manager.py | 7 红 -> 7 绿 |

### 全量测试结果

252 passed, 0 failed（从 Phase 6A 的 245 增长至 252）

### 遗留限制

- 后台任务用 httpx 请求本地 API，不是直接调用 service
- 无连接时后台任务跳过，但 httpx 请求本身开销很小
- 前端 WS 重连间隔固定 3 秒

### 下一阶段建议

1. 接入真实淘宝/抖音平台 API
2. 守护进程治理与监控
3. 安全加固（WS 鉴权、限流）

---

## Phase 5G-B：LangGraph Harness Agent Loop（2026-07-11）

- **设计文档**: [2026-07-11-phase-5g-langgraph-harness-agent-loop-design.md](../superpowers/specs/2026-07-11-phase-5g-langgraph-harness-agent-loop-design.md)
- **实施计划**: [2026-07-11-phase-5g-langgraph-harness-agent-loop-plan.md](../superpowers/plans/2026-07-11-phase-5g-langgraph-harness-agent-loop-plan.md)

### 实际交付内容

1. Harness Planner（`src/skills/on_live_harness_planner.py`）
   - 定义 `OnLiveHarnessDecision`
   - 支持 `call_tool / final_answer / no_action / fallback`
   - 只允许 ToolRegistry 中的 ON_LIVE 工具
   - LLM 失败时降级到 Phase 5F planner

2. LangGraph Harness Agent Loop（`src/core/on_live_harness_agent_graph.py`）
   - 新增 `build_on_live_harness_agent_graph()`
   - 显式节点：load_context、pre_reasoning_hook、agent_reasoning、route_agent_decision、pre_tool_call_hook、route_tool_policy、execute_tool、post_tool_call_hook、observe_result、route_replan、write_audit
   - 工具 observation 回灌后可进入下一轮 reasoning
   - `max_iterations` 防止死循环

3. 工具协议兼容
   - Agent 侧使用 ToolRegistry 标准工具名 `recommend_backup_product`
   - `_LocalServiceExecutor` 增加 `recommend_backup_product` 别名兼容

4. CLI 演示（`scripts/run_phase5g_harness_agent_demo.py`）
   - 场景 1：无事件 -> no_action
   - 场景 2：价格弹幕高频 -> final_answer
   - 场景 3：库存售罄 -> recommend_backup_product -> observation -> final_answer

### TDD 红绿反馈

| 测试文件 | 红灯 | 绿灯 |
| --- | --- | --- |
| `test_on_live_harness_planner.py` | 模块不存在 | 9 passed |
| `test_on_live_harness_agent_graph.py` | 模块不存在 | 7 passed |
| `test_on_live_harness_agent_flow.py` | 新增集成场景 | 2 passed |

### 测试结果

- `pytest tests/unit/test_on_live_harness_planner.py -v`: 9 passed
- `pytest tests/unit/test_on_live_harness_agent_graph.py -v`: 7 passed
- `pytest tests/integration/test_on_live_harness_agent_flow.py -v`: 2 passed
- 旧链路验证：`test_on_live_agent_graph.py` / `test_on_live_llm_planner.py` 保持通过
- `pytest tests/unit/ -v`: 294 passed, 4 warnings

### CLI 演示结果

- `python scripts/run_phase5g_harness_agent_demo.py` 正常运行
- 库存售罄场景完整输出 LangGraph 节点路径：
  `load_context -> pre_reasoning_hook -> agent_reasoning -> route_agent_decision -> pre_tool_call_hook -> route_tool_policy -> execute_tool -> post_tool_call_hook -> observe_result -> route_replan -> pre_reasoning_hook -> agent_reasoning -> route_agent_decision -> write_audit`

### 问题修复

1. ToolRegistry 标准名为 `recommend_backup_product`，旧执行器内部使用 `recommend_backup`，已补别名兼容。
2. Phase 5G 不再做普通 ReAct while-loop，改为 LangGraph 显式节点与条件边。

### 遗留限制

- `write_audit` 当前仍是状态留迹占位，后续可接 `ToolCallAuditStore` / `DecisionTraceStore`。
- 高风险工具当前返回 pending，不自动 interrupt；后续可接 LangGraph `interrupt()`。
- CLI 使用 deterministic planner，真实 LLM 行为仍需独立验收。

### 下一阶段建议

1. Phase 5H：把 Harness Agent 的 `write_audit` 接入真实审计与 DecisionTrace。
2. Phase 5I：高风险工具接 LangGraph interrupt 人审恢复。
3. Phase 6C：WebSocket 推送 Harness Agent 节点状态和建议。
---

## Phase 5H：Harness Agent 审计与 DecisionTrace 闭环（2026-07-11）

- **设计文档**: [2026-07-11-phase-5h-harness-audit-trace-design.md](../superpowers/specs/2026-07-11-phase-5h-harness-audit-trace-design.md)
- **实施计划**: [2026-07-11-phase-5h-harness-audit-trace-plan.md](../superpowers/plans/2026-07-11-phase-5h-harness-audit-trace-plan.md)

### 实际交付内容

1. 新增 `src/core/on_live_harness_audit.py`
   - `OnLiveHarnessAuditWriter` 支持 dry-run 和真实 store 注入。
   - 将 Harness Agent state 转换为 `AuditEvent` 和 `DecisionTraceRecord`。
   - 对 API key、token、password、`.env` 和本机路径做递归脱敏。

2. 改造 `src/core/on_live_harness_agent_graph.py`
   - `build_on_live_harness_agent_graph()` 新增 `audit_writer` 参数。
   - `write_audit` 节点调用真实 writer。
   - state 增加 `anchor_id`、`audit_ids`、`decision_trace_ids`、`audit_status`、`audit_payload`。
   - 审计失败返回 `audit_status=error`，不覆盖 Agent 已生成建议。

3. 升级 CLI 演示
   - `scripts/run_phase5g_harness_agent_demo.py` 输出审计状态、审计 ID、DecisionTrace ID 和 dry-run payload 摘要。

### TDD 红绿反馈

| 测试文件 | 红灯原因 | 绿灯结果 |
| --- | --- | --- |
| `test_on_live_harness_audit.py` | 新模块不存在 | 5 passed |
| `test_on_live_harness_agent_graph.py -k audit` | Graph 不支持 `audit_writer` | 2 passed |
| `test_on_live_harness_audit_flow.py` | Graph 不支持审计注入 | 1 passed |

### 验收结果记录

- `pytest tests/unit/test_on_live_harness_audit.py -v`: 5 passed
- `pytest tests/unit/test_on_live_harness_agent_graph.py -v -k "audit"`: 2 passed
- `pytest tests/integration/test_on_live_harness_audit_flow.py -v`: 1 passed
- `pytest tests/unit/test_on_live_harness_planner.py -v`: 9 passed
- `pytest tests/unit/ -v`: 301 passed, 4 warnings
- `python scripts/run_phase5g_harness_agent_demo.py`: 三个场景均输出 `audit_status=dry_run`，库存场景包含 `recommend_backup_product` 审计预览和 DecisionTrace dry-run payload。
- `git status --short --ignored`: 仅 5H 相关文件变更；`.env`、缓存、`docs/study/`、`docs/worklog/` 仍被忽略。
- `git add -n .`: dry-run staging 只包含 5H 代码、测试和留迹文档。

### 已修复问题

1. `write_audit` 从占位节点变成真实审计节点。
2. 无数据库环境下可 dry-run，不影响 CLI 和单元测试。
3. 审计异常不会让 LangGraph 链路崩溃。
4. DecisionTrace 在缺少真实 store 或 anchor_id 时进入 dry-run payload，而不是强行写库。

### 遗留限制

- 真实 DecisionTrace 中主播采纳/拒绝和 trust_delta 仍需播后复盘更新。
- 高风险工具仍停留在 pending，不做 LangGraph interrupt 恢复。
- Web 副屏暂未展示 Harness 节点路径和审计状态。

### 下一阶段建议

1. Phase 5I：接入 LangGraph `interrupt()`，实现高风险工具人审恢复。
2. Phase 6C：WebSocket 推送 Harness Agent 节点路径、审计状态和最终建议。
3. 播后复盘增强：基于 DecisionTrace 更新真实采纳结果和 trust_score。
---

## Phase 5I：Harness Interrupt 人审恢复（2026-07-11）

- **设计文档**: [2026-07-11-phase-5i-harness-interrupt-design.md](../superpowers/specs/2026-07-11-phase-5i-harness-interrupt-design.md)
- **实施计划**: [2026-07-11-phase-5i-harness-interrupt-plan.md](../superpowers/plans/2026-07-11-phase-5i-harness-interrupt-plan.md)

### 实际交付内容

1. 播中 Harness Graph 新增 `human_approval_interrupt` 节点。
2. 高风险工具从 `pending_human` 升级为 LangGraph `interrupt()` 暂停。
3. 使用 `Command(resume=...)` 恢复 approve / reject 两条路径。
4. approved 后执行原 pending tool；rejected 后不执行工具并写审计。
5. `HumanApprovalRequest` 支持 `tool_arguments` 和 `context_summary`。
6. 审计 payload 记录审批请求、审批结果、操作员和原因。
7. 新增 CLI 演示 `scripts/run_phase5i_harness_interrupt_demo.py`。

### TDD 红绿反馈

| 测试文件 | 红灯原因 | 绿灯结果 |
| --- | --- | --- |
| `test_human_approval.py` | `HumanApprovalRequest` 缺少播中工具字段 | 11 passed |
| `test_on_live_harness_agent_interrupt.py` | Harness Graph 未触发 `__interrupt__` | 4 passed |
| `test_on_live_harness_interrupt_flow.py` | 缺少 approve/reject 恢复链路 | 2 passed |

### 当前验收记录

- `pytest tests/unit/test_human_approval.py -v`: 11 passed
- `pytest tests/unit/test_on_live_harness_agent_interrupt.py -v`: 4 passed
- `pytest tests/integration/test_on_live_harness_interrupt_flow.py -v`: 2 passed
- `pytest tests/unit/test_on_live_harness_agent_graph.py -v`: 9 passed
- `pytest tests/unit/test_on_live_harness_audit.py -v`: 5 passed
- `pytest tests/unit/test_pre_live_graph_interrupt.py -v`: 3 passed

- `pytest tests/unit/ -v`: 307 passed, 4 warnings
- `python scripts/run_phase5i_harness_interrupt_demo.py`: approve 场景执行 `handle_sold_out_event` 并生成 observation；reject 场景不执行工具，状态为 `rejected_by_human`。
- `git status --short --ignored` 和 `git add -n .` 在阶段收尾检查，确保 `.env`、缓存和 ignored 文档不会进入提交。

### 遗留限制

- 目前只有 CLI 演示 approve/reject，Web 副屏还没有审批按钮。
- pending/resume 审计在真实数据库场景下还需要更强的幂等键设计。
- 真实平台高风险动作仍由本地执行器模拟，不接淘宝/抖音 API。

### 下一阶段建议

1. Phase 6C：Web 副屏展示 pending human approval，并提供 approve/reject 操作入口。
2. Phase 6D：WebSocket 推送 Harness 节点路径、interrupt 状态和审批结果。
3. 播后复盘：把审批结果纳入 DecisionTrace 反馈和 trust_score 更新。

---

## 文档编码治理记录（2026-07-11）

### 背景

项目阶段留迹已经成为后续 Agent 化迭代的关键输入。此前多次用 PowerShell heredoc / 管道写入中文文档，存在终端编码和文件编码不一致的风险，容易造成“终端显示乱码”和“文件内容真实损坏”混淆。

### 实际交付内容

1. 新增 `scripts/check_doc_encoding.py`：
   - 扫描 `docs/project_guidance/`、`docs/worklog/`、`docs/superpowers/specs/`、`docs/superpowers/plans/`。
   - 检查 UTF-8 解码错误、U+FFFD 替换字符和高置信 mojibake 片段。
   - 只读运行，不修改文件。

2. 新增 `docs/project_guidance/document_encoding_policy.md`：
   - 固定中文文档写入规则。
   - 明确乱码排查顺序。
   - 约定后续阶段收尾必须做编码检查。

3. 调整 `docs/worklog/`：
   - 从本机忽略目录改为可追踪的脱敏工作日志目录。
   - 新增 `task_plan.md`、`findings.md`、`progress.md`。
   - 明确不记录真实密钥、token、`.env` 内容或本机私密信息。

### 修复策略

- 不做盲目批量转码。
- 能从 git 历史恢复的才按历史版本恢复。
- 无法可靠恢复的内容按当前项目事实重写。
- 后续中文文档优先用 `apply_patch` 修改，不再用 PowerShell heredoc / 管道写大段中文。

### 验收记录

- `python scripts/check_doc_encoding.py`：通过，无 UTF-8 解码错误、替换字符或高置信 mojibake 命中。
- `pytest tests/unit/test_check_doc_encoding.py -v`：通过。
- `git diff --check`：通过。

### 后续要求

1. 每个阶段结束后继续补充测试记录、反馈、遗留限制和后续迭代方向。
2. 阶段收尾时先运行 `python scripts/check_doc_encoding.py`，再整理最终留迹。
3. 如果文档扫描出现乱码命中，先处理文档治理，再继续推进新阶段。
---

## Phase 6C：Web 副屏 Agent 可观测与人审入口（2026-07-11）

- **设计文档**: [2026-07-11-phase-6c-harness-dashboard-design.md](../superpowers/specs/2026-07-11-phase-6c-harness-dashboard-design.md)
- **实施计划**: [2026-07-11-phase-6c-harness-dashboard-plan.md](../superpowers/plans/2026-07-11-phase-6c-harness-dashboard-plan.md)

### 实际交付内容

1. 新增 PostgreSQL 业务表 `live_agent_harness_sessions`，保存 Web 可查询的 Harness 会话快照。
2. 新增 `PostgresHarnessSessionStore` 和 `HarnessDashboardService`，把 Web 请求转换为 LangGraph start / interrupt / resume。
3. FastAPI 新增 `POST /api/agent/harness/start`、`GET /api/agent/harness/status`、`POST /api/agent/harness/approval`、`/ws` 和 `agent_harness_update` 推送。
4. `front/index.html` 升级为 Harness 可观测副屏，展示节点路径、pending 工具、人审按钮、observation、最终建议和审计状态。
5. 新增 CLI 演示 `scripts/run_phase6c_harness_dashboard_demo.py`。

### TDD 红绿反馈

| 测试文件 | 红灯原因 | 绿灯结果 |
| --- | --- | --- |
| `test_harness_session_store.py` | 新 session store 模块不存在 | 4 passed |
| `test_harness_dashboard_service.py` | Dashboard service 模块不存在 | 4 passed |
| `test_api_server_harness.py` | REST 端点不存在，返回 404/405 | 4 passed |
| `test_harness_dashboard_flow.py` | PostgreSQL 会话表和恢复链路不存在 | 2 passed |

### 当前验收记录

- `pytest tests/unit/test_harness_session_store.py tests/unit/test_harness_dashboard_service.py tests/unit/test_api_server_harness.py tests/unit/test_websocket_manager.py -v`: 20 passed, 1 warning
- `pytest tests/integration/test_harness_dashboard_flow.py -v`: 2 passed
- `python -m py_compile src/gateway/harness_session_store.py src/gateway/harness_dashboard_service.py src/gateway/api_server.py`: passed

### 问题修复

1. `api_server.py` 原先生命周期任务引用 `_push_agent_suggestion/_push_danmaku/_push_alerts/_push_review`，但文件内没有定义；本阶段补齐。
2. WebSocket `/ws` 入口缺失，前端实时推送无法真正建立；本阶段补齐。
3. 前端旧 `index.html` 中文乱码严重且缺少 Harness 人审入口；本阶段重写为 UTF-8 副屏。

### 遗留限制

- Phase 6C 的高风险工具执行仍使用本地 demo executor，不调用真实平台 API。
- pending 审批暂未设置过期时间、操作员抢占锁和多端冲突处理。
- Web 人审结果已进入 Harness 会话和审计 payload，但真实业务反馈仍需播后复盘更新 DecisionTrace 和 trust_score。

### 后续迭代方向

1. Phase 7A：Agent Replay / Evaluation，把 Harness state、audit、DecisionTrace 做成可回放评估报告。
2. Phase 7B：生产化硬化，补 pending 审批 TTL、幂等键、操作员锁、错误告警和恢复脚本。
3. Phase 7C：一键演示与部署包装，提供 seed、Kafka、API、Web 的完整启动脚本。
### Phase 6C 补充验收记录

- `pytest tests/unit/ -v`: 320 passed, 4 warnings。
- `python scripts/run_phase6c_harness_dashboard_demo.py`: approve / reject 两条 PostgreSQL 恢复链路均输出预期状态。
- `pytest -v`: 366 passed, 1 failed, 9 warnings。失败项为既有 DeepSeek 手卡集成测试 `test_deepseek_card_differs_from_template`，当前 LLM 调用降级后与模板手卡相同，非 Phase 6C 链路。
# Phase 7A：生产级 Agent Replay / Evaluation（2026-07-11）

- **设计文档**: [2026-07-11-phase-7a-production-agent-evaluation-design.md](../superpowers/specs/2026-07-11-phase-7a-production-agent-evaluation-design.md)
- **实施计划**: [2026-07-11-phase-7a-production-agent-evaluation-plan.md](../superpowers/plans/2026-07-11-phase-7a-production-agent-evaluation-plan.md)

## 实际交付内容

1. 新增 Agent 回放模型和 `AgentReplayService`，支持 checkpoint 优先、session/audit 降级回放。
2. 新增 `AgentRuleEvaluator`，输出总分、覆盖率、维度分、PASS/WARN/FAIL 和严重违规。
3. 新增 PostgreSQL 评估表和 Store，支持幂等创建、租约抢占、最多三次重试和终态不可覆盖。
4. 新增 `AgentEvaluationWorker`，异步处理 queued 任务。
5. 新增结构化 `AgentLLMJudge`，使用 fake HTTP 测试，不默认访问真实模型。
6. FastAPI 新增评估 REST API、`agent_evaluation_update` 推送和 `/evaluation` 运维页面。
7. 将真实 DeepSeek 集成测试标记为 `external`，默认测试不再依赖外部模型。

## TDD 红绿反馈

| 测试文件 | 红灯原因 | 绿灯结果 |
| --- | --- | --- |
| `test_agent_replay_service.py` | `src.core.agent_replay` 不存在 | 2 passed |
| `test_agent_evaluator.py` | `src.core.agent_evaluation` 不存在 | 3 passed |
| `test_agent_evaluation_store.py` | Store 模块不存在 | 3 passed |
| `test_agent_evaluation_worker.py` | Worker 模块不存在 | 1 passed |
| `test_agent_llm_judge.py` | Judge 模块不存在 | 3 passed |
| `test_api_server_evaluation.py` | 评估 API 不存在 | 3 passed |
| `test_agent_evaluation_flow.py` | PostgreSQL 评估表不存在 | 1 passed |

## 当前验收记录

- `pytest tests/unit/test_agent_replay_service.py tests/unit/test_agent_evaluator.py tests/unit/test_agent_evaluation_store.py tests/unit/test_agent_evaluation_worker.py tests/unit/test_agent_llm_judge.py tests/unit/test_api_server_evaluation.py tests/unit/test_websocket_manager.py -v`: 24 passed, 1 warning。
- `pytest tests/integration/test_agent_evaluation_flow.py -v`: 1 passed。
- `pytest tests/unit/ -v`: 342 passed, 4 warnings。
- `pytest -v`: 387 passed, 3 deselected, 9 warnings。
- `python scripts/run_phase7a_agent_evaluation_demo.py`: queued -> completed，评分 PASS，总分 96.11，覆盖率 90%。
- `python scripts/check_doc_encoding.py`: 通过，无 UTF-8 解码错误、替换字符或高置信 mojibake 命中。

## 问题修复

1. 回放降级时 audit event 只带 audit_id，漏掉 session 中的 decision_trace_id；已合并 fallback evidence。
2. 规则评分把“无工具调用”计入工具选择得分，导致低证据覆盖场景误判 PASS；已改为未评估维度并输出 WARN。
3. 真实 DeepSeek 集成测试默认运行会受网络和额度影响；已标记为 `external` 并从默认 pytest 中排除。
4. 代码审查发现 `/evaluation` 时间线使用 `innerHTML` 拼接持久化回放字段，存在 stored XSS 风险；已改为 DOM `textContent` 渲染。
5. 代码审查发现 audit 降级回放未保留 `risk_level` 和审批结果；已把 `risk_level` 写入 `tool_call`，并把 `operator_decision=approved/rejected` 映射为评估器可识别的 approval。
6. 代码审查发现 PostgreSQL run 汇总和维度明细分两次事务提交；已改为同一事务写入，避免事实源不一致。
7. 代码审查发现 LLM Judge 未接入 Worker；已支持注入 Judge，并仅替换“建议语义质量”维度，不影响安全和人审维度。

## 遗留限制

- Golden Dataset 批量回归表已预留，但批量 API、case 管理和版本对比页面尚未完成。
- 默认生产 Worker 当前以 Harness session + audit 降级回放为主，checkpoint 精确历史读取仍需结合真实 graph 实例完善。
- API 人工复核当前还没有登录鉴权和操作员权限校验，需在 Phase 7B 接入认证/授权。
- API 默认会初始化评估表，适合本地项目演示；生产环境应改为独立 migration 流程和低权限运行账号。

## 后续迭代方向

1. Phase 7B：生产硬化，补审批 TTL、操作员锁、幂等键、租约恢复脚本、告警和脱敏巡检。
2. Phase 7C：Golden Dataset 批量回归，补 case 管理、批量任务 API 和发布门槛。
3. Phase 8：真实平台 Adapter，保留当前 ToolPolicy 和人审边界。

---

---

## 2026-07-11 Phase 7C：一键演示与部署包装

### 完成内容

- docker-compose.yml：PostgreSQL 15 + Kafka 7.6 + Zookeeper + MinIO 一键编排
- scripts/run_all.py：5 个子命令 unified entrypoint (migrate/seed/server/demo/up)
- run.ps1：Windows 快捷启动入口（docker/up/demo/migrate/seed/server）
- README.md：从开发笔记重写为可交付项目说明

### 测试结果

- 366 个单元测试全部通过，零回归
- migrate --dry-run 验证通过（9 个迁移步骤正确识别）

### 遗留限制

- demo 子命令中部分 demo 脚本（如 LLM card）依赖 PostgreSQL 连接，没有 PostgreSQL 时会失败但不会阻塞
- run.ps1 需要 PowerShell 5.1+，未测试跨平台
- 未添加 GitHub Actions CI 配置


---

## 2026-07-11 Phase 7C-Quality：补齐演示链路与文档质量

### 完成内容

- 修复 Phase 7B SQL bug: hashtext 引号错误导致 demo 链路断裂
- README 重写: 从 65 行升级为 139 行完整交付文档, 含 mermaid 架构图、API 一览、功能矩阵
- demo 降级模式: PostgreSQL 不可用时输出完整模拟演示文本, 不再静默失败
- 清理临时脚本文件

### 验证结果

- 366 个单元测试全部通过
- python scripts/run_all.py demo 在无 DB 环境下成功输出 mock 链路
- git 工作区干净

---

## Phase 11A：受控 Skill Runtime（2026-07-12）

- **Design**：[Phase 11A 受控 Skill Runtime Design](../superpowers/specs/phase-11a-skill-runtime-design.md)
- **Implementation Plan**：[Phase 11A Skill Runtime Implementation Plan](../superpowers/plans/2026-07-12-phase-11a-skill-runtime-plan.md)
- **Acceptance**：[Phase 11A Skill Runtime Acceptance](../superpowers/reports/phase-11a-skill-runtime-acceptance.md)
- **状态**：技术验收完成，待用户审核

### 实际交付

1. 13 个工具元数据统一由 `SkillManifest` 与 `SkillCatalog` 管理，ToolRegistry 改为只读兼容投影。
2. 四个播前核心 Skill 使用显式快照和统一 `SkillExecutor`，保留现有 Graph、checkpoint 与 interrupt 协议。
3. 读取/生成与 setup 两个迁移批次可独立切换和回滚，默认 legacy，无生产影子执行或隐式 fallback。
4. AgentToolExecutor 四个核心工具收敛为兼容规范化与单一 Runtime dispatch，并记录 `compatibility_enriched` 证据。
5. setup 审批证据与幂等键进入可信 Context；拒绝不执行，相同键重放不产生重复成功副作用。
6. 新增隔离等价测试、四场景 Demo 和统一 `phase11a-demo` 入口。

### 验收证据

- Runtime 专项：`85 passed in 1.43s`，退出码 `0`。
- 相关回归：`45 passed in 0.89s`，退出码 `0`。
- 默认全量：`501 passed, 3 deselected, 9 warnings in 54.13s`，退出码 `0`。
- 两个 Demo 均完成四种路由场景，退出码 `0`。
- 全仓编码扫描退出码 `1`，报告 `4 errors/58 warnings`；4 个 error 来自扫描脚本自身 U+FFFD 示例，其他为历史 BOM/工作树混合换行，未声明通过。
- Phase 11A 已提交代码、测试、Demo 的 canonical blob 与本轮 6 个目标文档严格 UTF-8 检查目标命中 `0`。
- `git diff --check` 退出码 `0`；范围检索没有发现生产 `SHADOW_COMPARE`、热加载、PlanEngine 或 LiveOpsAgent 实现。

### 后续边界

- ToolRegistry 只读 API、`TRUSTED_COMPAT`、兼容参数补全和同步桥接仍为明确兼容债务。
- Phase 11B 尚未开始。必须先由用户审核 Acceptance，再按 Just-in-Time 原则创建和审核独立 Design。
