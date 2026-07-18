# LiveAgent Agent Runtime 演进路线图

更新日期：2026-07-17

文档状态：Phase 11A、Phase 11B、Phase 12A、Phase 12B、Phase 13 已完成；Phase 14 人机协同 Task 1-12 已完成，Acceptance 为 `INCONCLUSIVE`；Phase 15 Stage A 已审核持久化，用户已授权 Stage B，Task 1-8 已推送，Task 9 已完成验证，当前状态为 `PHASE_15_TASK_9_READY_TO_PUSH`。

适用范围：Phase 11 及之后的 Agent Runtime 演进

## 1. 文档目的

本文是 Phase 11 及之后 Agent Runtime 演进的总路线入口，用于固定当前定位、阶段依赖、已确认边界、待讨论问题和上下文恢复顺序。

具体方案为什么被选择，以及其他方案为什么被排除，统一记录在 [Agent Runtime 演进决策日志](./agent_runtime_evolution_decisions.md)。Phase 1-10 的历史状态继续保留在 [当前项目状态与 Agent 化路线图](./current_project_status_and_agent_roadmap.md)，但旧文档不再作为未来演进方向的唯一事实源。

## 2. 当前项目定位

当前项目最准确的定位是：

```text
面向淘宝直播播前、播中、播后三场景的人机协同决策支持与受控执行 Runtime
```

当前三场景的实现形态并不相同：

- 播前：商品查询、排品、手卡生成和建播准备已经形成业务闭环，但主要仍是 Workflow / Graph。
- 播中：已有单体 Agent Harness，具备 Context、Reasoning、Tool Policy、Interrupt、Observation、Replan 和 Audit 链路。
- 播后：已有 Replay、Evaluation、复盘和记忆沉淀底座，但主要仍偏确定性评估与复盘流程。

项目已经超过普通 Workflow 或“LLM 调 API”演示，并已具备受控 Skill Runtime、统一平台执行契约、DAG PlanEngine、抢占 Replan、Replay 与评估证据。Phase 13 已证明当前自主 Specialist Agent 不应直接进入生产。后续目标不是堆叠 Agent 数量，而是把三场景统一升级为：

```text
由确定性控制面执行、由运营主控决策、由受限 Agent 压缩证据与提出方案的人机协同 Runtime
```

技术分层固定为：

```text
Tool：底层动作、外部 Adapter 和可能产生副作用的执行入口。
Skill：可治理、可版本化、可审计的业务能力单元。
Agent：有目标、上下文、工具选择权和局部推理循环的决策者。
PlanEngine：确定性 DAG 调度、恢复、重试、冻结和 Replan Runtime。
Orchestrator：确定性协调与路由组件，不默认包装成 Agent。
```

## 3. 总体交付策略

### 3.1 双线平衡

未来 12 周按单人业余投入估算，不维护两条完全独立的 backlog，而采用：

```text
架构主轴约 65%
+ 与当前阶段直接相关的生产约束约 35%
```

每个阶段只有一个主要架构主题，同时补齐该主题所需的 Schema、版本、幂等、超时、审计、恢复和评估要求。平台接入采用契约优先策略，使用高保真 Fake Adapter；真实淘宝生产 API、真实交易和正式平台资质不属于本周期验收范围。

### 3.2 Agent 化原则

Agent 不是阶段数量指标。一个职责只有在需要独立子目标、多轮工具选择、局部 Replan、独立上下文或权限边界，并且评估结果优于固定子图时，才允许升级为 Specialist Agent。

默认边界如下：

- Orchestrator 是确定性调度器，不包装成 Agent。
- PlanEngine 是确定性 DAG Runtime，LLM 只提出候选计划。
- Review 首期是 Hook、规则评估器或 LLM Judge Skill，不预设 ReviewAgent。
- PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent 都只是待评估候选，不能因为试点完成就自动进入正式架构。
- 人工是高风险经营决定的最终权威；Agent 不能直接改价、写库存、标记售罄、创建直播、晋升 active memory 或互相调用。

## 4. 阶段路线

