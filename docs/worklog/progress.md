# LiveAgent 工作进度记录

## 2026-07-18 Phase 16 Started

- 用户已授权连续执行：先完成 Task 1 文档持久化，再在同一授权内执行 Task 2-11。
- 已建立隔离分支 `codex/phase16-controlled-multi-agent`，基线为
  `ee0de7c4e333e1b247a587c4be793c771abcb0e4`；主工作区用户脏文件不进入本阶段。
- 已冻结高冲突三选二规则、双 Agent 责任边界、5 秒/4000 token/0.10 CNY 单 case 预算、
  1.00 CNY smoke 上限、默认关闭路由、48 例独立数据集和单人本地 Demo。
- 当前：Task 1 GREEN，待文档验证、独立提交和推送。下一任务为 Task 2 测试收集冲突 RED。

## 2026-07-18 Phase 16 Task 2 GREEN

- Task 1 已以 `69e92be docs: freeze phase 16 controlled multi-agent design` 提交并推送到
  `origin/codex/phase16-controlled-multi-agent`。
- Task 2 RED 重现三处 import mismatch；仅将三个 integration 文件重命名为
  `*_postgres.py`，根 pytest 收集恢复为 1537 个测试、4 个 external deselected、0 errors。
- 同名 unit/PostgreSQL 专项在临时加载本机 `.env` 后为 `14 + 9 + 9 passed`；当前进入完整
  unit/integration 与提交前审查。
- 完整 unit 发现 Phase 14 冻结 Manifest 在 Windows CRLF 工作树下错误漂移；根因已由
  `raw != LF-normalized == manifest` 摘要证据确认，新增 D-141 后通过 Git 属性在 Task 2
  内修复，不改变生成器或历史 Manifest。
- 修复后 root collect 无 import mismatch，完整 unit `1382 passed, 4 warnings`，完整
  integration `155 passed, 3 deselected, 5 warnings`；当前 Task 2 进入提交前编码与差异收口。

## 2026-07-18 Phase 16 Task 2 PUSHED / Task 3 RED

- Task 2 已以 `6ea5a57 test: stabilize phase 14 postgres collection` 提交并推送到
  `origin/codex/phase16-controlled-multi-agent`。隔离工作树从主工作区既有 `.env` 仅向测试
  进程注入 PostgreSQL 凭据；该文件从未复制、写入或提交。
- 连续游标进入 Task 3。该任务只建立零权限、不可变、闭合代码的双 Agent/领域协议；先写
  RED 测试，不创建 Store、Coordinator、API 或任何执行/模型调用路径。

## 2026-07-18 Phase 16 Task 3 VERIFY

- 新增 `CONFLICT_ANALYSIS`、`LIVE_DECISION_PLANNING`、精确零 Skill Profile、升级/分析/
  Outcome 事实与 multi-Agent Proposal lineage；没有新增选择器、Store、Coordinator、HTTP、
  WebSocket、真实模型调用或经营写路径。
- D-142 将 Phase 13 历史 baseline 的静态源码身份与当前正式评估的 Git HEAD 源码身份分离；
  D-143 使旧预算路径对 Phase 16 task kind 受控拒绝，避免 KeyError 或借用历史额度。
- 审查整改补齐实际 Profile digest、Bundle digest/EvidenceRef lineage、FINAL AgentAction 信封、
  Planner Schema 的备品/展示安全约束，以及真实 ScriptedRunner 无网络路径。
- 干净数据库验证顺带修复了既有 Phase 7B SQL 双重转义、播后 Trace 隐式前置数据与真实
  Embedding 集成测试遗漏 `external` 标记的问题；这些修复不扩大 Phase 16 业务接口或权限。
- 最终证据：Task 3 专项聚合 `16 passed`；从空库执行 17 步官方迁移全部 PASS；完整 unit
  `1395 passed, 4 warnings`；integration `151 passed, 7 deselected, 5 warnings`；compileall 与
  根 collect `1546/1554`、8 个 external deselected 均通过。官方 seed 已确认至少 3 次 HTTP
  请求为 401 认证拒绝，未获得响应或 usage；最终复核已为 unit/integration 注入离线 Embedding，
  后续默认回归不再访问该外部路径。当前仅待编码/差异检查、独立提交与推送。

## 2026-07-18 Phase 16 Task 3 PUSHED / Task 4 RED

- Task 3 已以 `ad0e185 feat: add controlled multi-agent contracts` 提交并推送到
  `origin/codex/phase16-controlled-multi-agent`；远端与本地 HEAD 一致。
- 连续游标进入 Task 4。该任务只为既有冻结领域模型建立内存/PostgreSQL append-only 事实链；
  RED 必须先覆盖父事实、Workspace CAS、fencing、唯一升级、幂等重放和重启恢复。

## 2026-07-18 Phase 16 Task 4 REVIEW / VERIFY

- 内存与 PostgreSQL Store 已追加 escalation、analysis 与 outcome 事实；专项单元 `5 passed`、
  隔离 PostgreSQL `6 passed`。完整 unit JUnit `1398` tests、完整 integration JUnit `155` tests，
  failures/errors 均为 0；真实模型新增费用为 0。
- 两份只读审查均无 Critical。D-135 的三选二信号现在只由 Bundle 重建；人工请求拒绝触发码；
  关系事实的数据库 trigger 校验 LIVE、全证据引用和 CAS。D-145 令 READY Outcome 在 Task 6
  Proposal 持久化前 fail-closed。
- 后续整改将 `proposal_eligible`、`valid_until`、自动触发码顺序、CAS 锁内 LIVE 复核与
  `DEGRADED` 终态形状全部下沉到内存/PostgreSQL 双实现。最终 unit `1402 passed, 4 warnings`，
  integration `160 passed, 7 deselected, 5 warnings`；真实模型新增费用仍为 0。

## 2026-07-18 Phase 16 Task 4 PUSHED / Task 5 RED

- Task 4 已以 `1ea229a feat: persist multi-agent escalation facts` 推送到
  `origin/codex/phase16-controlled-multi-agent`；远端和本地 HEAD 一致。
- 连续游标进入 Task 5。该任务只实现确定性选择与 Analyst 协调，默认失败必须形成可解释
  `DEGRADED` Outcome；Planner、READY Proposal、HTTP 与经营执行继续留在后续任务。

## 2026-07-18 Phase 16 Task 5 GREEN / REVIEW

- Task 5 RED 已确认：选择器/协调器不存在。新增受控协调器后，自动路径只对 fresh、
  proposal-eligible、LIVE Bundle 的任意三选二信号调用一次 Analyst；正常、对账阻断或
  伪造对象在模型边界前停止。
- 成功只追加完整 `ConflictAnalysis`；模型、身份、输出或超时失败只追加一条安全摘要
  `DEGRADED` Outcome。重试优先恢复已有 analysis/outcome，不重新发送冻结任务。
- 当前专项 unit `8 passed`、Phase 16 相关 unit `25 passed`、隔离 PostgreSQL `10 passed`、
  目标 compileall 通过；真实模型新增费用仍为 `0.000000 CNY`。正在进行最终双重复审。

## 2026-07-18 Phase 16 Task 5 REVIEW REMEDIATION

- 双重复审发现并已整改 6 项 Important：跨 Coordinator 重复模型发送、过期 Bundle 下已完成
  事实无法恢复、响应丢失后重发、Store/DDL finding 旁路和错误 Runner Profile 冒充冻结身份。
- 新增 D-146 和 append-only dispatch claim：同一冻结 Analyst task 至多发送一次；活跃 claim
  返回 pending，过期/未知响应只写 `DEGRADED`。分析 finding/Profile 在内存和 PostgreSQL
  都与父升级和冻结 Profile 精确绑定，终态后禁止追加分析。
- 整改专项：selector/Store unit `20 passed`，隔离 PostgreSQL `12 passed`，真实模型费用仍为 0；
  正在执行整改复审和最终全量验证。

## 2026-07-18 Phase 16 Task 5 FINAL REVIEW REMEDIATION

- 最终只读复审发现内存普通 operator lease 被错误限制为两秒，以及 claim、`LIVE -> REVIEW` 与
  Evidence freshness 没有共享线性化点。已恢复普通续租的正整数契约；新 claim 必须在根 Workspace
  锁内确认 `LIVE`、proposal-eligible，并保证完整两秒等待窗未过期；活跃 claim 暂时阻断进入
  `REVIEW`，已有 claim 的恢复读取不重发模型。
- PostgreSQL payload 触发器现在在检查 Analysis/Outcome 互斥前锁定同一根 Workspace。新增双连接
  直写回归，证明已成功 Analysis 后，攻击者即使猜中下一 Workspace 版本也不能提交无 Analysis 的
  `DEGRADED` Outcome。Task 5 聚合为 `63 passed`；真实模型费用仍为 `0.000000 CNY`。

## 2026-07-18 Phase 16 Task 5 D-147 REMEDIATION

- 最终双复审继续发现：人工请求的空触发码无法满足非空 `ConflictAnalysis` finding，claim 创建后
  再等待完整两秒会跨越其自身预算，而 PostgreSQL `REVIEW` 会拒绝已发送请求的审计终态。D-147
  固定人工至少一项、自动至少两项服务端重建信号；Coordinator 只等待 claim 剩余时间。
