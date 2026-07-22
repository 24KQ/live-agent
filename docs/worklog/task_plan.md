# LiveAgent 工作日志计划

## 2026-07-18 Phase 16 Controlled Multi-Agent Escalation

- [x] Task 1：持久化已批准的 Design、Implementation Plan、D-134 至 D-140、路线图、总控和恢复入口；等待本 Task 验证、提交与推送。
- [x] Task 2：修复根 pytest 的三处 Phase 14 PostgreSQL 测试模块同名收集冲突，并以 `.gitattributes` 固定 Python LF 检出消除冻结生成器伪漂移；根 collect、专项、完整 unit/integration 和最终审查已通过，`6ea5a57` 已提交并推送。
- [x] Task 3：新增 `CONFLICT_ANALYSIS`、`LIVE_DECISION_PLANNING`、冻结 Profile 和不可变领域协议。Profile/lineage/预算/历史闭包/Prompt/展示安全整改、历史迁移/测试基线整改、双重复审、空库迁移和完整 unit/integration 均已通过，`ad0e185` 已独立提交并推送。
- [x] Task 4：实现 escalation、analysis、outcome 的内存/PostgreSQL append-only Store。四轮审查整改和最终验证均已完成，`1ea229a` 已独立提交推送；D-145 固定数据库 CAS、LIVE 线性化复核与 Task 6 前 READY fail-closed。
- [x] Task 5：实现三选二选择器、运营 lease 显式升级和 Analyst Coordinator 段。D-146/D-147 整改、专项 unit `25 passed`、PostgreSQL `20 passed`、完整 unit `1420 passed`、integration `172 passed` 均已通过，`b584808` 已独立提交并推送。
- [x] Task 6：实现 Planner 段、整份 Validator 拒绝和受控 Proposal lineage。D-148 至 D-152、专项 `83 passed`、PostgreSQL `29 passed`、完整 unit `1440 passed`、integration `181 passed, 7 deselected` 与双重复审均已通过，`d42eab9` 已独立提交推送；不改变 OperatorDecision/Compiler 或经营执行。
- [x] Task 7：接入 operator-authenticated HTTP、WebSocket 和 Workspace 投影。D-153 至 D-158 的窄请求、认证、Bundle/lease 装配、重试、投影与人工/自动所有权门禁已通过，`2f4b7ef` 已独立提交推送。
- [x] Task 8：扩展本地三视图工作台的高冲突事故展示与交互。D-159 至 D-163 的 Bundle 白名单摘要、route/trigger/analysis/outcome 展示、READY/lineage 禁用、安全订阅/撤销和 UNAVAILABLE 状态已通过完整回归，`502b67c` 已独立提交推送。
- [x] Task 9：生成独立冻结 48 例数据集及 ScriptedModel 配对评估。独立 48 例资产、实际 Coordinator/ScriptedModel 重放、
  全新 PostgreSQL Store 恢复与完整回归已通过，`be6de97` 已独立提交并推送。
- [x] Task 10：实现 10 例/1.00 CNY 真实 smoke 预检和独立预算账本。D-164 至 D-166、最终双重复审、专项/全量回归、迁移与编码验证已通过，`c6cb13a` 已独立提交并推送。
- [x] Task 11：生成 `live-session-p001-sold-out-v2` Demo、Acceptance，并停止在 Phase 17 Gate。Demo 使用权威
  Phase 12B 保护、完整多 Agent 谱系、PlanStore 命令账本和新 Store 的 lease/dispatch claim 重放；Acceptance
  为 `INCONCLUSIVE`，默认路由保持 `DETERMINISTIC_ONLY`，当前为 `AWAITING_PHASE_17_GATE`。
- [x] Phase 16 PR coverage remediation：新增冻结 11 文件 source-closure Manifest，unit/integration 联合采样，补齐
  测试分支，line/branch `92.035%/85.081%` 达到不变的 `90/85` 门槛；测试提交 `599c98e`、CI 提交 `6216f9f` 已推送，
  等待 PR Gate 查询与 merge commit。真实模型证据仍为 `INCONCLUSIVE`，不自动进入 Phase 17。

所有 Task 使用 RED -> GREEN -> REFACTOR -> REVIEW -> VERIFY -> DOCS -> COMMIT -> PUSH；
Task 10 前不访问真实模型，任何严重安全违规、预算风险或强制基础设施阻塞均停止当前 Task。