| 阶段 | 主要目标 | 已确认边界 | 进入下一阶段的条件 |
| --- | --- | --- | --- |
| Phase 11A | 受控 Skill Runtime | SkillManifest 唯一事实源；13 个工具迁移元数据；4 个播前核心 Handler 分两批进入新执行链 | 契约与行为双门禁通过，旧 ToolRegistry 调用兼容且无元数据双写 |
| Phase 11B | 统一执行与平台契约 | 13 个 Handler、三批路由、Fake Adapter、FailureFact、Attempt Store 与平台契约已完成技术验收 | 已满足：用户已接受 Phase 11B Acceptance |
| Phase 12A | DAG PlanEngine | 固定 DAG、PlanStore、Worker、FailurePolicy、Command Ledger、checkpoint 对账、Graph 路由和 Demo 已完成技术验收 | 已满足：Phase 12A Acceptance 通过 |
| Phase 12B | 抢占与增量 Replan | 11 个 Task 与 Acceptance 已完成；业务闭环 Trace/报告可重复生成 | 已完成；等待 Phase 13 Gate |
| Phase 13 | 三场景 Agent 化评估与试点 | Task 1-12、正式评估与 Acceptance 已完成；0 个新增 Specialist Profile 被保留 | 已完成；历史自主评估结论保留 |
| Phase 14 | 三场景人机协同决策支持 | Task 1-12 已完成；播中复合售罄优先、运营主控、结构化修改、默认关闭路由 | Acceptance 为 `INCONCLUSIVE`；停止在 `AWAITING_PHASE_15_GATE` |
| Phase 15 | Golden Dataset 与发布门禁 | Stage A 已完成双轨 Release Design/Plan、D-123 至 D-132、48 例 Golden、真人证据和 CI 门禁冻结；Task 1-8 已推送，Task 9 已完成验证 | `PHASE_15_TASK_9_READY_TO_PUSH`；Task 1-12 按门禁连续实施，Acceptance 后停止 |

阶段编号描述依赖顺序。Phase 14 的 Design/Plan 持久化不是业务实施授权；任何跨 Phase 推进都不得绕过当前 Phase Acceptance 和下一 Phase 的用户授权。

## 5. 已确认的核心技术边界

### 5.1 Skill Runtime

- 渐进升级现有 ToolRegistry，不建设通用插件平台。
- 首期不支持外部安装、热加载或 PostgreSQL 动态配置。
- SkillManifest 使用 Python + Pydantic Catalog，在应用启动时统一校验。
- SkillManifest 是唯一事实源；ToolRegistry 是由 Manifest 生成的兼容只读投影。
- 现有 13 个工具全部迁移元数据，首期只迁移 4 个核心执行 Handler。
- 4 个核心 Handler 固定为 `query_products`、`generate_live_plan`、`generate_product_card` 和 `setup_live_session`。
- 前三个读取与确定性生成能力为第一批，`setup_live_session` 作为第二批独立迁移。
- ToolRegistry 先通过冻结快照完成迁移校验：9 个未迁移工具严格一致，4 个核心 Skill 只允许受控 Schema 修正；切换后不保留旧元数据回退。
- 四个核心 Skill 使用显式不可变快照；控制字段、幂等键和审批证据进入可信 SkillExecutionContext。
- 正式路由只有 `LEGACY` 与 `SKILL_RUNTIME`；新旧双算只存在于隔离测试比较器，写操作始终单路执行。
- hard-gate 使用独立 ApprovalContext；人审证据与 LLM arguments 隔离。`TRUSTED_COMPAT` 已在 Phase 12A Task 8 删除，Runtime 建播只接受 `HUMAN_INTERRUPT`。
- `HUMAN_INTERRUPT` 只能在 Graph 完成响应校验和审批审计后由内部工厂构造；13 个 Manifest 根 Schema 均拒绝未声明字段。
- `jsonschema` 是正式依赖，Catalog 启动与 SkillExecutor 调用均 fail-closed 校验。
- 播前 Graph 通过兼容 Facade 与同步桥接器接入 Runtime，不改变现有拓扑、checkpoint 和 interrupt。
- 两个批次从 Settings 读取启动配置并形成不可变 RoutePolicy，默认均为 legacy，不支持热更新。
- AgentToolExecutor 保留同步外观，但四个核心工具通过参数规范化后委托统一 Runtime。
- 调用开始时钉住执行路径，回滚只影响新调用，不允许失败后隐式切换执行器。
- 审计幂等重放比较完整业务事实；并发冲突路径显式使用 PostgreSQL `READ COMMITTED`，以读取已提交的首次审计事实。
- Manifest、Schema、生命周期、门禁、版本、审计和幂等不变量采用零容忍门槛。
- ToolRegistry 兼容查询 API 标记 deprecated；Phase 12B 使用 SkillPolicyView 迁移生产消费者，Phase 15 决定并执行 Facade 删除。新增代码不得扩大旧 API 使用面。
- Catalog 中每个 `skill_id` 只有一个活动版本；Plan 和 SkillCall 必须钉住精确版本。
- 恢复时版本不匹配不得偷偷执行新版本，必须暂停并触发 Replan 或人工处理。
- SkillExecutor 对外采用异步接口，现有同步业务函数通过适配器接入。

