# LiveAgent 工作进度记录

## 2026-07-11

- 启动文档编码治理，目标是先防止继续乱码，再处理已有风险文档。
- 新增只读编码扫描脚本 `scripts/check_doc_encoding.py`。
- 新增编码规范文档 `docs/project_guidance/document_encoding_policy.md`。
- 将 `docs/worklog/` 从忽略目录调整为可追踪工作日志目录。
- 明确后续文档写入规范：优先 `apply_patch`，避免 PowerShell heredoc / 管道写大段中文。
- 追加项目状态和阶段执行日志，确保后续迭代能看到本次治理背景。

## 下一步

- 每个阶段完成后，继续按“测试记录、反馈、遗留限制、后续迭代方向”四类信息补充留迹。
- 如果继续推进 Phase 6C/Phase 7，需要先确认文档编码扫描通过。

## 2026-07-11 Agent 架构评估进度

- 已阅读四份 study 文档，抽取 Agent Harness、多 Agent、Skill、Workflow 的区分标准。
- 已阅读项目核心代码，包括 README、播前 graph、播中普通 Agent graph、播中 Harness Agent graph、Harness Planner、ToolRegistry、LifecycleHooks、Context、ToolExecutor、Audit、Replay、Evaluation、Harness Dashboard Service。
- 已使用两个只读 sub-agent 分别并行分析 study 文档和代码实现边界，结论与主线程一致。
- 已删除误建在项目根目录的重复 `task_plan.md`、`findings.md`、`progress.md`，改为更新 `docs/worklog/` 下既有日志。
- 当前评估结论的历史表述曾强调“播前 Workflow + 播中单体 Agent Harness”；后续已纠偏为三场景全链路主播 Agent Runtime，播前、播中、播后分别处于不同技术成熟度。

## 2026-07-11 Agent Runtime 架构讨论进度

- 已确认未来 12 周采用双线平衡，但以架构主轴约 65%、生产约束约 35% 的单阶段整合方式推进。
- 已确认平台接入采用契约优先和 Fake Adapter，不把真实淘宝生产 API 作为本周期验收条件。
- 已确认 Skill Runtime 采用渐进升级：Python + Pydantic Catalog、Manifest 唯一事实源、单活版本钉住、异步 Executor 和同步适配器。
- 已确认 PlanEngine 采用 LLM 提案、确定性执行，使用独立 PlanStore、不可变版本、受控节点状态和有界并发。
- 已确认首期垂直场景为手卡生成过程中的售罄抢占，使用协作式冻结和依赖闭包 + 输入指纹完成增量 Replan。
- 已修正早期预设多个 Specialist Agent 的方案，改为先建立固定子图基线，再以严格量化门槛评估 Specialist Agent 候选。
- 已确定长期文档采用总路线、决策日志、每阶段 Design/Plan/Acceptance 和三个 worklog 的分层体系。
- 本次只持久化架构讨论，没有修改业务代码，也没有开始 Phase 11A 实施。
- 下一讨论项为 PlanEngine 失败分类、自动重试边界、Replan 触发条件、人工处理条件和紧急 DAG 失败后的恢复策略。

### 首次持久化验证（D-001 至 D-023 草案）

- `git diff --check` 返回 0；Git 只提示现有工作区的 LF/CRLF 转换策略，没有空白错误。
- 六个目标文档均通过严格 UTF-8 解码检查，不含 UTF-8 BOM、replacement char、混合换行或尾随空白。
- 决策日志共 23 个唯一编号；标准决策字段完整，`D-018` 正确标记为 `SUPERSEDED`，`D-023` 正确标记为 `OPEN`。
- 全仓 `python scripts/check_doc_encoding.py` 仍被历史问题阻断，共报告 4 个错误和 57 个警告；4 个错误均来自现有扫描脚本自身的 replacement-char 示例，本次六个目标文档没有出现在错误或警告列表中。

## 2026-07-11 PlanEngine 失败语义讨论进度

- 已将 D-023 从开放问题收敛为“结构化失败事实 + 集中式 FailurePolicy”，并固定 8 类失败事实和 5 类恢复动作。
- 已确认 PlanEngine 是唯一自动重试所有者；只读操作最多 3 次，可靠幂等写最多 2 次，副作用未知等失败禁止自动重试。
- 已确认退避通过持久化 `RETRY_WAIT` 调度，使用指数退避、抖动、`Retry-After` 和节点 deadline，不在线程内等待。
- 已确认 Replan 使用确定性触发矩阵，每个 root plan 最多创建 2 个新版本，并用失败签名和输入指纹阻止等价循环。
- 已确认新增 `WAITING_RECONCILIATION`，与执行前 `WAITING_APPROVAL` 分离；审批和对账 TTL 分别为 10 分钟和 30 分钟。
- 已确认紧急 DAG 失败后按 impact scope 恢复：局部风险阻断受影响分支，全局风险未解除时保持整张计划冻结。
- 本轮只更新架构文档，没有修改业务代码；下一讨论项为 PlanStore 与 LangGraph checkpoint 的一致性协议。

### 失败语义持久化验证

- 决策日志已扩展为 D-001 至 D-027，共 27 个唯一编号；除保留历史格式的 D-018 外，标准决策字段完整。
- D-023 已由 `OPEN` 更新为 `ACCEPTED`；D-024 至 D-027 分别记录重试、Replan、人工处理和紧急 DAG 恢复策略。
- 六个目标文档通过严格 UTF-8 解码和字节往返检查，不含 BOM、replacement char、混合换行或尾随空白。
- `git diff --check` 返回 0；全仓编码扫描仍被既有的 4 个错误和 57 个警告阻断，本次目标文档未被命中。

## 2026-07-11 PlanStore 与 Checkpoint 一致性讨论进度

- 已确认 PlanStore 是节点执行事实权威源，节点结果提交后 graph 才返回并推进 checkpoint。
- 已确认 PlanStore 领先时从旧 checkpoint 重放并复用成功结果；checkpoint 领先时冻结计划并人工对账。
- 已确认 Worker 使用 `FOR UPDATE SKIP LOCKED`、lease 和 fencing token，租约按 Skill timeout 派生并心跳续租。
- 已确认审批、对账和恢复命令使用 Command Ledger、唯一 command_id、预期计划版本和预期节点状态。
- 已确认对账服务在启动、每 30 秒周期扫描和人工命令前运行，且不直接修改官方 checkpoint 表。
- 本轮只更新架构文档，没有修改业务代码；下一讨论项为 Phase 11A 兼容迁移、回滚和验收边界。

### 一致性决策持久化验证

- 决策日志已扩展为 D-001 至 D-034，共 34 个连续唯一编号；除保留历史格式的 D-018 外，标准字段完整。
- 六个目标文档通过严格 UTF-8 解码和字节往返检查，不含 BOM、replacement char、混合换行或尾随空白。
- 路线图和 task plan 均将下一讨论项指向 Phase 11A 兼容迁移、回滚和验收边界。
- `git diff --check` 返回 0；全仓编码扫描仍有既有的 4 个错误和 57 个警告，本次目标文档命中数为 0。

## 2026-07-12 Phase 11A 兼容迁移讨论进度

- 已确认 ToolRegistry 采用“冻结旧元数据影子校验 -> 13 项完全一致 -> 统一切换 Manifest 投影”的方式，切换后不保留旧元数据回退。
- 已确认首批 Handler 为 `query_products`、`generate_live_plan`、`generate_product_card` 和 `setup_live_session`。
- 已确认分两批迁移：前三个读取与确定性生成能力先迁移，`setup_live_session` 单独迁移。
- 已确认按批次显式路由；第一批只在测试或 Fake Adapter 环境做有限影子比较，写操作始终单路执行。
- 已确认调用开始时钉住执行路径，回滚只影响新调用，不允许单次调用失败后隐式切换执行器。
- 已确认 Manifest、Schema、生命周期、风险门禁、版本、审计和幂等不变量零容忍。
- 已确认 Phase 11A 使用契约与行为双门禁，ToolRegistry 兼容 API 保留至 Phase 12 验收。
- 已将以上选择及淘汰理由写入 D-035 至 D-042，并更新 Agent Runtime 总路线图。
- 已生成 `docs/superpowers/specs/phase-11a-skill-runtime-design.md`，当前状态为待用户审核。
- 本轮详细架构讨论到 Phase 11A 边界结束；Phase 11B 至 Phase 14 只保留大纲，后续按 Just-in-Time 方式设计。
- 本轮仅修改架构文档，没有修改业务代码、没有运行业务测试，也没有生成 Implementation Plan。

