# LiveAgent Agent Runtime Phase-Gated 总控计划

文档状态：`PHASE_15_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`

最后更新：2026-07-18

当前授权边界：Phase 11A-13 已完成并有 Acceptance；Phase 14 Human-Centered Decision Support Task 1-12 已完成，Acceptance 结论为 `INCONCLUSIVE`；Phase 15 Stage A Design/Plan 已完成持久化，当前等待用户单独授权 Stage B。Stage A 不修改业务代码、数据库、CI 或真实模型。

## 1. 文档职责

本文是 Phase 12B 至 Phase 15 最终验收的总控事实源，负责回答：

- 当前完成到哪里，下一任务是什么。
- 各阶段为什么存在、依赖什么、通过什么门禁。
- 哪些结果由测试确定，哪些结果必须由评估数据条件化决定。
- 上下文压缩、中断或新会话如何恢复。

阶段级接口、状态机和验收场景以对应 Design 为准；文件级 TDD 步骤、测试命令和提交边界以对应 Implementation Plan 为准；实际结果以 Acceptance 为准。本文不替代三者。

## 2. 项目定位与固定原则

LiveAgent 面向淘宝直播播前、播中、播后三个场景，目标是建设人机协同决策支持与受控执行 Runtime，而不是机械增加 Agent 数量或让模型替代运营。

- Tool 表达底层动作和外部副作用。
- Skill 表达可版本化、可审计、可门禁的业务能力。
- PlanEngine 负责确定性 DAG 校验、调度、恢复和 Replan。
- Agent 只在存在真实决策空间时作为受限的证据归纳与方案生成者接受评估。
- Orchestrator 默认是确定性协调器，不包装成 Agent。
- 严重安全违规不能由平均分、LLM Judge 或业务收益抵消。
- 高风险经营决定由运营主控确认；确定性系统可自动执行可信事实的保护动作。

## 3. 当前实现基线

最新已推送业务提交为 Phase 14 Task 12 的 Acceptance；当前仓库事实恢复时必须以 `git log -1 --oneline --decorate` 读取精确 HEAD。已完成：

- Phase 11A Skill Runtime 和用户验收。
- Phase 11B 统一执行与平台契约和用户验收。
- Phase 12A Task 1-9 与 Phase 12A Acceptance。
- checkpoint 对账、播前 Graph 手卡路由、`TRUSTED_COMPAT` 退役和五场景 Demo。
- Phase 12B Task 1-11：事件契约、Inbox、Kafka、冻结、紧急 DAG、CAS、对账、Replan、
  Harness Evidence、业务闭环 Demo 与 Acceptance。

Phase 13 Task 1-12、正式评估和 Acceptance 已完成：没有新增 Specialist Profile 进入生产。Phase 14 Task 1-12、Demo 和 Acceptance 已完成；结论为 `INCONCLUSIVE`。Phase 15 Stage A 已重新冻结双轨 Release、48 例 Golden、真人证据、CI、预算和路由规则；Stage B 尚未授权。

## 4. 阶段依赖与自动门禁

当前 Gate 与未来 Phase 内依赖顺序固定为：

```text
Phase 13 Acceptance
-> Phase 14 Design/Plan REVIEWED
-> Phase 14 Task 1-12
-> Phase 14 Acceptance
-> Phase 15 Stage A Design/Plan 持久化
-> PHASE_15_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION
-> 用户单独授权 Stage B
-> Phase 15 Task 1-12
-> Phase 15 Acceptance / Final Acceptance
-> STOP
```

Phase 14 Design/Plan 已读取 Phase 12B/13 Acceptance、预算、风险和现有 Harness 基线。用户已授权 Phase 14 Task 1-12 连续实施；Task 12 Demo 与 Acceptance 已完成，因真实模型证据不足结论为 `INCONCLUSIVE`。Phase 15 Stage A 已完成文档持久化，只有实时状态明确记录用户授权 Stage B 后才可开始 Task 1。

Stage A 完成不等于业务实施授权；没有 Stage B 授权，不得修改 `src/`、数据库、CI、前端、真人采集器或真实模型入口。
以下情况必须暂停：

- 严重安全门禁失败且无法在当前设计内修复。
- 真实模型累计费用将超过当前阶段的冻结预算。
- PostgreSQL、PostgresSaver、Kafka 或模型服务持续不可用，导致强制验收证据无法生成。
- 需要扩大到真实淘宝 API、外部插件、热加载、自由 A2A、动态 handoff 或共享 scratchpad。
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
场景扩展为运营工作台和人工协同证据，Phase 15 才将实际接口纳入 Golden 与 Release 证据。

## 7. Phase 13：三场景 Specialist Agent 评估

Phase 13 采用共享评估内核后按 LiveOpsAgent、PlannerAgent、ReviewMemoryAgent 三个纵向切片独立评估，最终允许保留 0 至 3 个新增 Specialist；现有播中 Agent Harness 不计入该数量。

共同约束：

- 共用 `BoundedSpecialistRunner`、`AgentTask`、`AgentAction`、`AgentResult` 和 `EvidenceRef`。
- 模型只产生结构化动作和简短 `reason_summary`，不要求或持久化 chain-of-thought。
- Orchestrator、PlanEngine、SkillExecutor、Hook、授权和发布门禁保持确定性。
- 每个候选使用 20 个 development、40 个 validation 和 20 个 release holdout。
- Agent 使用 `deepseek-v4-flash`、temperature 0；Prompt、Schema、模型、价格表和数据集全部哈希。
- Phase 13 的历史上限为 2.40 元且正式实际费用为 0.042344 元；新 Phase 14 上限为 1.00 元，Phase 15 Release 预留 0.60 元，项目规划上限为 4.00 元。
- LiveOps、Planner、Review 的历史初始额度分别为 0.60、1.00、0.80 元；该 Phase 13 规则不允许借用当时的旧 Release 预留。
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

