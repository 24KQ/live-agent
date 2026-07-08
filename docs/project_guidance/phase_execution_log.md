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
- 对应提交：待提交
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