- 仅当同一 dispatch claim 已存在时，`REVIEW` 可追加一条不含 Analysis/Proposal 的 `DEGRADED`
  Outcome；首次 CAS 被视图切换抢占时只重试同一审计写一次，不重发模型。Analysis、READY、Proposal
  和执行仍严格限定 `LIVE`。
- PostgreSQL Analysis 写入要求 Store 上下文，因此完整 Pydantic 的 Unicode/Schema/canonical
  digest 验证不会被裸 JSONB 直写绕过。专项现为 `43 passed`，新增真实 PostgreSQL 人工正向路径；
  真实模型费用仍为 `0.000000 CNY`。

## 2026-07-15 Phase 13 Just-in-Time Design/Plan 审核

- 基于 Phase 12B Acceptance 重新审核 Phase 13，采用共享评估内核与 LiveOps、Planner、ReviewMemory 三个纵向候选切片。
- 将候选生产接入改为 RETAINED 后创建默认关闭路由，并通过 Registry/统一协议预留受控多 Agent 扩展；不实现 A2A。
- 将去留门改为候选绝对质量与相对提升严格 AND，明确 0 表示 0 个新增 Specialist，不影响现有播中 Agent Harness。
- 保持每候选 20/40/20 共 240 例，增加 10 例 validation shard 的安全/数学早停和配对 Wilson 区间。
- Phase 13 上限固定为 2.40 元，Phase 14 首次 Release 预留 0.60 元；Judge 只做最多 10 对诊断抽样。
- 确认 ReviewMemory 完成双证据确定性晋升和下一次播前读取闭环。
- 重写 Phase 13 Design 和 12-Task Implementation Plan，新增 D-100 至 D-108；本轮未修改业务代码、未运行模型、未开始实施。
- 持久化验证：D-001..D-108 连续唯一，八个标准字段各 108 项；9 个目标文档严格 UTF-8/LF/空白检查通过，`git diff --check` 通过。
- 全仓编码扫描仍为既有 `4 errors/53 warnings`，错误来自扫描器 replacement-character 自测样例，本次目标文档命中 0。

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

# 2026-07-15 Phase 13 Task 1

- 初始 RED 因 `src.specialist_runtime` 不存在而失败；实现 AgentTask/Action/Result、EvidenceRef、SpecialistProfile、Registry 与确定性 Orchestrator 后进入 GREEN。
- 规格审查补出结果 Schema 哈希绑定、结构化失败互斥和启动冻结路由；对应红灯均按最小实现闭合。
- 代码质量审查复现 Pydantic copy、dict 基类写入、可变显式路由和 endpoint authority 绕过；替换为不可变 Mapping、严格复制协议、规范路由与 ASCII DNS 校验。
- Task 1 最终专项为 `30 passed`，规格和质量复审均无阻断项；真实模型调用与费用仍为 0。
- 下一步只暂存 Task 1 代码、测试和四份 worklog，独立提交并推送后进入 Task 2 RED。

# 2026-07-15 Phase 13 Task 2

- 新增冻结 ModelRequest/Success/Failure/Usage 与 async AgentModelPort，DeepSeek Adapter 每次只调用一次 transport，不复用旧 LLMClient 重试。
- 默认 transport 使用原生 `httpx.AsyncClient`；固定 HTTPS DeepSeek endpoint、模型身份、temperature、Prompt/Schema hash、max tokens 和绝对 deadline。
- 错误分类覆盖限流、HTTP、deadline、transport、非法 envelope/output、模型漂移和思维链字段；结果不保存 API key、原始 header、异常文本或响应正文。
- ScriptedAgentModel 按 request ID 顺序消费冻结 outcome，支持无 usage、稳定失败和序列耗尽证据。
- Task 1+2 专项最终 `50 passed`，规格复审无阻断，质量复审无 Critical/Important；真实模型费用仍为 0。

# 2026-07-15 Phase 13 Task 3

- 新增内存/PostgreSQL ModelBudgetStore、Ledger/候选/reservation/model-call 三层持久事实及 required Phase 13 迁移。
- reserve 在全局 Ledger 与 candidate 行锁内校验总额、阶段预留、候选初始额度和释放后的共享池；并发连接不能突破临界余额。
- settle 支持已知 usage 退还差额与未知 usage 上限结算；release、重放、冲突和重启待对账扫描均有测试。
- DDL 通过候选复合外键、结算事实复合外键和全金额 NaN CHECK 防止绕过 Python Store。
- Task 3 最终单元+真实 PostgreSQL 专项 `19 passed`；测试 fixture 精确清理随机 scope；真实模型费用仍为 0。

# 2026-07-15 Phase 13 Task 4

- 新增八类权威 Evidence Resolver、BoundedSpecialistRunner、RuntimeSkillPort 与 retained-only 生产 fallback 门面。
- Runner 固定执行 Profile、anchor/Evidence、Token/费用预检、单次模型、动作/Skill/结果 Schema 和审计顺序；正式评估从不调用 baseline。
- 费用超额按实际值持久化，重复冻结 Task 禁止第二次模型发送；模型响应身份、Skill 调用序号、取消 Attempt 闭环和预算恢复均 fail-closed。
- 专项 Runner `47 passed`，Phase 13 Task 1-4 聚合 `109 passed`，完整 unit `1071 passed`，integration `104 passed, 3 deselected`；真实模型费用仍为 0。

# 2026-07-16 Phase 13 Task 5