## 下一步

- 用户审核 Phase 11A Design。
- 审核通过后，再单独生成 Phase 11A Implementation Plan。
- 在 Implementation Plan 获得确认前，不修改业务代码。

### Phase 11A 决策与 Design 持久化验证

- 决策日志已扩展为 D-001 至 D-042，共 42 个连续唯一编号；标准字段完整，D-018 保留历史替代格式。
- 路线图、决策日志、Phase 11A Design 和三个 worklog 的当前状态均指向“Design 待用户审核”。
- 六个目标文档通过严格 UTF-8 解码和字节往返检查，不含 BOM、replacement character、混合换行或尾随空白。
- `git diff --check` 返回 0；输出只有现有 Git 换行策略提示，没有空白错误。
- 全仓编码扫描仍报告既有的 4 个错误和 57 个警告；4 个错误来自 `scripts/check_doc_encoding.py` 自身的 replacement-character 检测样例，本次六个目标文档命中数为 0。
- 按本轮范围未运行业务测试。

## 2026-07-12 Phase 11A Design 审核与实施计划进度

- 已对照 ToolRegistry、AgentToolExecutor、PreLiveBusinessFlowService、PreLiveGraph、human approval 和项目依赖审核原 Design。
- 已确认原 Design 存在输入 Schema 与真实执行语义错位、第一批审计副作用、审批证据信任边界、Graph 接入点和可选 Schema 校验五类问题。
- 已选择显式不可变快照作为四个核心 Skill 输入；控制字段、幂等键和审批证据进入可信 SkillExecutionContext。
- 已选择测试专用隔离比较器，正式 Router 删除 `SHADOW_COMPARE`。
- 已选择 ApprovalContext + TRUSTED_COMPAT 内部映射、兼容 Facade + 同步桥接、正式 jsonschema 依赖和启动配置 + 构造注入。
- 已选择 AgentToolExecutor 增加旧参数规范化适配，并委托统一 Runtime。
- 已将 D-035 标记为 `CONDITIONAL`、D-038 标记为 `SUPERSEDED`，新增 D-043 至 D-049 保留评审选择与淘汰理由。
- 已重写并冻结 `phase-11a-skill-runtime-design.md`。
- 已生成 `docs/superpowers/plans/2026-07-12-phase-11a-skill-runtime-plan.md`，包含九个 TDD 任务、精确文件、测试命令、提交边界和完成条件。
- 本轮只修改文档，没有修改业务代码、测试、依赖或配置，也没有运行实施计划中的业务测试。

## 下一步

- 用户审核 Implementation Plan 并选择执行方式。
- 确认后按 Task 1 至 Task 9 实施；每个任务先红灯、再最小实现、再绿灯。
- Phase 11A Acceptance 审核完成前不讨论 Phase 11B 详细设计。

### Phase 11A Design 审核与 Implementation Plan 持久化验证

- 决策日志已扩展为 D-001 至 D-049，共 49 个连续唯一编号；D-035 为 `CONDITIONAL`，D-038 为 `SUPERSEDED`，其余本轮新增决策为 `ACCEPTED`。
- Design、Implementation Plan、路线图和 task plan 当前状态一致，均指向“Implementation Plan 待执行”。
- Implementation Plan 包含 9 个任务和 55 个可跟踪步骤，无 TBD、TODO、`implement later` 或“自行决定”等占位表达。
- 七个目标文档通过严格 UTF-8 解码与字节往返检查，不含 BOM、replacement character、混合换行或尾随空白。
- `git diff --check` 返回 0；输出仅包含工作区既有 Git 换行策略提示。
- 全仓编码扫描仍报告既有的 4 个错误和 57 个警告；本轮七个目标文档命中数为 0。
- 按本轮范围未运行业务测试，未修改业务代码、测试、依赖或配置。

## 2026-07-12 Phase 11B-14 高层大纲持久化进度

- 已在 Agent Runtime 路线图新增 Phase 11B、12A、12B、13、14 的独立高层大纲。
- 每个阶段严格记录阶段目标、前置依赖、进入条件、退出条件和待决策项，不包含接口、Schema、表结构、类设计或实施步骤。
- 大纲内容来自 D-009 至 D-034、D-042 和现有 Agent 保留门槛的结构化汇总，没有新增业务架构决策编号。
- 已补充 D-003 的影响：远期阶段只保留五类边界，待决策项保持开放，详细 Design 和 Plan 按 Just-in-Time 方式生成。
- 当前主线仍是 Phase 11A Implementation Plan；本轮没有修改业务代码、测试、依赖或配置，也没有运行任何业务测试。

## 下一步

- 回到 Phase 11A，选择执行方式并按已冻结 Implementation Plan 开始 Task 1。
- Phase 11A Acceptance 通过后，才详细讨论 Phase 11B Design。

### Phase 11B-14 高层大纲持久化验证

- Phase 11B、12A、12B、13、14 均恰好包含阶段目标、前置依赖、进入条件、退出条件和待决策项五个字段。
- 大纲与 D-009 至 D-034、D-042、确定性 PlanEngine 边界和当时的 LiveOpsAgent 量化保留门槛一致；后续已将该门槛泛化为三场景 Specialist Agent 候选的默认保留门槛。
- 路线图与 task plan 的下一执行项仍为 Phase 11A Implementation Plan。
- 五个目标文档通过严格 UTF-8、字节往返、无 BOM、无 replacement character、无混合换行和无尾随空白检查。
- `git diff --check` 返回 0；全仓编码扫描仍有既有的 4 个错误和 57 个警告，本轮目标文档命中数为 0。

## 2026-07-12 三场景定位与上下文恢复提示词持久化进度

- 已将项目当前定位从过窄的播中单体 Harness 表述，纠偏为面向播前、播中、播后三场景的全链路主播 Agent Runtime。
- 已明确当前实现形态：播前偏 Workflow / Graph，播中已有单体 Agent Harness，播后偏 Replay / Evaluation / 复盘流程。
- 已固定 Tool、Skill、Agent、PlanEngine 和 Orchestrator 的技术分层，避免把业务三场景机械等同于三个 Agent。
- 已新增 D-050 至 D-052，并将 D-019 标记为 `SUPERSEDED`；D-020 的量化门槛被泛化为所有 Specialist Agent 候选的默认保留门槛。
- 已将 Phase 13 从单一 LiveOpsAgent 对照升级为三场景 Agent 化评估与试点，候选包括 PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent。
- 已新增 `docs/project_guidance/agent_runtime_context_recovery_prompt.md`，用于后续上下文压缩后恢复定位、阶段状态、技术边界和执行约束。
- 本轮只修改文档，没有修改业务代码、测试、依赖或配置，也没有开始 Phase 11A 实施。

## 下一步

- 回到 Phase 11A，执行前重新读取 Phase 11A Design 和 Implementation Plan。
- Phase 11A Acceptance 通过后，才详细讨论 Phase 11B Design。

## 2026-07-12 Phase 11A Task 1-6 实施纠偏进度

- 已核对提交历史：Task 1-4 各有提交，但原测试覆盖不足；AgentToolExecutor 兼容提交被误标为 Task 6，实际属于 Task 7。
- 已修正模型、Catalog、Executor 和 Handler 的关键契约，使 query -> plan -> card -> setup 可以使用原生输出连续执行。
- 已完成 Task 5 路由与 Facade 重构：默认 LEGACY、非法配置 fail-fast、路由冻结、Runtime 失败不 fallback、Graph 领域对象接口保持不变。
- 已完成真正的 Task 6：批准恢复生成带 operator_id 和 approval audit ID 的 HUMAN_INTERRUPT 证据；拒绝恢复不调用 setup Handler。
- 已新增真实 Runtime Graph 集成测试，覆盖 PostgreSQL 货盘、审计、三张手卡、interrupt、批准恢复和拒绝无副作用。
- Task 1-6 冻结计划专项命令当前共 108 个测试通过；Task 7 尚未执行，旧 AgentToolExecutor 测试的红灯将由该任务的参数规范化和单一 dispatch 处理。

