# LiveAgent Agent Runtime 演进路线图

更新日期：2026-07-14

文档状态：Phase 11A、Phase 11B 用户验收均已接受，下一步为 Phase 12A Design 讨论

适用范围：Phase 11 及之后的 Agent Runtime 演进

## 1. 文档目的

本文是 Phase 11 及之后 Agent Runtime 演进的总路线入口，用于固定当前定位、阶段依赖、已确认边界、待讨论问题和上下文恢复顺序。

具体方案为什么被选择，以及其他方案为什么被排除，统一记录在 [Agent Runtime 演进决策日志](./agent_runtime_evolution_decisions.md)。Phase 1-10 的历史状态继续保留在 [当前项目状态与 Agent 化路线图](./current_project_status_and_agent_roadmap.md)，但旧文档不再作为未来演进方向的唯一事实源。

## 2. 当前项目定位

当前项目最准确的定位是：

```text
面向淘宝直播播前、播中、播后三场景的全链路主播 Agent Runtime 项目
```

当前三场景的实现形态并不相同：

- 播前：商品查询、排品、手卡生成和建播准备已经形成业务闭环，但主要仍是 Workflow / Graph。
- 播中：已有单体 Agent Harness，具备 Context、Reasoning、Tool Policy、Interrupt、Observation、Replan 和 Audit 链路。
- 播后：已有 Replay、Evaluation、复盘和记忆沉淀底座，但主要仍偏确定性评估与复盘流程。

项目已经超过普通 Workflow 或“LLM 调 API”演示，并已具备受控 Skill Runtime 与统一平台执行契约，但尚未具备 DAG PlanEngine、Agent 间协议和经过评估证明有效的 Specialist Agent。后续目标不是堆叠 Agent 数量，而是把三场景统一升级为：

```text
面向高风险直播业务的可控 Agent Runtime
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

## 4. 阶段路线

| 阶段 | 主要目标 | 已确认边界 | 进入下一阶段的条件 |
| --- | --- | --- | --- |
| Phase 11A | 受控 Skill Runtime | SkillManifest 唯一事实源；13 个工具迁移元数据；4 个播前核心 Handler 分两批进入新执行链 | 契约与行为双门禁通过，旧 ToolRegistry 调用兼容且无元数据双写 |
| Phase 11B | 统一执行与平台契约 | 13 个 Handler、三批路由、Fake Adapter、FailureFact、Attempt Store 与平台契约已完成技术验收 | 已满足：用户已接受 Phase 11B Acceptance |
| Phase 12A | DAG PlanEngine | LLM 提案、确定性执行；独立 PlanStore；不可变计划版本 | DAG 校验、节点状态机、持久化恢复和有界并发可测试 |
| Phase 12B | 抢占与增量 Replan | 手卡生成 + 售罄抢占；协作式冻结；依赖闭包 + 输入指纹 | 紧急 DAG 处理后只重算真正受影响节点 |
| Phase 13 | 三场景 Agent 化评估与试点 | 播前、播中、播后分别建立确定性基线，再评估 PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent 候选 | 达到量化门槛才保留对应 Agent，否则保留确定性子图 |
| Phase 14 | Golden Dataset 与发布门禁 | 规则评估优先，LLM Judge 不得覆盖严重违规 | 安全、成功率、恢复率、成本和版本回归均达到发布门槛 |

阶段编号描述的是依赖顺序，不代表所有远期实现细节已经冻结。每个阶段开始前仍需完成独立 Design 审核，再生成实施 Plan。

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
- hard-gate 使用独立 ApprovalContext；人审证据与 LLM arguments 隔离，旧 confirmed 只允许内部兼容映射。
- `HUMAN_INTERRUPT` 只能在 Graph 完成响应校验和审批审计后由内部工厂构造；13 个 Manifest 根 Schema 均拒绝未声明字段。
- `jsonschema` 是正式依赖，Catalog 启动与 SkillExecutor 调用均 fail-closed 校验。
- 播前 Graph 通过兼容 Facade 与同步桥接器接入 Runtime，不改变现有拓扑、checkpoint 和 interrupt。
- 两个批次从 Settings 读取启动配置并形成不可变 RoutePolicy，默认均为 legacy，不支持热更新。
- AgentToolExecutor 保留同步外观，但四个核心工具通过参数规范化后委托统一 Runtime。
- 调用开始时钉住执行路径，回滚只影响新调用，不允许失败后隐式切换执行器。
- 审计幂等重放比较完整业务事实；并发冲突路径显式使用 PostgreSQL `READ COMMITTED`，以读取已提交的首次审计事实。
- Manifest、Schema、生命周期、门禁、版本、审计和幂等不变量采用零容忍门槛。
- ToolRegistry 兼容查询 API 保留至 Phase 12 验收，并标记 deprecated；新增代码不得扩大旧 API 使用面。
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

### 5.3 Agent 化决策门

Phase 13 使用相同 Skill、输入、Hook、权限和 Golden Cases 比较确定性基线与受限 Specialist Agent 候选。候选 Agent 不代表承诺交付物，只代表需要用数据验证的可能职责边界：

```text
播前确定性计划子图 vs 受限 PlannerAgent
播中确定性售罄 / 控场子图 vs 受限 LiveOpsAgent
播后确定性复盘 / 记忆流程 vs 受限 ReviewMemoryAgent
```

每个候选 Specialist Agent 只有同时满足下列约束才允许保留：

- 严重安全违规为 0。
- 任务成功率至少提升 5 个百分点，或对应场景的恢复率 / 归因有效率 / 记忆命中收益至少提升 10 个百分点。
- 延迟和 Token 成本增幅均不超过 20%。
- 工具权限和人审规则不得弱于固定子图。

未达到门槛时删除对应 Agent 试点，正式架构继续使用确定性子图。若某个候选 Agent 的收益指标无法套用上述默认门槛，必须先新增决策定义可量化指标，不能直接保留。

## 6. 文档体系与更新规则

未来采用“总路线 + 每阶段三件套 + 工作记忆”：

```text
docs/project_guidance/agent_runtime_evolution_roadmap.md
docs/project_guidance/agent_runtime_evolution_decisions.md

