# LiveAgent 工作日志计划

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
- [ ] Phase 12B：Event Inbox、抢占、紧急 DAG 与增量 Replan（当前下一实施阶段）。
- [ ] Phase 13：三个 Specialist Agent 候选评估与条件化去留。
- [ ] Phase 14：Golden Dataset、发布门禁与 Final Acceptance。

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
