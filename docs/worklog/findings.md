# LiveAgent 工作发现记录

## 2026-07-11 文档编码治理发现

- 乱码问题需要区分两类：终端显示乱码，以及文件内容已经被写坏。
- 之前的风险主要来自 PowerShell heredoc / 管道写入大段中文，终端编码和文件编码不一致时容易把乱码写回文件。
- 当前项目重点留迹文档位于 `docs/project_guidance/`，过程记录位于 `docs/worklog/`。
- `docs/worklog/` 之前被 `.gitignore` 忽略，不利于后续项目迭代留迹回放。
- 本阶段将 `docs/worklog/` 改为可追踪目录，但不记录真实密钥、token、`.env` 内容或本机私密路径。

## 当前治理结论

- 优先使用 `apply_patch` 修改中文文档。
- 不再使用 PowerShell heredoc / 管道写入大段中文。
- 不把终端中已经乱码的内容复制回 Markdown 文件。
- 已新增 `scripts/check_doc_encoding.py` 作为只读扫描工具。
- 后续阶段收尾时应同时运行编码扫描和 `git diff --check`。

## 后续观察点

- 如果扫描脚本出现高置信 mojibake 命中，应先人工确认上下文，再决定从 git 历史恢复还是重写。
- 如果 VS Code 显示正常但终端显示异常，优先调整终端编码，不要修改文件内容。
- 如果某个历史文档已经无法恢复，应按当前项目事实重写摘要，不做盲目转码。

## 2026-07-11 Agent 架构评估发现

- 四份 study 文档的核心要求一致：不要做“大而全 Agent + 长 Prompt + 状态塞上下文”，而应做薄主控、专才隔离、原子 Skill、状态外置、Hook/Gate/Eval 前置。
- 当前项目不是纯 Workflow。播中 `OnLiveHarnessAgentGraph` 已具备 Context -> Reason -> Tool Policy -> Interrupt -> Tool Execution -> Observation -> Replan -> Audit 的单体 Agent Harness 链路。
- 当前项目也不是成熟多 Agent。代码中没有真实 Orchestrator、Dispatcher、Specialist Agent、Handoff 协议或 Agent Registry；现阶段主要是一个 planner 在一条 LangGraph state 内循环。
- `src/skills/` 当前更像业务能力模块目录，不是真正 Skill Runtime。`ToolRegistry` 是工具白名单和风险元数据，不是可动态发现、可版本化、可灰度、可沙箱执行、可评估的 SkillRegistry。
- 项目已有技术深度集中在 Harness 工程：工具白名单、生命周期门禁、上下文预算、人审 interrupt/resume、审计、Replay、规则评分和 Web 可观测。
- 距离淘宝主播 Agent 文档里的高含金量形态，核心差距是 DAG PlanEngine、动态 SubAgent 编排、Skill Runtime、统一真实执行 Adapter、Golden Dataset 和评估反哺闭环。
- 后续不宜继续单纯堆播前/播中/播后业务能力；更有价值的方向是把项目升级为“面向高风险直播业务的可控 Agent Runtime”。

## 2026-07-11 Agent Runtime 架构讨论发现

- “两条线平衡”不能机械理解为两条独立 backlog 各占 50%。单人业余开发更适合以架构主轴推进，并在每个阶段补齐直接相关的可靠性、审计、恢复和评估约束。
- Skill Runtime 的近期价值是统一、版本化、可执行和可评估的能力契约，不是插件热加载。没有外部 Skill 分发需求时建设完整插件平台属于过度设计。
- SkillManifest 与 ToolRegistry 不能长期双写安全元数据。Manifest 应成为唯一事实源，ToolRegistry 只保留兼容只读投影。
- PlanEngine 与 LangGraph 的图需要分层：LangGraph 承载 Harness 控制循环，业务 DAG 作为独立持久化数据由确定性 PlanEngine 执行。
- LangGraph checkpoint 适合恢复控制位置，不适合作为计划版本、节点执行和评估证据的唯一事实源，因此需要独立 PlanStore。
- 增量 Replan 的技术价值不在“再问一次 LLM”，而在确定性识别受影响节点、复用未变化结果并给出可回放证据。
- 不能预设 PlannerAgent、ReviewAgent 等数量。Orchestrator 和 PlanEngine 默认应是确定性组件，Review 首期继续使用 Hook/Skill。
- LiveOpsAgent 必须先与相同 Skill、相同输入和相同安全边界的固定子图比较；指标没有显著收益时应删除 Agent 试点。该结论已被三场景 Agent 化评估泛化，LiveOpsAgent 只是播中候选之一。
- 决策日志需要保留被淘汰方案和被后续修正的历史，否则上下文恢复后容易重新提出已经否决的架构方向。