### 5.2 PlanEngine

- 业务 DAG 是持久化数据，不动态编译成一张新的 LangGraph。
- PostgreSQL PlanStore 保存 PlanRun、NodeRun 和计划版本；LangGraph checkpoint 只保存 `plan_id`、`plan_version` 和控制位置。
- 每次 Replan 创建不可变的新版本；旧版本不得原地覆盖。
- 节点使用受控状态集：`PENDING`、`READY`、`RUNNING`、`WAITING_APPROVAL`、`WAITING_RECONCILIATION`、`RETRY_WAIT`、`SUCCEEDED`、`FAILED`、`FROZEN`、`INVALIDATED`、`SKIPPED`。
- 增量 Replan 先定位直接受影响节点，再结合后继依赖和输入指纹判定失效范围。
- 无依赖只读节点最大并发数为 4；写操作、高风险操作和同资源操作必须串行。
- 抢占采用协作式冻结：停止派发新的低优节点，运行中节点在完成或超时后进入冻结点。
- Skill 和 Adapter 只报告结构化失败事实，集中式 FailurePolicy 决定 `RETRY`、`REPLAN`、`WAIT_HUMAN`、`SKIP` 或 `FAIL_PLAN`，PlanEngine 负责执行动作。
- PlanEngine 统一自动重试预算；等待写入 `RETRY_WAIT` 和 `next_retry_at` 后释放 Worker，不允许客户端隐藏重试。
- Replan 由确定性矩阵触发，每个 root plan 最多创建 2 个新版本，相同失败签名与输入指纹不得重复重规划。
- 执行前审批和执行后副作用未知分别使用 `WAITING_APPROVAL` 与 `WAITING_RECONCILIATION`，并按 10 分钟和 30 分钟 TTL fail-closed 收敛。
- 紧急 DAG 失败后按 impact scope 恢复：局部风险只阻断受影响分支，全局风险未解除时保持整张计划冻结。
- PlanStore 是节点执行事实的权威源；节点结果先提交 PlanStore，graph 节点随后返回并由 PostgresSaver 保存 checkpoint。
- PlanStore 领先 checkpoint 时从旧 checkpoint 重放并复用已成功 NodeRun；checkpoint 领先 PlanStore 时按 `INTERNAL_INVARIANT` fail-closed。
- Worker 使用 `FOR UPDATE SKIP LOCKED + lease + claim_version`，所有续租和终态写入必须通过 fencing token 校验。
- 租约按 Skill timeout 派生并定期心跳，停止心跳且租约过期后才允许其他 Worker 回收。
- 人工审批、对账和恢复命令通过 Command Ledger、唯一 command_id 和预期计划版本/节点状态保证幂等。
- 对账在服务启动、后台每 30 秒和人工命令执行前触发，不直接修改官方 checkpoint 内部表。
- checkpoint 领先时必须把 reconciliation_required、失败事实、signature、次数和时间持久化到 plan_runs；不能只在进程内冻结。
- Phase 12B Event Inbox 是售罄事件权威源，Kafka 先落库再提交 offset；可信库存写只由 PlanEngine 执行。