- 新增冻结 EvaluationManifest/Run/Claim、CaseAttempt、PairedMetric 与 RetentionDecisionRecord，并建立内存/PostgreSQL Store。
- 持久化不可覆盖 Attempt 历史、跨 Run 唯一 selected 结果、独立业务指标事实、配对胜负/Wilson 区间和严重违规计数。
- 正式写入统一要求 active claim；PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`、数据库时钟和 claim version fencing。
- decision 在单事务重算摘要、共同硬门、严重违规和完成配对数，并按 Manifest/Candidate 原子完成当前 Run、取消兄弟 Run。
- Manifest 行作为候选生命周期锁，串行化新建 Run 与最终 decision；结论后不能重新打开同一 Manifest/Candidate。
- 多轮规格/质量审查的重要发现均已补红灯整改；Task 5 最终 unit `30 passed`、真实 PostgreSQL `8 passed`。
- 完整回归为 unit `1101 passed, 4 warnings`、integration `112 passed, 3 deselected, 5 warnings`；迁移连续执行两次、compileall 均通过，真实模型费用仍为 0 元。
- 下一步在 Task 5 独立提交并推送后进入 Task 6 RED，生成 240 例字节稳定脱敏数据集；不会调用真实模型。

# 2026-07-16 Phase 13 Task 6（实施中）

- 已生成三个候选各 80 例的脱敏 case/label、严格 Schema、Prompt、结果 Schema、固定 seed 生成器和 `phase13-v2` 数据集基线 Manifest。
- 首轮专项为 `8 passed`，生成器字节稳定；中间完整回归为 unit `1110 passed`、integration `112 passed, 3 deselected`。
- 复审发现并整改 Runtime 拼接缺口：Profile 冻结并发送真实 Prompt、精确 Skill 版本在 Port 前校验、完整与嵌套结果证据绑定权威 EvidenceRef；相关 Profile/Runner 聚合 `83 passed`。
- 新增 D-109，明确 Task 11 必须在 Task 7-10 完成后基于最终 Git commit 生成正式 Manifest；真实模型调用与费用仍为 0 元。
- Manifest-bound Loader、全部源码闭包、独立价格快照和最终复审仍在收口，Task 6 尚未提交。
- 后续复审补齐外部 Manifest 锚点、深冻结 case、可执行 AgentAction Prompt，以及 Store 对数据集基线的正式 Run 禁令；Task 6 领域/数据集聚合 `126 passed`，真实 PostgreSQL Evaluation Store `8 passed`。
- 最终授权整改增加 Git/source digest 公开预检与内部注册证据；最新 Task 6 聚合 `128 passed`，真实 PostgreSQL Evaluation Store `8 passed`，真实模型费用仍为 0 元。
- 再次复审后将授权门禁延伸到每次 create_run，并拒绝源码目录 symlink、ignored/untracked Python；最新聚合 `129 passed`、PostgreSQL `8 passed`。
- Task 6 最终完整回归为 unit `1121 passed, 4 warnings`、integration `112 passed, 3 deselected, 5 warnings`；两轮最终复审均无 Critical/Important，真实模型费用 0 元。

# 2026-07-16 Phase 13 Task 7（实施中）

- Task 6 已以 `f13ae6e` 推送；Task 7 首轮缺模块 RED 后完成四类 PriorityLiveOpsPolicy、严格建议模型和冻结 Profile 工厂。
- 发现 v2 label 与严格保留门数学冲突，新增 D-110；保留 v2 审计基线并生成独立 `phase13-live-ops-v3` case/label/Manifest。
- v3 评分从 acceptable/recovery 动作集合计算，不复制 gold success；validation 早停使用 40 例整数目标 `max(36, baseline+2)` / `max(34, baseline+4)`。
- 已完成 AgentTask adapter：固定 case/profile/room/trace/evidence 身份，只调用一次 BoundedSpecialistRunner，失败不 fallback。
- 已完成 80 例 ScriptedModel 无网络演练和 Evaluation Store 配对：baseline 为零 token/成本，Agent 保存全部共同门禁；validation 从 selected Attempt 重建并在 40 例后解锁 holdout。Task 7 当前 unit `14 passed`，真实模型费用仍为 0 元；PostgreSQL 集成、审查和全量验证尚未完成。
- 已补 PostgreSQL 重启恢复用例（`1 passed`）和跨候选 case 拒绝。当前 Task 7 unit `15 passed`，Task 6+7 数据集聚合 `25 passed`；一次完整 unit 进程结束时工具遗失最终汇总，需重新捕获后才能进入提交，真实模型费用仍为 0 元。
- 提交前复审发现 infrastructure AgentResult 会在 baseline selected 后失败，留下半 pair；已新增模型错误/预算错误红灯并改为 Store 写入前拒绝，Task 7 unit 更新为 `17 passed`。
- 最终完整验证已明确捕获退出码：unit `1138 passed, 4 warnings`，integration `113 passed, 3 deselected, 5 warnings`，相关 Harness/Preemption/Store/Skill 权限聚合 `182 passed`；规格与质量复审无剩余 Critical/Important，真实模型费用 0 元。
- Task 7 已以 `4b26a31 feat: evaluate live ops specialist` 提交并推送，远端 `origin/main` 与本地 HEAD 一致；下一任务为 Task 8 PlannerAgent，尚未开始代码修改。

# 2026-07-16 Phase 13 Task 8（验证完成，待提交）

- 新增 `retrieve_anchor_memory@1.0.0`，严格要求 anchor/room/limit，Handler 按可信 room 二次校验，只返回 active、同主播、当前房间或主播级的白名单结构化引用，不返回正文、embedding、抑制理由或任意 metadata。
- Catalog 增至 14 个单活 Skill。旧播前兼容 Facade 保持 13 个可执行 Handler，防止在没有 `memory_port` 时暴露必然失败的伪入口；ToolRegistry/PolicyView 仍正确投影新增只读 Skill。
- 新增 Planner 的受限节点、依赖、绑定、循环与执行控制字段校验，Compiler 从 Catalog 注入版本、风险、deadline、资源锁和并发，正式 Runner Profile 固定为 0 次 Skill 调用。
- 使用 80 个冻结 Planner case 经真实 BoundedSpecialistRunner、ScriptedModel 和 Evaluation Store 完成配对；四个 validation shard 完整后才解锁 holdout，评分使用 executable/constraint recovery 两个独立指标。
- 修订 `phase13-v2` 数据 Manifest 的源码闭包摘要。Task 8 专项及相关回归 `104 passed`，完整 unit `1148 passed, 4 warnings`，完整 integration `115 passed, 3 deselected, 5 warnings`；真实模型费用仍为 0 元。
- Task 8 已本地提交为 `204aec0 feat: evaluate planner specialist`。向 `origin/main` 连续三次推送均因 GitHub TLS `missing close_notify`/handshake 失败；远端仍为 `5f31383`，按推送门禁暂停，尚未开始 Task 9。
- 随后网络恢复，`204aec0` 已成功推送并确认 `origin/main=204aec0`；Task 9 进入 RED，后续继续按已授权的 Task 9-12 执行。

# 2026-07-16 Phase 13 Task 9（验证完成，待提交）

- Catalog 增至 17 个 Skill：播后证据收集只经注入 Port，归因只消费显式快照，候选 staging 只写结构化 Candidate Store。
- 实现内存/PostgreSQL Candidate Store、命令幂等、乐观版本转换和确定性 PromotionPolicy；双 DecisionTrace、同作用域和货盘白名单通过后才写模板 active memory。
- `model_copy` 夹带的自由文本、单证据、跨作用域和白名单不匹配均 fail-closed；旧播前兼容 Facade 不注册缺少播后依赖的 Handler。
- Task 9 专项/真实 PostgreSQL `8 passed`；完整 unit `1155 passed, 4 warnings`；完整 integration `116 passed, 3 deselected, 5 warnings`；真实模型费用 0 元。
- Task 9 已以 `b6c1cdf feat: govern post live memory promotion` 提交并推送；Task 10 进入 RED。

# 2026-07-16 Phase 13 Task 10（验证中）

- 新增受限 ReviewMemory 输出/Profile/adapter、确定性库存优先 baseline、paired evaluator 和三分类 macro-F1；Agent 只能 stage 单一结构化 candidate，不能写 active memory 或自由文本。
- D-111 固定单候选 JSON Schema/Pydantic 边界、冻结货盘白名单严重违规、避免 replay 主信号泄漏的 baseline，以及真实 macro-F1 门。
- 80 个冻结 case 已经真实 BoundedSpecialistRunner、AUDIT EvidenceResolver、ScriptedModel 和 Evaluation Store 配对；40 validation selected facts 可重建并解锁 holdout。
- PostgreSQL 重启恢复、Task 10 专项共 `11 passed`，相关数据集/Runner/LiveOps/Planner 回归 `66 passed`；完整 unit `1164 passed, 4 warnings`，完整 integration `118 passed, 3 deselected, 5 warnings`，严格目标编码与 `git diff --check` 通过；已提交推送为 `e12de15 feat: evaluate review memory specialist`，真实模型费用仍为 0 元，Task 11 进入 RED。

# 2026-07-17 Phase 13 Task 11-12 与 Acceptance

- 正式 Manifest、Git 源码闭包、HTTPS endpoint、价格快照与持久预算预检通过后执行真实模型；LiveOps 为 `REJECTED`，Planner 与 ReviewMemory 为 `INCONCLUSIVE`，0 个新增 Profile 接入生产。
- 实际费用 `0.042344` CNY，Phase 14 的 `0.60` CNY 预留未使用。Demo 验证默认确定性路由、显式 Specialist 模式与禁止 Agent-to-Agent。
- Acceptance 与 `live-session-p001-sold-out-v1` 只读附录已生成；Phase 状态转为 `AWAITING_PHASE_14_GATE`，未开始 Phase 14。

# 2026-07-17 Phase 14 人机协同 Design/Plan 持久化

- 项目定位更新为播前、播中、播后三场景的人机协同决策支持与受控执行 Runtime；历史 Phase 0-13 Design/Acceptance 保留原文，Phase 13 自主候选结论不改写。
- 新 Phase 14 固定统一 `PREPARE | LIVE | REVIEW` 工作台和播中复合售罄优先切片：确定性系统自动保护，运营主控确认经营恢复，Copilot 只生成可审计结构化方案。
- 新增 Phase 14 Design/Implementation Plan 与 D-113 至 D-120，固定一个播中 Copilot、结构化修改、规则资格加人工确认记忆晋升、质量与效率严格 AND 门、1.00 元 smoke 预算和默认关闭路由。
- 旧 Golden/CI/发布门禁从 Phase 14 顺延为 Phase 15 Discussion Baseline；Phase 14 Acceptance 后必须停止并重新进行 Phase 15 Just-in-Time Gate。
- 本轮只持久化文档，不修改业务代码、不运行真实模型，等待用户单独授权 Phase 14 实施。

# 2026-07-17 Phase 14 Task 1

- 用户已授权 Task 1-12 连续实施。Task 1 新增启动冻结的 `DETERMINISTIC_ONLY | DECISION_SUPPORT` 路由，默认不调用旧 Planner 或 Executor；PlanEngine 售罄 evidence-only 路径保持不变。
- 旧 HumanApproval interrupt 不再授予经营写权限；普通 Graph state 不能伪造 OperatorDecision。显式 Decision Support 的默认 Planner 禁止 Phase 5F fallback，失败记录为 `DEGRADED`。
- 多轮独立审查补齐三项执行边界：真实旧 checkpoint 已排队 `execute_tool` 时仍按冻结路由阻断、授权型 Skill 最终节点二次校验、执行器 `TypeError` 不再触发第二次调用。
- Dashboard 的无 interrupt 会话改为单事务原子终态创建，不经过 `pending_human`；API 文档同步退役旧审批授权语义。
- 最终验证为 unit `1191 passed, 4 warnings`、integration `119 passed, 3 deselected, 5 warnings`；专项复审无 Critical/Important/Minor，真实模型费用仍为 `0.042344` 元累计值且本 Task 新增费用为 0。

# 2026-07-17 Phase 14 Task 2

- 新增 `LiveSessionWorkspace` 三视图及 Incident、EvidenceBundle、Proposal、OperatorDecision、ExecutionCommand 五类深冻结事实，内存与 PostgreSQL Store 使用一致的 CAS、幂等、父作用域和单向状态机。
- PostgreSQL 使用根行锁、数据库墙钟、operator lease 与单调 fencing；事实、幂等账本和 Workspace 版本位于同一事务，故障注入证明三者共同回滚。
- 数据库层新增复合 scope 外键、payload 同构、双向幂等账本、append-only、Proposal 单决定/最新版本、Decision/Command lease 与父 fencing 约束；绕过 Store 同样 fail-closed。
- PostgreSQL 集成测试改用独立随机 schema，覆盖重启、并发 CAS、锁等待过期、迁移重复执行和中断后 fencing；正常结束后整体回收。
- 最终专项 `41 passed`；完整 unit `1209 passed, 4 warnings`；完整 integration `142 passed, 3 deselected, 5 warnings`；规格与质量/安全复审无 Critical/Important。
- Phase 13 manifests 已按最终源码闭包重生成；本 Task 未调用真实模型，累计费用保持 `0.042344` 元。

# 2026-07-17 Phase 14 Task 3

- 新增六角色受治理 EvidenceBundle：可信售罄事件、库存、根/紧急计划、弹幕聚合和主播节奏经固定只读 Resolver 聚合；控制节点不注入 Store、SQL 或写 Skill。
- `EvidenceAssemblyRequest` 只接收稳定 `live_session_id`、`incident_id` 与六个 EvidenceRef；启动冻结的 Context Resolver 加载并复核权威父事实，Bundle Snapshot 同时绑定 Incident 业务摘要。
- 内存与 PostgreSQL Store 均在追加事务内核对 Workspace scope 和 Incident 业务摘要，公开 Store 入口不能绕过 Assembler 用同 ID 的伪造父事实落库。
- 复审整改包含确定性重放时间、完整 envelope digest、外层 Snapshot 重载校验、固定弹幕主题模板与测试夹具真实六角色事实；Phase 13 Manifest 连续生成哈希稳定。
- 当前验证：Task 3 聚合 `71 passed`；相关回归 unit `176 passed`、integration `17 passed`；完整 unit `1238 passed, 4 warnings`、integration `143 passed, 3 deselected, 5 warnings`。真实模型新增费用为 0。
- 最终整改：receipt 由受控 Assembler 闭包登记并绑定原始 Bundle 身份；新增 `EvidenceBundleAssemblyService`，调用面只接受 `EvidenceAssemblyRequest`；D-121 明确无插件/热加载下的进程信任边界。
- 最终验证：Task 3 聚合 `79 passed`；Phase 13 数据/Planner 回归 `23 passed`；完整 unit `1244 passed, 4 warnings`；完整 integration `145 passed, 3 deselected, 5 warnings`；`compileall`、`git diff --check` 前置检查和 Manifest 两次生成哈希均通过；真实模型新增费用 0。
- Task 3 已以 `d3a53a8 feat: assemble governed live evidence` 独立提交并推送至 `origin/main`；用户已有脏文件未纳入。连续游标切换到 Phase 14 Task 4，尚未开始编码。

# 2026-07-18 Phase 14 Task 4

- 新增 `live_ops_decision_support@1.0.0` Copilot Profile，固定两次模型、三次只读 Skill、4000 tokens、五秒 deadline 和结构化 `LiveDecisionProposal`；Agent 只生成供运营比较的建议，不创建 SkillCall、PlanCommand 或经营写入。
- Proposal 领域模型固定 `READY | DEGRADED`、1-3 个封闭 option、完整 EvidenceRef 闭合、备品策略、时机和风险码白名单；模型失败、过期/不可提案证据、Schema 或身份不一致均返回确定性 `DEGRADED` 摘要。
- Copilot 启动时重跑完整 Profile 校验并核对 `profile_digest`；实际执行通过共享 `BoundedSpecialistRunner + ScriptedAgentModel`，写 Skill 请求在 Runner 白名单处拒绝，无网络调用。
- D-122 新增独立 `PHASE14_COPILOT` 预算身份；Phase 13 保持 2.40 元、Phase 14 为 1.00 元、Phase 15 0.60 元保留，总规划账本为 4.00 元。内存/PostgreSQL 预算隔离、settled exposure 和旧 scope 迁移均已覆盖。
- Phase 13 v2/v3 Manifest 已由正式生成器重建，以包含新增源码闭包；case/label 内容未变化。真实模型新增费用为 0。
- 最终验证：Task 4/预算专项 `28 passed`，完整 unit `1260 passed, 4 warnings`，完整 integration `146 passed, 3 deselected, 5 warnings`；compileall、迁移 dry-run、`git diff --check` 和目标文件编码检查通过。
- Task 4 已以 `4ad8de5 feat: add live decision support copilot` 独立提交并推送；用户已有脏文件未纳入。连续游标切换到 Phase 14 Task 5 RED。

# 2026-07-18 Phase 14 Task 5

- 新增 `src/decision_support/commands.py`，定义 `OperatorDecisionDraft`、`OperatorModification`、权威 `DecisionExecutionContext` 和 `DecisionSupportCommandCompiler`；编译器只构造 append-only 事实，不执行任何 Runtime 或平台调用。
- APPROVE/MODIFY 必须绑定 READY Proposal、精确版本、操作员有效 lease/fencing、选项和结构化修改；修改仅允许备品、提示语、优先级和时机。REJECT 只保存人工拒绝事实，不生成命令。
- 编译结果分别生成 `OperatorDecision`、`ExecutionCommand` 和节点级 `PlanCommandType.APPROVE`；原 Proposal 不可变，实际执行继续由 PlanStore/Skill Runtime 负责。
- 新增真实 PostgreSQL Task 5 测试，覆盖六角色 EvidenceRef Proposal、Workspace CAS、operator lease、fencing、幂等重放和重启读取。
- Phase 13 v2/v3 Manifest 已由正式生成器重建以绑定新增 `commands.py` 源码闭包；case/label 未改变，真实模型费用新增 0。
- 当前验证：Task 5 unit `8 passed`，PostgreSQL 专项 `1 passed`，完整 unit `1268 passed, 4 warnings`，完整 integration `147 passed, 3 deselected, 5 warnings`；compileall 和 `git diff --check` 通过，待提交推送。
- Task 5 已以 `c20d1ab feat: compile operator decisions safely` 独立提交并推送；9 个目标文件进入提交，用户已有脏文件和无关脚本未纳入。Task 5 规格与质量复核由主模型接管完成，无阻断项。

# 2026-07-18 Phase 14 Task 6

- 已切换到 Task 6 RED，目标是接入可信售罄自动保护与人工经营恢复，严格复用 Phase 12B Preemption/售罄 CAS/对账控制面。
- 当前尚未编写 Task 6 生产代码或运行真实模型；下一步先建立可信事件、冻结/CAS/陈旧阻断、无 OperatorDecision 拒绝恢复和 `SIDE_EFFECT_UNKNOWN` 保持对账的预期失败测试。
- Task 6 RED/GREEN 已完成：新增 `HumanGuidedSoldOutFlow`、内存/PostgreSQL root Workspace 查询和可信 Incident 事实链；Task 6 unit `7 passed`，Task 5/6 聚合 `15 passed`，Task 6 PostgreSQL `2 passed`。
- 复审补齐 Compiler 的 `incident_id` 绑定和恢复入口的完整模型重载；自动保护不调用 CommandService，原始 PlanCommand 直接入口 fail-closed，真实模型费用仍为 `0.042344` 元。
- Task 6 最终规格与质量复核无 Critical/Important 阻断；完整 unit `1275 passed, 4 warnings`，完整 integration `149 passed, 3 deselected, 5 warnings`。新增 `sold_out_flow.py` 后按正式生成器重建 Phase 13 v2/v3 Manifest，所有静态和编码门禁通过，准备提交推送。
- Task 6 已以 `43d182f feat: coordinate human guided sold out recovery` 独立提交并推送，连续执行游标切换到 Phase 14 Task 7 RED。

# 2026-07-18 Phase 14 Task 7

- Task 7 开始前保持真实模型费用 `0.042344` 元；不新增模型调用，先测试 API 鉴权、幂等、Proposal 版本和 WebSocket 事件顺序。
- Task 7 已完成 GREEN 和复审：新增 API Service 门面、Operator 鉴权、Proposal/Decision 幂等键校验、REJECT 只读事实路径、approved 未装配时 fail-closed，以及按 live_session scope 的 WebSocket 广播。
- 最终验证：Task 7 专项 `7 passed`，旧 API/WebSocket/Harness `14 passed`，完整 unit `1282 passed, 4 warnings`，完整 integration `149 passed, 3 deselected, 5 warnings`；Manifest、compileall、迁移 dry-run、diff 和编码检查通过，待独立提交推送。
- Task 7 已以 `eb28885 feat: expose decision support workspace api` 独立提交并推送，连续执行游标切换到 Phase 14 Task 8 RED。

# 2026-07-18 Phase 14 Task 8 验证完成，待提交

- Task 8 完成 `PREPARE | LIVE | REVIEW` 三视图工作台，固定单一 `live_session_id`，运营区拥有结构化决定入口，主播提示区只读；UI 不直接访问 Store、PlanEngine、Skill 或 Adapter。
- 首轮 RED 暴露稳定 session ID 和资源后缀契约缺口；随后补齐 `live-session-id`、`/proposals`、`/decisions` 和 WebSocket 事件契约。
- 独立前端审查发现并修复对账/降级/重连仍可写、旧 HTTP/WS session 竞态、方案选择重置、Token 缺失、Proposal 仅静态声明、Review 候选/执行结果缺失和移动状态换行风险；修复不改变后端公开接口。
- 修复后 Task 8 专项 `6 passed`；相关 API/Store/WebSocket 聚合 `60 passed, 1 warning`；完整 unit `1288 passed, 4 warnings`；完整 integration `149 passed, 3 deselected, 5 warnings`；JavaScript 语法、compileall、真实模型费用门禁通过，新增真实模型费用为 0。
- 当前待完成：严格 UTF-8/LF/尾随空白、`git diff --check`、最终复审收口、独立提交 `feat: build operator decision workspace` 并推送；推送后切换 Task 9 RED。

# 2026-07-18 Phase 14 Task 8 已提交，Task 9 RED

- Task 8 已以 `0a8f08c feat: build operator decision workspace` 独立提交并推送，提交只包含工作台、契约测试、路线图和本阶段工作日志；用户已有脏文件和无关脚本未纳入。
- Task 8 最终证据为专项 `6 passed`、相关 API/Store/WebSocket 聚合 `60 passed, 1 warning`、完整 unit `1288 passed, 4 warnings`、完整 integration `149 passed, 3 deselected, 5 warnings`；新增真实模型费用为 0。
- 连续游标切换到 Phase 14 Task 9 RED。Task 9 只复用 Phase 13 Candidate Store/PromotionPolicy 与受治理 `retrieve_anchor_memory`，增加规则资格事实和人工确认命令，不允许 Agent 或 UI 直接写 active memory。

# 2026-07-18 Phase 14 Task 9 验证完成，待提交

- 新增 `review_feedback.py` 与 Phase 14 资格/确认 DDL；候选状态固定为 `STAGED -> ELIGIBLE_AWAITING_OPERATOR -> APPLIED`，资格事实、人工确认意图和确认结果均可幂等重放。
- PromotionPolicy 现在必须验证持久化资格事实、可信 Trace Resolver、候选版本和 operator confirmation intent；直接调用、伪造 Trace、跨作用域/冲突/敏感字段、白名单不匹配均 fail-closed。
- 资格事实先持久化再 CAS 状态转换，重试可修复中断；active memory 使用稳定 key，active 写入后候选 CAS 失败时可通过同一确认命令恢复，不创建第二条记忆。
- Phase 13 历史直接晋升回归迁移到新受控门面；正式生成器重建 `phase13-v2/v3` Manifest 以包含新源码闭包。
- 最终验证：Task 9 专项 unit `12 passed`，相关 unit `34 passed`，相关 PostgreSQL integration `4 passed`，完整 unit `1300 passed, 4 warnings`，完整 integration `150 passed, 3 deselected, 5 warnings`；compileall、Manifest、diff 和真实模型费用门禁通过。
- 当前待完成：目标文件严格 UTF-8/LF/尾随空白检查，提交 `feat: confirm governed memory promotion` 并推送。

# 2026-07-18 Phase 14 Task 9 最终复核

- 未返回可验证报告的只读复审线程已停止，主模型完成规格与安全边界复核。
- Task 9 复跑证据：相关 unit `20 passed`、相关 PostgreSQL integration `2 passed`、完整 unit `1301 passed, 4 warnings`、完整 integration `150 passed, 3 deselected, 5 warnings`。
- 当前进入严格编码/差异检查和提交推送；不暂存用户已有脏文件。

# 2026-07-18 Phase 14 Task 9 已提交，Task 10 RED

- Task 9 已以 `dbd5768 feat: confirm governed memory promotion` 提交并推送，`origin/main=dbd5768`。
- 连续游标切换到 Task 10 RED：先实现固定复合事故数据集、离线规则回归、配对人机评估和 3-5 名代理运营的随机交叉对照；真实模型仍需等待 Task 11 全部预检。

# 2026-07-18 Phase 14 Task 10 验证完成，待提交

- 新增 `src/decision_support/evaluation.py` 和 `evaluation/phase14_human_support/`，固定四组复合事故、16 个脱敏 case、ScriptedModel 基线、3-5 名运营员的 24-40 次随机交叉记录和规则优先严格 AND 指标。
- 审查整改补齐过期证据、CAS/版本冲突、未知副作用、Manifest/case 身份绑定、完整 schema/generator digest、同 case 配对、精确门槛和工作负担字段；不接入真实模型或经营写路径。
- Task 10 专项 `9 passed`，受影响数据回归 `20 passed`，完整 unit `1310 passed, 4 warnings`，完整 integration `150 passed, 3 deselected, 5 warnings`；当前只剩文档留痕、暂存、提交和推送。

# 2026-07-18 Phase 14 Task 10 已提交，Task 11 RED

- Task 10 已以 `3dc7f40 test: add human decision support evaluation` 提交并推送，`origin/main=3dc7f40`。
- 连续游标切换到 Task 11 RED：先实现真实模型正式预检、ScriptedModel 全量演练和 `PASS | INCONCLUSIVE | FAIL` 严格结论；预检通过前不访问外部模型。

# 2026-07-18 Phase 14 Task 11 验证完成，待提交

- Task 11 已完成正式预检、可信发送门、Scripted rehearsal、未知 usage 保守结算和默认跳过 external smoke；真实模型未调用。
- 验证：专项 `7 passed`，Task 10/11/Manifest `27 passed`，完整 unit `1317 passed, 4 warnings`，完整 integration `150 passed, 3 deselected, 5 warnings`，external `1 skipped`。
- 当前只剩编码/差异确认、暂存、提交和推送，之后进入 Task 12 Demo/Acceptance。

# 2026-07-18 Phase 14 Task 11 已提交，Task 12 RED

- Task 11 已以 `6a79359 feat: evaluate human decision support formally` 提交并推送，`origin/main=6a79359`。
- 连续游标切换到 Task 12 RED：固定 PREPARE/LIVE/REVIEW 三视图同一 session 的无外部依赖 Demo、回放证据和 Acceptance；真实模型仍不调用。

# 2026-07-18 Phase 14 Task 12 Demo 与 Acceptance

- 新增无外部依赖 `scripts/run_phase14_human_support_demo.py` 和 Demo 契约测试，复用 Workspace、售罄保护 Flow、OperatorDecision Compiler、PromotionPolicy 与 Task 11 Scripted rehearsal。
- Demo 输出固定三场景同会话回放、自动保护 `APPLIED`、结构化人工 `MODIFY` 决定、未提交经营命令、记忆 `APPLIED`/幂等重放、生产默认 `DETERMINISTIC_ONLY` 和真实模型 `INCONCLUSIVE`。
- 验证：Task 12 专项 `3 passed`；Task 10/11/Manifest 回归 `19 passed`；CLI `exit 0`；src 与 Task 12 定向编译、`git diff --check` 通过；全量 unit `1320 passed, 4 warnings`，integration `150 passed, 3 deselected, 5 warnings`，external smoke `1 skipped`。
- 生成 Phase 14 Acceptance 报告；阶段状态改为 `AWAITING_PHASE_15_GATE`，不实施 Phase 15。
- Task 12 已以 `c4124ce docs: accept phase 14 human decision support` 提交并推送，状态留痕随后以 `d250533`、`5cd090b` 更新并推送；连续执行停止，等待 Phase 15 Just-in-Time Gate。恢复时以 `git log -1 --oneline --decorate` 读取最新 HEAD。

# 2026-07-18 Phase 15 Stage A 设计持久化

- 新增并审核 Phase 15 Golden Release Gates Design、Implementation Plan 和连续恢复入口；旧 Phase 14 Golden/CI 文档标记为 `MIGRATED_TO_PHASE_15_JIT_BASELINE`，历史 Discussion Baseline 标记为 `SUPERSEDED_BY_PHASE_15_DESIGN`。
- 追加 D-123 至 D-132，冻结双轨 Release、48 例 Golden、规则优先评估、真人交叉对照、0.60 元模型预算、三级 CI/覆盖率、ToolRegistry 退役、默认路由两次 Release 和 Stage A/B 边界。
- 更新路线图、总控计划、task_plan、findings、progress 和 continuous state，使阶段状态、预算、路由和下一步一致。
- 本轮不修改业务代码、数据库、CI、真人采集器或真实模型；不运行测试；仅在目标文档验证通过后提交 `docs: define phase 15 release gates` 并推送。
- 当前状态：`PHASE_15_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`；Stage B Task 1-12 等待用户单独授权，Phase 15 完成后停止，不自动进入新 Phase。

# 2026-07-18 Phase 15 Task 1 RED

- 用户已授权 Phase 15 Stage B，连续游标切换到 Task 1：发布入口、迁移清单与仓库事实。
- 已同步实时状态、总控计划、路线图、task_plan、findings、progress 和恢复入口；Task 1 尚未修改业务代码。
- 下一步建立迁移/入口/敏感扫描的预期失败测试，再按最小 GREEN 对齐已有事实；真实模型仍禁止调用。

# 2026-07-18 Phase 15 Task 1 GREEN

- Task 1 预期失败测试已确认四项缺口，最小实现后专项与相关历史迁移/Demo 回归为 `24 passed`。
- 迁移 dry-run 现在包含 17 步，Phase 13 Memory、Phase 14 两组事实和 Phase 15 基础 ReleaseRun 均存在；统一入口 help、Phase 13/15 Demo、tracked 敏感扫描和 compileall 通过。
- 当前进入规格/质量审查；审查通过后再运行完整 unit/integration、严格编码检查、暂存边界检查并提交推送。

# 2026-07-18 Phase 15 Task 1 VERIFY

- 规格审查和代码质量审查无 Critical/Important 阻断；修复 README 表格插入位置和专项测试未使用导入。
- 完整 unit `1324 passed, 4 warnings`，integration `150 passed, 3 deselected, 5 warnings`；实际 PostgreSQL/Kafka 回归沿用现有套件，Task 1 未运行真实模型。
- 当前只剩目标文件严格编码、`git diff --check`、暂存边界、独立提交和推送；通过后切换 Task 2 RED。

# 2026-07-18 Phase 15 Task 1 READY TO PUSH

- Task 1 已完成 RED/GREEN/REVIEW/VERIFY：迁移从 13 步扩展到 17 步，统一入口新增三阶段 Demo，tracked 敏感扫描可编译并严格运行。
- 完整 unit `1324 passed`、integration `150 passed, 3 deselected`；目标文件编码、compileall 和 `git diff --check` 通过。
- 下一步只暂存 Task 1 文件，提交 `build: align phase 15 release entrypoints` 并推送，然后开始 Task 2 RED。

# 2026-07-18 Phase 15 Task 2 RED

- Task 1 已推送：`2a88224 build: align phase 15 release entrypoints`。
- 连续游标切换到 Task 2：48 例 Golden Dataset、冻结 Schema、Manifest 和字节稳定生成器；真实模型仍禁止调用。
- 下一步检查 Phase 13/14 数据集复用边界，先写 split、case ID、脱敏、Manifest 摘要和双次生成一致性的红灯测试。

# 2026-07-18 Phase 15 Task 2 GREEN

- Task 2 红灯确认缺失 `src.release_gates.dataset`，最小实现后专项 `5 passed`。
- 已生成 `phase15-runtime-v1` 的 cases/labels/Manifest；48 例、三场景来源、12/24/12 split、脱敏、case/artifact digest 和历史 supersedes 约束闭合。
- Manifest 额外绑定规则摘要和当前 `release_gates` 源码闭包摘要；下一步运行 Phase 13/14 数据回归、完整 unit 和规格/质量审查。

# 2026-07-18 Phase 15 Task 2 REVIEW

- Phase 13/14 数据回归 `25 passed`，完整 unit `1329 passed, 4 warnings`；新增 labels split/ID 校验通过。
- 修复历史 Phase 13 generator 的目录级 Schema 污染，v2/v3 Manifest 只更新来源闭包摘要；Phase 13 240 例 case/label 内容保持不变。
- 当前进入完整 integration、严格编码、敏感扫描、双次生成和 diff 验证；通过后提交 `feat: version phase 15 golden dataset`。

# 2026-07-18 Phase 15 Task 2 VERIFY

- 全量 integration 明确通过：`150 passed, 3 deselected, 5 warnings`；全量 unit 明确通过：`1329 passed, 4 warnings`；退出码均为 `0`。
- Task 2 专项和 Phase 13/14 聚合回归共 `25 passed`，真实模型和外部服务费用保持 `0`。
- 阶段状态改为 `PHASE_15_TASK_2_READY_TO_PUSH`；下一步只执行严格编码/敏感扫描、Manifest 双次生成、差异检查、暂存、提交和推送。

# 2026-07-18 Phase 15 Task 2 COMMIT/PUSH

- Task 2 已提交并推送：`eb31dd9 feat: version phase 15 golden dataset`，`origin/main=eb31dd9`。
- 只提交 Golden/labels、Manifest/Schema、生成器、历史 Phase 13 闭包修复、Task 2 测试和阶段留痕；用户脏文件仍未暂存。

# 2026-07-18 Phase 15 Task 3 RED

- 连续游标切换到 Task 3：统一 Subject Runner 与规则门禁。
- RED 将先证明当前仓库缺少 `GoldenCase`/`SubjectManifest`/`EvaluationCaseResult`、五类受限 Runner 和规则严重违规门禁。

# 2026-07-18 Phase 15 Task 3 GREEN/VERIFY

- Task 3 专项与 Task 2 聚合 `15 passed`；全量 unit `1337 passed, 4 warnings`；全量 integration `150 passed, 3 deselected, 5 warnings`。
- 规则优先门禁已覆盖 Skill 精确版本/权限、Schema、EvidenceRef、Plan/Event 状态、CAS/fencing、幂等、敏感输出、预算、调用次数和 no-fallback；Subject 异常固定为 `BLOCKED`。
- 为保持 Phase 13 历史资产不可变，源码 digest/Generator/测试均排除后续 `src/release_gates`；v2/v3 生成器回归通过。
- 当前状态改为 `PHASE_15_TASK_3_READY_TO_PUSH`；下一步严格编码、敏感扫描、编译、差异检查、暂存、提交和推送。

# 2026-07-18 Phase 15 Task 3 COMMIT/PUSH

- Task 3 已提交并推送：`9f9d835 feat: enforce release subject rules`，`origin/main=9f9d835`。
- 规则 Runner 的严重门禁、五类 Subject 域绑定、历史 Phase 13 闭包隔离和完整 unit/integration 证据均已落档。

# 2026-07-18 Phase 15 Task 4 RED

- 连续游标切换到 Task 4：Release Store、双轨结论与 Phase 15 预算。
- RED 将先证明缺少 ReleaseRun/CaseResult 唯一性、双轨结论状态机、digest 完整性和 `PHASE15_COPILOT_SMOKE=0.60` 元隔离。
# 2026-07-18 Phase 15 Task 4 GREEN/VERIFY

- Release Store、双轨结论和独立 Phase 15 预算已完成；PostgreSQL 事实重放、唯一键、并发预算边界和迁移 dry-run 通过。
- 全量 unit `1341 passed, 4 warnings`；全量 integration `152 passed, 3 deselected, 5 warnings`；真实模型/真人采集/外部服务费用保持 `0`。
- 预算从共享账本调整为 Phase 15 自有模块和表，避免改变 Phase 13 历史 Manifest code digest；Phase 13 数据稳定性回归恢复通过。
- 阶段状态改为 `PHASE_15_TASK_4_READY_TO_PUSH`；下一步严格编码/敏感扫描、差异检查、暂存、提交和推送。

# 2026-07-18 Phase 15 Task 4 COMMIT/PUSH

- Task 4 已提交并推送：`fefd926 feat: persist dual release decisions`，`origin/main=fefd926`。
- 共享 Phase 13 预算与历史 Manifest 保持不变；Phase 15 使用自有预算模块、DDL、Release Store 和双轨状态机。

# 2026-07-18 Phase 15 Task 5 RED

- 连续游标切换到 Task 5：真人交叉对照采集器。
- RED 先固定 3-5 名真实参与者、每人 8 次、四组等价场景和 Promotion digest 隔离，禁止伪造真人证据。

# 2026-07-18 Phase 15 Task 5 GREEN/REVIEW

- 内存 Store 红灯修复后专项 `5 passed`；PostgreSQL 真实 session/assignment/response 重启恢复、digest、study 隔离和 Manifest 漂移测试 `2 passed`；Phase 15 Store/预算聚合 `9 passed`。
- 修复 PostgreSQL `Path` 初始化缺口、身份 fallback、study 范围过滤、冻结 Manifest/artifact 校验和 participant limit advisory lock；DDL 幂等补齐 assignment/session 联合唯一键与 response 联合外键。
- 当前只剩完整 unit/integration、严格编码/敏感扫描、迁移 dry-run、最终复审、提交和推送；真实模型费用 `0`。

# 2026-07-18 Phase 15 Task 5 VERIFY

- Task 5 API `2 passed`、PostgreSQL study `2 passed`；完整 unit `1348 passed, 4 warnings`，integration `154 passed, 3 deselected, 5 warnings`，退出码均为 0。
- 修复 Phase 13 动态源码闭包对 Task 5 gateway 集成面的污染，v2/v3 Manifest 重建后字节稳定；case、label、prompt、Schema、价格和历史结论未改变。
- 迁移 dry-run、compileall、目标编码扫描、敏感扫描和 `git diff --check` 通过。全仓编码扫描的既有 4 errors/51 warnings 单独报告，不阻断本 Task。
- 状态切换为 `PHASE_15_TASK_5_READY_TO_PUSH`；下一步只暂存 Task 5 文件，提交并推送，之后进入 Task 6 RED，真实模型继续禁止直到预检完成。

# 2026-07-18 Phase 15 Task 5 COMMIT/PUSH 与 Task 6 RED

- Task 5 已提交并推送：`d181cd1 feat: capture blinded operator studies`，本地与 `origin/main` 一致；用户既有脏文件未纳入。
- 连续游标切换到 Task 6 RED：新建 `copilot_smoke.py` 的测试契约，真实模型/外部 endpoint 继续禁止访问。

# 2026-07-18 Phase 15 Task 6 GREEN

- Task 6 首轮 RED 为缺少 `src.release_gates.copilot_smoke`；实现后专项 unit `7 passed`、PostgreSQL 预算/重启 `1 passed`。
- 相关 Phase 15 聚合 unit `18 passed`、integration `5 passed`，compileall 通过；真实模型和网络 endpoint 仍未访问。
- 下一步执行规格/质量审查、完整 unit/integration、严格编码与敏感扫描，之后提交 `feat: evaluate phase 15 copilot smoke` 并推送。

# 2026-07-18 Phase 15 Task 6 VERIFY

- Task 6 专项 unit `8 passed`、PostgreSQL `1 passed`；完整 unit `1356 passed, 4 warnings`，integration `155 passed, 3 deselected, 5 warnings`。
- unknown usage 严格返回 `BLOCKED`；超 reservation usage 封顶并阻断；真实模型与网络 endpoint 费用保持 `0`。
- 状态切换为 `PHASE_15_TASK_6_READY_TO_PUSH`；下一步只暂存 Task 6 文件，提交并推送，之后进入 Task 7 RED。

# 2026-07-18 Phase 15 Task 7 GREEN

- Task 7 RED 确认缺少 `src.release_gates.report`；实现严格 PromotionDecision、FinalReleaseDecision 和稳定 JSON/Markdown 后专项 `5 passed`。
- 与 Task 5/6/Store 相关聚合 `22 passed`，真实模型/网络 endpoint 未访问；下一步进行完整回归和提交前门禁。

# 2026-07-18 Phase 15 Task 7 VERIFY

- 完整 unit `1361 passed, 4 warnings`、integration `155 passed, 3 deselected, 5 warnings`，退出码均为 0。
- 状态切换为 `PHASE_15_TASK_7_READY_TO_PUSH`；下一步只暂存 Task 7 文件，提交并推送，之后进入 Task 8 RED。

# 2026-07-18 Phase 15 Task 8 RED

- 从 `984b3ff` 继续。Task 8 先补统一 Release CLI、覆盖率检查和 GitHub Actions evidence 读取的红灯契约。
- 目标是稳定区分 `PASS`、`FAIL`、`BLOCKED` 和非法输入；PR/Nightly 默认不调用真实模型，Release 缺少外部证据时保持 fail-closed。
- 用户已有脏文档和临时脚本保持 unstaged，不纳入本 Task。

# 2026-07-18 Phase 15 Task 8 GREEN / REVIEW

- 统一 CLI、覆盖率门禁、Actions evidence 读取和 Demo 已完成；初轮专项 `13 passed`。
- 独立只读审查发现 Release 强制证据、36/48 split、冻结身份和 EvidenceRef 的 Important/Critical 缺口，已逐项修复并复跑相关聚合 `20 passed`。
- 当前报告事实：PR 48 -> 36 个非 holdout，技术 PASS、Promotion BLOCKED；Release 缺数据库/覆盖率/Actions 时技术 BLOCKED、最终 NOT_RELEASED；真实模型/网络调用仍为 0。

# 2026-07-18 Phase 15 Task 8 COMMIT/PUSH 与 Task 9 RED

- Task 8 已提交并推送：`d2d4c89 build: add local phase 15 release gates`。
- Task 9 开始：先为三层 GitHub Actions workflow 建立契约红灯；不执行外部 Actions，不伪造托管 run evidence。

# 2026-07-18 Phase 15 Task 9 GREEN / REVIEW

- 新增 PR/Nightly/Release 三层 workflow；workflow contract `3 passed`，静态 YAML、敏感扫描、目标编码和 diff 检查通过。
- 审查整改已补齐 Release coverage/DSN/受保护 evidence 校验、Kafka/Zookeeper 探活、PostgresSaver 专项入口和三层顶层/job 权限与 trigger 断言。
- 真实 GitHub Actions run、仓库 tag ruleset 和 protected environment 实际配置仍未取得；这些外部事实保持 `BLOCKED`，没有伪造绿色 evidence。

# 2026-07-18 Phase 15 Task 9 VERIFY

- workflow contract `3 passed`；完整 unit `1375 passed, 4 warnings`；integration `155 passed, 3 deselected, 5 warnings`；目标 YAML 解析、敏感扫描、迁移 dry-run、编码和 diff 检查通过。
- 真实 Actions run、protected environment secrets 和 tag ruleset 仍是外部 `BLOCKED` 证据，不影响 workflow 代码契约完成；下一步只提交并推送 Task 9。

# 2026-07-18 Phase 15 Task 10 GREEN

- 删除 `src/config/tool_registry.py`，`AgentToolExecutor` 去除旧 `registry` 构造参数，生产消费者统一使用 Catalog/SkillPolicyView。
- 迁移旧单元测试、Security Hook 最小 Fixture、Phase 11B 路由和 Phase 3A Demo；生产 `ToolRegistry/get_default_tool_registry/src.config.tool_registry` 命中 0。
- Task 10 聚合 `104 passed`、无 warning；正在进行规格/质量审查和全量验证。

# 2026-07-18 Phase 15 Task 10 VERIFY

- 独立审查发现 0 Critical、4 Important；已修复售罄 Runtime 幂等键业务字段泄漏、Legacy 异常回显和 README 退役说明，PolicyView 同进程注入按 D-121 留痕处理。
- Phase 13 v2/v3 与 Phase 15 Manifest 已按最终源码闭包重建，case/label、价格、Prompt、Schema 和历史评估事实未变化。
- Task 10 专项 `21 passed`；完整 unit `1372 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`；目标 compileall、生产 Facade 扫描和 `git diff --check` 通过；真实模型新增费用 `0`。

# 2026-07-18 Phase 15 Task 11 RED/GREEN

- Task 10 已以 `1f4af05 refactor: retire tool registry facade` 推送，连续游标进入 Task 11。
- RED 首次因缺少 `src.release_gates.routing` 收集失败；新增 `ReleaseRouteProfile`、`PHASE15_ROUTE_PROFILE`、`PHASE15_DECISION_SUPPORT_PROMOTION` 和三类路由的启动解析。
- GREEN 固定 `LEGACY_DEFAULT -> EXPLICIT_RELEASE -> VERIFIED_DEFAULTS`：显式 Release 强制 Skill Runtime/PlanEngine，Verified Defaults 需要 Technical PASS，只有 Promotion PROMOTE 开启 Decision Support。
- D-133 已新增；Task 11 路由专项 `5 passed`，真实模型和外部 Release 证据仍为 0。

# 2026-07-18 Phase 15 Task 11 VERIFY

- Task 11 的显式 Release、Verified Defaults、三路启动冻结和独立 Promotion 门禁已完成；专项 `18 passed`。
- 完整 unit `1379 passed, 4 warnings`，integration `155 passed, 3 deselected, 5 warnings`；正式源码/入口 compileall、生产 import、严格编码和 `git diff --check` 通过。
- 全仓 compileall 仅被用户已有临时脚本的语法错误阻断；未修改这些脚本。真实模型、GitHub Actions 和外部 Release 证据仍为 0，待提交推送 Task 11。

# 2026-07-18 Phase 15 Task 11 COMMIT/PUSH 与 Task 12 RED

- Task 11 已提交并推送：`efe16c5 feat: promote verified runtime defaults`，远端与本地一致。
- Task 12 开始核对三场景 Demo、48 例 Golden、双轨 Release 结论、外部证据状态和最终停止条件；真实模型、GitHub Actions 和外部 Release 仍不调用/不伪造。

# 2026-07-18 Phase 15 Task 12 VERIFY

- 三场景业务闭环、两次本地 Release profile、48 例冻结 Manifest 摘要、Promotion BLOCKED 和确定性默认路由均已写入可重复报告。
- unit `1382 passed, 4 warnings`，integration `155 passed, 3 deselected, 5 warnings`；Task 12 专项 `3 passed`、相关聚合 `33 passed`，迁移 dry-run 和正式源码 compileall 通过。
- 真实模型、真人对照、coverage artifact、PostgreSQL Release 事实和 GitHub Actions evidence 未提供；Release 返回 BLOCKED，Phase 15/Final Acceptance 为 INCONCLUSIVE，阶段停止且不自动进入下一阶段。

# 2026-07-18 Phase 15 Task 12 COMMIT/PUSH 与最终状态

- Task 12 已提交并推送：`c01a5da docs: accept agent runtime release`，远端与本地一致。
- Phase 15 以 `PHASE_15_COMPLETE_INCONCLUSIVE` 收口；报告、双轨结论和业务闭环证据已保存，不自动进入下一阶段。

# 2026-07-18 Phase 16 Task 5 REVIEW 整改

- 最终质量复审发现 Coordinator 以本地业务墙钟计算 PostgreSQL dispatch claim 剩余时间，慢 Worker 可能把两秒数据库窗口放大后采纳迟到 Analysis。
- 已新增真实 PostgreSQL RED `1 failed`，再由 InMemory/PostgreSQL Store 各自的权威时钟返回剩余秒数；GREEN 为 `1 passed`。Coordinator 继续只允许单次发送、超时只追加 `DEGRADED`，不重新调用模型。
- D-147、Task Plan 和实时状态已同步；当前仍在 Task 5 VERIFY，等待规格复审、全量 unit/integration 与提交前门禁，真实模型费用为 `0.000000 CNY`。

# 2026-07-18 Phase 16 Task 5 VERIFY

- D-147 最终复核新增 `REVIEW` 终态形状 RED：仅有 claim 不能授权携带 Analysis 的
  `DEGRADED` Outcome。内存与 PostgreSQL 都已收紧为同 claim、无 Analysis/Proposal 的唯一审计闭合。
- 专项 unit `25 passed`、隔离 PostgreSQL `20 passed`、完整 unit `1420 passed, 4 warnings`、完整 integration `172 passed, 7 deselected, 5 warnings`。数据库测试临时使用隔离 `5434` 容器，未修改 5432 用户服务或仓库配置；真实模型费用仍为 `0.000000 CNY`。
- 当前进入 COMMIT：只暂存 Phase 16 Task 5 代码、DDL、测试、决策与工作日志，随后推送并切换到 Task 6 RED。

# 2026-07-18 Phase 16 Task 5 PUSHED / Task 6 RED

- Task 5 已以 `b584808 feat: analyze high-conflict live evidence` 推送到 `origin/codex/phase16-controlled-multi-agent`；本地与远端一致，用户脏文件未纳入。
- 连续游标进入 Task 6。该任务将新增受限 Planner、完整 Proposal 父链/摘要、整份 Validator 和 READY Outcome；OperatorDecision、Compiler、HTTP/WebSocket、前端和自动经营恢复仍不在范围内。

# 2026-07-18 Phase 16 Task 6 GREEN / REVIEW

- Task 6 已从 Planner 缺失的预期 RED 转为 GREEN：受限 Planner 只接收精确 Bundle 与持久化
  Analysis，输出经整份 Validator 验证后才以完整 immutable lineage 持久化 Proposal 与 READY Outcome。
- 聚合专项为 `53 passed`，新增 Planner Profile identity 与 PostgreSQL direct SQL bypass 专项为
  `2 passed`，PostgreSQL READY/restart 专项为 `1 passed`。未运行真实模型，费用仍为
  `0.000000 CNY`。
- D-148 固定双 Agent/Coordinator 聚合预算和 Store/DDL 写入边界。当前正在独立规格审查，之后
  才能进入质量/安全审查、全量 VERIFY、独立提交 `feat: validate multi-agent live proposals` 与推送。

# 2026-07-18 Phase 16 Task 6 REVIEW 整改

- 规格审查的 Planner 重复发送、Proposal/READY 中断恢复和 Analyst 总预算三项 Important 已验证并
  修复。D-149 固定 Planner Analysis-bound claim、Proposal 终态补写和端到端最小剩余时间规则。
- 额外修复普通 Phase 14 Proposal 被误解为多 Agent Schema 的历史回归；显式 `MULTI_AGENT` marker
  是新 Validator 的唯一入口。
- 当前专项单元为 `56 passed`；PostgreSQL 已有完整套件单独绿色证据前仍不进入提交，真实模型
  费用保持 `0.000000 CNY`。

# 2026-07-18 Phase 16 Task 6 REVIEW 整改二至四

- D-152：通用 Proposal Store/API 拒绝 `MULTI_AGENT`，Coordinator 使用专用 Store 入口及独立
  PostgreSQL context；多 Agent `APPROVE/MODIFY` 必须绑定精确 READY Outcome，Planner 全局预算超时
  统一归类为 `COORDINATOR_TIMEOUT`。新增 RED/GREEN `3 passed`，Phase 14 API/OperatorDecision/Task 6
  聚合为 `83 passed`；真实模型费用仍为 `0.000000 CNY`。

- D-151 补齐 Analyst 返回后、Analysis 验证后和每个模型派生事实写入前的五秒预算重检；Planner
  正文只保留精确 Bundle 与已验证 Analysis。过期 Planner claim 的 LIVE->REVIEW 竞态可同次写入无
  父链超时终态，任何其他 failure code 由内存 Store、PostgreSQL Store 与 DDL trigger 共同拒绝。
- 新增 unit `5 passed`、PostgreSQL Store/DDL `2 passed`、D-147/D-150 正向 PostgreSQL `3 passed`，
  以及直接 SQL trigger 旁路 `1 passed`；专用 `5434` 容器已重放正式 DDL，真实模型费用仍为 `0.000000 CNY`。

- D-150 修复入口前 I/O 未计入五秒预算，以及 Planner 已发送后 REVIEW 返回半成品 Proposal 的缺口。
- 新规则为：公共入口启动 deadline；LIVE 只补 READY；REVIEW 只对已发送 Planner 追加无父链
  DEGRADED。内存新专项为 `58 passed`，PostgreSQL REVIEW 闭合为 `1 passed`；完整回归仍待最终
  独立退出码，真实模型费用保持 `0.000000 CNY`。

# 2026-07-18 Phase 16 Task 6 VERIFY / READY_TO_COMMIT

- D-152 已将多 Agent Proposal 关进 Coordinator 专用 Store/DDL 写入边界，并要求经营 `APPROVE/MODIFY`
  读取精确匹配的 READY Outcome；两项质量/安全 Important 已补 RED/GREEN，整改复审 PASS。
- 最终验证：Task 6 相关聚合 `83 passed`、真实 PostgreSQL 套件 `29 passed`、direct-SQL 拒绝 `1 passed`、
  完整 unit `1440 passed, 4 warnings`、完整 integration `181 passed, 7 deselected, 5 warnings`。真实模型未调用，Phase 16 费用为
  `0.000000 CNY`。
- 当前只待重新执行目标文件严格编码检查、`git diff --check` 和独立提交 `feat: validate multi-agent live proposals`；
  推送并核验远端 SHA 前不开始 Task 7。

# 2026-07-18 Phase 16 Task 6 PUSHED / Task 7 RED

- Task 6 已以 `d42eab9 feat: validate multi-agent live proposals` 推送到
  `origin/codex/phase16-controlled-multi-agent`，本地与远端 SHA 一致；用户已有脏文件未纳入。
- Task 7 只开始受治理 escalation API/WebSocket 的 RED。请求不能携带 Profile、scope、trigger 或授权
  自证，服务端必须从 append-only Store 重建；运营仍是经营恢复唯一授权方。

# 2026-07-18 Phase 16 Task 7 GREEN / REVIEW

- 新增规范幂等的人工升级端点与 Service；默认未装配 Coordinator 时 `503`，认证关闭时也在任何
  兼容管理员路径前 `503`。服务端重载 Bundle、校验 LIVE/CAS、获取 lease/fencing，才调用显式注入的
  Coordinator；没有模型或经营执行直连。
- Workspace/WS 统一广播完整 append-only 投影，包含 escalations、conflict_analyses、
  multi_agent_outcomes、proposals 与既有事实。D-153/D-154 记录公开请求与认证关闭门禁。
- 当前 API/Service 聚合 `21 passed`、隔离 PostgreSQL Service/Coordinator 集成 `1 passed`；进入
  规格与质量/安全双重复审，真实模型费用仍为 `0.000000 CNY`。

# 2026-07-18 Phase 16 Task 7 D-155 REVIEW REMEDIATION

- 双重复审发现同规范 key 的响应丢失重试会被首次写入后的旧 CAS 错误阻断，且完整 Workspace 根
  payload 会破坏既有副屏 `data.workspace` 消费。D-155 固定同 Bundle 人工事实的恢复例外和兼容封装。
- 新增 PostgreSQL response-loss RED/GREEN：重试使用当前 Store 版本恢复同一 READY Outcome，Analyst/
  Planner 调用数保持各 1。WebSocket RED/GREEN 恢复 `data.workspace`，其中仍是完整权威投影。
- 当前 API/Phase 14 回归 `21 passed`、PostgreSQL Service 集成 `1 passed`；进入整改后的双重复审，真实模型费用 `0.000000 CNY`。

# 2026-07-18 Phase 16 Task 7 D-156 REVIEW REMEDIATION

- 最终质量复审发现自动 escalation 可在 Service 预读后、Coordinator 调用前写入，旧 Coordinator 会把
  它作为 manual replay 返回。D-156 将最终 Store 观察收紧为只有 `OPERATOR_REQUESTED` 才能恢复。
- 新 RED/GREEN 覆盖自动/人工竞争；Task 7 API、Phase 14 API 与 Coordinator 聚合 `22 passed`。
- 完整 integration 的唯一 Kafka 失败是测试跨分区顺序假设，四条断言顺序的消息现使用同一 partition key；
  生产 EventStore、消费者和 Kafka 配置未修改，单项通过。下一步最终双重复审与全量重跑。

# 2026-07-18 Phase 16 Task 7 D-157 REVIEW REMEDIATION

- 最终规格复审发现认证关闭的畸形 JSON 会被 FastAPI 类型化参数抢先返回 `422`，以及 HTTP 返回完整
  Workspace。D-157 改为安全门禁后手动 Schema 校验，HTTP 只返回稳定事实 ID；完整投影仅保留给
  WebSocket `data.workspace`。
- API/WebSocket 聚合 `31 passed`、PostgreSQL Service 集成 `1 passed`；进入最终双重复审、全量 unit/
  integration 和提交前门禁。真实模型费用仍为 `0.000000 CNY`。

# 2026-07-18 Phase 16 Task 7 D-158 REVIEW REMEDIATION / VERIFY

- 质量/安全复审发现自动入口会为 pending manual escalation 继续模型协调，绕过当前人工 lease。D-158
  收紧为自动路径只读恢复或返回 pending；只有 `run_operator_requested` 在当前 lease/fencing 下可推进。
- RED/GREEN 定向 `3 passed`；完整 unit `1457 passed, 4 warnings`，完整 integration `182 passed, 7 deselected,
  5 warnings`。compileall、迁移 dry-run、D-001 至 D-158 审计、目标文件严格 UTF-8 和 `git diff --check`
  均通过。独立整改复审 PASS，下一步提交推送；真实模型费用保持 `0.000000 CNY`。