## 2026-07-22 Phase 16 Official Real-Model Smoke Evidence Closure

- [x] Task 0：持久化正式真实模型 smoke 的 Design、Implementation Plan、D-168 至 D-171 和状态事实源；仅文档提交 `a603159` 已推送。
- [x] Task 1：恢复固定 LIVE Profile，新增隔离 Smoke Profile/Manifest、provider receipt 合同和离线预检；不联网，`d032cda` 已推送。
- [-] Task 2：新增版本化 PostgreSQL append-only formal ledger，导入 `0.073220 CNY` 历史事实并锁定十个 `.092000 CNY` slot；不改旧 `PHASE16_MULTI_AGENT_SMOKE` 表。实现/测试提交 `b2387e9`、`469483e` 已形成，等待本任务文档提交与分支推送。
- [ ] Task 3：让唯一 CLI 通过 `BoundedSpecialistRunner`、smoke-only 预算端口和六角色只读投影运行；默认 dry-run，只有 `--execute` 能联网。
- [ ] Task 4：在所有本地 Gate 通过后，最多执行一次严格 10/10 正式 smoke，读取脱敏 receipt 渲染报告和 Acceptance；任何已发送失败立即停止且不重试。
- [ ] Task 5：完整验证、审查、PR 和 merge commit；无论外部结论如何都保持 `DETERMINISTIC_ONLY` 和 `AWAITING_PHASE_17_GATE`。

## 目标

把 `docs/worklog/` 从本机临时记录升级为可追踪的项目工作日志，用于记录阶段计划、发现、进度和后续迭代方向。

## 记录原则

- 只记录项目事实、阶段结论、测试结果和后续计划。
- 不记录真实 `.env`、API key、平台 token、本机私密路径和个人账号密码。
- 中文内容统一 UTF-8，无 BOM 优先。
- 修改后运行 `python scripts/check_doc_encoding.py`。

## 2026-07-11 文档编码治理任务

- [x] 新增 `scripts/check_doc_encoding.py`，用于扫描文档编码风险。
- [x] 新增 `docs/project_guidance/document_encoding_policy.md`，固定中文文档写入规范。
- [x] 将 `docs/worklog/` 纳入版本控制，作为后续迭代留迹入口。
- [x] 更新 `current_project_status_and_agent_roadmap.md`，记录编码治理状态。
- [x] 更新 `phase_execution_log.md`，追加本次治理记录。

## 后续维护要求

- 每个阶段结束后更新 `phase_execution_log.md`。
- 重要架构判断同步更新 `current_project_status_and_agent_roadmap.md`。
- 长期任务过程记录可追加到 `docs/worklog/progress.md`。
- 排障结论和设计取舍追加到 `docs/worklog/findings.md`。

## 2026-07-11 Agent 架构评估任务

- [x] 阅读 `docs/study/agent_harness_practice.md`。
- [x] 阅读 `docs/study/ai_discipline_harness.md`。
- [x] 阅读 `docs/study/harness_discussion_history.md`。
- [x] 阅读 `docs/study/taobao_anchor_agent_harness.md`。
- [x] 对照 `README.md`、播前 graph、播中 Harness graph、ToolRegistry、LifecycleHooks、Replay、Evaluation 代码评估当前项目边界。
- [x] 判断当时项目技术形态：播前偏 Workflow、播中已有单体 Agent Harness，不是成熟多 Agent 系统。
- [x] 确认后续优先设计 Skill Runtime、DAG PlanEngine、Agent 化决策门和 Golden Dataset 回归体系。

## 2026-07-11 Agent Runtime 架构讨论持久化

