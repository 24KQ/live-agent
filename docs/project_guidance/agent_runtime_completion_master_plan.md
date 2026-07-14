# LiveAgent Agent Runtime 连续实施总控计划

文档状态：`IN_PROGRESS`

最后更新：2026-07-15

当前授权边界：Phase 12A-14 正式连续实施已获授权；技术门禁通过后自动进入下一阶段。

## 1. 文档职责

本文是 Phase 12A 剩余任务至 Phase 14 最终验收的总控事实源，负责回答：

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

最新阶段提交为 `d794ff3 feat: add phase 12b event contracts`，已完成：

- Phase 11A Skill Runtime 和用户验收。
- Phase 11B 统一执行与平台契约和用户验收。
- Phase 12A Task 1-9 与 Phase 12A Acceptance。
- checkpoint 对账、播前 Graph 手卡路由、`TRUSTED_COMPAT` 退役和五场景 Demo。
- Phase 12B Task 1：SkillPolicyView、严格事件事实和不可伪造事件授权。

当前进入 Phase 12B Task 2；Phase 13、14 尚未实施。

## 4. 阶段依赖与自动门禁

正式连续实施已经获得授权，依赖顺序固定为：

```text
Phase 12A Task 6-9
-> Phase 12A Acceptance
-> Phase 12B Implementation Plan
-> Phase 12B Acceptance
-> Phase 13 Implementation Plan
-> Phase 13 条件化 Agent 去留结论
-> Phase 14 Implementation Plan
-> Phase 14 Acceptance
-> Agent Runtime Final Acceptance
```

各阶段 Acceptance 的技术门禁全部通过后可以自动进入下一阶段，不再等待逐阶段人工批准。以下情况必须暂停：

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

## 7. Phase 13：三场景 Specialist Agent 评估

三个候选按 LiveOpsAgent、PlannerAgent、ReviewMemoryAgent 顺序独立评估，最终允许保留 0 至 3 个。

共同约束：

- 共用 `BoundedSpecialistRunner`、`AgentTask`、`AgentAction`、`AgentResult` 和 `EvidenceRef`。
- 模型只产生结构化动作和简短 `reason_summary`，不要求或持久化 chain-of-thought。
- Orchestrator、PlanEngine、SkillExecutor、Hook、授权和发布门禁保持确定性。
- 每个候选使用 20 个 development、40 个 validation 和 20 个 release holdout。
- Agent 使用 `deepseek-v4-flash`、temperature 0；Prompt、Schema、模型、价格表和数据集全部哈希。
- 真实模型总费用硬上限为 3 元人民币。
- Phase 13 与本轮 Phase 14 首次 Release 共用 `agent-runtime-completion-v1` 预算账本；Phase 13 已消费金额必须从 Phase 14 可用余额扣除，余额不足时按 `BLOCKED` 暂停，不自动增额。
- 严重安全违规必须为 0。
- 成功率至少提升 5 个百分点，或领域恢复/归因/记忆指标至少提升 10 个百分点。
- 非零基线延迟和 Token 成本增幅不得超过 20%；零 Token 基线使用绝对预算。
- 未跑满 60 个正式样本的候选为 `INCONCLUSIVE`，不得保留。

候选边界：

- LiveOpsAgent：最多 2 次模型调用、3 次 Skill 调用、4k tokens、p95 5 秒、0.01 美元/例；只给安全建议，不重复执行售罄写。
- PlannerAgent：最多 3 次模型调用、5 次 Skill 调用、8k tokens、p95 15 秒、0.02 美元/例；提交受限候选 DAG，由 PlanValidator 注入执行事实。
- ReviewMemoryAgent：最多 3 次模型调用、4 次 Skill 调用、8k tokens、p95 20 秒、0.02 美元/例；候选记忆只进入 staging。

新增最小受治理能力：

- `retrieve_anchor_memory`
- `collect_post_live_evidence`
- `calculate_post_live_attribution`
- `stage_memory_candidates`

候选记忆只有在两个独立 DecisionTrace 支持、无冲突且命中白名单时才自动晋升；其他候选等待人工命令。未通过评估的 Agent 必须删除生产 Profile、Prompt 和接入代码，保留数据集与评估报告。

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
- 不派发子智能体，由主模型直接实施、审查和验证。
- 不提交红灯、半成品或已知失败代码。
- 不覆盖、还原或提交用户已有未提交文件。
- 技术门禁通过后按实时游标自动进入下一 Task，不再恢复到历史的“等待正式实施授权”状态。

## 12. 完成定义

整条路线只有在以下条件全部满足时结束：

- Phase 12A、12B、13、14 Acceptance 均已生成并满足门禁。
- 三个 Agent 候选均有 `RETAINED`、`REJECTED` 或 `INCONCLUSIVE` 证据结论。
- CI、Golden Dataset、覆盖率和发布报告可重复运行。
- 默认路由已按 Phase 14 决策切换。
- ToolRegistry 公共 Facade 已删除。
- Agent Runtime Final Acceptance 已提交并推送。

最终保留 0 个 Agent 仍可视为成功：这表示评估证明确定性 Orchestrator、PlanEngine 和 Skill Runtime 更适合当前约束，而不是为了项目名称强行保留 Agent。