## 2026-07-11 PlanEngine 失败语义讨论发现

- 失败类别和恢复动作必须分层。Skill/Adapter 只描述失败事实，FailurePolicy 结合 Manifest、幂等性和副作用状态做决策，PlanEngine 执行决策。
- 不能让 LLMClient、Adapter 和 PlanEngine 同时重试，否则实际请求次数会形成乘积，审计记录也无法解释真实成本。
- 自动重试资格不能只看 HTTP 状态码：只读操作、有可靠幂等保证的写操作和副作用未知的写操作必须采用不同规则。
- `RETRY_WAIT` 必须持久化 `next_retry_at` 并释放 Worker；线程内 `sleep()` 无法跨重启恢复，也会浪费有界并发槽位。
- Replan 不是通用异常处理。只有确定性策略确认原计划假设失效或存在替代能力时，才允许 LLM 生成替代子图。
- 执行前审批与执行后对账是不同的人工作业。将副作用未知塞进 `WAITING_APPROVAL` 会让操作员误以为仍在决定是否执行。
- 紧急 DAG 失败后应按影响范围恢复：商品级风险不应冻结所有无关任务，但无法判断影响范围时必须按全局风险 fail-closed。

## 2026-07-11 PlanStore 与 Checkpoint 一致性发现

- PlanStore 和官方 PostgresSaver 即使共用 PostgreSQL，也不能假设能够共享同一事务；跨连接“伪原子”会制造比显式最终一致性更难排查的故障。
- 固定写入顺序比尝试修改官方 checkpoint 表更可靠：业务事实先提交，checkpoint 只在 graph 正常返回后推进。
- PlanStore 领先 checkpoint 可以通过重放和结果复用安全恢复；反向领先意味着业务证据缺失，必须 fail-closed，不能自动补造成功记录。
- 仅有 lease 不能阻止旧 Worker 晚到写入；外部副作用任务需要单调 fencing token 保护终态更新。
- thread_id 不是业务幂等键。人工恢复必须同时校验 command_id、计划版本和节点预期状态。
- 启动、周期和按需对账必须复用同一幂等服务，不能形成三套不同的恢复规则。

## 2026-07-12 Phase 11A 兼容迁移讨论发现

- 影子校验与长期双读不是同一件事。旧 ToolRegistry 元数据可以在迁移测试中作为冻结对照，但切换后继续运行时双读会恢复双事实源并掩盖配置漂移。
- 首批 Handler 应形成可独立验收的业务闭环。选择播前四个能力可以同时覆盖查询、确定性生成、审计、幂等写和人工门禁，比跨播前播中的零散纵切更适合作为 Runtime 基线。
- 四个 Handler 全量原子切换会把通用执行器问题和高风险写入问题混在一起；逐个切换又会扩大开关矩阵。按“前三个读取/生成 + 单独 setup”分两批更符合单人项目成本。
- 影子执行只能用于可隔离的无副作用能力。`setup_live_session` 即使已有幂等保护，也不能用双执行证明等价，否则审计和外部副作用归属会变得含混。
- 单次调用失败后自动切换旧执行器并不是无成本降级。它会隐藏新链缺陷，写操作还可能重复产生副作用，因此回滚必须在批次路由层显式发生。
- 当前没有真实生产流量，不能用任意错误率阈值代替正确性。Manifest、Schema、生命周期、门禁、版本、审计和幂等属于零容忍不变量。
- ToolRegistry 作为只读兼容门面保留到 Phase 12 验收，可以控制迁移范围；兼容期内新增代码不能继续扩大旧 API 使用面。
- 远期阶段应采用 Just-in-Time 设计。Phase 11A 尚未实施时提前冻结 Phase 11B-14 细节，会把未验证假设变成后续约束。

