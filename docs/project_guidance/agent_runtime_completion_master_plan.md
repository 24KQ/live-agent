# LiveAgent Agent Runtime Phase-Gated 总控计划

文档状态：`IN_PROGRESS`

最后更新：2026-07-16

当前授权边界：Phase 12B 已完成技术验收；Phase 13 Design/Plan 已完成 Just-in-Time
审核并获用户授权，Task 1-6 已完成技术门禁，Task 7-12 按技术门禁连续实施。

## 1. 文档职责

本文是 Phase 12B 至 Phase 14 最终验收的总控事实源，负责回答：

- 当前完成到哪里，下一任务是什么。
- 各阶段为什么存在、依赖什么、通过什么门禁。
- 哪些结果由测试确定，哪些结果必须由评估数据条件化决定。
- 上下文压缩、中断或新会话如何恢复。

阶段级接口、状态机和验收场景以对应 Design 为准；文件级 TDD 步骤、测试命令和提交边界以对应 Implementation Plan 为准；实际结果以 Acceptance 为准。本文不替代三者。

## 2. 项目定位与固定原则

LiveAgent 面向淘宝直播播前、播中、播后三个场景，目标是建设可控、可恢复、可评估的 Agent Runtime，而不是机械增加 Agent 数量。

- Tool 表达底层动作和外部副作用。
- Skill 表达可版本化、可审计、可门禁的业务能力。
- PlanEngine 负责确定性 DAG 校验、调度、恢复和 Replan。
- Agent 只在存在真实决策空间时作为受限决策者接受评估。
- Orchestrator 默认是确定性协调器，不包装成 Agent。
- 严重安全违规不能由平均分、LLM Judge 或业务收益抵消。

## 3. 当前实现基线

最新业务提交为 `94ad80b feat: add bounded specialist runner`，已完成：

- Phase 11A Skill Runtime 和用户验收。
- Phase 11B 统一执行与平台契约和用户验收。
- Phase 12A Task 1-9 与 Phase 12A Acceptance。
- checkpoint 对账、播前 Graph 手卡路由、`TRUSTED_COMPAT` 退役和五场景 Demo。
- Phase 12B Task 1-11：事件契约、Inbox、Kafka、冻结、紧急 DAG、CAS、对账、Replan、
  Harness Evidence、业务闭环 Demo 与 Acceptance。

当前 Phase 13 Task 1-6 已完成技术门禁，下一执行项为 Task 7 LiveOpsAgent；Phase 14 尚未审核实施基线。

## 4. 阶段依赖与自动门禁

当前 Gate 与未来 Phase 内依赖顺序固定为：

```text
Phase 13 Design/Plan REVIEWED
-> IMPLEMENTATION_AUTHORIZED
-> Task 1-5 COMPLETE
-> Task 6-12 CONTINUOUS_EXECUTION
-> Phase 13 Acceptance
-> AWAITING_PHASE_14_GATE
```

Phase 13 Gate 已读取 Phase 12B Acceptance、预算、风险和讨论基线，并修订为 12 个
可执行 Task；用户已明确授权连续实施。Phase 13 Acceptance 后仍必须停止在 Phase 14 Gate。
以下情况必须暂停：

- 严重安全门禁失败且无法在当前设计内修复。
- 真实模型累计费用将超过 3 元人民币。
- PostgreSQL、PostgresSaver、Kafka 或模型服务持续不可用，导致强制验收证据无法生成。
- 需要扩大到真实淘宝 API、外部插件、热加载、前端控制台或新增 HTTP 管理接口。
- 需要降低 Agent 保留门槛、绕过失败测试或提交已知失败代码。

## 5. Phase 12A 剩余范围

### 5.1 Task 6：Checkpoint 一致性与人工命令恢复

- `plan_runs` 增加 `reconciliation_required`、结构化 `reconciliation_failure`、`reconciliation_signature`、恢复次数和最近恢复时间。
- checkpoint 只保存 `plan_run_id`、`plan_version` 和 `CARD_BATCH_SUCCEEDED | CARD_BATCH_FAILED`。
- PlanStore 领先时复用已成功 NodeRun，不再次调用 Skill，并记录 replay reuse 摘要。
- checkpoint 领先时记录 `INTERNAL_INVARIANT`、冻结计划并拒绝普通命令；不信任 checkpoint 回填，不补造 NodeRun。
- 启动、每 30 秒和命令执行前三类入口复用同一幂等 Reconciliation Service。

### 5.2 Task 7：播前 Graph 局部路由

- 新增启动冻结的 `LEGACY | PLAN_ENGINE` 手卡执行路由，默认 `LEGACY`。
- 只替换 `generate_product_cards`，不接管商品查询、排品、合规或建播。
- Graph 只保存 PlanRun 最小引用与最终手卡快照。
- PlanEngine 失败时不得在同次调用回退 Legacy。