### 5.3 Agent 化决策门

Phase 13 使用相同 Skill、输入、Hook、权限和 Golden Cases 比较确定性基线与受限 Specialist Agent 候选。候选 Agent 不代表承诺交付物，只代表需要用数据验证的可能职责边界：

```text
播前确定性计划子图 vs 受限 PlannerAgent
播中确定性售罄 / 控场子图 vs 受限 LiveOpsAgent
播后确定性复盘 / 记忆流程 vs 受限 ReviewMemoryAgent
```

每个候选 Specialist Agent 只有同时满足下列约束才允许保留：

- 严重安全违规为 0。
- 同时满足候选绝对质量下限与相对 baseline 提升门，不使用宽松 OR 门。
- 使用 Profile 的模型、Skill、Token、deadline 和人民币绝对预算，不把零 Token baseline 套入相对成本门。
- 工具权限和人审规则不得弱于固定子图。

未达到门槛时不创建对应生产 Profile，正式架构继续使用确定性 baseline。统一 Registry、AgentTask/Result、EvidenceRef 和确定性 Orchestrator 预留未来多 Agent 扩展，但 Phase 13 不实现 Agent 互调。

## 6. 文档体系与更新规则

未来采用“总控计划 + 总路线 + 每阶段三件套 + 实时状态 + 工作记忆”：

```text
docs/project_guidance/agent_runtime_evolution_roadmap.md
docs/project_guidance/agent_runtime_evolution_decisions.md
docs/project_guidance/agent_runtime_completion_master_plan.md
docs/project_guidance/agent_runtime_continuous_recovery_prompt.md

docs/superpowers/specs/<phase>-design.md
docs/superpowers/plans/<phase>-plan.md
docs/superpowers/reports/<phase>-acceptance.md

docs/worklog/task_plan.md
docs/worklog/findings.md
docs/worklog/progress.md
docs/worklog/continuous_execution_state.md
```

- Roadmap：记录方向、依赖、阶段状态和决策门。
- Decisions：记录选项、选择理由、淘汰理由、影响和重审条件。
- Design：回答阶段为什么这样设计；用户确认后冻结。
- Plan：回答具体如何实施和验证。
- Acceptance：记录实际交付、测试证据、偏差和遗留问题。
- Worklog：保存当前任务、过程发现和会话进度，不复制完整 Design。
- Continuous State：保存唯一实时游标、最近证据、用户脏文件和下一条精确操作。

每个阶段开始前创建 Design，Design 经审核后再创建 Plan；阶段结束后创建 Acceptance，并同步更新 Roadmap、Decisions 和三个 worklog。

## 7. 上下文恢复协议

任何新会话或上下文压缩恢复时，按以下顺序读取：

```text
1. docs/worklog/continuous_execution_state.md
2. docs/project_guidance/agent_runtime_completion_master_plan.md
3. 当前阶段 Design
4. 当前阶段 Implementation Plan
5. docs/worklog/task_plan.md
6. docs/worklog/findings.md 与 progress.md 的最新相关记录
7. Agent Runtime 演进决策日志
8. git status 与最近提交
```

恢复后必须能够回答：当前阶段是什么、已经决定了什么、为什么这样决定、下一步是什么、哪些内容仍未决定。

## 8. Phase 11B-15 阶段边界

本节保存阶段目标、依赖和门槛。Phase 12A、12B、13、14 已完成并由 Acceptance 冻结实际证据；Phase 15 Stage A 已持久化，Stage B 已授权，Task 1-8 已推送，Task 9 已完成验证，准备提交推送。

### 8.1 Phase 11B：统一执行与平台契约

- **阶段目标**：统一全部 Skill 的执行、超时、幂等、错误和审计契约，以高保真 Fake Adapter 代替真实淘宝 API。
- **前置依赖**：Phase 11A 验收通过；Manifest、SkillExecutor、显式输入和两批路由稳定。
- **进入条件**：四个核心 Handler 已进入 Runtime；无严重安全回归；兼容债务已有清单。
- **退出条件**：剩余 Handler 迁移完成；单次尝试、超时、错误分类、幂等和审计链可重复测试。
- **待决策项**：Adapter 边界、Fake 保真度、deadline 所有权、外部错误映射、剩余 Handler 迁移批次、同步桥接器处理。