docs/superpowers/specs/<phase>-design.md
docs/superpowers/plans/<phase>-plan.md
docs/superpowers/reports/<phase>-acceptance.md

docs/worklog/task_plan.md
docs/worklog/findings.md
docs/worklog/progress.md
```

- Roadmap：记录方向、依赖、阶段状态和决策门。
- Decisions：记录选项、选择理由、淘汰理由、影响和重审条件。
- Design：回答阶段为什么这样设计；用户确认后冻结。
- Plan：回答具体如何实施和验证。
- Acceptance：记录实际交付、测试证据、偏差和遗留问题。
- Worklog：保存当前任务、过程发现和会话进度，不复制完整 Design。

每个阶段开始前创建 Design，Design 经审核后再创建 Plan；阶段结束后创建 Acceptance，并同步更新 Roadmap、Decisions 和三个 worklog。

## 7. 上下文恢复协议

任何新会话、上下文压缩恢复或执行 Agent 接手时，按以下顺序读取：

```text
1. 本路线图
2. Agent Runtime 演进决策日志
3. docs/worklog/task_plan.md
4. 当前阶段 Design
5. 当前阶段 Plan
6. docs/worklog/findings.md 与 progress.md 的最新相关记录
7. git status 与最近提交
```

恢复后必须能够回答：当前阶段是什么、已经决定了什么、为什么这样决定、下一步是什么、哪些内容仍未决定。

## 8. Phase 11B-14 高层大纲

本节只用于保存远期方向和阶段门槛，不是提前完成的 Design 或 Implementation Plan。每个阶段只记录阶段目标、前置依赖、进入条件、退出条件和待决策项；待决策项必须在对应阶段开始前重新基于代码、验收证据和运行数据讨论。

### 8.1 Phase 11B：统一执行与平台契约

- **阶段目标**：统一全部 Skill 的执行、超时、幂等、错误和审计契约，以高保真 Fake Adapter 代替真实淘宝 API。
- **前置依赖**：Phase 11A 验收通过；Manifest、SkillExecutor、显式输入和两批路由稳定。
- **进入条件**：四个核心 Handler 已进入 Runtime；无严重安全回归；兼容债务已有清单。
- **退出条件**：剩余 Handler 迁移完成；单次尝试、超时、错误分类、幂等和审计链可重复测试。
- **待决策项**：Adapter 边界、Fake 保真度、deadline 所有权、外部错误映射、剩余 Handler 迁移批次、同步桥接器处理。

### 8.2 Phase 12A：DAG PlanEngine

- **阶段目标**：实现 LLM 计划提案、确定性 DAG 校验与执行，以及独立 PlanStore。
- **前置依赖**：Phase 11B 验收通过；全部计划节点可通过稳定 Skill 契约执行。
- **进入条件**：错误分类、超时、幂等、审计和 Fake Adapter 可作为 PlanEngine 基础设施。
- **退出条件**：DAG 校验、不可变版本、节点状态机、PlanStore/checkpoint 恢复、有界并发、重试和人工命令均有测试证据。
- **待决策项**：数据库物理 Schema 与索引、候选 DAG 输入格式、校验规则、调度进程模型、节点输入输出引用格式和查询 API。

### 8.3 Phase 12B：抢占与增量 Replan

- **阶段目标**：完成“批量手卡生成期间发生售罄”的协作式冻结、紧急 DAG 和增量恢复。
- **前置依赖**：Phase 12A 验收通过；PlanStore、调度器、状态机和恢复协议稳定。
- **进入条件**：售罄事件可提供商品、直播间、资源键和可判断的影响范围。
- **退出条件**：紧急 DAG 后只失效依赖闭包与指纹变化节点；局部/全局失败恢复和结果复用可回放。
- **待决策项**：impact scope 契约、优先级与资源键、输入指纹规范化、紧急计划关联方式、冻结超时和专项 Golden Cases。

### 8.4 Phase 13：三场景 Agent 化评估与试点

- **阶段目标**：分别比较播前、播中、播后的确定性基线与受限 Specialist Agent 候选，判断 PlannerAgent、LiveOpsAgent 或 ReviewMemoryAgent 是否真正产生收益。
- **前置依赖**：Phase 12B 固定子图基线稳定；Replay、Evaluation 和测试场景可重复运行。
- **进入条件**：每个候选 Agent 都有可比较的确定性基线，并可使用相同输入、Skill、Hook、权限和评估样本。
- **退出条件**：完成量化对照；严重违规为 0，且收益与成本达到既定门槛才保留对应 Agent，否则删除试点。
- **待决策项**：三场景 Agent 候选优先级、职责范围、上下文与工具预算、样本量、模型和 Prompt 固定方式、提前终止条件，以及通过后才讨论的 handoff 协议。

### 8.5 Phase 14：Golden Dataset 与发布门禁

- **阶段目标**：建立版本化 Golden Dataset、对抗样本和自动发布回归门禁。
- **前置依赖**：Phase 13 已确定正式架构；Replay、规则评估、版本和成本证据稳定。
- **进入条件**：固定子图和保留的 Agent 均可通过统一 Evaluation Interface 执行。
- **退出条件**：数据集可追踪版本；严重安全违规自动 fail；成功率、恢复率、延迟、成本和版本回归均进入发布判定。
- **待决策项**：数据集存储与版本治理、标注复核流程、训练/评估污染隔离、LLM Judge 版本、CI 集成和报告保留期限。

## 9. 当前状态与下一步

Phase 11A Design 已完成代码对照审核并冻结，实施依据为 [Phase 11A Skill Runtime Implementation Plan](../superpowers/plans/2026-07-12-phase-11a-skill-runtime-plan.md) 与 [Phase 11A Skill Runtime Design](../superpowers/specs/phase-11a-skill-runtime-design.md)，技术验收证据见 [Phase 11A Skill Runtime Acceptance](../superpowers/reports/phase-11a-skill-runtime-acceptance.md)。用户已经接受该验收，Phase 11A 正式完成。

Phase 11B 的讨论结论已写入 [Phase 11B Unified Execution and Platform Contract Design](../superpowers/specs/phase-11b-unified-execution-platform-contract-design.md)，实施依据为 [Phase 11B Unified Execution and Platform Contract Implementation Plan](../superpowers/plans/2026-07-12-phase-11b-unified-execution-platform-contract-plan.md)。详细选择见 D-054 至 D-064：全部 13 个 Skill 已进入 deadline、FailureFact、Attempt 审计和三批路由契约；平台状态能力通过业务域 Port 和有状态 Fake Adapter 执行；改价使用单活 `1.1.0`、显式 CAS 版本和受控审批边界。

Phase 11B 技术验收已完成，证据见 [Phase 11B Unified Execution and Platform Contract Acceptance](../superpowers/reports/phase-11b-unified-execution-platform-contract-acceptance.md)。用户已于 2026-07-14 审核并接受该验收，Phase 11B 正式完成。

下一步按 Just-in-Time 原则讨论并生成 Phase 12A Design。Design 审核前不得实施 PlanEngine；Phase 12B 至 Phase 14 继续只保留需求、目标、依赖和决策门，不提前冻结实现细节。