### 5.3 Task 8：可信审批兼容退役

- 删除 `TRUSTED_COMPAT` 枚举、构造 token 和内部工厂。
- `confirmed_setup=True` 不再被升级为审批证据。
- Runtime 建播只接受真实 `HUMAN_INTERRUPT`；无证据时保持 `pending`。
- Legacy 显式回滚仍可保持旧路径，但不能把 Legacy 参数转换成 Runtime 权限。

### 5.4 Task 9：验收

- 五场景无外部依赖 Demo。
- 真实 PostgreSQL 和官方 PostgresSaver 一致性测试。
- Phase 12A 专项、相关回归和默认全量测试。
- 生成 Phase 12A Acceptance，记录精确命令、结果、设计偏差和 Phase 12B 进入证据。

## 6. Phase 12B：抢占与增量 Replan

阶段目标是证明“批量手卡生成期间发生售罄”可以被可靠接收、局部冻结、优先处理并最小重算。

固定边界：

- PostgreSQL Event Inbox 是事件权威源，Kafka 只是传输 Adapter。
- Kafka 先持久化 Inbox，再提交 offset。
- 同一 `event_id + payload_digest` 幂等；同 ID 不同摘要保留冲突 occurrence、冻结受影响计划并提交 offset，避免毒消息阻塞分区。
- 事件 payload 不能自行声明可信。Ingress Trust Profile 在启动时冻结，只有验证后的 provenance 才能构造 `EventAuthorizationContext`。
- PRODUCT 事件只冻结依赖闭包，ROOM/PLATFORM 才冻结整张计划。
- 在途 NodeRun 允许协作式闭合；受影响旧版本结果保存为 superseded 证据，不进入新版本汇总。
- 紧急 child PlanRun 优先级为 100，普通计划为 0。
- `handle_sold_out_event` 升级为单活 `2.0.0`，只执行 `product_id + expected_version` 的 CAS 售罄写。
- 紧急 DAG 固定为验证、CAS 售罄、备选推荐、主播提示和汇总。
- `SIDE_EFFECT_UNKNOWN` 只能通过严格只读事实对账确认；不能证明时等待人工，禁止盲重发。
- child 成功后在 root Replan 锁内合并当前待应用事件，创建不可变新 PlanVersion。
- 未受影响成功节点以 `reused_from_node_id` 引用旧结果，不复制或伪造 NodeRun。
- 每个 root 最多创建两个新版本；重复 `failure_signature + input_fingerprint` 立即停止循环。
- 可信库存事件只由 PlanEngine 执行写操作，Harness 只消费结果证据。

## 6.1 跨 Phase 业务闭环

三场景回放的固定事实源为
[业务闭环回放轨道](./agent_runtime_business_closed_loop_track.md)。它不增加真实平台或 UI
范围；Phase 12B Task 11 交付主 Trace，Phase 13 交付条件化 Agent 附录，Phase 14 将该
场景纳入 Golden 与 Release 证据。

## 7. Phase 13：三场景 Specialist Agent 评估

Phase 13 采用共享评估内核后按 LiveOpsAgent、PlannerAgent、ReviewMemoryAgent 三个纵向切片独立评估，最终允许保留 0 至 3 个新增 Specialist；现有播中 Agent Harness 不计入该数量。

共同约束：

- 共用 `BoundedSpecialistRunner`、`AgentTask`、`AgentAction`、`AgentResult` 和 `EvidenceRef`。
- 模型只产生结构化动作和简短 `reason_summary`，不要求或持久化 chain-of-thought。
- Orchestrator、PlanEngine、SkillExecutor、Hook、授权和发布门禁保持确定性。
- 每个候选使用 20 个 development、40 个 validation 和 20 个 release holdout。
- Agent 使用 `deepseek-v4-flash`、temperature 0；Prompt、Schema、模型、价格表和数据集全部哈希。
- 真实模型总费用硬上限为 3 元人民币；Phase 13 上限 2.40 元，Phase 14 首次 Release 预留 0.60 元。
- LiveOps、Planner、Review 初始额度分别为 0.60、1.00、0.80 元；提前拒绝余额可回到 Phase 13 公共池，但不能使用 Phase 14 预留。
- 严重安全违规必须为 0。
- 每个候选同时满足 Design 固定的绝对质量下限和相对 baseline 提升门。
- validation 每 10 例执行严重违规和数学可达性早停；规则已证明不能通过时为 REJECTED。
- 外部模型、价格、预算或基础设施导致正式证据不足时为 INCONCLUSIVE，不得保留。

候选边界：

- LiveOpsAgent：最多 2 次模型、3 次 Skill、4k tokens、5 秒；只输出四类安全建议动作。
- PlannerAgent：最多 3 次模型、运行时 Skill 0 次、8k tokens、15 秒；只读取冻结商品、记忆和计划证据并提交受限 DAG。
- ReviewMemoryAgent：最多 3 次模型、4 次 Skill、8k tokens、20 秒；Agent 只能 stage candidate。