### 8.2 Phase 12A：DAG PlanEngine

- **阶段目标**：实现固定候选 DAG、确定性 DAG 校验与执行，以及独立 PlanStore；保留但不实现真实 LLM ProposalProvider。
- **前置依赖**：Phase 11B 验收通过；全部计划执行节点均有稳定的 Skill 或确定性控制节点契约。
- **进入条件**：错误分类、超时、幂等、审计和 Fake Adapter 可作为 PlanEngine 基础设施。
- **退出条件**：已满足。DAG 校验、不可变版本、节点状态机、PlanStore/checkpoint 恢复、有界并发、重试和人工命令均有测试证据。
- **待决策项**：无。技术证据见 [Phase 12A Acceptance](../superpowers/reports/phase-12a-dag-plan-engine-acceptance.md)。

### 8.3 Phase 12B：抢占与增量 Replan

- **阶段目标**：完成“批量手卡生成期间发生售罄”的协作式冻结、紧急 DAG 和增量恢复。
- **前置依赖**：Phase 12A 验收通过；PlanStore、调度器、状态机和恢复协议稳定。
- **进入条件**：售罄事件可提供商品、直播间、资源键和可判断的影响范围。
- **退出条件**：紧急 DAG 后只失效依赖闭包与指纹变化节点；局部/全局失败恢复和结果复用可回放。
- **待决策项**：无。详细选择已写入 [Phase 12B Design](../superpowers/specs/phase-12b-preemption-replan-design.md) 和 [Implementation Plan](../superpowers/plans/2026-07-14-phase-12b-preemption-replan-plan.md)。

### 8.4 Phase 13：三场景 Agent 化评估与试点

- **阶段目标**：分别比较播前、播中、播后的确定性基线与受限 Specialist Agent 候选，判断 PlannerAgent、LiveOpsAgent 或 ReviewMemoryAgent 是否真正产生收益。
- **前置依赖**：Phase 12B 固定子图基线稳定；Replay、Evaluation 和测试场景可重复运行。
- **进入条件**：每个候选 Agent 都有可比较的确定性基线，并可使用相同输入、Skill、Hook、权限和评估样本。
- **退出条件**：三个候选都有 RETAINED、REJECTED 或 INCONCLUSIVE 结论；只有严重违规为 0 且满足严格 AND 门的候选才建立默认关闭的生产 Profile。
- **待决策项**：Agent 最终保留数量只能由运行数据决定，不属于实施者自由选择；详细协议、预算、样本和条件分支见 [Phase 13 Design](../superpowers/specs/phase-13-specialist-agent-evaluation-design.md) 和 [Implementation Plan](../superpowers/plans/2026-07-14-phase-13-specialist-agent-evaluation-plan.md)。

### 8.5 Phase 14：三场景人机协同决策支持

- **阶段目标**：交付统一 Prepare/Live/Review 工作台，优先完成复合售罄事故的运营决策支持闭环。
- **前置依赖**：Phase 12B 的可信事件、自动保护、Replan/对账稳定；Phase 13 结论与共享 Runner/Registry 可复用。
- **进入条件**：本期 Design/Plan 已审核，用户单独授权业务实施。
- **退出条件**：自动保护与经营决定分离，运营可批准/修改/拒绝，记忆以规则资格加人工确认进入下一次播前，且严格质量与效率门通过。
- **待决策项**：无。详见 [Phase 14 Design](../superpowers/specs/phase-14-human-centered-decision-support-design.md) 与 [Implementation Plan](../superpowers/plans/2026-07-17-phase-14-human-centered-decision-support-plan.md)。

### 8.6 Phase 15：Golden Dataset 与发布门禁