- [x] 明确未来 12 周采用架构主轴约 65% + 生产约束约 35% 的双线策略。
- [x] 明确 Skill Runtime 渐进升级边界和 SkillManifest 唯一事实源。
- [x] 明确 13 个工具迁移元数据、4 个核心 Handler 首期迁移执行链。
- [x] 明确 LLM 提案 + 确定性 PlanEngine 的职责边界。
- [x] 明确首期“手卡生成 + 售罄抢占”场景和协作式冻结语义。
- [x] 明确独立 PlanStore、不可变版本、节点状态集、增量失效算法和并发策略。
- [x] 明确当时的固定子图基线与 LiveOpsAgent 对照实验，以及严格量化保留门槛；后续已升级为三场景 Agent 化评估。
- [x] 新建 Agent Runtime 总路线图和决策日志，记录备选方案、选择理由和淘汰理由。
- [x] 完成 PlanEngine 失败分类、自动重试、Replan、人工处理和紧急 DAG 失败恢复讨论。
- [x] 明确结构化失败事实、集中式 FailurePolicy 和 PlanEngine 恢复动作边界。
- [x] 明确 PlanEngine 统一重试预算、风险感知资格和持久化 `RETRY_WAIT`。
- [x] 明确确定性 Replan 触发矩阵、最多 2 次预算和失败签名去重。
- [x] 区分 `WAITING_APPROVAL` 与 `WAITING_RECONCILIATION`，并明确分类 TTL。
- [x] 明确紧急 DAG 失败后按 impact scope 部分恢复或全局冻结。
- [x] 完成 PlanStore 与 LangGraph checkpoint 的写入顺序、崩溃恢复和对账协议讨论。
- [x] 明确 PlanStore 权威、有序写入和旧 checkpoint 重放复用策略。
- [x] 明确 checkpoint 领先时按 `INTERNAL_INVARIANT` fail-closed。
- [x] 明确 Worker lease、fencing token、派生租约和心跳续租。
- [x] 明确 Command Ledger、乐观版本和三类对账触发。
- [x] 讨论 Phase 11A 的兼容迁移、回滚和验收边界。
- [x] 明确 ToolRegistry 影子校验后切换，旧元数据只作冻结快照且不提供运行时回退。
- [x] 明确四个播前核心 Handler 和“前三个生成能力 + setup 写操作”的两批迁移顺序。
- [x] 明确分组路由、测试专用隔离行为比较、调用路径钉住和批次显式回滚。
- [x] 明确关键不变量零容忍、契约与行为双门禁及 ToolRegistry 兼容期限。
- [x] 完成本轮详细架构讨论并生成 Phase 11A Skill Runtime Design。
- [x] 用户审核 Phase 11A Design，并根据代码评审修正输入 Schema、审批、影子执行和接入边界。
- [x] 新增 D-043 至 D-049，并将 D-035 标记为 CONDITIONAL、D-038 标记为 SUPERSEDED。
- [x] 生成 `2026-07-12-phase-11a-skill-runtime-plan.md`，按九个 TDD 任务拆分实施与验收。
- [x] 补齐 Phase 11B、12A、12B、13、14 的阶段目标、前置依赖、进入条件、退出条件和待决策项。
- [x] 明确远期大纲只用于恢复方向，待决策项不构成默认实施方案。
- [x] 完成三场景定位纠偏：项目业务范围明确为播前、播中、播后三场景全链路主播 Agent Runtime。
- [x] 固定 Agent / Skill / Tool / PlanEngine / Orchestrator 分层边界，避免把三场景机械等同于三个 Agent。
- [x] 将 Phase 13 从单一 LiveOpsAgent 对照升级为三场景 Specialist Agent 候选评估。
- [x] 新增上下文恢复提示词文档，用于上下文压缩后恢复项目定位、当前阶段和执行约束。
- [x] 用户确认 Phase 11A Implementation Plan 的执行方式后开始业务代码实施。
- [ ] Phase 11B 至 Phase 14 在对应阶段开始前按 Just-in-Time 方式展开，不提前细化。

## 2026-07-12 Phase 11A Task 1-6 实施纠偏