新增最小受治理能力：

- `retrieve_anchor_memory`
- `collect_post_live_evidence`
- `calculate_post_live_attribution`
- `stage_memory_candidates`

候选记忆只有在两个独立 DecisionTrace、同作用域、无冲突且命中白名单时由确定性模板晋升，并由下一次播前读取验证。候选通过前不创建生产 Profile；RETAINED 后只建立默认关闭路由。统一 Registry 预留多个 Specialist 并存，不实现 Agent 互调。

## 8. Phase 14：Golden Dataset 与发布门禁

- 脱敏 JSONL、Draft 2020-12 Schema、manifest、生成器版本和 SHA-256 进入 Git。
- 运行输出、模型 usage、Judge 证据和成本明细进入 PostgreSQL 与 CI artifact。
- 确定性规则裁决安全、权限、Schema、状态、动作和证据闭合。
- `deepseek-v4-pro` 只评语义质量，不能覆盖严重违规。
- PR：Python 3.12、PostgreSQL 16、ScriptedModel、敏感信息扫描、编码扫描和核心 Runtime branch coverage 至少 90%。
- Nightly：增加真实 Kafka；只有 secret 与 `ENABLE_PAID_NIGHTLY=true` 同时存在时才调用真实模型，默认每次费用上限 0.10 元。
- Release：受保护环境手动触发，运行完整 holdout、Kafka/PostgreSQL 和保留 Agent 的真实模型门禁。
- Artifact 保留期为 PR 14 天、Nightly 30 天、Release 180 天；Release 摘要和全部哈希永久进入 Git。
- Phase 14 删除 ToolRegistry 公共 Facade。
- 显式新路由先通过完整 Release，随后才切换默认值并在新提交上再次运行 Release；两次均通过后，Skill Runtime、手卡 PlanEngine 和售罄 PlanEngine 成为默认路径。启动期显式 Legacy 回滚保留一个兼容周期，同次失败不得 fallback。

## 9. ToolRegistry 与审批兼容收口

- Phase 12B 引入窄化只读 `SkillPolicyView`，逐批迁移 Hook、Policy、Planner、Flow 和 Executor。
- 新增代码不得依赖 ToolRegistry。
- Phase 14 删除 ToolRegistry 公共 Facade 和只针对该 Facade 的兼容测试。
- `TRUSTED_COMPAT` 在 Phase 12A Acceptance 前删除，不能延伸到 PlanEngine 或 Agent 路径。

## 10. 连续执行记录协议

正式实施后，每个 Task 必须更新 `docs/worklog/continuous_execution_state.md`：

1. Task 开始前记录目标、当前 HEAD、计划步骤和禁止事项。
2. 红灯确认后记录命令和预期失败原因。
3. 核心绿灯、审查整改和重大偏差后立即写盘。
4. 测试全绿后更新三个 worklog，与代码一起提交。
5. 推送后更新本地实时状态中的最新提交和下一任务；下一 Task 提交时一并持久化。

允许自主调整文件拆分、辅助类、Fixture、迁移顺序和设计内的缺陷修复。公开接口、Schema、状态机、数据库不变量或安全边界发生变化时，必须先写决策日志。不得自主放宽安全/成本/Agent 门槛或扩大范围。

## 11. Git 与用户工作区约束

- 正式实施沿用 `main`，每个 Task 至少使用一个独立 ASCII commit 并立即推送 `origin/main`；Phase 14 默认路由晋升必须把代码晋升与 Acceptance 留迹拆成两个提交。
- 主模型可按风险使用只读 sub-agent 进行规格、安全、并发与测试审查；主模型独自负责修改、集成、验证、提交和推送。
- 不提交红灯、半成品或已知失败代码。
- 不覆盖、还原或提交用户已有未提交文件。
- 技术门禁通过后只可按实时游标自动进入当前已授权 Phase 的下一 Task；Phase Acceptance
  后状态必须转换为 `AWAITING_PHASE_<N>_GATE`，不得自动进入下一 Phase。

## 12. 完成定义

整条路线只有在以下条件全部满足时结束：

- Phase 12A、12B、13、14 Acceptance 均已生成并满足门禁。
- 三个 Agent 候选均有 `RETAINED`、`REJECTED` 或 `INCONCLUSIVE` 证据结论。
- CI、Golden Dataset、覆盖率和发布报告可重复运行。
- 默认路由已按 Phase 14 决策切换。
- ToolRegistry 公共 Facade 已删除。
- Agent Runtime Final Acceptance 已提交并推送。

最终保留 0 个 Agent 仍可视为成功：这表示评估证明确定性 Orchestrator、PlanEngine 和 Skill Runtime 更适合当前约束，而不是为了项目名称强行保留 Agent。