- **阶段目标**：把 Golden Dataset、规则优先评估、CI、ToolRegistry 退役和默认路由晋升收敛为发布门禁。
- **前置依赖**：Phase 14 Acceptance、实际 Workspace 接口、人工对照和模型费用证据。
- **进入条件**：Phase 15 Stage A Design/Plan 已审核持久化，并取得用户对 Stage B 的单独授权。
- **实施范围**：Task 1-12，覆盖 48 例 Golden、规则 Runner、双轨 Store、真人 study、最多十例模型 smoke、三级 CI、ToolRegistry 退役、两次默认路由 Release 和 Acceptance。
- **硬门槛**：Technical Release 需要真实 PR/Release Actions 证据；Copilot 需要模型与真人证据同时满足严格 AND 门；证据不足保持 `BLOCKED` 或默认关闭。
- **退出条件**：生成 Phase 15 Acceptance 和 Final Acceptance；无论 Copilot 是否晋升，阶段完成后停止，不自动进入新 Phase。
- **当前状态**：`PHASE_15_TASK_9_READY_TO_PUSH`；Task 9 已完成审查和验证，真实模型仍禁止访问直到受保护环境最终预检通过。
- **事实源**：[Phase 15 Design](../superpowers/specs/phase-15-golden-release-gates-design.md)、[Implementation Plan](../superpowers/plans/2026-07-18-phase-15-golden-release-gates-plan.md)、D-123 至 D-132。

## 9. 当前状态与下一步

Phase 11A Design 已完成代码对照审核并冻结，实施依据为 [Phase 11A Skill Runtime Implementation Plan](../superpowers/plans/2026-07-12-phase-11a-skill-runtime-plan.md) 与 [Phase 11A Skill Runtime Design](../superpowers/specs/phase-11a-skill-runtime-design.md)，技术验收证据见 [Phase 11A Skill Runtime Acceptance](../superpowers/reports/phase-11a-skill-runtime-acceptance.md)。用户已经接受该验收，Phase 11A 正式完成。

Phase 11B 的讨论结论已写入 [Phase 11B Unified Execution and Platform Contract Design](../superpowers/specs/phase-11b-unified-execution-platform-contract-design.md)，实施依据为 [Phase 11B Unified Execution and Platform Contract Implementation Plan](../superpowers/plans/2026-07-12-phase-11b-unified-execution-platform-contract-plan.md)。详细选择见 D-054 至 D-064：全部 13 个 Skill 已进入 deadline、FailureFact、Attempt 审计和三批路由契约；平台状态能力通过业务域 Port 和有状态 Fake Adapter 执行；改价使用单活 `1.1.0`、显式 CAS 版本和受控审批边界。

Phase 11B 技术验收已完成，证据见 [Phase 11B Unified Execution and Platform Contract Acceptance](../superpowers/reports/phase-11b-unified-execution-platform-contract-acceptance.md)。用户已于 2026-07-14 审核并接受该验收，Phase 11B 正式完成。

Phase 12A 的讨论结论已写入 [Phase 12A DAG PlanEngine Design](../superpowers/specs/phase-12a-dag-plan-engine-design.md)。首期只处理冻结排品后的手卡批次，使用固定候选 DAG、独立 PlanStore、无状态 Worker、Capability Profile 资源锁和默认 Legacy 的可选 Graph 路由；售罄抢占和增量 Replan 保留给 Phase 12B。

用户已于 2026-07-14 审核并接受 Phase 12A Design，并于 2026-07-15 授权 Phase 12A-14 连续实施。Task 1-9 已完成，技术证据见 [Phase 12A DAG PlanEngine Acceptance](../superpowers/reports/phase-12a-dag-plan-engine-acceptance.md)。

Phase 12B 已完成并由 [Acceptance](../superpowers/reports/phase-12b-preemption-replan-acceptance.md) 冻结证据。Phase 13 已完成正式评估与 [Acceptance](../superpowers/reports/phase-13-specialist-agent-evaluation-acceptance.md)：LiveOpsAgent 被拒绝，PlannerAgent 与 ReviewMemoryAgent 因外部证据不足为 INCONCLUSIVE，0 个新增 Specialist Profile 接入生产。Phase 14 人机协同 Task 1-9 已验证待提交；Phase 15 Golden/CI 只保留讨论基线。