- [x] 重新核验 Task 1-4 的提交与冻结计划，不以专项测试全绿替代规格验收。
- [x] 修正 ApprovalContext 决定约束和 TRUSTED_COMPAT 信任边界。
- [x] 修正四个核心 Skill 的显式 arguments、完整商品快照和真实 LivePlanDraft Schema。
- [x] 修正 Handler 使用可信 Context、真实领域模型、原子手卡入口和显式幂等键。
- [x] 完成 Task 5：Literal 启动配置、冻结 RoutePolicy、Graph 兼容 Facade、严格失败和两批独立路由。
- [x] 完成 Task 6：LangGraph 批准恢复传递 HUMAN_INTERRUPT ApprovalContext，拒绝分支不执行 setup。
- [x] 使用真实 PostgreSQL、审计 Store 和 LangGraph invoke/resume 验证 Runtime 批准与拒绝流程。
- [x] 完成 Task 7：AgentToolExecutor 旧参数规范化、可信兼容证据与四个核心工具单一 Runtime dispatch；正式提交从 `4f77403` 开始，为 `4f77403`、`7e132f3`、`b60a85d`。`96a5adb` 属于提前错误实施，已由 `94e2766` 完整删除，不计入有效交付。
- [x] 完成 Task 8：隔离等价测试、四场景 Demo 与统一入口；提交 `7154c89`、`fd54005`。
- [x] 完成 Task 9：全量技术验收与阶段留迹；Acceptance 状态为“技术验收完成，待用户审核”。
- [x] 完成验收前审计幂等复审整改：完整事实冲突检测、显式 `READ COMMITTED`、测试替身语义对齐、PostgreSQL 流程 trace 隔离和全阶段 diff 空白修复。
- [x] 完成最终审查 P1 整改：人工审批受控工厂、13 个 Manifest 根 Schema fail-closed、Demo 调用点收敛与 D-053 留迹。
- [x] 用户审核并接受 Phase 11A Acceptance。
- [x] 完成 Phase 11B 业务域 Adapter、Fake、deadline、FailureFact、Attempt Store、三批迁移和验收门槛讨论。
- [x] 生成 Phase 11B Unified Execution and Platform Contract Design。
- [x] 用户审核并接受 Phase 11B Design。
- [x] 生成 `2026-07-12-phase-11b-unified-execution-platform-contract-plan.md`，按 TDD 拆分实施、回归和验收。
- [x] 用户确认执行 Phase 11B Implementation Plan。
- [x] Phase 11B Task 1：FailureFact、deadline、Adapter 公共模型与 Manifest 单次尝试上限；提交 `3e33ec3`。
- [x] Phase 11B Task 2：独立 Attempt Store、PostgreSQL 迁移和并发 claim 语义；提交 `5033dcf`。
- [x] Phase 11B Task 3：有状态 Fake Platform、业务域 Port、Fixture 和声明式故障脚本；提交 `770ba8f`。
- [x] Phase 11B Task 4：原生 async Executor、deadline、Attempt Store 和 FailureFact 传播；提交 `8eff0b2`。
- [x] Phase 11B Task 5 前置纠偏：新增 D-063，确认 `LiveOperationsPort.resolve_product_context` 为只读商品上下文解析契约。
- [x] Phase 11B Task 5：统一 Handler 工厂、批次一 10 个 Skill 装配、只读商品上下文 Port 与播前兼容工厂收敛。
- [x] Phase 11B Task 6：三批启动冻结路由与 AgentToolExecutor 无 fallback 接入；提交 `edb27d6`。
- [x] Phase 11B Task 7：批次二建播/售罄 Handler 与播中 Harness Runtime 接入；提交 `6908f41`。
- [x] Phase 11B Task 8 前置契约纠偏：用户选择改价显式 `expected_version` + 单活 `1.1.0`，AgentToolExecutor 保持 pending；新增 D-064 并修订 Design/Implementation Plan/worklog。
- [x] Phase 11B Task 8：完成 `set_product_price@1.1.0`、显式 CAS 版本、审批/幂等前置、单次 Port 调用和稳定重放；提交 `3feab86`。
- [x] Phase 11B Task 9：完成真实 Legacy 建播对照、Runtime-only 失败契约、六场景无外部依赖 Demo 与统一入口；提交 `778d52b`。
- [x] Phase 11B Task 10：完成专项、系统回归、默认全量、Demo、编码检查与 Acceptance 留迹。
- [x] 用户审核并接受 Phase 11B Acceptance，Phase 11B 正式完成。
- [x] 重新读取 Phase 12A 高层大纲、D-009 至 D-034 和 Phase 11B Acceptance，完成 Phase 12A Design 的 Just-in-Time 讨论。
- [x] 生成 `phase-12a-dag-plan-engine-design.md`，新增 D-065 至 D-072，固定首期 DAG、PlanStore、Worker、Command Ledger 和验收边界。
- [x] 用户审核并接受 Phase 12A Design。
- [x] 生成 `2026-07-14-phase-12a-dag-plan-engine-plan.md`，等待用户确认执行。
- [x] 用户已授权并完成 Phase 12A Task 1-5；最新业务提交为 `37d6f8a`。

## 2026-07-14 Agent Runtime 全程计划持久化

