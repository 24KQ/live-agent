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

- Task 7 已完成 AgentToolExecutor 四个核心工具单一 Runtime dispatch、旧参数规范化、`compatibility_enriched` 证据和可信边界硬化；提交为 `4f77403`、`7e132f3`、`b60a85d`，并承接 `96a5adb`。
- Task 8 已完成隔离等价测试、四场景 Demo 与 `run_all.py phase11a-demo` 入口；提交为 `7154c89`、`fd54005`。
- Runtime 专项命令退出码 `0`：`85 passed in 1.43s`，无 deselected、无 warnings。
- 相关回归命令退出码 `0`：`45 passed in 0.89s`，无 deselected、无 warnings。
- `pytest -q` 退出码 `0`：`501 passed, 3 deselected, 9 warnings in 54.13s`；warnings 为现有 FastAPI/Starlette 与 Kafka 弃用告警。
- `python scripts/run_phase11a_skill_runtime_demo.py` 退出码 `0`；全 legacy、第一批 Runtime、两批 Runtime、setup 回滚四场景均输出 4 商品、4 计划项、3 手卡、`prepared` 和 8 条审计。
- `python scripts/run_all.py phase11a-demo` 退出码 `0`，复现相同四场景结果。
- `python scripts/check_doc_encoding.py` 退出码 `1`：`4 errors/58 warnings`。4 个 error 均命中扫描脚本自身 U+FFFD 示例；历史 BOM/工作树混合换行 warning 仍保留，本任务未修改脚本或顺手治理。
- 初次 `git diff --check` 退出码 `0`，仅输出 Git 的 LF/CRLF 转换提示；范围检索只命中 `compatibility.py` 中“未来 PlanEngine 不应复用兼容层”的禁止说明，没有 PlanEngine 实现。
- Task 9 已生成 Acceptance 并同步路线图、执行日志和 worklog；状态只能记为“技术验收完成，待用户审核”，Phase 11B 未开始。

# 2026-07-11 Phase 7A 进度

- 完成 Phase 6C 功能提交和编码治理提交，避免 7A 改动混入历史收尾。
- 新增 Agent Replay、规则评分、评估 Store、Worker、LLM Judge、API 和 `/evaluation` 页面。
- 新增 PostgreSQL 评估表，使用任务租约和 `FOR UPDATE SKIP LOCKED` 支持多 Worker 抢占。
- 将真实 DeepSeek 集成测试标记为 `external`，默认测试不访问外部模型。
- 当前 7A 聚焦测试、全量 unit、全量 pytest、demo、编码扫描和 diff 检查均已通过。

---