## 下一步

- 按冻结计划执行 Task 7，不把旧参数重新放回核心 Skill Schema。
- Task 7 先用失败测试固定 product_id 补全、计划快照补全、兼容证据和 setup 审批语义，再修改 AgentToolExecutor。

## 2026-07-12 Phase 11A Task 7-9 验收进度

- Task 7 已完成 AgentToolExecutor 四个核心工具单一 Runtime dispatch、旧参数规范化、`compatibility_enriched` 证据和可信边界硬化；正式提交从 `4f77403` 开始，为 `4f77403`、`7e132f3`、`b60a85d`。`96a5adb` 是提前错误实施，已由 `94e2766` 完整删除，不计入有效交付。
- Task 8 已完成隔离等价测试、四场景 Demo 与 `run_all.py phase11a-demo` 入口；提交为 `7154c89`、`fd54005`。
- Runtime 专项命令退出码 `0`：`85 passed in 1.43s`，无 deselected、无 warnings。
- 相关回归命令退出码 `0`：`45 passed in 0.89s`，无 deselected、无 warnings。
- `pytest -q` 退出码 `0`：`501 passed, 3 deselected, 9 warnings in 54.13s`；warnings 为现有 FastAPI/Starlette 与 Kafka 弃用告警。
- `python scripts/run_phase11a_skill_runtime_demo.py` 退出码 `0`；全 legacy、第一批 Runtime、两批 Runtime、setup 回滚四场景均输出 4 商品、4 计划项、3 手卡、`prepared` 和 8 条审计。
- `python scripts/run_all.py phase11a-demo` 退出码 `0`，作为同一四场景 Demo 的统一运行入口复现相同结果。
- `python scripts/check_doc_encoding.py` 退出码 `1`：`4 errors/58 warnings`。4 个 error 均命中扫描脚本自身 U+FFFD 示例；历史 BOM/工作树混合换行 warning 仍保留，本任务未修改脚本或顺手治理。
- 初次 `git diff --check` 退出码 `0`，仅输出 Git 的 LF/CRLF 转换提示；范围检索只命中 `compatibility.py` 中“未来 PlanEngine 不应复用兼容层”的禁止说明，没有 PlanEngine 实现。
- Task 9 已生成 Acceptance 并同步路线图、执行日志和 worklog；状态只能记为“技术验收完成，待用户审核”，Phase 11B 未开始。
- Task 9 质量审查补充纳入冻结决策日志、Design 和 Plan，并为 Acceptance 增加两条完整 pytest 复现命令；本轮不重跑业务测试，沿用已记录的真实测试证据。

## 2026-07-12 Phase 11A 验收前幂等复审整改进度

- 审查发现审计 Store 的并发重放算法依赖 `READ COMMITTED`，已在首条 SQL 前显式固定连接隔离级别，并新增单元测试锁定该顺序。
- 审计 Store 已对同工具同幂等键的重放比较完整事实；不同 room、trace、计划、载荷或 JSON 类型均 fail-closed，首次审计行保持不变。
- 等价比较器与业务流 FakeAuditStore 已同步完整事实和 JSON 严格比较语义；真实播前集成流程改用 UUID trace，避免历史数据影响派生幂等键。
- 本轮专项为 `28 passed`，Runtime 专项为 `108 passed`，相关回归为 `45 passed`，默认全量为 `541 passed, 3 deselected, 9 warnings`；两个四场景 Demo 均通过。
- 全范围 `git diff --check 8f386cd^..HEAD` 已通过；全仓编码扫描仍为 `4 errors/59 warnings`，均按脚本自身样例或既有 BOM/混合换行记录，未修改业务范围外文件。
- 当前仍是“Phase 11A 技术验收完成，待用户审核”，Phase 11B 未开始。

## 2026-07-12 Phase 11A 最终审查 P1 整改进度

- 最终全阶段审查发现 `HUMAN_INTERRUPT` 可被普通对象直接伪造，以及 9 个未迁移 Manifest 根 Schema 未拒绝额外字段；Acceptance 未对这两项放行。
- `HUMAN_INTERRUPT` 已改为仅能由 Graph 在审批响应校验和审计写入后使用内部工厂创建；全部 13 个 Manifest 根 Schema 显式 `additionalProperties: false`，并更新冻结哈希。
- Demo 同步改用受控工厂。Runtime 专项为 `110 passed`，默认全量为 `543 passed, 3 deselected, 9 warnings`；Phase 11B 仍未开始。

## 2026-07-12 Phase 11B Design 持久化进度

- 用户已审核并接受 Phase 11A Acceptance，Phase 11A 正式完成。
- 已按 Just-in-Time 原则完成 Phase 11B Design 讨论，并新增 D-054 至 D-062：业务域 Port、有状态 Fake、绝对 deadline、原生 async、FailureFact、Attempt Store、三批路由、不可达 switch_product 清理、版本规则和验收门槛。
- 已生成 `docs/superpowers/specs/phase-11b-unified-execution-platform-contract-design.md`；当前状态为“Phase 11B Design 待用户审核”。
- 本轮只修改架构文档，没有修改业务代码、测试、依赖、配置或用户既有未提交文件，也没有运行业务测试。

## 下一步

- 用户确认执行 Phase 11B Implementation Plan。
- 执行前重新读取 Phase 11B Design、Plan、决策日志和 worklog；按 TDD 实施 Adapter、Attempt Store 和三批 Handler 迁移，不提前实现 PlanEngine 或多 Agent。

## 2026-07-12 Phase 11B Design 审核与实施计划进度

- 用户已审核并接受 Phase 11B Design，Design 状态改为已冻结。
- 已生成 `docs/superpowers/plans/2026-07-12-phase-11b-unified-execution-platform-contract-plan.md`，覆盖 Runtime 模型、Attempt Store、有状态 Fake、三批 Handler/路由迁移、同步桥接、Demo 与最终验收。
- 本轮只修改设计与计划文档，没有修改业务代码、测试、依赖、配置或用户既有未提交文件，也没有运行业务测试。

## 下一步

- 用户确认执行 Phase 11B Implementation Plan。
- 执行中必须按每个 Task 的 RED、GREEN、REFACTOR 和独立提交边界推进；任一关键不变量失败不得进入后续批次。

## 2026-07-12 Phase 11B Task 1 进度

- 用户已批准执行 Phase 11B Implementation Plan，当前在 `main` 工作区按既有提交链推进。
- 已按 TDD 新增 `test_phase11b_models.py`；首次运行因缺少 `FailureCategory` 在收集阶段失败，随后实现 FailureFact、时区感知 deadline、AdapterRequest/AdapterSuccess、尝试上限和结果关联字段。
- Task 1 专项加既有 Executor/Catalog 回归为 `32 passed in 0.48s`，并完成 diff 审查。
- 已提交 Task 1：`3e33ec3 feat: add phase 11b runtime contracts`。
- 尚未开始 Attempt Store、Fake Adapter、async Executor、剩余 Handler、路由或任何 Phase 12 代码。

## 2026-07-12 Phase 11B Task 2 进度

- 已按 TDD 新增 Attempt Store 单元测试；首次因模块缺失在收集阶段失败，随后实现内存 Operation/Attempt Store。
- 已新增 PostgreSQL 集成测试和独立 Attempt DDL。组合运行发现 unit/integration 同名测试模块冲突，已重命名集成文件为 `test_phase11b_postgres_attempt_store.py`。
- 并发集成测试进一步发现 SQL 只处理业务唯一索引冲突、未处理确定性 Operation ID 主键冲突；已改为 `ON CONFLICT DO NOTHING` 后读取并校验首次意图，组合验证为 `10 passed in 0.95s`。
- Task 2 专项为 `7 passed in 0.88s`，迁移 dry-run 包含 `phase11b`；已提交 `5033dcf feat: persist phase 11b execution attempts`。