- [x] 完成 Phase 12A 剩余、Phase 12B、Phase 13 和 Phase 14 的完整架构讨论。
- [x] 明确本轮只持久化文档，不执行 Phase 12A Task 6。
- [x] 新建全程总控计划、连续执行实时状态和新的上下文恢复入口。
- [x] 修订 Phase 12A Design/Plan：持久化 reconciliation 事故事实，增加 TRUSTED_COMPAT 退役 Task，将验收调整为 Task 9。
- [x] 生成 Phase 12B 抢占与增量 Replan Design/Implementation Plan。
- [x] 生成 Phase 13 Specialist Agent 评估 Design/Implementation Plan。
- [x] 生成 Phase 14 Golden Dataset 与发布门禁 Design/Implementation Plan。
- [x] 新增 D-073 至 D-093，并修正 D-042、D-045 的历史状态。
- [x] 更新 Agent Runtime 路线图与恢复顺序。
- [x] 完成冻结计划可执行性复核：售罄版本切换与 Handler 原子提交、SkillPolicyView 独立迁移、持久化模型预算、Runtime Golden case 和两次 Release 路由晋升。
- [x] 完成编号、状态一致性、UTF-8、编码扫描和 `git diff --check` 验证；全仓扫描的 4 个错误/58 个警告均为非目标历史问题。
- [x] 锁定本轮提交边界为 16 个目标文档；提交与推送结果以 Git 历史和远端状态为准。
- [x] 用户已授权从 Phase 12A Task 6 连续实施至 Phase 14 Final Acceptance，采用受控自主调整。

## 2026-07-15 Phase 12A-14 正式连续实施

- [x] Phase 12A Task 6：Checkpoint 一致性与人工命令恢复（`6029ad3` 已推送）。
- [x] Phase 12A Task 7：播前 Graph 局部路由（`7cbf026` 已推送）。
- [x] Phase 12A Task 8：移除 `TRUSTED_COMPAT`（`9a8e5a6` 已推送）。
- [x] Phase 12A Task 9：Demo、全量验收与 Acceptance（技术门禁通过）。
- [x] Phase 12B：Event Inbox、抢占、紧急 DAG、增量 Replan 与 Acceptance 已完成。
  - [x] Task 1：SkillPolicyView 与事件公共模型（`d794ff3` 已推送）。
  - [x] Task 2：Event Inbox 内存 Store 与状态机（`8b1600b` 已推送）。
  - [x] Task 3：PostgreSQL Event Store 与计划 lineage（`25793f2` 已推送）。
  - [x] Task 4：Kafka 入站与启动冻结 Trust Profile（`0762c2c` 已推送）。
  - [x] Task 5：ImpactAnalyzer 与协作式冻结（`375b671` 已推送）。
  - [x] Task 6：售罄 CAS Skill 与严格对账（`9d4bf97` 已推送）。
  - [x] Task 7：高优先级紧急 child DAG（`703f072` 已推送）。
  - [x] Task 8：增量 Replan 与结果复用（`e98df2a` 已推送）。
  - [x] Task 9：SkillPolicyView 生产消费者迁移（`f6a7d1d` 已推送）。
  - [x] Task 10：PreemptionCoordinator、Harness 证据接入与路由（`e6f3414` 已推送）。
  - [x] Task 11：业务闭环 Demo、验收和阶段留迹（`d585412` 已推送）。
- [x] Phase 13 Just-in-Time Gate：候选价值、基线、样本、预算、多 Agent 扩展与 12-Task Plan 已审核持久化。
- [ ] Phase 13 业务实施：已获用户授权，按 12 个 TDD Task 连续推进。
  - [x] Task 1：协议、Profile Registry 与确定性路由（`28b2764` 已推送）。
  - [x] Task 2：原生 async 单次 AgentModelPort（`344cb82` 已推送）。
  - [x] Task 3：持久模型预算账本（`653ebb8` 已推送）。
  - [x] Task 4：BoundedSpecialistRunner 与 Evidence Resolver（`94ad80b` 已推送）。
  - [x] Task 5：Evaluation Store、配对比较与迁移（`6edd833` 已推送）。
  - [x] Task 6：240 例字节稳定数据集与冻结 Evaluation Manifest（`f13ae6e` 已推送）。
  - [x] Task 7：LiveOpsAgent 纵向切片（`4b26a31` 已推送）。
  - [x] Task 8：PlannerAgent 与记忆读取切片（`204aec0` 已推送）。
  - [x] Task 9：播后 Skill、MemoryCandidate 与 PromotionPolicy（`b6c1cdf` 已推送）。
  - [x] Task 10：ReviewMemoryAgent 纵向切片（`e12de15` 已推送）。
  - [x] Task 11：正式评估预检、ScriptedModel 演练与候选去留结论（`ca1e66d` 已推送）。
  - [x] Task 12：Demo、业务附录与 Acceptance（`e7d6fbb` 已推送）。
  - [x] Phase 13 Acceptance：0 个新增 Specialist Profile 被保留；历史自主评估结论冻结。