## 8. Phase 14：三场景人机协同决策支持

- 统一 `PREPARE | LIVE | REVIEW` 工作台，复用同一直播会话、事实、命令、Replay 和 Evaluation 身份。
- 首期播中深切片固定为可信售罄、备品冲突、弹幕噪声和主播节奏的复合事故。
- 自动保护仅执行冻结、CAS、陈旧执行阻断和严格对账；经营恢复必须由运营结构化决定确认。
- 首期只有 `live_ops_decision_support@1.0.0`，它只生成受限方案并可调用白名单只读 Skill。
- 记忆晋升采用规则资格加人工确认；默认路由保持 `DETERMINISTIC_ONLY`。
- 真实模型上限 1.00 元；人工对照使用三至五名代理运营和四组随机交叉场景。
- Phase 14 Acceptance 后停止在 Phase 15 Gate。

## 9. Phase 15：Golden Dataset 与发布门禁

- Stage A 已完成并冻结 Phase 15 Design/Implementation Plan、D-123 至 D-132、路线图、worklog 和恢复协议；旧讨论基线仅保留历史链接。
- 双轨结论固定为 Technical `PASS | FAIL | BLOCKED` 与 Copilot `PROMOTE | KEEP_DISABLED | BLOCKED`；技术发布通过不自动开启 Decision Support。
- 活跃 Golden Dataset 固定为 48 例，拆分 `12 development / 24 validation / 12 holdout`；Phase 13 的 240 例只做历史 Manifest 完整性检查。
- Stage B Task 1-12 依次交付迁移与入口、Golden、规则 Runner、双轨 Store、真人 study、最多十例真实 smoke、Acceptance、三级 CI、ToolRegistry 退役、两次默认路由 Release 和 Final Acceptance。
- 真实模型预算固定 0.60 元；真人证据固定为 3-5 名真实参与者、24-40 条记录；任一强制外部证据不足时保持 `BLOCKED`，不得伪造。
- 当前状态为 `PHASE_15_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`。用户单独授权 Stage B 后才可修改业务代码、数据库、CI、前端或调用真实模型。
- Phase 15 Acceptance 完成后停止，不自动进入新 Phase。

## 10. ToolRegistry 与审批兼容收口

- Phase 12B 引入窄化只读 `SkillPolicyView`，逐批迁移 Hook、Policy、Planner、Flow 和 Executor。
- 新增代码不得依赖 ToolRegistry。
- Phase 15 决定并删除 ToolRegistry 公共 Facade 和只针对该 Facade 的兼容测试。
- `TRUSTED_COMPAT` 在 Phase 12A Acceptance 前删除，不能延伸到 PlanEngine 或 Agent 路径。

## 11. 连续执行记录协议

正式实施后，每个 Task 必须更新 `docs/worklog/continuous_execution_state.md`：

1. Task 开始前记录目标、当前 HEAD、计划步骤和禁止事项。
2. 红灯确认后记录命令和预期失败原因。
3. 核心绿灯、审查整改和重大偏差后立即写盘。
4. 测试全绿后更新三个 worklog，与代码一起提交。
5. 推送后更新本地实时状态中的最新提交和下一任务；下一 Task 提交时一并持久化。

允许自主调整文件拆分、辅助类、Fixture、迁移顺序和设计内的缺陷修复。公开接口、Schema、状态机、数据库不变量或安全边界发生变化时，必须先写决策日志。不得自主放宽安全/成本/Agent 门槛或扩大范围。

## 12. Git 与用户工作区约束

- 正式实施沿用 `main`，每个 Task 至少使用一个独立 ASCII commit 并立即推送 `origin/main`；Phase 15 默认路由晋升必须把代码晋升与 Acceptance 留迹拆成两个提交。
- 主模型可按风险使用 sub-agent 进行独立分析、规格、安全、并发与测试审查；主模型独自负责安全边界、迁移整合、验证、提交和推送，并按 Phase 14 Plan 的超时/阻塞规则监控和接管 sub-agent。
- 不提交红灯、半成品或已知失败代码。
- 不覆盖、还原或提交用户已有未提交文件。
- 技术门禁通过后只可按实时游标自动进入当前已授权 Phase 的下一 Task；Phase Acceptance
  后状态必须转换为 `AWAITING_PHASE_<N>_GATE`，不得自动进入下一 Phase。

## 13. 完成定义

整条路线只有在以下条件全部满足时结束：

- Phase 12A、12B、13、14、15 Acceptance 均已生成并满足门禁。
- 三个 Agent 候选均有 `RETAINED`、`REJECTED` 或 `INCONCLUSIVE` 证据结论。
- CI、Golden Dataset、覆盖率和发布报告可重复运行。
- 默认路由已按 Phase 15 决策切换。
- ToolRegistry 公共 Facade 已删除。
- Agent Runtime Final Acceptance 已提交并推送。

最终保留 0 个 Agent 仍可视为成功：这表示评估证明确定性 Orchestrator、PlanEngine 和 Skill Runtime 更适合当前约束，而不是为了项目名称强行保留 Agent。