## 2026-07-12 Phase 11B Task 3 进度

- 已按 TDD 建立三个原生 async 业务域 Port：商品与价格、直播会话、播中运营；新增实例级有状态 Fake、冻结 Fixture 和按操作、资源、调用序号匹配的声明式故障脚本。
- 首轮红灯覆盖 Port/Fake 缺失；随后发现售罄写操作在 `UNKNOWN_AFTER_SEND` 时错误返回成功，已补回归测试并修正为“状态可见、结果未知”的 `SIDE_EFFECT_UNKNOWN` 事实。
- Task 1 至 Task 3 聚焦验证为 `17 passed in 0.94s`；本任务代码提交前未运行全量业务测试，也未修改 PlanEngine、多 Agent、真实淘宝 API 或动态插件机制。
- 下一项为 Task 4：将 SkillExecutor 收敛为原生 async 单次尝试，接入 deadline、Attempt Store 与 FailureFact 传播。

## 2026-07-12 Phase 11B Task 4 进度

- 已将 SkillExecutor 收敛为唯一原生 async 单次尝试核心；四个既有播前 Handler 改为 async 签名，旧同步 Graph 只经拒绝嵌套事件循环的 `SyncSkillExecutorAdapter` 调用该核心。
- 对带幂等键的调用，Runtime 先写入或重放唯一 Operation；首次调用才检查 deadline。发送前 deadline 到期闭合为 `TRANSIENT_INFRA/NOT_SENT`，Handler 已开始后 timeout 闭合为 `SIDE_EFFECT_UNKNOWN/UNKNOWN`，两者均不调用 Legacy fallback 或重试。
- 成功终态以 Runtime 私有包络持久化业务输出和兼容审计关联，重放可返回首次 `audit_id`，避免建播幂等重放丢失既有审计证据。
- Task 4 专项为 `19 passed in 0.41s`；全量 unit 为 `501 passed, 4 warnings in 6.19s`，warnings 是现有 FastAPI/Starlette 与 Kafka 弃用提示。本任务未修改 PlanEngine、多 Agent、真实淘宝 API 或动态插件机制。
- 下一项为 Task 5：建立 13 个 Handler 的统一局部装配，并迁移批次一能力到业务域 Port 与统一 Handler 工厂。

## 2026-07-13 Phase 11B Task 5 暂停记录

- 已按 TDD 写入批次一统一 Handler 装配红灯并验证当前缺少 `handlers.py`；在实现前对照三个 Port、Manifest 与既有播中领域函数，发现备选商品和主播提示两个批次一 Skill 缺少获得可信商品状态的契约。
- 为避免工作树遗留预期失败测试，已撤回本次尚未对应生产实现的红灯测试；没有修改业务代码、没有提交 Task 5。
- 等待确认最小 Design 修正后恢复 Task 5，不能通过旧 Graph 状态读取、模拟结果或隐式 Legacy fallback 绕过该缺口。

## 2026-07-13 Phase 11B Task 5 设计纠偏进度

- 用户已批准最小 Port 契约修正。
- 已新增 D-063：`LiveOperationsPort.resolve_product_context` 作为只读商品上下文解析方法，返回售罄商品和可选备选商品可信快照。
- 该修正不新增 Skill、不改变公开参数 Schema、不升级 `1.0.0` 版本、不允许 Legacy fallback；接下来恢复 Task 5 的 RED/GREEN/REFACTOR。

## 2026-07-13 Phase 11B Task 5 完成进度

- 已按 TDD 新增 `tests/unit/test_phase11b_handlers_batch1.py`，红灯为缺少 `src.skill_runtime.handlers`；新增 Fake 只读上下文测试，红灯为缺少 `resolve_product_context`。
- 已实现 `LiveOperationsPort.resolve_product_context` 和 Fake 同名只读方法；缺失售罄商品返回 `INVALID_INPUT`，成功解析不修改库存、价格、版本或会话状态。
- 已新增统一 `build_skill_handlers()` 与 `SkillRuntimeDependencies`，批次一 10 个 Skill 均由局部工厂装配；`pre_live_handlers.py` 收敛为兼容装配层，不再维护第二套播前核心 Handler 逻辑。
- 回归中发现 Runtime 生成路径审计少于 legacy，已修正为兼容装配下继续通过 `PreLiveBusinessFlowService` 写排品和手卡审计，并通过内部 `__trace_id` 保留原 trace。
- Task 5 专项与等价回归为 `38 passed`；AgentToolExecutor/Graph 相关回归为 `77 passed`；默认 unit 全量为 `517 passed, 4 warnings`。Warnings 为既有 FastAPI/Starlette 与 Kafka 弃用提示。
- 已运行 `git diff --check`，仅出现 Windows 行尾提示，无尾随空白错误。未实现 Task 6 路由、批次二/三、PlanEngine、多 Agent 或真实淘宝 API。

## 2026-07-13 Phase 11B Task 8 契约纠偏进度

- Task 8 实施前发现 `set_product_price@1.0.0` 的 Manifest 没有 `expected_version`，但现有 ProductPricingPort 已以该资源版本执行 CAS；严格 Schema 下无法合法表达成功改价调用。
- 用户选择方案 A：把 `expected_version` 作为显式业务参数，并把改价 Skill 升级为单活 `1.1.0`；旧 `1.0.0` 调用受控返回 `VERSION_MISMATCH`，资源版本过期仍由 Adapter 返回 `VERSION_CONFLICT`。
- 用户选择 AgentToolExecutor 保持 pending：不新增批准参数或专用批准方法；可信批准只由内部 `SkillCall + ApprovalContext` 集成测试证明，未来 Graph/Facade 接入另行设计。
- 发现冲突的首个 Task 8 实现代理在报告前未修改业务代码、测试、依赖或配置；本轮先持久化 D-064、Design、Implementation Plan 与工作日志，业务 TDD 尚未开始。
- 本轮文档纠偏将完成后执行编号、UTF-8、编码扫描、`git diff --check` 和暂存范围验证，再以独立文档提交保存。

## 2026-07-14 Phase 11B Task 8-10 验收进度

- Task 8 已完成高风险改价迁移：`set_product_price` 单活版本为 `1.1.0`，显式要求 `expected_version`，幂等键和审批保留在可信 Context；非法价格在 Attempt 前拒绝。契约与实现提交为 `5ca05cf`、`76afbdf`、`3feab86`。
- Task 9 已完成真实 Legacy 建播与 Runtime 的隔离契约比较、Runtime-only 改价失败测试、六场景无外部依赖 Demo 和 `run_all.py phase11b-demo`；最终提交为 `778d52b`。
- Runtime 专项退出码 `0`：`76 passed in 1.54s`。
- 原系统回归命令引用不存在的 `tests/integration/test_phase11b_attempt_store.py`，退出码 `4` 且未收集测试；改用真实文件 `test_phase11b_postgres_attempt_store.py` 后退出码 `0`：`124 passed in 6.61s`。
- 默认全量退出码 `0`：`636 passed, 3 deselected, 9 warnings in 63.48s`；warning 为既有 FastAPI/Starlette 与 Kafka 弃用提示。
- 直接 Demo 与 `run_all.py phase11b-demo` 均退出码 `0`，按固定顺序输出建播成功、售罄、限流、版本冲突、deadline 和副作用未知六个场景。
- `git diff --check` 退出码 `0`；全仓编码扫描退出码 `1`，仍为 `4 errors/59 warnings`。4 个 error 来自扫描脚本自身 U+FFFD 示例，warning 为历史 BOM/混合换行，本阶段目标文件命中为 0。
- 已生成 Phase 11B Acceptance，并完成专项、系统回归、全量、Demo 和文档验收留迹。
- 用户已于 2026-07-14 审核并接受 Phase 11B Acceptance，Phase 11B 正式完成。当前尚未实施 PlanEngine、自动重试、真实淘宝 API 或多 Agent。