- [x] Phase 14 Design Persistence：三场景人机协同定位、Design/Plan、决策、恢复协议和 Phase 15 Discussion Baseline 已生成。
- [ ] Phase 14：Human-Centered Decision Support 已获连续实施授权。
  - [x] Task 1：旧 Planner/Harness 权限审计、默认关闭路由、旧 checkpoint 最终执行门与原子终态会话。
  - [x] Task 2：统一 Workspace 与不可变 PostgreSQL Store（`42991ec` 已推送）。
  - [x] Task 3：确定性 EvidenceBundle 与只读 Resolver（`d3a53a8` 已提交并推送）。
  - [x] Task 4：播中 Copilot 与结构化方案（`4ad8de5` 已推送）。
  - [x] Task 5：人工决定与受控执行编译（`c20d1ab` 已推送）。
  - [x] Task 6：复合售罄自动保护与人工恢复（`43d182f` 已推送）。
  - [x] Task 7：统一 API 与 WebSocket 协议（`eb28885` 已推送）。
  - [x] Task 8：三视图运营工作台（`0a8f08c` 已推送）。
  - [x] Task 9：播后反馈与人工确认记忆晋升（`dbd5768` 已提交并推送）。
  - [x] Task 10：固定复合事故数据集、离线规则回归、人机配对评估与人工对照（`3dc7f40` 已提交并推送）。
  - [x] Task 11：真实模型 smoke 预检与严格结论（`6a79359` 已提交并推送）。
  - [x] Task 12：三场景 Demo、Phase 14 Acceptance 与 Phase 15 Gate（`c4124ce` 已提交并推送）。
- [x] Phase 15 Stage A：Golden Dataset、发布门禁、双轨结论、真人证据、预算、CI、路由和 Final Acceptance Design/Plan 已审核持久化；D-123 至 D-132 已追加，旧 Discussion Baseline 已标记为历史输入。
- [x] Phase 15 Stage B：用户已授权，按 Task 1-12 连续执行；Task 12 验证完成，Acceptance 为 `INCONCLUSIVE`，阶段停止。
  - [x] Task 1：发布入口、迁移清单与仓库事实（`2a88224` 已推送）。
  - [x] Task 2：48 例 Golden Dataset 与 Manifest（`eb31dd9` 已推送）。
  - [x] Task 3：统一 Subject Runner 与规则门禁（`9f9d835` 已推送）。
  - [x] Task 4：Release Store、双轨决策与 Phase 15 预算（`fefd926` 已推送）。
  - [x] Task 5：真人交叉对照采集器（`d181cd1` 已推送）。
  - [x] Task 6：真实 Copilot Smoke 与 Promotion 证据（`4965116` 已推送）。
  - [x] Task 7：PromotionDecision 与双轨 Acceptance 报告（`984b3ff` 已推送）。
  - [x] Task 6：真实 Copilot Smoke 与 Promotion 证据。
  - [x] Task 7：PromotionDecision 与双轨 Acceptance。
  - [x] Task 8：统一 Release CLI 与报告（`d2d4c89` 已推送）。
  - [x] Task 9：GitHub Actions 三层门禁（`3a34381` 已推送）。
  - [x] Task 10：ToolRegistry Facade 退役（`1f4af05` 已推送）。
  - [x] Task 11：显式 Release、默认路由与第二次 Release（`efe16c5` 已推送）。
  - [x] Task 12：Demo、Phase 15 Acceptance 与 Final Acceptance（`c01a5da` 已推送）。

## 2026-07-18 Phase 15 Task 8