## 2026-07-12 Phase 11A Design 代码对照评审发现

- ToolRegistry Schema、AgentToolExecutor dispatch 和播前 Graph 数据流不是天然一致的契约。迁移不能只做字段搬运，必须先区分“旧兼容外观”和“未来 Runtime 显式输入”。
- DAG、回放和输入指纹要求 Handler 不在内部偷偷重查上游数据。排品应接收商品快照，手卡应接收单商品快照，建播应接收计划快照。
- 旧元数据快照适合发现无意漂移，但不能把已确认的错误契约变成永久基线。因此 9 个工具严格一致，4 个核心 Skill 使用有白名单证据的 Schema 修正。
- “读取或生成能力”不等于“完全无副作用”。现有查询、排品和手卡服务都会写审计，运行时双算仍会污染正式证据。
- 新旧等价比较应属于测试基础设施：相同快照、两套服务栈、两个内存 AuditStore。生产 Router 不应携带 `SHADOW_COMPARE` 分支。
- hard-gate 的批准证据不能放在 LLM 可控 arguments。可信 ApprovalContext 应由 interrupt 恢复或内部兼容 Facade 构造，并在 Handler 前验证。
- 当前 Graph 是同步 Protocol，直接异步化会扩大 checkpoint、interrupt 和调用方回归面。兼容 Facade + 单一执行核心的同步桥接更适合 Phase 11A。
- jsonschema 作为可选依赖会导致环境相关 fail-open。Runtime 必须把它升级为正式依赖，并在 Catalog 启动和每次调用两个时机校验。
- AgentToolExecutor 虽然当前主要由单元测试覆盖，但仍是公开兼容入口；四个核心工具不能继续保留独立 dispatch，否则同一 skill_id 会形成双行为。

## 2026-07-12 Phase 11B-14 高层大纲发现

- 远期高层大纲的作用是保存方向、依赖和阶段门槛，使上下文压缩后仍能恢复路线；它不等于提前完成阶段 Design。
- 每个远期阶段固定五类信息已经足够：阶段目标、前置依赖、进入条件、退出条件和待决策项。继续增加接口、Schema 或任务拆分会越过 Just-in-Time 边界。
- “待决策项”是显式开放问题，不是推荐默认值。执行者不得因为它出现在路线图中就直接实施，必须在对应阶段重新读取代码与前序 Acceptance。
- 进入条件描述开始详细设计前必须具备的事实，退出条件描述阶段验收证据；二者不能混写成愿望清单。
- Phase 13 的目标不是交付 LiveOpsAgent，而是完成三场景确定性基线与受限 Agent 候选的可比实验。未达门槛时删除 Agent 仍然是成功退出。
- Phase 14 必须建立在 Phase 13 已确定的正式架构上，避免为最终未保留的 Agent 提前建设专用发布体系。

## 2026-07-12 三场景定位与 Agent 分层纠偏发现

- 项目业务范围是播前、播中、播后三场景，不应继续用“播中单体 Agent Harness”概括整个项目；该表述只能描述当前播中技术形态。
- 业务三场景不等于三个 Agent。是否引入 PlannerAgent、LiveOpsAgent 或 ReviewMemoryAgent，必须由职责复杂度、独立上下文、工具选择需求和评估收益共同决定。
- Tool、Skill、Agent、PlanEngine 和 Orchestrator 是不同技术层：Tool 执行动作，Skill 治理能力，Agent 做局部推理与工具选择，PlanEngine 做确定性 DAG 调度，Orchestrator 做确定性协调。
- Phase 13 原先过窄聚焦 LiveOpsAgent，容易让后续上下文把多 Agent 讨论限制在播中；应升级为三场景 Specialist Agent 候选评估。
- PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent 都只是候选。达不到严重安全违规为 0、收益超过基线且成本受控的门槛时，删除试点而保留确定性子图。
- 上下文恢复提示词应固定“先读路线图、决策日志、Phase 11A Design / Plan、worklog、git status”的顺序，防止压缩后重新回到过窄定位。

## 2026-07-12 Phase 11A Task 1-6 实施排障发现