## 下一步

- 已完成 Phase 12A Just-in-Time Design 讨论：首期为冻结排品后的手卡批次，使用固定候选 DAG、类型化绑定、关系行 + JSONB PlanStore、独立 Worker、Capability Profile 资源锁、默认 Legacy 路由和通用 Command Ledger。
- 已生成 `docs/superpowers/specs/phase-12a-dag-plan-engine-design.md`，并新增 D-065 至 D-072；本轮未修改业务代码、未生成 Implementation Plan、未进入 Phase 12B。
- 用户已于 2026-07-14 审核并接受 Phase 12A Design，并已生成 `docs/superpowers/plans/2026-07-14-phase-12a-dag-plan-engine-plan.md`；当前仍未实施 PlanEngine。
- Implementation Plan 生成后等待用户确认执行；未确认前不修改 PlanEngine 业务代码。

## 2026-07-14 Agent Runtime 全程计划持久化进度

- 用户希望未来在无人监控和多次上下文压缩下连续实施，因此先要求把 Phase 12A 剩余至 Phase 14 的全部讨论持久化。
- 已明确本轮授权只覆盖文档，不修改业务代码、不执行 Phase 12A Task 6、不运行真实模型。
- 已新建 `agent_runtime_completion_master_plan.md`、`continuous_execution_state.md` 和 `agent_runtime_continuous_recovery_prompt.md`。
- 已修订 Phase 12A Design/Plan，增加 reconciliation 事故字段和 TRUSTED_COMPAT 退役 Task，剩余任务调整为 Task 6-9。
- 已生成 Phase 12B Event Inbox/抢占/Replan、Phase 13 三候选 Agent 评估、Phase 14 Golden/CI 发布门禁的 Design 与 Implementation Plan。
- 已新增 D-073 至 D-093，记录 ToolRegistry 退役、事件授权、紧急 DAG、Agent 评估、3 元预算、三级 CI 和最终默认路由等选择。
- 已更新路线图：Phase 12A Task 1-5 完成，远期 Design/Plan 已冻结但实施未授权。
- 当前正在执行文档编号、状态一致性、UTF-8、编码和 diff 验证；验证完成前不提交。
- 网络中断恢复后已核对 `main`、`origin/main` 与业务基线 `37d6f8a`，没有半提交，Phase 12A Task 6 仍为 `NOT_STARTED`。
- 前一轮组合校验曾因 JavaScript 包装字符串报 `SyntaxError: Unexpected identifier 'r'`，本轮改为拆分命令；首次决策分段正则只识别 1 项，已改用按标题行切片并确认 D-001 至 D-093 共 93 项连续唯一、标准字段齐全。
- 一次跨文件 `apply_patch` 因总控计划预算原句上下文不匹配而在校验阶段整体拒绝，未产生部分写入；随后按文件拆分补丁并逐段复核。
- 可执行性复核修正了四类计划缺口：Phase 12B 售罄版本切换时序、ToolRegistry 生产消费者迁移范围、Phase 13 持久化模型预算与播后 Port 边界、Phase 14 Golden 数据来源及 Release 后默认路由晋升顺序。
- 当前目标文档的首次严格检查结果为 16 个文件全部 UTF-8 无 BOM、无 U+FFFD、统一 LF、无尾随空白；计划修订后仍需重新执行完整检查，不能沿用修订前证据。
- 修订后重新验证：四份 Implementation Plan 的 Task 编号分别连续为 1-9、1-11、1-10、1-10；不存在 TBD、TODO、待确认或由实现者决定等占位项。
- 决策日志按标题行切片确认 D-001 至 D-093 共 93 项连续唯一，每项均包含状态、背景、候选方案、最终选择、选择理由、未选理由、影响和重新评估条件。
- 16 个本轮目标文档严格 UTF-8 解码及字节往返通过，BOM、U+FFFD、混合换行和尾随空白命中数均为 0；阶段状态断言全部通过。
- `git diff --check` 退出码 `0`，仅输出 Git for Windows 的未来 LF/CRLF 转换提示，没有空白错误。
- 全仓 `python scripts/check_doc_encoding.py` 退出码 `1`，报告 `4 errors/58 warnings`：4 个 error 都来自扫描脚本自身的 U+FFFD 检测示例，58 个 warning 是本轮目标外的历史 BOM/混合换行；16 个目标文档命中数为 0。
- 本轮没有运行业务测试，因为未修改代码、依赖、数据库或运行配置，也未执行 Phase 12A Task 6。

## 下一步

- 完成本次目标文档验证，区分历史编码问题与新增问题。
- 只暂存本轮文档，提交 `docs: persist agent runtime completion plan` 并推送 `origin/main`。
- 文档提交后保持 `AWAITING_IMPLEMENTATION_AUTHORIZATION`，等待用户单独授权正式实施。

## 2026-07-15 Phase 12A-14 正式连续实施启动