- [x] Task 8：统一 Release CLI、覆盖率门禁和 GitHub Actions 证据读取入口（`d2d4c89` 已推送）。
- RED：非法 mode、Manifest/Subject 不匹配、数据库缺失、覆盖率不足和外部证据缺失必须有稳定非零退出码或明确 `BLOCKED`。
- 约束：复用 `src/release_gates`，PR/Nightly 不调用真实模型；不修改用户已有脏文件。

# 2026-07-11 Phase 7A 任务

- [x] 提交 Phase 6C 功能代码。
- [x] 提交编码治理和阶段记录。
- [x] 新增 AgentReplayService 和回放模型。
- [x] 新增规则评估器和维度分模型。
- [x] 新增内存 Store、PostgreSQL Store 和 Worker。
- [x] 新增 LLM Judge 结构化接口。
- [x] 扩展 FastAPI 评估接口和 WebSocket 消息。
- [x] 新增 `/evaluation` 运维页面。
- [x] 跑全量测试、demo、编码扫描和 diff 检查。

---

# 2026-07-18 Phase 16 Task 6 GREEN / REVIEW

- RED 已确认：Task 5 Coordinator 不存在 Planner 段，无法形成完整多 Agent Proposal 或 READY Outcome。
- GREEN 已实现同 Bundle/已验证 Analysis 的 Planner 输入、严格 Profile 身份、整份 Proposal Validator、
  append-only Proposal/READY 父链、重启恢复和无 fallback 降级；OperatorDecision 继续是唯一经营恢复权限主体。
- D-148 固定 Analyst `2s/1200/0.03`、Planner `2s/2800/0.07` 与 Coordinator
  `5s/4000/0.10` 聚合预算，并规定 PostgreSQL 多 Agent Proposal/READY 必须经 Store 事务上下文。
- 当前仅处于 REVIEW，尚未提交；真实模型费用为 `0.000000 CNY`。规格只读审查
  `019f75d1-1c20-7b40-8b92-4bd1eadc3560` 已登记，主模型负责整改、全量验证、提交和推送。

## 2026-07-18 Phase 16 Task 6 REVIEW 整改

- 独立规格审查完成并关闭；三项 Important 均已按 D-149 收口：Planner 绑定 Analysis 的
  持久化单次 claim、Proposal 已写而 READY 未写的恢复闭合、以及覆盖 Analyst 的 5 秒总预算。
- 同时修复了历史普通 Proposal 被错误按多 Agent Schema 重载的回归。显式 `MULTI_AGENT` 才进入
  Task 6 Validator，Phase 14 通用审计快照维持原有契约。
- 新增内存并发/恢复/预算 RED-GREEN，以及 PostgreSQL claim 并发、跨 Store Coordinator 并发、
  Proposal/READY 恢复和 direct SQL context 门禁。真实模型费用仍为 `0.000000 CNY`。

## 2026-07-18 Phase 16 Task 6 REVIEW 整改二至四

- [x] D-152：通用 Proposal 创建拒绝 `MULTI_AGENT`，仅 Coordinator 专用 Store/DDL context 可写入。
- [x] D-152：多 Agent `APPROVE/MODIFY` 必须提供 Proposal/Analysis/Escalation 精确匹配的 READY Outcome。
- [x] D-152：全局 deadline 限制 Planner 的 timeout 归类为 `COORDINATOR_TIMEOUT`，可按受限规则闭合。

- [x] D-151：Analyst/Planner 的模型返回、验证和每个派生事实 append 前重检同一五秒 deadline。
- [x] D-151：Planner 输入仅含 `evidence_bundle` 与 `analysis`，不传 Escalation 控制面字段。
- [x] D-151：REVIEW 无父链闭合仅接受同 claim 的 `COORDINATOR_TIMEOUT`；内存、PostgreSQL Store 与
  直接 SQL trigger 测试同构覆盖。

- D-150 将五秒单调 deadline 前移到 `run_automatic`/`run_operator_requested` 入口，权威 Bundle
  重载与选择器耗时不再获得新的模型预算。
- Planner 已发送但 Proposal/READY 在 LIVE 内未闭合时，REVIEW 恢复只能追加无 Analysis/Proposal 的
  `DEGRADED` 审计终态；没有 Planner claim 的历史 Analysis 保持原状。
- 内存与 PostgreSQL Store/DDL 均收紧为 Analyst 无 Analysis 或 Planner 已发送两种受限闭合来源，
  并新增对应 RED/GREEN。真实模型费用仍为 `0.000000 CNY`。