- 专项测试全绿不等于任务完成。原 Task 1-4 测试遗漏了 TRUSTED_COMPAT 拒绝、9 个非核心元数据严格快照、完整 CatalogProduct 串联和真实 LivePlanDraft 串联。
- Runtime arguments 与可信 Context 必须严格分层；room_id、trace_id、幂等键和审批证据进入 Context，业务 arguments 只保存可持久化业务快照。
- Facade 对内使用 JSON 快照，不代表可以改变现有 Graph Protocol。Graph 边界仍必须返回 CatalogProduct、LivePlanDraft、ProductCard 和 GateResult。
- Runtime 失败返回空列表或空计划会把契约错误伪装成正常业务结果。Phase 11A 无隐式 fallback，失败必须显式暴露并由批次路由回滚。
- 原“Task 6”兼容提交实际属于冻结计划的 Task 7；真正 Task 6 是 HUMAN_INTERRUPT 审批证据从 LangGraph 恢复链传入 Runtime。
- 名为 Graph 集成测试的文件必须真正执行 build_pre_live_graph、invoke 和 Command(resume=...)；直接调用 Facade 不能证明 checkpoint/interrupt 协议兼容。
- 仅在业务服务中执行“先查审计、再写审计”不能提供并发幂等；相同键必须由数据库部分唯一索引和 ON CONFLICT 原子返回原 audit_id。
- 全局 Handler 注册可以作为装配入口，但 Executor 必须在构造时复制并钉住 Handler 映射，否则后创建 Facade 会覆盖已运行实例的 Repository 和 AuditStore。

## 2026-07-12 Phase 11A Task 7-9 验收发现

- `compatibility_enriched` 必须是可信执行上下文中的正式结构化字段，才能在 AgentToolExecutor 兼容补全后被序列化、审计和测试断言；仅写入摘要文案无法形成稳定证据。
- `TRUSTED_COMPAT` 需要内部工厂令牌与 room/trace 一致性校验。外部 arguments 即使字段形状相同，也不能获得可信审批来源或覆盖调用上下文。
- 等价测试的隔离不仅是两个 AuditStore；Fake Repository 也必须深复制嵌套商品快照，否则一侧对 tags 等可变成员的修改会污染另一侧，制造伪等价证据。
- 生产路由只允许 `LEGACY` 与 `SKILL_RUNTIME`。`SHADOW_COMPARE` 仅作为非法配置测试输入出现；PlanEngine 仅在兼容层“未来禁止复用”的说明中出现，没有对应实现。
- 全仓编码扫描退出 `1`，报告 `4 errors/58 warnings`。4 个 error 来自扫描脚本自身的 U+FFFD 检测示例，warnings 是仓库既有 BOM 或工作树混合换行；不得写成编码扫描通过。
- 已提交 Phase 11A 代码、测试和 Demo 的 Git canonical blob，与 6 个 Task 9 文档及 3 个冻结事实源需分层严格检查。这样既能验证交付对象的 UTF-8 规范，也不会掩盖工作树全仓扫描的历史告警。
- 提交历史必须区分被后续完整删除的提前实现与正式交付：`96a5adb` 已由 `94e2766` 删除，Task 7 的有效提交链从 `4f77403` 开始。
- Acceptance 引用的冻结 Design、Plan 和决策日志必须与报告一起进入 Git；仅在工作树存在无法形成可独立复跑、可审计的阶段事实源。

# 2026-07-11 Phase 7A 发现

- 生产级 Agent 项目不能只证明“能跑”，还要能回放、评分和复核，否则很难解释 Agent 决策是否可靠。
- Replay 不能只依赖 LangGraph checkpoint；checkpoint 适合恢复状态，业务评估还需要 Harness session、ToolCallAudit 和 DecisionTrace 作为证据。
- 规则评分必须先于 LLM Judge。安全、人审和工具合规不能交给 LLM 改判。
- 外部模型测试需要显式标记，默认测试使用 fake HTTP，避免网络、额度和模型波动污染工程验收。
- 运维页面也要按生产标准处理持久化数据，不能因为是内部页面就用 `innerHTML` 直接拼接 replay 字段。
- 评估任务的汇总和维度明细必须事务一致，否则会产生“任务完成但证据缺失”的排障陷阱。

---