- 历史上曾授权从 Phase 12A Task 6 连续执行至 Phase 14 Final Acceptance；该跨 Phase 授权已由 D-094 替换为当前 Phase 内连续、Phase 结束后 Just-in-Time Gate。
- 调整策略为受控自主调整：设计内修正可直接推进；公开接口、Schema、状态机、数据库或安全边界变化先写决策日志；不得放宽安全、预算和 Agent 去留门槛。
- 已恢复并核对 `HEAD=27a20e4`、`origin/main=27a20e4`，最新业务代码基线为 `37d6f8a`；用户既有 7 个脏文件保持未暂存。
- 当前进入 Phase 12A Task 6 `RED`，下一步只编写 checkpoint 双向不一致和命令前对账失败测试，不提前修改生产代码。
- Task 6 RED 已确认：单元与 DDL 契约命令得到 `7 failed, 5 passed`；六项失败明确指向缺少 `src.plan_engine.reconciliation`，一项失败指向 `plan_runs` 尚无 reconciliation 持久化字段。
- Task 6 GREEN 已实现：PlanRun 对账字段、内存/PostgreSQL Store、公开 checkpointer 读取、启动/周期/命令前三类入口和普通命令 fail-closed 门禁。
- 自审发现非法 checkpoint 引用原先只抛校验异常，新增红灯测试后改为持久化 `INTERNAL_INVARIANT` 并冻结活动计划。
- Task 6 相关回归为 `59 passed`；默认单元测试为 `816 passed, 4 warnings`；完整集成测试为 `77 passed, 3 deselected, 5 warnings`。
- PowerShell 不展开传给 pytest 的 glob，首次专项聚合命令退出 `4`，已改用 `Get-ChildItem` 生成精确文件数组并得到 `256 passed`。本机未安装 `ruff`，使用 compileall、完整测试、差异审阅与编码检查替代。
- Task 6 提交前重新取得新鲜证据：专项 `16 passed`、完整单元 `816 passed, 4 warnings`、完整集成 `77 passed, 3 deselected, 5 warnings`；严格 UTF-8、`compileall` 与 `git diff --check` 均通过。
- 一次完整集成测试的输出句柄因任务中断丢失，未将该次运行计入验收；确认无残留 pytest 进程后独立重跑并取得明确退出码 `0`。
- Task 6 已以 `6029ad3 feat: reconcile phase 12a plan checkpoints` 独立提交并推送 `origin/main`；缓存区只包含 12 个目标文件，7 个用户既有脏文件未纳入提交。
- 连续执行游标已切换到 Phase 12A Task 7 `RED`，先验证启动冻结路由、局部 Graph 接入和禁止同次 fallback。
- Task 7 RED 得到 `7 failed`：路由模块、Settings 字段、Graph state 与 bridge 均尚不存在，失败原因与冻结计划一致；开始最小 GREEN 实现。
- Task 7 GREEN 复用固定 Provider、Capability Profile、PlanStore、SyncPlanWorkerAdapter 和 SkillExecutor，新增启动冻结的 `LEGACY | PLAN_ENGINE` 手卡路由；新专项 `9 passed`。
- 自审新增“候选绑定冻结输入外商品”红灯，确认原实现会创建无效 PlanRun；最小修复后在 Store 前拒绝，测试由 `1 failed` 转为 `1 passed`。
- 旧播前 Graph/checkpoint/interrupt/Skill Runtime 回归 `18 passed`，Phase 12A 聚合 `266 passed`；进入完整验证。
- Task 7 提交前完整验证：默认单元测试 `824 passed, 4 warnings`，完整集成测试 `78 passed, 3 deselected, 5 warnings`；9 个目标文件严格 UTF-8、`compileall` 和 `git diff --check` 均通过。
- Task 7 已以 `7cbf026 feat: route pre-live cards through plan engine` 提交并推送 `origin/main`；用户既有脏文件未纳入提交。
- 连续执行游标切换到 Task 8 `RED`，开始清点并退役 `TRUSTED_COMPAT`，保持 Legacy 显式回滚路径不变。
- Task 8 RED 为 `3 failed, 26 passed`：兼容枚举、内部工厂和 Facade 的 `confirmed_setup` 提权均被新测试准确捕获；开始最小 GREEN 删除。
- Task 8 GREEN 删除兼容 token、枚举值、内部工厂和 Facade 映射；Runtime 分支只转发显式 `approval_context`，Legacy 仍消费旧 `confirmed_setup`。专项回归 `31 passed`，`src` 中兼容标识扫描为 0 命中。
- Task 8 提交前完整验证：默认单元测试 `824 passed, 4 warnings`，完整集成测试 `78 passed, 3 deselected, 5 warnings`；11 个审批链目标文件严格 UTF-8、`compileall` 与 `git diff --check` 均通过。
- Task 8 已以 `9a8e5a6 refactor: remove trusted compatibility approval` 提交并推送 `origin/main`；用户既有脏文件未纳入提交。
- 连续执行游标切换到 Phase 12A Task 9 `RED`，开始五场景 Demo 与最终阶段验收，不提前进入 Phase 12B 代码。
- Task 9 Demo RED 为 `4 failed`：新脚本和 `phase12a-demo` 子命令均不存在，失败原因与冻结计划一致；进入真实内存 Runtime 场景实现。
- Task 9 GREEN 建立五个隔离内存场景和 `run_all.py phase12a-demo` 入口；Demo 专项为 `4 passed`，两个入口均退出码 `0`，直接脚本只输出五行 JSON。
- Phase 12A 单元聚合为 `259 passed`；指定 PostgreSQL/PostgresSaver 集成聚合为 `14 passed`；默认全量为 `906 passed, 3 deselected, 9 warnings`。
- 数据库迁移 dry-run 退出码 `0`，识别 11 个迁移步骤并包含 required 的 Phase 12A；`git diff --check` 退出码 `0`。
- 初次编码扫描因本次 `run_all.py` 混合换行得到 `4 errors/59 warnings`；统一为 UTF-8 无 BOM/LF 后恢复到既有 `4 errors/58 warnings`，本次目标文件零命中。
- 首次严格目标检查因 PowerShell 的 `"$file: ..."` 变量语法退出，第二版正则又把行尾字母 `t` 误报为制表符；改用 `EndsWith(' ')` 与 `char(9)` 后，9 个目标文件严格 UTF-8/LF 检查通过。两次均为验证器问题，没有据此改动业务内容。
- 已生成 Phase 12A Acceptance；连续实施授权允许技术门禁通过后直接进入 Phase 12B Task 1，不再等待单独阶段批准。
- Phase 12A Task 9 已以 `c88efdf feat: add phase 12a plan engine demo` 提交并推送；缓存区只包含 9 个目标文件，用户既有文件未纳入提交。
- 已重新读取 Phase 12B Design 与 11 Task Implementation Plan，连续执行游标切换到 Task 1 `RED`。Task 1 只建立 Policy View 与事件/授权契约，不提前发布 `handle_sold_out_event@2.0.0`。
- 一次 `rg` 使用 Windows 不支持的测试路径 glob，退出码非零；后续改用目录搜索和显式路径，未重复该命令，也未据此修改代码。
- Phase 12B Task 1 RED 为 `30 failed`：Policy View、事件模块、授权要求枚举和不可伪造事件授权均尚不存在；失败原因与冻结计划一致，开始最小 GREEN。
- Task 1 首个跨文件 GREEN 补丁因 Catalog import 上下文不符被整体拒绝，没有产生部分写入；随后按文件拆分应用。
- Task 1 核心 GREEN 为 `30 passed`。质量审查新增 View 整体重绑定和事件授权 `model_copy` 重绑定两项红灯，得到 `2 failed, 29 passed`。
- 修复时一次上下文不足的补丁误命中 `ApprovalContext` 同名私有字段；逐行检查在运行测试前发现并精确恢复。随后处理 Pydantic 嵌套重验证 context 丢失，模型专项最终 `43 passed`。
- Catalog、ToolRegistry、Executor、Phase 11B 售罄 Handler 和路由共享回归为 `106 passed`；`handle_sold_out_event` 仍为单活 `1.0.0`，未提前进入 Task 6。
- Task 1 完整单元测试为 `859 passed, 4 warnings`；完整集成测试为 `78 passed, 3 deselected, 5 warnings`。
- 11 个 Task 1 目标文件严格 UTF-8 往返、无 BOM/U+FFFD/混合换行/尾随空白检查通过；`compileall`、Catalog 边界扫描和 `git diff --check` 退出码均为 `0`。
- 全仓编码扫描仍为既有 `4 errors/58 warnings`，Task 1 目标文件命中为 0；没有修改历史编码文件。
- Phase 12B Task 1 已以 `d794ff3 feat: add phase 12b event contracts` 提交并推送；用户既有文件未纳入提交。
- 连续执行游标切换到 Task 2 `RED`，开始内存 Event Inbox、Occurrence、Application、lease/fencing 和显式状态机，不提前实现 PostgreSQL 或 Kafka。
- 首次 Task 2 状态补丁因总控计划实际句子不是列表项而整体拒绝；按实际文本拆分后重新应用，没有产生部分状态更新。
- Phase 12B Task 2 RED 为 `13 failed`：`event_store` 与 `event_state_machine` 尚不存在，失败原因与冻结计划一致；开始最小 GREEN。
- Task 2 核心 GREEN 实现显式 Inbox/Application 状态机、线程安全内存 Store、首次/重复/冲突 occurrence、lease/fencing 和 event/root Application 唯一性，专项为 `13 passed`。
- 质量审查新增回拨接收时钟事务红灯，得到 `1 failed, 14 passed`；登记调整为先验证全部快照、最后统一发布，并保持 `updated_at` 单调，随后为 `15 passed`。
- 质量审查新增完整 EventStore Protocol 红灯，准确列出 6 个缺失声明；补齐与内存实现一致的查询、heartbeat 和转移签名后，Task 2 专项为 `16 passed`。
- 最终审查新增 heartbeat 时间单调和 Application 关联事实 write-once 红灯，得到 `2 failed, 14 passed`；统一使用单调更新时间并拒绝覆盖 Impact/plan 关联后，专项恢复为 `16 passed`。
- Task 1-2 公共契约聚合为 `94 passed`；最终完整单元测试为 `875 passed, 4 warnings`，完整集成测试为 `78 passed, 3 deselected, 5 warnings`。
- Task 2 最终专项为 `16 passed`；8 个目标文件严格 UTF-8 往返、`compileall` 与 `git diff --check` 通过，全仓编码扫描仍只有既有 `4 errors/58 warnings`。
- Phase 12B Task 2 已以 `8b1600b feat: add phase 12b event inbox` 提交并推送，`main` 与 `origin/main` 在 Task 3 开始前一致。
- 连续执行游标已切换到 Phase 12B Task 3。RED 为 `11 failed`，失败点明确是 Phase 12B 迁移未注册、DDL 文件缺失、`initialize_event_store_schema` 与 `PostgresEventStore` 尚未实现。
- Task 3 新增三张权威事件表，扩展 PlanRun kind/priority/root/parent/trigger 与 PlanVersion change reason/source events；迁移保持 Phase 12A 旧 insert shape 可用。
- `PostgresEventStore` 已实现完整 EventStore Protocol：原子登记、精确重放、冲突 occurrence、查询、SKIP LOCKED claim、heartbeat、lease/fencing 终态、event/root Application 和关联事实 write-once。
- lineage 公开投影补充红灯确认 `PlanRunView` 缺少 `plan_kind`；随后增加受控 `PlanRunKind` 与 PlanVersion 来源事件冻结视图。PostgreSQL 查询使用动态 JSON 行投影，避免 Phase 12A 独立表结构反向依赖 Phase 12B。
- Task 3 当前证据：迁移契约 `6 passed`，PostgreSQL 专项 `6 passed`，PlanStore/EventStore 相关单元回归 `51 passed`，完整单元 `881 passed, 4 warnings`，Phase 12A/12B PostgreSQL 聚合 `16 passed`。
- Task 3 完整 integration 已独立重跑并取得明确退出码 `0`：`84 passed, 3 deselected, 5 warnings`。此前并行验证只保留点号、没有完整汇总，因此未把那次输出计入最终证据。
- Task 3 迁移 dry-run 已识别 12 个步骤并包含 required Phase 12B；9 个当时目标文件严格 UTF-8、无 BOM/U+FFFD/混合换行/尾随空白。全仓编码扫描仍为历史 `4 errors/58 warnings`，目标文件命中为 0。
- 最终工作日志更新后复核 12 个 Task 3 目标文件：严格 UTF-8 往返、BOM/U+FFFD/混合换行/尾随空白均为 0；`compileall`、11 个 EventStore 方法签名等价检查和 `git diff --check` 通过。
- 一次相关单元回归命令误写不存在的 `test_phase12a_models.py`，未收集测试；读取仓库真实文件清单后改为 `test_phase12a_plan_models.py` 并取得 `51 passed`，没有据此修改业务实现。
- Phase 12B Task 3 已以 `25793f2 feat: persist phase 12b event facts` 独立提交并推送；缓存区严格为 12 个目标文件，用户既有 7 个脏文件未纳入提交。
- 连续执行游标已切换到 Task 4 `RED`，只建立 Kafka 入站与 Trust Profile，不提前驱动冻结、紧急 DAG 或 PlanEngine。
- Task 4 RED 为 `9 failed`：可信入站模块和 durable consumer 尚不存在；真实 Kafka 测试也明确缺少新 Adapter。
- Task 4 GREEN 新增严格库存事件解析、启动冻结 `IngressTrustProfile`、稳定 provenance/delivery 身份和手动 offset Consumer；旧一次性 `LiveAgentKafkaConsumer` 保持原语义。
- 安全默认经过独立红灯收紧为 `INVENTORY_INGRESS_ENABLED=false` 与 `KAFKA_INVENTORY_AUTO_OFFSET_RESET=latest`；直接构造 Profile 省略 enabled 时也保持禁用。
- Task 4 单元专项为 `9 passed`；真实 Kafka + PostgreSQL 为 `2 passed`，证明重复/冲突落库后前移 offset、冲突不阻断后续事件、Store 失败不提交且同 group 重启收到原消息。
- 相关 Kafka/EventStore 聚合首次出现 `2 failed`：Task 4 留下的 VERIFIED 全局事件被 Task 3 claim 测试合法领取。数据库查询确认失败 winner 均为 Task 4 event ID；增加专用前缀前后清理后，相关 unit 为 `66 passed`、integration 为 `10 passed`。
- 一次相关单元命令误写不存在的 `test_phase12b_events.py`，改用仓库真实 `test_phase12b_event_models.py` 后通过；该命令错误未用于调整生产行为。
- Task 4 首轮完整回归为 unit `890 passed, 4 warnings`、integration `86 passed, 3 deselected, 11 warnings`。其中 6 条新 warning 来自本 Task lambda serializer/deserializer；移除无必要 lambda、直接发送 UTF-8 bytes 后，Task 单元 `9 passed`、真实 Kafka `2 passed` 且无新增 warning。
- 已修改的历史 `kafka_consumer.py` 原带 UTF-8 BOM；目标文件严格检查将其提升为本 Task 阻断项，机械移除 BOM 后首字节恢复为 ASCII 文档字符串，代码内容未改变。
- Task 4 提交前最终完整回归：unit `890 passed, 4 warnings`；integration `86 passed, 3 deselected, 5 warnings`，5 条均为既有 Kafka 测试弃用提示，本 Task 新增 warning 为 0。
- `compileall` 与 `git diff --check` 通过；全仓编码扫描由既有 `4 errors/58 warnings` 收敛为 `4 errors/56 warnings`，减少项正是本次已修改 Consumer 的历史 BOM/混合换行，Task 4 目标文件命中为 0。
- Phase 12B Task 4 已以 `0762c2c feat: ingest durable inventory events` 独立提交并推送；连续执行游标进入 Task 5 `RED`。
- Phase 12B Task 5 RED 为 `10 failed`：ImpactAnalyzer、未开始节点冻结、NodeRun superseded 列与内存/PostgreSQL Store 原语均尚未闭合，失败面与冻结计划一致。
- Task 5 核心 GREEN 实现确定性 PRODUCT/ROOM/PLATFORM 分析、依赖闭包与稳定摘要；冻结事务采用 PlanRun、最新 NodeRun、PlanNode 锁序，PRODUCT 保持局部运行，ROOM/PLATFORM 阻断整计划新 claim。
- 规格复核发现 Store 只读投影遗漏依赖、资源键和 superseded 证据，导致商品事件被保守提升为 ROOM 且调用方看不到废弃标记；补齐投影后 Task 聚合为 `10 passed`。
- Phase 12A 状态机穷举回归准确捕获四条 Phase 12B 新冻结边未加入测试白名单；更新受控白名单后 Store、状态机、迁移与 PostgreSQL 回归为 `155 passed`，其余 11x11 非法迁移仍全部拒绝。
- 一次相关回归命令误写不存在的 `test_phase12a_store.py`，使用 `rg --files` 找到真实 `test_phase12a_plan_store.py` 后重跑；该命令错误未用于调整生产行为。
- 质量审查新增 superseded attempt 禁止重试/租约回收红灯，得到 `4 failed, 10 passed`；内存与 PostgreSQL Store 均在创建第二次执行前 fail-closed，专项恢复为 `14 passed`。
- 二次审查发现 superseded 失败仍会把 PRODUCT 局部风险升级为整计划 `FAILED`，新增内存/PostgreSQL 红灯得到 `2 failed, 14 passed`；修复后失败 NodeRun/节点证据保留，PlanRun 继续 ACTIVE，无关分支可 claim。
- PostgreSQL reclaim 同步调整为 PlanRun-first 锁序，避免冻结事务等待旧 NodeRun 时遗漏并发新建 attempt；一次补丁上下文误命中 `record_node_input`，在运行测试前通过源码扫描发现并精确移除，未留下无关锁范围变化。
- Task 5 最终专项 `16 passed`；完整 unit `900 passed, 4 warnings`；完整 integration `92 passed, 3 deselected, 5 warnings`，新增 warning 为 0。
- 12 个目标文件严格 UTF-8 往返、无 BOM/U+FFFD/混合换行/尾随空白；`compileall`、迁移 dry-run 和 `git diff --check` 通过。全仓编码扫描仍为历史 `4 errors/56 warnings`，Task 5 目标命中 0。
- 一次迁移预览命令误写不存在的 `scripts/run_migrations.py`，读取现有入口后使用 `scripts/run_db_migrations.py --dry-run` 成功识别 12 个迁移步骤；错误命令未触发文件修改。
- Phase 12B Task 5 已以 `375b671 feat: freeze impacted plan branches` 独立提交并推送；连续执行游标进入 Task 6 `RED`。
- Phase 12B Task 6 RED 固定了售罄单活 2.0.0、可信事件/人工授权、CAS、限流、版本冲突与未知副作用的边界；集成测试最初因严格只读对账模块不存在而按预期无法收集。
- 最小 GREEN 将售罄 Handler 收敛为一次 `mark_sold_out` 调用，Fake 平台按 `expected_version` 执行 CAS；`SIDE_EFFECT_UNKNOWN` 仅通过新只读对账服务确认，不改写原 Attempt，也不创建第二个 Operation。
- 完整回归发现三个旧兼容入口仍使用 1.0.0 或把 Context 字段放入 2.0.0 业务参数；已通过红灯测试修正为 Catalog 版本快照、无事件授权时 pending，以及带受控事件授权的无外部依赖 Demo。
- 当前 Task 6 专项为 `64 passed`，完整单元为 `911 passed, 4 warnings`，完整集成套件无失败；下一步执行静态、编码与暂存范围验证后独立提交。
- Phase 12B Task 6 已以 `9d4bf97 feat: execute versioned sold out writes` 提交并推送；缓存区未包含用户已有文档修改和无关脚本。
- 执行治理改为 Phase-Gated：Phase 12B 内 Task 可连续推进，Phase 12B Acceptance 后必须进入用户授权的 Phase 13 Gate；Phase 13/14 详细文档降级为讨论基线。
- 已新增固定 `live-session-p001-sold-out-v1` 业务闭环轨道；Task 11 将输出可重复 Trace/Markdown 报告，Phase 13 只追加条件化 Agent 结论，Phase 14 再纳入 Golden/Release 证据。
- Task 7 实施前发现现有 Worker 只能按指定 PlanRun claim，无法证明 priority 100 紧急 child 优先于普通 READY 节点；已以 D-097 收敛为 Store 权威的跨 PlanRun priority claim，保留全部既有播前按计划调用。
- Task 7 首个 RED 为 `2 failed`：紧急输入模型和固定售罄 Proposal 均不存在，失败与 D-097 后的受限 child DAG 设计一致；尚未写入 Task 7 生产实现。
- Task 7 第一段 GREEN 新增冻结 `EmergencySoldOutPlanningInput` 与固定五节点 `SoldOutEmergencyProposalProvider`，专项为 `2 passed`；Store/Worker 与 PostgreSQL 优先级尚未实施。
- Task 7 已完成固定五节点物化门禁、priority 100、`ready_at`、跨 PlanRun 全局 claim、PlanRun -> Node 锁序、资源互斥、只读 EventStore 验证和售罄写授权重建；其他紧急 Skill 不获得事件授权。
- Task 7 审查整改覆盖非规范 Proposal、滚动迁移前 CARD_BATCH 兼容、两个 PostgreSQL Worker 的 `SKIP LOCKED`、验证后迟到冲突和全部 READY 转换路径。最终 unit `922 passed`，integration `95 passed, 3 deselected`。
- Task 8 发现 PlanRun 初始输入不足以支持不可变 Replan，已新增 D-098：PlanVersion 保存 planning input、failure signature 与 input fingerprint。首个 RED 为 `2 failed`（缺少模块），内存 GREEN 为 `2 passed`，已证明单商品指纹变化时只复用另外两张成功手卡且不复制 NodeRun。
- Task 8 已完成 root 级内存/PostgreSQL CAS、版本 2/3 预算、等价循环冻结、跨版本复用链、Worker 读取版本输入与旧 NodeRun 输出、Application 部分提交补偿和旧 source version 拒绝。最终 unit `930 passed`、integration `96 passed, 3 deselected`，独立复审无剩余阻断。
- Phase 12B Task 9 RED 为 `7 failed, 21 passed`：生产目录仍有 10 处 ToolRegistry import，ToolMaskPolicy、AgentLifecycleHooks 和 AgentToolExecutor 尚不能消费 SkillPolicyView。
- Task 9 GREEN 将 Planner、Policy、Hook、四个 Flow、AgentToolExecutor 与 SkillExecutor 全部迁入启动冻结的 SkillPolicyView；ToolRegistry Facade 与兼容测试继续保留到 Phase 14，生产 import 扫描为 0。
- 自审新增 Catalog/PolicyView 装配漂移和 Planner 注入白名单被模型绕过两项 RED，均得到预期失败；修复为 Executor 启动拒绝版本集合漂移，以及 Planner 对结构化 LLM 决策执行当前快照二次校验。
- Task 9 首次独立审查复现 BLOCK 继续 Legacy/Flow、版本快照分裂、未知 lifecycle 默认 PRE_LIVE 和旧 Registry 可变引用等安全问题；新增 7 个红灯后修复为启动转换冻结、全治理一致性断言和副作用前强制门禁。
- 二次审查发现 Harness Hook 尚未读取 `gate_decision`；补充低风险 BLOCK 红灯并让 Hook 先执行通用门禁。最终复核确认原问题全部清零，无阻断或重要项。
- 并行全量时一个 20ms deadline 测试因资源竞争在 Handler 前到期；单独重复 10 次零失败，随后串行完整 unit 全绿，未修改生产 deadline 分类。
- Task 9 最终专项为 `124 passed`，完整 unit 为 `943 passed, 4 warnings`，完整 integration 为 `96 passed, 3 deselected, 5 warnings`；生产 ToolRegistry import 为 0。
- Phase 12B Task 10 RED 首次覆盖默认 Legacy、显式 PlanEngine、无 fallback 和 Harness 不重复售罄写，得到 `4 failed`，随后实现 PreemptionCoordinator、EvidenceRef、room-scoped Event Inbox claim、启动冻结路由和 Harness/API 证据入口。
- Coordinator 形成 Inbox -> ImpactAnalyzer -> PlanStore freeze -> emergency child -> strict read-only reconciliation -> Replan 的可恢复链；未知副作用只复用原 Attempt，等待/重试/失败均不生成成功 EvidenceRef。
- 独立审查发现并修复 7 项问题：跨 room/root 错绑、对账崩溃窗口、等待状态伪成功、失败状态丢失、事件 lease 不续租、Harness 仍调用 Planner、Dashboard/API 未接路由/证据。最终复核无阻断或重要项。
- Task 10 专项最终 `141 passed`；完整 unit `957 passed, 4 warnings`；完整 integration `97 passed, 3 deselected, 5 warnings`；Task 12B EventStore/PostgreSQL/Harness 聚合、compileall 和 diff 检查通过。

# 2026-07-11 Phase 7A 进度

- 完成 Phase 6C 功能提交和编码治理提交，避免 7A 改动混入历史收尾。
- 新增 Agent Replay、规则评分、评估 Store、Worker、LLM Judge、API 和 `/evaluation` 页面。
- 新增 PostgreSQL 评估表，使用任务租约和 `FOR UPDATE SKIP LOCKED` 支持多 Worker 抢占。
- 将真实 DeepSeek 集成测试标记为 `external`，默认测试不访问外部模型。
- 当前 7A 聚焦测试、全量 unit、全量 pytest、demo、编码扫描和 diff 检查均已通过。

---
# 2026-07-15 Phase 12B Task 11 与 Acceptance

- 新增 `run_phase12b_preemption_demo.py` 与 `run_all.py phase12b-demo`，固定业务场景输出规范 JSON Trace 和 Markdown 报告。
- 主场景通过真实内存 PlanStore、EventStore、PlanWorker、PreemptionCoordinator 和 ReplanCoordinator 运行；售罄写只调用一次，SIDE_EFFECT_UNKNOWN 由只读对账闭合。
- 新增 Demo 单元测试，覆盖产物字段、字节稳定性和八场景默认输出；专项结果 `3 passed`。
- Phase 12B 单元聚合 `104 passed`，真实 PostgreSQL/Kafka 集成聚合 `19 passed`，全仓回归 `1057 passed, 3 deselected, 9 warnings`。
- 生成 Phase 12B Acceptance，路线图与实时状态切换为 `AWAITING_PHASE_13_GATE`；未开始 Phase 13，真实模型费用仍为 0 元。
- Task 10 已提交并推送为 `e6f3414 feat: coordinate sold out preemption`；Task 11 等待本次独立提交与推送。
