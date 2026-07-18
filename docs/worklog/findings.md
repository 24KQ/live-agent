# LiveAgent 工作发现记录

## 2026-07-18 Phase 16 Design Baseline

- 项目定位扩展为生命周期感知、人机协同、受控多 Agent 决策 Runtime；这不意味着
  PREPARE、LIVE、REVIEW 并发运行。Phase 16 只增加 LIVE 高冲突的串行双 Agent。
- 现有 `EvidenceBundle` 已提供 proposal eligibility、备品集合、弹幕噪声和节奏信号；
  可用作无需模型的三选二升级规则事实源。
- 现有 `SpecialistProfile.deadline_seconds` 是整数。Phase 16 保持共享协议，采用两个
  2 秒 Profile 和一个 5 秒 Coordinator，而不是扩大为小数秒公共契约。
- Phase 15 的 48 例 Golden 与 `INCONCLUSIVE` Acceptance 是历史事实，不得为 Phase 16
  直接改写。Phase 16 将使用独立 48 例 Manifest。
- 根 `python -m pytest -q` 当前会因三处 unit/integration 同名 `test_phase14_*` 模块产生
  import mismatch；文件重命名是行为无关的 Task 2 前置修复。
- Task 2 验证确认根因是 tests 下无 `__init__.py` 时 pytest 使用顶级 basename 缓存模块。
  三个 PostgreSQL 测试改为唯一 `*_postgres.py` 后，根 collect 从 3 errors 恢复为 0 errors。
  隔离 worktree 不复制用户未跟踪 `.env`；需要真实 PostgreSQL 的测试只在子进程临时读取
  主工作区已有凭据，不写入 worktree 或 Git。
- 默认路由继续 `DETERMINISTIC_ONLY`。本阶段技术通过或 smoke 证据不足都不能自动开启
  决策支持，更不能开启经营恢复自动执行。
- Windows 工作树的 CRLF 会改变 `Path.read_bytes()` 生成器摘要，而冻结 Manifest 记录 Git
  LF 内容摘要。D-141 通过 `.gitattributes` 强制 Python 源以 LF 检出，保留原始源码摘要的
  严格代码变更语义并避免平台投影伪造 Manifest 漂移。
- Task 2 全量证据：root collect `1537/1541`（3 external deselected、0 errors）；unit
  `1382 passed, 4 warnings`；integration `155 passed, 3 deselected, 5 warnings`。Kafka/
  FastAPI 的既有 deprecation warnings 未由本 Task 引入。
- Task 2 已以 `6ea5a57` 推送。Task 3 的公共协议必须保持单向：Analysis 只能承接
  EvidenceBundle，Planner 只能承接已验证的 Analysis，任何 Outcome 仍只可流向既有
  OperatorDecision/Compiler 边界，不能形成 Agent 互调或经营写路径。
- Task 3 双重复审的四项 Important 已确认为有效：两个 Profile digest 必须等于精确工厂
  identity；多 Agent Proposal 必须以 origin、Bundle digest 和全量 EvidenceRef 闭合 lineage；
  旧预算映射必须 fail-closed 而非抛 KeyError；Phase 13 历史源码闭包不能吸收新阶段模块。
- Task 3 的最终质量复审继续暴露两项易漏边界，均已回归覆盖：Profile Prompt 必须声明
  `AgentAction FINAL` 信封而非直接结果 JSON；Planner JSON Schema 必须在模型输出边界拒绝
  备品条件冲突、非法 option ID、首尾空白和 ASCII 控制字符。完整 Unicode category C 仍由
  领域 Pydantic 作为最终 fail-closed 门禁。
- 干净 PostgreSQL 验证揭示既有 `init_phase7b_production_hardening.sql` 把独立 SQL 与
  dollar-quoted PL/pgSQL 正文都错误地双写了字符串字面量；现已由 3 条 SQL 回归测试覆盖，
  官方 17 步迁移可从空库完整执行。
- 既有播后同步 unit/integration 测试依赖开发库中残留的固定 Trace；现改为由测试创建最小
  脱敏货盘与真实不可变 Trace，并在播后集成测试中显式禁用外部 Embedding。
- `test_semantic_retrieval_flow.py` 会真实调用 Embedding API，先前遗漏 `external` 标记；
  现在默认回归正确排除它，受控凭证环境仍可显式用 `-m external` 执行。
- Task 3 已以 `ad0e185` 推送。Task 4 的唯一持久化权威仍是 Decision Support Store：模型、
  Coordinator 和 HTTP 将在后续 Task 读取其不可变事实，不能自行构造 escalation/analysis/outcome。
- Task 4 审查确认：三类 Phase 16 事实必须由数据库 CAS trigger 推进 Workspace 版本，不能仅靠
  append-only ledger；自动升级从 Bundle 重建完整三选二信号，人工升级不携带触发码；Task 6 前
  `READY` Outcome 必须 fail-closed，防止未持久化的 Proposal digest 被误当作已验证父事实。
- Task 4 最终复审还确认：CAS trigger 必须在持有根行锁后再次检查 `LIVE`，否则 payload 校验与
  版本推进之间存在生命周期竞态；数据库直写的 `DEGRADED` Outcome 必须和领域模型一样带封闭
  failure code、非空事实摘要且没有 Proposal lineage，避免 append-only 审计链写入不可重载行。
- Task 5 最终复审以 D-147 修正 Task 4 的“人工升级不携带触发码”旧留痕：人工请求仍不能提交
  任何触发码，但服务器必须从同一 Bundle 重建至少一项真实冲突，否则 `ConflictAnalysis` 的非空
  finding 契约会使合法人工升级必然失败。自动路径继续要求至少两项。
- dispatch claim 的两秒预算从持久化创建时开始。claim 到期后切到 `REVIEW` 只能写入绑定既有
  claim、没有 Analysis/Proposal 的一次 `DEGRADED` 审计终态；不能补写 Analysis、READY、方案、
  命令或经营恢复。默认 5432 实例认证失败是本机环境差异，专项和全量数据库验证临时使用隔离
  `5434` 容器，不写入仓库配置。
- PostgreSQL 无法可靠复刻 Python 的全部 Unicode category C 与 canonical JSON 哈希规则；D-147
  把 Analysis 写入收束到同事务 Store 上下文，Store 的严格 Pydantic 重载仍为唯一 Schema/digest
  权威。该标记防止可信服务内的意外裸 SQL，不声称能隔离已失陷服务进程。

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
- 审计表的幂等唯一键只能定位首次事实，不能单独证明重放等价；冲突后必须比较 room、trace、工具、门禁、操作员和请求/结果 JSON 的完整语义。
- “冲突 INSERT 后再 SELECT”依赖每条语句的新快照，因此 Store 不能依赖数据库默认隔离级别，必须在连接上显式固定 `READ COMMITTED`。
- 测试专用 AuditStore 也必须模拟完整冲突语义，尤其不能让 Python 的 `True == 1` 掩盖 JSON 类型差异；集成测试使用 UUID trace 以隔离历史审计行。
- 人工审批字段完整不等于来源可信。`HUMAN_INTERRUPT` 必须从已校验响应与已写入的审批审计派生，普通 Pydantic 构造不能成为 hard-gate 放行凭据。
- Manifest 是唯一运行时契约时，额外字段拒绝不能只在四个已迁移 Skill 上实现；所有 13 个根 Schema 都必须显式 fail-closed，安全修正作为 D-035 的受控例外留痕。

## 2026-07-12 Phase 11B Design 发现

- “统一全部 Skill”不等于给每个确定性计算伪造外部 Adapter。统一的是 Runtime 的 deadline、失败事实、尝试证据和路由契约；只有读取或修改平台状态的能力跨越业务域 Port。
- 高保真 Fake 的价值来自可重放的状态和故障顺序，而不是模拟 HTTP 外观。实例级状态与版本化 Fixture 能覆盖建播重放、售罄、价格版本冲突、限流和副作用未知，且不会污染测试。
- deadline 只有在 Adapter 原生 async、支持协作取消时才有可靠语义。线程池超时不能证明外部写操作已经停止，发送后无法确认的结果必须保留为 `SIDE_EFFECT_UNKNOWN`。
- 既有 `tool_call_audit` 的幂等性与外部调用意图不是同一类事实。独立 Attempt Store 先记录意图、后闭合终态，才能为重复调用、崩溃恢复和后续人工对账提供证据。
- `FailureFact` 只描述事实，不能承载自动重试或 Replan 决定；否则会提前绕过 Phase 12 的集中 FailurePolicy。
- 不可达的 `switch_product` dispatch 说明历史代码不能因出现在文档或 Reducer 中就被当作正式 Skill。Catalog 是否注册、是否有调用入口和是否受测试约束才是当前迁移范围的依据。

## 2026-07-12 Phase 11B Task 3 发现

- `UNKNOWN_AFTER_SEND` 不是“没有结果”的成功分支。对于改价、售罄等写操作，Fake 必须保留已发生的状态变更，同时返回 `SIDE_EFFECT_UNKNOWN`，使上层在后续阶段进入对账而非自动重放。
- 故障脚本的调用序号属于单次 Adapter 调用事实。前置检查和写后未知处理必须复用同一条已匹配规则，否则同一次调用会错误消耗两次序号并改变副作用语义。

## 2026-07-12 Phase 11B Task 4 发现

- 对同一幂等 Operation 的重放，已持久化成功、确定失败或副作用未知事实必须优先于调用时已经到期的 deadline；否则一次安全重放会被错误改写为新的超时结论。
- 首次调用若在发送前到期，仍需为带幂等键的请求写入并闭合“未发送”终态。这样后续重复请求只重放该事实，不会因继续尝试而改变外部副作用边界。
- 同步 Graph 只允许通过一个拒绝嵌套事件循环的桥接器调用 async Runtime。在线程池或嵌套 loop 中伪造同步成功会破坏 deadline 的可解释性。

## 2026-07-15 Phase 12B Task 3 发现

- Event Inbox 的并发幂等不能只依赖 Python 锁，也不能只做“先查再写”。PostgreSQL 实现以关系唯一约束作为最终权威，并按稳定顺序取得事件、occurrence 和传输坐标的事务级 advisory lock，才能同时闭合首次登记、精确 delivery 重放与不同摘要冲突。
- `FOR UPDATE SKIP LOCKED` 只负责 Worker claim；heartbeat 和终态提交仍必须在行锁内同时核对 state、lease owner、绝对过期时间与 fencing token。仅比较 token 会让租约已经过期但尚未被重领的 Worker 继续晚到提交。
- Phase 12B 给 `plan_runs` 和 `plan_versions` 增加 lineage 后，Phase 12A 的独立 Schema 测试仍应可运行。查询端通过 `to_jsonb(table_row)` 读取可选列并提供 `CARD_BATCH / 0 / INITIAL` 默认值，可以兼容尚未执行 Phase 12B 迁移的历史表，而不把未来迁移复制回 Phase 12A DDL。
- PostgreSQL 集成测试的 Event Inbox 是全局队列。测试必须只清理自己的专用 event ID 前缀，既避免前次失败留下的 VERIFIED 事件干扰 claim，又不能通过全表 TRUNCATE 破坏其他阶段的持久化证据。

## 2026-07-15 Phase 12B Task 4 发现

- 现有 `anchor.inventory` 已承载 Phase 3D 旧格式消息。新的 durable consumer group 若默认 `earliest`，会在第一条缺少 event ID/version/source 的历史消息上 fail-closed 并永久停住；因此生产默认是入站禁用且从 `latest` 开始，测试或受控回放才显式启用 `earliest`。
- “Store 先提交、offset 后提交”要求 delivery 身份跨重启稳定。occurrence ID 必须由 topic/partition/offset 派生，received_at 优先使用 Kafka record timestamp；若使用进程当前时间，同一 record 重投会被误判为 occurrence ID 改绑。
- 摘要冲突是已经可靠持久化的安全事实，不等于解析失败。冲突 occurrence 应提交 offset 并继续分区；非法 JSON、未知字段、权限自报、来源不可信或 Store 失败则不得提交。
- Event Inbox 的 claim 是生产全局队列，跨文件集成测试也必须隔离数据。Task 4 使用专用 event ID 前缀并按外键顺序删除自己的事实，不能通过修改生产 claim 过滤器掩盖测试污染。

## 2026-07-13 Phase 11B Task 5 实施前发现

- 当前 `recommend_backup_product` 只有 `room_id` 与 `sold_out_product_id` 输入，但 `LiveOperationsPort` 没有“查询备选商品”或“读取直播间商品状态”的方法；Handler 若直接读取旧 Graph State，会绕过已冻结的 Port 边界。
- 当前 `generate_on_live_prompt` 只有商品 ID，现有确定性领域函数需要售罄商品与可选备选商品的完整领域对象；在不改变 Skill 契约、也不增加可信读取 Port 的情况下，不能正确复用它。
- 这是 Design/Port 契约遗漏，不应通过伪造文案、隐藏旧服务读取或新增未经决策的第十四个 Skill 绕过。Task 5 必须先完成最小设计纠正后才能继续。
- 已确认的最小纠正是给 `LiveOperationsPort` 增加只读 `resolve_product_context`。它补平台状态读取边界，不产生副作用、不新增 Skill、不改变公开 Schema、不升级版本；Fake 与生产 Port 必须保持同一语义。
- 旧播前服务经 Product Port 适配后，仍必须使用原始 `trace_id` 写审计；若把 operation_id 当 trace 写入，会让 Runtime 结果正确但等价审计丢失 `query_products` 事件。
- 统一 Handler 工厂可以复用确定性领域函数，但兼容装配下仍要调用 `PreLiveBusinessFlowService` 写排品和手卡审计；否则 Runtime 路径会少于 legacy 的审计证据。

## 2026-07-13 Phase 11B Task 8 契约纠偏发现

- `set_product_price@1.0.0` 的 Manifest 缺少 `expected_version`，而 `ProductPricingPort.set_price` 已使用商品资源版本执行 CAS；调用方无法表达预期版本时，改价 CAS 契约在 Runtime 入口不可实现。
- 已选择把 `expected_version` 作为必填业务参数显式加入 Schema，并将 `set_product_price` 的单活版本升级到 `1.1.0`。这是公开输入契约变化，必须遵循 D-061，不能继续沿用 `1.0.0`。
- 已拒绝把商品资源版本隐藏在 `SkillExecutionContext`。资源版本是可持久化的业务并发前置条件，应参与 Schema 校验、审计、重放和输入摘要；Context 只承载 room、trace、deadline、route、幂等键和批准等控制字段。
- `AgentToolExecutor` 不扩展为可信批准入口：它只从 Catalog 冻结精确单活版本，并仅为 `set_product_price` 搬移 `idempotency_key`；`approval` 保持 `None`，因此 Runtime 路由下的改价调用保持 `pending`。
- Task 8 原计划伪代码错误调用了生产 `InMemoryAttemptStore.list_attempts()`，但真实 Store 没有该 API。测试应定义 `CountingAttemptStore`，覆写 `claim_or_replay` 并维护 `claims` 计数，再对所有前置拒绝路径断言 `claims == 0`；不得为了测试给生产 Store 新增列表接口。
- 真实 `SkillExecutor` 会在 Handler 执行和 `AttemptStore.claim_or_replay` 前完成版本、Schema、幂等键与审批校验，因此 `CountingAttemptStore.claims == 0` 可以直接证明这些前置路径没有创建 Attempt；当前 `AgentToolExecutor` 则确实硬编码 `1.0.0`，并在读取幂等键后把该控制字段继续留在业务 arguments 中。
- `FakeLiveCommercePlatform` 的调用序号属于内部故障脚本状态，没有公开调用计数 API。Operation 重放的一次 Port 调用证据也应由测试内计数 Fake 或记录型 Port 提供，不能为了断言给生产 Fake 增加无关观测接口。

## 2026-07-14 Phase 11B Task 8-10 验收发现

- 高风险价格字段只声明 `type: string` 不足以形成安全契约。`Infinity` 可穿过 Decimal 比较并被写入，`NaN` 可能在 Handler 后被误闭合为未知副作用；必须在 Attempt 前限制为非负普通十进制字符串。
- AgentToolExecutor 默认兼容 Port 拒绝改价是当前权限边界，不是漏接 Handler。该入口没有可信审批 API；为让默认装配“能改价”而放开 Port 会绕过 D-064。
- 等价比较器不能手工复制 Runtime 的 Attempt、AdapterRequest 和输出映射后称为 Legacy；两边一起写错时会产生虚假等价。最终只对可安全执行的真实生产 Legacy 建播入口做新旧比较，旧路径没有的平台失败语义明确改为 Runtime-only 测试。
- 旧 Legacy 本来没有 Fake Platform 和 Attempt Store。测试应保留这项迁移差异，只比较双方共同公开的业务事实，不能为了对象形状一致而补造不存在的旧基础设施。
- 六场景 Demo 测试不能只断言场景名称和错误分类；还必须逐场景锁定 Attempt 终态、副作用状态和平台变化，否则场景函数错接也可能通过。
- Implementation Plan 中的 PostgreSQL Attempt Store 集成测试文件名已过期。验收命令必须先用 `rg --files` 对照仓库事实，再把计划偏差写入 Acceptance，不能以“文件不存在”代替系统回归。
- `run_all.py phase11b-demo` 是人类可读统一入口，保留既有 `[INFO]` 包装日志；恰好六行机器可读 JSON 的契约属于直接 Demo 脚本。

## 2026-07-14 Phase 11B 用户验收结论

- 用户已明确接受 Phase 11B Acceptance，Skill Runtime 与统一平台执行契约可以作为 Phase 12A Design 的稳定前置基线。
- 阶段验收通过只解除 Phase 12A Design 的进入门，不等于授权实施 PlanEngine。必须先按 Just-in-Time 原则复核 D-009 至 D-034，并完成独立 Design 审核。

## 2026-07-14 Phase 12A Design 发现

- 冻结排品后的三张手卡天然是并行 Skill；仅保存三个节点虽可运行，但不足以证明依赖、输入指纹和恢复边界。因此首期需要持久化准备与汇总两个确定性控制节点，不为编排细节新增 Skill。
- 在固定手卡批次中强行让 LLM 决定 DAG 没有实际业务收益。保留 ProposalProvider Port 与候选格式，但 Phase 12A 用版本化固定 Provider，真实 LLM Provider 等到存在可评估业务分歧时再引入。
- 关系表负责 lease、fencing、版本和依赖查询，JSONB 负责快照与证据；把完整计划塞入单行 JSONB 或只依赖 Skill Attempt 都会丢失并发与调度事实。
- 首期只读手卡不自然触发审批，但 Command Ledger 不能因此延后：它是 D-033 的跨场景基础设施，必须用合成节点验证命令去重、旧版本拒绝和 fail-closed TTL。

## 2026-07-14 Phase 12A Design 审核结论

- 用户已接受 Phase 12A Design。后续实现计划必须保持 Graph 手卡节点局部接入，禁止把 PlanEngine 塞入现有 `RoutedPreLiveBusinessService.generate_cards()`，否则会丢失 PlanRun、版本和 checkpoint 一致性证据。
- Phase 12A 实现计划必须把 `plan_engine_card_execution_route` 与既有 `skill_route_phase11b_batch1` 分离；后者同时控制查询和排品，不能承担“只接管手卡批次”的路由语义。

## 2026-07-14 Agent Runtime 全程计划持久化发现

- 长周期连续实施不能只依赖不断增长的 task_plan/progress；必须有一份短小实时游标，明确当前 Task、子步骤、最近验证和下一条命令。
- Phase 12A 原 Task 6 只描述 checkpoint 领先时冻结，缺少权威原因、signature 和恢复历史；仅进程内错误不能支持重启后命令 fail-closed，因此选择扩展 plan_runs，而不是新增第七张事故表。
- TRUSTED_COMPAT 已没有不可替代的调用场景。真实播前 interrupt 能生成 HUMAN_INTERRUPT，继续把 confirmed_setup 升级为权限只会把迁移兼容延伸到新 Runtime。
- ToolRegistry 当前没有独立元数据，但生产调用仍较多。立即删除会污染 Phase 12A 验收，长期保留又会维持两套公共概念；因此 Phase 12B 先迁到 SkillPolicyView，Phase 14 再删除 Facade。
- Phase 12B 的技术价值不只是“收到售罄后重跑”：Event Inbox、可信 provenance、局部冻结、child plan、CAS、严格对账和不可变 Replan 必须形成一条可回放证据链。
- 播中 Harness 不应成为可信库存写的第二入口。PlanEngine 处理安全事实，Agent 只消费证据并决定如何建议，才能公平评估 Agent 的额外价值。
- ReviewMemoryAgent 若直接调用旧 Replay/Memory Service，会绕过 Skill Runtime。增加最小播后 Skill 集是三场景公平评估的前置，不代表为了数量扩充 Skill。
- Phase 13 的完成定义允许保留 0 个 Agent。候选未跑满正式样本、严重违规非零或收益/成本不达标时，删除生产接入比保留“项目亮点”更符合路线目标。
- 3 元人民币是真实模型硬预算，不是事后统计。预算不足的候选只能 INCONCLUSIVE，不能缩小样本后继续使用百分点门槛。
- Phase 14 需要把新 Runtime 切成默认路径，否则前面阶段只会停留在显式 Demo；但同次 fallback 仍被禁止，Legacy 只作为启动期回滚。
- 本轮存在用户未提交的旧恢复提示词、历史路线和 Phase 11A 文档。新建连续恢复入口比覆盖旧文件更安全，也能保证本次提交边界可验证。
- 单活 Skill 的公开 Schema 不能早于 Handler 切换。若 Task 1 先发布 `handle_sold_out_event@2.0.0`、Task 6 才迁移 Handler，中间提交会让 Catalog 与执行行为错位；因此版本、Schema、授权和 Handler 必须在同一 Task 原子切换。
- ToolRegistry 退役不是 PreemptionCoordinator 的附带步骤。当前 `src/` 有 Planner、Policy、Hook、多个 Flow 和两个 Executor 消费者，必须使用独立迁移 Task 和明确回归清单，Phase 14 才能只删除 Facade。
- 默认路由不能在 Release 证据产生前切换。正确顺序是显式新路由 Release PASS、提交默认值晋升、在新提交上再次 Release；第二次失败使用新回滚提交，不能改写历史。
- 3 元模型费用门需要持久化预算作用域和并发预留/结算，否则多个 Worker 可同时越过进程内检查。Phase 13 与本轮 Phase 14 首次 Release 共用同一预算余额。
- Phase 14 不能“聚合 Phase 12B Golden Cases”却没有来源文件。首版补充 24 个确定性 Runtime core case，并明确复用 Phase 13 已冻结的 240 个 case，总数为 264。

## 2026-07-15 Phase 12A 验收发现

- 无外部依赖 Demo 仍应走真实 PlanWorker、FailurePolicy、PlanStore、Reconciliation 和 Command Ledger；只替换单次 Skill 结果，才能证明重试和恢复属于 PlanEngine，而不是脚本手写状态机。
- PlanStore 领先恢复场景必须同时断言重启后 Skill 调用数为 0 和结果完整复用；只比较最终 `SUCCEEDED` 无法排除重复外部副作用。
- checkpoint 领先事故清除不能自动解冻业务计划。清除 reconciliation 门禁和恢复业务状态是两个不同命令边界，否则人工对账可能意外绕过 fail-closed。
- 官方 Saver 边界可以通过公开 `get_tuple()` 完成引用核对，不需要读取或修改 checkpoint 内部表，也不能用跨连接伪事务掩盖有序写入协议。
- Windows 工作区中，对既有 CRLF 文件应用 LF 补丁会产生混合换行。目标文件必须做严格字节检查；本次 `run_all.py` 在提交前统一为 UTF-8 无 BOM/LF。
- Phase 12A 的真实 PostgreSQL/PostgresSaver 聚合 `14 passed`，全量回归 `906 passed`。这些证据满足 D-072，允许按连续实施授权进入 Phase 12B。

## 2026-07-15 Phase 12B Task 1 发现

- 冻结 Pydantic 模型仍可通过 `model_copy(update=...)` 生成携带旧 PrivateAttr 的新对象。可信事件授权不能只保存布尔标记，必须把私有验证身份绑定到 event、provenance、digest 和 observed version 四元组。
- Pydantic 嵌套模型重验证不会转发首次 `model_validate(..., context=...)` 的工厂 context。合法证据复用应验证私有身份指纹仍与公开字段一致；重绑定副本则因指纹失配 fail-closed。
- 只把内部字典包装为 MappingProxy 还不足以称为启动冻结视图，外层对象也必须禁止重新绑定整个 mapping。`SkillPolicyView` 因此使用 frozen/slots 值对象并持有不可写投影。
- 事件 canonical JSON 不能依赖 `json.dumps` 的默认宽松转换；tuple、非字符串 key、NaN、Infinity 和非 JSON 类型必须在摘要前显式拒绝。
- Task 1 只发布授权要求字段。现有两个 hard-gate Skill 标记 `HUMAN_APPROVAL`，售罄 Skill 继续保持 `1.0.0 + NONE`，避免在 Task 6 Handler 原子切换前破坏运行契约。

## 2026-07-15 Phase 12B Task 2 发现

- “同摘要重复事件”和“同一传输 delivery 崩溃重放”不是同一种幂等：前者必须追加 `DUPLICATE` occurrence，后者必须返回原 occurrence，避免数据库已提交但 offset 未提交时制造第二条投递事实。
- 摘要冲突是对事件身份的安全否定，不能受普通业务状态机终态限制。Store 保留首次 payload，但可从任意当前状态收敛到 `CONFLICT`、清除 lease，并让旧 Worker 的状态/token 校验失败。
- 内存 Store 也需要事务式构造顺序。先占用传输坐标、后构造 Pydantic 快照会在验证失败时留下部分状态；正确顺序是先构造全部新快照，最后在锁内一次性发布映射。
- lease 到期本身就必须拒绝 Worker 晚到提交，不能等到另一个 Worker 重领后才依靠新 fencing token 拒绝；重领则进一步递增 token，形成双重保护。
- `EventStore` Protocol 必须声明查询、heartbeat、Inbox/Application 转移的完整契约，否则 PostgreSQL 实现可能在类型层漏掉恢复能力，即使内存实现已经存在这些方法。
- Store 的审计时间必须单调。墙钟回拨时 heartbeat 可以保持现有 lease，但不能把 `updated_at` 写回更早时刻；所有记录更新取旧值与新时刻的最大值。
- EventApplication 的 ImpactAnalysis、emergency plan ID 和 applied version 是关联事实，不是可变缓存。字段一旦写入只能重复相同值，后续状态转移不得覆盖，否则恢复查询会失去原处理证据。

## 2026-07-15 Phase 12B Task 7 发现

- 跨 PlanRun claim 不能先锁 PlanNode 再锁 PlanRun；freeze 与既有 claim 使用 PlanRun -> NodeRun -> PlanNode。全局入口必须先按权威事实排序，再以相同锁序逐项 `SKIP LOCKED` 重验，避免锁序反转。
- priority 100 本身是一种调度权限。仅校验 Capability 与候选 Skill ID 一致仍可夹带额外 Skill，因此紧急输入在 `MaterializedPlan` 边界必须与固定 Provider 的完整五节点快照相等。
- `ready_at` 不只是初始列。重试到期、依赖满足、人工批准和对账成功后开放后继都必须写新的 READY 时刻，否则全局调度会永久遗漏合法节点。
- Phase 12B 代码与数据库迁移存在滚动发布窗口。紧急计划在缺少增量列时必须 fail-closed，普通 CARD_BATCH 则继续使用 Phase 12A 列集，不能因新 Store 版本提前中断旧路径。
- 事件验证控制节点与售罄写之间仍可能到达冲突 occurrence；Worker 必须在真正派发写 Skill 前再次读取 EventStore 并重建授权，不能复用控制节点输出作为权限。

## 2026-07-15 Phase 12B Task 8 发现

- PlanRun 初始输入不能充当所有版本输入；D-098 将 planning input 与循环签名放入不可变 PlanVersion，Worker 通过 node_id 定位所属版本后读取。
- PlanVersion 提交与 EventApplication 更新不能伪装成跨 Store 原子事务。恢复协议必须允许剩余 source event 子集补偿，并把已 APPLIED 且版本相同视为幂等完成。
- `reused_from_node_id` 可以形成多版本引用链。Coordinator 与 Worker 都要沿链找到最终成功且未 superseded 的 NodeRun，Store 还必须在 root 锁内再次复核，消除冻结并发造成的 TOCTOU。
- Replan source version 需要双重规则：新事件只接受当前版本；已出现在最新 PlanVersion `source_event_ids` 的旧版本事件只用于崩溃补偿，不能再次改变 DAG。
- schema readiness 必须覆盖当前 Task 新增列，不能只检查早期 Phase 12B 列，否则滚动发布会把未迁移数据库误判为可用。

## 2026-07-15 Phase 12B Task 9 发现

- 只把 Planner prompt 的工具列表换成 SkillPolicyView 还不够。结构化模型校验若继续读取默认全局白名单，模型仍可选择当前装配已移除的 Skill；Planner 必须用自身冻结快照二次校验决策。
- Catalog 和 SkillPolicyView 分别控制 Handler 契约与治理门禁。两者 ID 或精确版本漂移时不能拖到首次调用才抛异常，Executor 必须在启动装配阶段 fail-closed。
- 生产消费者迁移不要求提前删除 ToolRegistry Facade。保留旧测试和位置参数鸭子类型兼容，可以把 Phase 14 的删除动作与本阶段治理事实源切换解耦。
- BLOCK 与 hard-gate 的语义不能只靠风险等级近似。Hook、Legacy Executor 和不支持 pending 的确定性 Flow 都必须读取真实 `gate_decision`；BLOCK 需在 Reducer、Repository、Runtime 或审计副作用前终止。
- 旧 Registry 兼容参数不能作为运行时策略对象继续保存。正确做法是启动时核对其与 Catalog 投影一致，随后丢弃旧对象并持有新的冻结 SkillPolicyView。
- Event Inbox 的全局 claim 不能由调用方事后用 root 校验补救；必须先按 room 原子筛选，claim 后再复核活动 root，歧义时用当前 fencing 退回 VERIFIED。
- 对账跨 PlanStore/EventStore 不能伪装成原子事务。NodeRun 已闭合、Application 未闭合和 Application 已恢复、Inbox 未闭合都必须有显式补偿分支，且等待/失败不能返回 EvidenceRef。
- PlanEngine Harness 的“证据优先”不能只在 pre-tool hook 拦截写 Skill；应在 agent_reasoning 前直接绕过 Planner，并要求 `EvidenceRef=APPLIED + digest + final_suggestion` 三者一致。
- Dashboard 与 HTTP API 是启动路由的真实生产装配点；只在 Graph 工厂增加参数而不从 Settings 和请求入口传入，会形成不可用的伪路由。

## 2026-07-15 Phase 12B Task 10-11 发现

- EvidenceRef 不能只是“有 event_id 的建议”。只有 APPLIED Application、applied PlanVersion、child lineage、最终建议和摘要全部闭合时，Harness 才能跳过 Planner 消费该证据。
- Coordinator 的恢复边界至少包含 NodeRun 已闭合但 Application 未闭合、Application 已恢复但 Inbox 未闭合，以及 FAILED Application 未闭合 Inbox 三个窗口；跨 Store 顺序必须显式补偿。
- 业务闭环 Trace 需要字节稳定，但 Store 内部 PlanRun/NodeRun 使用随机 UUID。规范产物应保留稳定业务身份、版本、lineage 与摘要校验结论，不把随机内部 ID 当作跨运行比较字段。
- Demo 报告必须区分可证明的工程事实与不可证明的业务收益。当前证据可证明受控 Fixture 下的抢占、恢复、复用和审计，不能声明真实 GMV、转化率或库存收益。
- Phase 12B Acceptance 后必须停止在 Phase 13 Gate。已有 Phase 13 文档只提供讨论基线，不能因技术门禁通过而自动运行真实模型。

## 2026-07-15 Phase 13 Just-in-Time 审核发现

- “允许保留 0 个 Agent”必须写成“允许保留 0 个新增 Specialist Agent”；现有播中 Agent Harness 仍然存在，不能把候选数量误解为项目 Agent 总数。
- 多 Agent 扩展能力与候选去留应解耦。统一 Profile Registry、AgentTask/Result、EvidenceRef 和确定性路由可以预留多个 Specialist 并存，无需提前实现 A2A、自由 handoff 或共享 scratchpad。
- 旧同步 LLMClient 的隐藏重试无法形成逐请求预算和审计证据；正式评估必须使用原生 async 单次 AgentModelPort。
- 明显失败候选跑满 60 个正式 case 会浪费 2.40 元阶段预算。10 例 validation shard 的严重违规/数学可达性早停能产生可信 REJECTED，而不是把失败伪装成 INCONCLUSIVE。
- Phase 13 不能消费全部 3 元后让 Phase 14 首次 Release 无预算；2.40/0.60 元预留需要由持久 ledger 在并发边界强制执行。
- Planner 在正式评估中重新查询商品会破坏相同输入配对；商品、记忆和计划事实必须在 case 准备阶段冻结。
- ReviewMemory 只有完成“双 DecisionTrace -> candidate -> 确定性晋升 -> 下一次播前读取”才能形成受控业务闭环；Agent 仍不能直接写 active memory。

## 2026-07-15 Phase 13 Task 1 发现

- Task 携带 `profile_id/version` 不等于调用方拥有 Profile 选择权。Orchestrator 必须先按启动冻结的 `task_kind` 路由确定身份，再核对 Task 钉住值；同类 Profile 歧义必须在装配时失败。
- Pydantic `frozen=True` 只阻止字段赋值，不能自动封闭 `model_copy(update=...)`；继承 `dict` 的冻结包装也可被 `dict.__setitem__` 绕过。审计事实需要组合式不可变 Mapping 和禁止免校验复制。
- Profile 的显式路由值属于安全配置，不能只依赖类型注解。运行时必须校验枚举 key、二元字符串 tuple，并复制为内部规范快照。
- endpoint 配置只接受 ASCII DNS hostname；scheme、用户信息、端口、path、query 和 fragment 由后续 Adapter 固定装配，不能来自 Profile 自由文本。
- Skill 白名单是集合语义。规范排序后再计算 Profile 摘要，避免配置顺序变化制造虚假身份冲突。

## 2026-07-15 Phase 13 Task 2 发现

- 正式模型 Request 与 Profile 必须同时固定 endpoint 和 model；只验证“像 hostname”仍可能把 API key 发往错误主机。
- `asyncio.wait_for` 不能单独证明绝对 deadline：错误 Transport 可吞掉取消。Adapter 必须在响应返回后再次核对权威绝对时间。
- 外部 JSON 要同时限制字节数和嵌套深度，并捕获 `RecursionError`；思维链 key 检查使用迭代遍历，避免安全检查本身耗尽调用栈。
- usage 缺失可显式表示为未计价成功；usage 对象一旦存在就必须字段完整且总数一致，任何缺损归类为 `INVALID_RESPONSE`。
- 非阻断债务：默认 Adapter 后续应暴露连接池关闭入口；当前 1 MiB 限制在 httpx 完整缓冲后检查，固定 HTTPS host 限制了风险，但未来通用 endpoint 前需改为流式硬上限。

## 2026-07-15 Phase 13 Task 3 发现

- 3.00/2.40/0.60 元和候选初始额度必须是持久 Ledger 事实，进程常量只用于首次建账；每次 reserve 都在同一 scope 行锁内读取持久限额。
- 候选提前拒绝后，只有“初始额度减已结算费用”进入共享池；候选状态转为 RELEASED 后不得再预留，且共享池要扣除其他 ACTIVE 候选已借额度。
- 候选借用共享额度后再被释放时，超出自身额度的已结算费用是共享池负债；释放贡献必须按净额求和，不能逐候选截断为 0。
- usage 缺失按完整 reservation 保守结算；未发送请求才可 release。进程重启通过 `list_pending_reservations()` 扫描恢复，不能依赖旧内存中的 request ID。
- Python Decimal 必须无损落入 `NUMERIC(12,6)`；超精度、超范围、NaN 和 Infinity 在 Store 前转为稳定领域错误。
- 数据库约束也必须拒绝 NUMERIC NaN。ModelCall 通过 state、amount、usage 的复合外键绑定已 SETTLED reservation，不能伪造独立费用事实。

## 2026-07-15 Phase 13 Task 4 发现

- 预算预留必须对完整 ModelRequest 计价，并在发送前用冻结价格策略的 token counter 扣除输入 Token；把剩余总 Token 全部当输出上限会事后突破硬门。
- 同一 AgentTask 的执行身份使用稳定 task digest；重复执行在模型发送前 fail-closed，不能用随机 UUID 把崩溃重试变成第二笔付费请求。
- 已知实际费用即使高于预留也必须如实写入 Ledger，并立即停止 case；少记为预留值会把已消费预算错误释放回共享池。
- EvidenceRef 校验结果必须以冻结投影进入模型上下文，并在成功、失败和 fallback 结果中保留全部中间动作与证据链。
- Skill Handler 已开始后的外部取消必须先闭合 SIDE_EFFECT_UNKNOWN；若 Store 同时故障，保留原始取消并让持久 pending Attempt 进入恢复扫描，禁止重发。

## 2026-07-16 Phase 13 Task 5 发现

- 顶层 `success` 只能表达整体 case 结果，不能承载 LiveOps/Planner 的两项独立指标。正式 Attempt 必须冻结按 `metric_id` 区分的事实，Store 再从 selected baseline/Agent 重算配对指标。
- EvaluationRun 的 lease 不能在首次 claim 前可选。Attempt、selected、metric 和 decision 四类正式写入都必须持有未过期 claim，并使用数据库 `now()` 判断 PostgreSQL 租约。
- Retention decision 是 `manifest + candidate` 级事实，不只是单 Run 事实。保存结论时必须稳定锁住全部兄弟 Run，当前 Run 完成、其他 Run 取消，阻止第二结论和结论后的 selected 漂移。
- 只取消 decision 当时存在的兄弟 Run 仍不够；`create_run` 与 decision 必须先锁同一 Manifest 生命周期行，才能阻止结论提交后重新打开同候选 Run。
- 全局 claim 在多个正式 Manifest 或并行测试间会误领其他批次；Worker 应显式传入冻结 `manifest_id`，Store 仍保留无过滤模式供单一全局调度器使用。
- `EvaluationRun` 模型需要表达历史终态，但 `create_run` 入口只能接受 `RUNNING`，否则可制造没有 retention decision 的伪完成记录。
- `metrics_digest`、严重违规、共同硬门和完成 case 数都必须在同一事务从持久 selected/metric 事实重算。调用方布尔值或 40/20 计数不能作为权威证据。
- 外部 endpoint、价格、预算或基础设施造成的证据不足只能记录 `INCONCLUSIVE`；规则已证明失败且证据充分时才可记录 `REJECTED`。
- Pydantic 数值边界需要与 PostgreSQL `BIGINT`、`INTEGER` 和 `NUMERIC` 精确一致，否则内存测试可接受而数据库在运行时溢出。
- 候选专属严格 AND 阈值和 ReviewMemory macro-F1 计算仍属于 Task 7-11；Task 5 只冻结可独立重算的指标事实，不提前硬编码后续 evaluator。

## 2026-07-16 Phase 13 Task 6 发现

- Prompt 摘要本身不是模型输入证据。Profile 必须同时冻结真实 Prompt 正文和摘要，Runner 必须发送该正文；占位 system message 会让 Manifest 与真实请求脱节。
- Skill 白名单只冻结 ID 不足以保证评估可重复。Profile 需要一一对应的精确版本映射，Catalog 漂移必须在 Skill Port 前 fail-closed。
- 结果 Schema 只能证明字段形状，不能证明证据来源。完整 `evidence_refs` 与 ReviewMemory 嵌套 `evidence_ids` 都必须绑定本轮已由 Resolver 解析的 FINAL 动作证据。
- Task 6 无法提前冻结 Task 7-10 尚未实现的最终代码。`phase13-v2` 只作为数据集基线；Task 11 必须基于候选最终 Git commit 生成并注册新的正式 Manifest。
- 数据文件摘要不能只在 CI 中检查；case loader 每次消费都要校验 Manifest、原始字节摘要、严格 Schema 和 case 身份，防止测试后替换。
- Manifest 内部摘要自洽仍不能抵抗 case、Schema 和摘要一起替换。Loader 必须从调用方接收进程外冻结的预期摘要，并把校验后的 case 深冻结后返回。
- “Task 6 基线不得正式运行”不能只写在计划中。EvaluationManifest 与 Store 必须区分 DATASET_BASELINE/FORMAL_EVALUATION，并要求正式清单绑定最终 Git commit。
- 40 位十六进制字符串仍可由普通调用方伪造。正式 Manifest 注册还需要不可由普通构造器生成的绑定授权，Task 11 公开预检必须核对清洁源码、真实 Git HEAD 与重算 code digest。
- 注册时通过预检不代表后续执行进程仍处于同一 HEAD。每次 create_run 也必须携带当前进程授权；源码闭包还要拒绝目录 symlink，并与 Git tracked Python 集合精确相等，防止 ignored 外部代码进入 import 路径。
- 候选 Prompt 必须描述真实 AgentAction envelope、允许 Skill 与规范结果 Schema；只写业务目标会让真实模型输出与 Runner 协议错位，而 ScriptedModel 无法暴露该问题。
- 当前不可信执行边界是没有本地文件系统的远端模型，不是任意第三方 Python 插件。holdout label 只进入受审计 Evaluator；未来开放第三方候选代码时必须新增进程级权限隔离。

## 2026-07-16 Phase 13 Task 7 发现

- v2 LiveOps label 是场景属性，不是候选输出评分：HUMAN_ATTENTION 固定 action 失败，人工关注和弹幕固定 recovery 失败，导致任何候选最多 75%/50%。
- 简单改成“动作匹配即成功”也不成立，因为当前 PriorityLiveOpsPolicy 可命中全部四类循环模板，baseline 会达到 100%，相对提升门不可达。
- 评估数据必须允许候选在相同显式输入上有可解释改善，同时允许失败。D-110 采用版本化 case/label、可接受动作集合和整数早停目标，不覆盖 Task 6 v2 基线或放宽门槛。
- v3 EvidenceRef 必须带由 case_id 派生的稳定 `anchor_id`。这不是给测试放宽 Resolver，而是让冻结数据满足 Task 4 已存在的权威证据作用域契约。
- Store 的 selected-result 幂等写不等于评估流程可安全重放。LiveOps recorder 在写入前必须拒绝已 selected 的 case，恢复只能从 selected Attempt 重建 shard gate。
- `phase13-v2` 生成器原本递归收集整个 cases/labels 目录，导致独立 v3 资产反向改变 v2 的 Manifest。v2 现在仅绑定自身 `phase13` 子树，完整源码闭包仍独立覆盖所有评估代码。
- infrastructure Attempt 按 Task 5 约束不得进入 selected。LiveOps recorder 若先选择 baseline 再处理模型/预算失败，会留下半个正式 pair；因此该状态必须在任何 Store 写入前交给 Task 11 的重试或 INCONCLUSIVE 流程。

## 2026-07-16 Phase 13 Task 8 发现

- Catalog 新增可执行 Skill 时，历史“固定 13 个能力”的测试、ToolRegistry 投影与低信任工具掩码都必须显式更新；否则旧基线会把已审核的新增只读能力误判为回归。
- 兼容 Facade 不能注册缺少受控依赖的 Handler。`retrieve_anchor_memory` 只能由显式提供 `memory_port` 的统一 Runtime 装配，旧播前 Facade 保持原有 13 个可执行 Handler。
- Planner 只声明能力、DAG 和受限输入绑定；Skill 版本、风险、deadline、资源键和并发必须由 Catalog/Compiler 注入。输出可信编译证据不等于创建 PlanRun，避免在 Phase 13 扩大 Phase 12A PlanEngine 范围。
- 全量 integration 中的历史记忆回滚用例必须隔离 embedding 网络调用；该用例的断言对象是 PostgreSQL 事务回滚，固定本地 embedding 结果可防止外部模型等待掩盖数据库语义。

## 2026-07-16 Phase 13 Task 10 发现

- 单 case 只有一个 ReviewMemory 分类标签时，输出数组必须同样限制为一个 candidate；否则任意命中评分会奖励并列输出所有类别，无法代表 macro-F1。
- replay 的 dominant signal 与 review gold 归因同源，确定性 baseline 直接复制它会达到 40/40 并使相对提升门不可达；固定库存优先规则保留可审计且可重复的真实对照。
- ReviewMemory 的分类门必须从 APPLY/REJECT/REVIEW 混淆矩阵计算 macro-F1，不能把多数类逐例正确率冒充为 macro-F1。恢复时读取 evaluator-only 冻结标签与 selected output，不从 Agent 输出反推 gold。
- 货盘白名单是与 DecisionTrace 同级的冻结事实。候选商品、类目或标签越界即为严重违规，不能只依赖 PromotionPolicy 在后续阶段兜底。

## 2026-07-17 Phase 13 Task 11-12 发现

- D-110 的 LiveOps v3 case/label 必须进入正式 240-case 基线；保留 v2 审计资产并新增组合 `phase13-v3`，避免以淘汰标签得出正式结论。
- 正式模型失败前不得选择 baseline Attempt；基础设施失败必须持久为 `INCONCLUSIVE`，规则数学早停必须为 `REJECTED`，两者都不允许 fallback 或第二次请求。
- 本轮正式结论为 0 个新增 Specialist Profile 保留。统一 Profile/路由接口仍可支持未来受控多 Agent 扩展，但没有 A2A、动态 handoff 或生产默认切换。

## 2026-07-17 Phase 14 人机协同定位纠偏发现

- Phase 13 的自主候选未保留不等于项目没有 Agent 价值；它只说明“同权限下替代确定性基线自主执行”没有通过冻结门，不能代表 Agent 无法帮助运营理解复合事实。
- 可信售罄的冻结、CAS 和陈旧执行阻断属于客观保护，应由确定性控制面及时完成；备品、提示和时机属于经营决策，应由运营确认。两者混合会同时损失安全与产品价值。
- 三场景需要共享 session、证据、命令、Replay 和反馈身份，不能靠三个独立页面拼出闭环；首期播中优先做深，播前/播后提供可用的准备和反馈入口。
- 人工确认不是简单 approve/reject。受限结构化修改可记录运营对建议的真实贡献，又不会让自由文本绕过 Schema、权限和审计。
- 多 Agent 扩展应通过 Profile Registry 和确定性路由预留；本期只实现一个播中 Copilot，禁止用“三场景”推导出三个 Agent。
- sub-agent 可以提高独立分析和审查效率，但共享迁移、状态机、决策和提交必须串行；没有可验证进展、重复阻塞或越界时主模型必须停止并接管。

## 2026-07-17 Phase 14 Task 1 发现

- 默认关闭不能只放在 Graph 起点或条件路由。升级前 checkpoint 可能已经排队到 `execute_tool`，因此最终执行边界也必须消费启动冻结路由，并在 `DETERMINISTIC_ONLY` 下阻止包括只读工具在内的全部旧调用。
- 旧 HumanApproval state、普通字符串 ID 和恢复 payload 都不是可信 `OperatorDecision`。Task 5 受控编译链建立前，授权型 Skill 必须在 pre-hook 与最终执行节点双重 fail-closed。
- 执行器调用不能捕获任意 `TypeError` 后换签名重试；第一次调用可能已经产生副作用。兼容签名必须在装配阶段统一，运行时保持单次调用。
- 无 interrupt 的禁用、降级或完成会话必须通过单事务终态 INSERT 创建，不能先写 `pending_human` 再更新，否则故障窗口会留下虚假审批事实。
- `phase13-v2/v3` 是生成器持续校验的源码闭包数据基线，并非正式去留结论行；新增生产源码后必须由生成器更新，手工排除会使字节稳定和完整源码集合测试失败。

## 2026-07-17 Phase 14 Task 2 发现

- Workspace 根行锁必须在取得锁后读取 `clock_timestamp()`；事务起始时间会忽略锁等待，错误放过等待期间已经过期的 lease。
- 同一 Workspace 的五类事实共享幂等命名空间，不同 Workspace 相互隔离；幂等重放必须先于版本和 lease 业务门禁，但控制字段类型仍在重放前严格校验。
- 关系列与 JSONB 审计 payload 是同一事实的两种表示，数据库触发器必须校验身份、父关系、时间和账本双向一致，不能只依赖 Store。
- 同一 Proposal 只能形成一个 OperatorDecision；数据库和 Store 都必须验证预期版本与 lineage 最新性，防止原始 SQL 固化陈旧决定。
- Decision 与首次 ExecutionCommand 必须属于同一 operator/fencing epoch；重新取得 lease 不能为旧决定补造命令。
- append-only PostgreSQL 测试应使用独立 schema 并整体回收，随机 ID 只能避免冲突，不能避免测试数据永久污染。

## 2026-07-17 Phase 14 Task 3 发现

- 可信墙钟只能判断证据是否新鲜；写入 Bundle 的时间必须由已摘要绑定的证据 envelope 派生，否则同一引用在 TTL 内重放会产生不同事实。
- `EvidenceRef.digest` 需要绑定角色、完整 scope、版本、来源时间和 payload；只摘要 payload 会让同一引用被替换为另一份接收时间或作用域事实。
- Assembler 的窄只读 Context Resolver 防止正常调用伪造 Workspace/Incident；公开 Store 入口仍必须在事务中比对持久化 Incident 业务摘要和 Workspace scope，避免直接构造 Bundle 绕过 Assembler。
- 弹幕聚合摘要不能依赖控制词黑名单。固定主题码映射到确定性模板，才能阻止自由文本或换行注入进入后续 Copilot 上下文。
- PostgreSQL 测试使用持久化 memory_key 时必须每次生成唯一前缀；历史 upsert 不重置 status 会让重跑读取旧 suppressed 行，造成与业务无关的非确定性。
- receipt 只能在正常受控调用面代表 Assembler 产物；Task 3 通过 `EvidenceBundleAssemblyService` 隐藏 receipt/Store，WeakKeyDictionary 绑定签发时的 Bundle 身份，并用 D-121 明确任意同进程代码执行属于服务进程失陷而非插件安全边界。
- Task 3 最终证据为专项 `79 passed`、完整 unit `1244 passed`、integration `145 passed, 3 deselected`；Phase 13 Manifest 在源码闭包变化后连续两次生成哈希稳定。

## 2026-07-18 Phase 14 Task 5 发现

- OperatorDecision 输入必须和 Proposal 版本、操作员身份、有效 lease/fencing 以及幂等键分别校验；普通 Graph approval 或自由文本不能代替事实。
- MODIFY 只允许备品、主播提示、优先级和时机四个结构化字段；策略、EvidenceRef、工具调用和任意 JSON 均不可修改。空修改在模型边界直接拒绝。
- Proposal、OperatorDecision、ExecutionCommand 和最终 PlanCommand 必须分别追加；REJECT 不创建经营命令，APPROVE/MODIFY 的命令必须使用节点 `APPROVE`，不能误用只针对冻结 PlanRun 的 `RESUME`。
- Compiler 只构造不可变命令意图，不调用 SkillExecutor、Adapter 或真实平台；PlanStore/Workspace Store 在后续追加时再次执行 CAS、状态、lease、fencing 和幂等门禁。
- PostgreSQL Task 5 专项覆盖真实六角色证据、决定/命令重启读取、当前 lease 约束和重复投递；完整验证为 unit `1268 passed`、integration `147 passed, 3 deselected, 5 warnings`。
- Task 5 提交前只读审查线程未返回可验证报告，已由主模型按实际差异、Design/Plan 和全量测试接管复核；未发现需要新增决策或放宽安全边界的问题。提交 `c20d1ab` 已推送。

## 2026-07-18 Phase 14 Task 6 开始

- Task 6 固定沿用 Phase 12B 的可信售罄自动保护：冻结受影响计划、CAS 售罄标记、阻断陈旧执行和严格只读对账；备品、主播提示、优先级与恢复时机没有 `OperatorDecision` 不得执行。
- Task 6 不新增模型调用、真实平台或新的经营写 Skill；未知副作用必须保持 `WAITING_RECONCILIATION`，不得被工作台建议或人工恢复命令掩盖。
- Task 6 GREEN 覆盖事件/Workspace room 与 root 绑定、自动保护 Incident 幂等、APPLIED 重放、只读对账、`SIDE_EFFECT_UNKNOWN` 等待和原始 PlanCommand 拒绝；单元专项 `7 passed`，PostgreSQL 专项 `2 passed`。
- 复审发现并修复两个边界：Compiler 快照显式保存 `incident_id`，恢复入口重载 `CompiledOperatorDecision` 并从权威 Store 重新读取 Incident；不接受 model_construct 产生的伪造恢复事实。
- Task 6 最终验证：相关 unit `79 passed`、相关 integration `38 passed`；完整 unit `1275 passed, 4 warnings`、完整 integration `149 passed, 3 deselected, 5 warnings`；Manifest 因新增源码闭包重新生成后字节稳定，compileall、迁移 dry-run、diff 和 12 文件编码门禁通过。
- Task 6 已以 `43d182f feat: coordinate human guided sold out recovery` 独立提交并推送；用户既有脏文件和无关脚本未纳入。连续执行游标进入 Task 7 RED。

## 2026-07-18 Phase 14 Task 7 开始

- Task 7 只新增受操作员认证保护的 Workspace/Proposal/OperatorDecision API 和 `decision_support_workspace_update` WebSocket 更新；旧 `agent_harness_update` 协议必须保持兼容。
- API 不直接执行 Skill/Adapter/平台写入；Proposal 创建和决定提交必须复用 Workspace Store、Operator lease、Proposal 版本、幂等和 Task 5 Compiler。
- Task 7 GREEN 新增 `DecisionSupportService` 和三类受 Operator 鉴权的 Workspace/Proposal/Decision API；APPROVE/MODIFY 仍需 Task 6 Recovery Flow，未装配时返回 503，REJECT 可安全追加事实。
- WebSocket 使用 `decision_support_workspace_update`、按 session 单调 sequence 和 scope 定向广播；旧 `agent_harness_update` 与无 scope 的历史全局广播保持兼容。
- Task 7 验证为专项 `7 passed`、旧 API/WebSocket/Harness `14 passed`、完整 unit `1282 passed, 4 warnings`、integration `149 passed, 3 deselected, 5 warnings`，真实模型费用未增加。
- Task 7 已以 `eb28885 feat: expose decision support workspace api` 独立提交并推送；用户既有脏文件和无关脚本未纳入。连续执行游标进入 Task 8 RED。

## 2026-07-18 Phase 14 Task 8 开始

- Task 8 固定单一 `live_session_id` 跨 `PREPARE | LIVE | REVIEW` 三视图；运营主控拥有方案比较、结构化修改和决定入口，主播端只读取确认后的提示。
- UI 只调用 Task 7 API，不直接连接 Store、PlanEngine、Skill 或 Adapter；错误、DEGRADED、等待对账和 WebSocket 重连必须可见且不改变执行权限。

## 2026-07-18 Phase 14 Task 4 发现

- Copilot 的 Profile 身份不能只核对 ID、版本和 task kind；启动时必须重跑完整 Profile 校验并比较 `profile_digest`，否则绕过 Pydantic validator 的对象可能改变预算、Skill 或 Schema。
- `LiveDecisionProposal` 的每个 option 必须引用 Bundle 的完整有序 EvidenceRef 闭包；子集引用会让模型遗漏关键库存、计划或节奏事实。
- `proposal_eligible=false` 或 Bundle `valid_until` 已过期时，Copilot 必须在构造 AgentTask/调用 Runner 前返回 `DEGRADED`；模型失败和证据不可用不能用成功方案冒充。
- 备品 ID 必须与冻结库存快照中当前可用的备品精确匹配；风险码使用固定白名单并同时进入领域模型和 Runner JSON Schema。
- Phase 14 Copilot 使用独立 `PHASE14_COPILOT` 预算；内存与 PostgreSQL 都必须把其 reservation/settled exposure 与 Phase 13 共享池分离，且 snapshot 可用余额要扣除已结算费用。
- 新增 Phase 14 源码后，Phase 13 v2/v3 Manifest 的完整源码闭包必须由正式生成器重建；不能手工排除新模块，否则字节稳定和源码集合验收会失败。

# 2026-07-11 Phase 7A 发现

- 生产级 Agent 项目不能只证明“能跑”，还要能回放、评分和复核，否则很难解释 Agent 决策是否可靠。
- Replay 不能只依赖 LangGraph checkpoint；checkpoint 适合恢复状态，业务评估还需要 Harness session、ToolCallAudit 和 DecisionTrace 作为证据。
- 规则评分必须先于 LLM Judge。安全、人审和工具合规不能交给 LLM 改判。
- 外部模型测试需要显式标记，默认测试使用 fake HTTP，避免网络、额度和模型波动污染工程验收。
- 运维页面也要按生产标准处理持久化数据，不能因为是内部页面就用 `innerHTML` 直接拼接 replay 字段。
- 评估任务的汇总和维度明细必须事务一致，否则会产生“任务完成但证据缺失”的排障陷阱。

---

## 2026-07-18 Phase 14 Task 8 发现与整改

- 首个 RED 暴露前端稳定身份契约缺口：会话输入使用非冻结 ID，Proposal/Decision 资源后缀也未显式保留；已统一为 `live-session-id`、`/proposals` 和 `/decisions`。
- 只读审查发现决定按钮在 `WAITING_RECONCILIATION`、`DEGRADED` 和 `RECONNECTING` 时仍可点击；已增加 `decisionBlockReason`、Token 检查、连接状态和二次提交门禁，主播区继续只读。
- 只读审查发现旧 HTTP/WS 会话竞态、方案选择重置、Proposal 仅静态声明、Review 不显示候选/执行结果；已用请求序列号、目标 session 绑定、`selectedOptionId`、显式 Proposal 同步、`memory_candidates` 和命令结果渲染修复。
- 修复后 Task 8 专项 `6 passed`，相关 API/Store/WebSocket 聚合 `60 passed, 1 warning`，完整 unit `1288 passed, 4 warnings`，完整 integration `149 passed, 3 deselected, 5 warnings`；真实模型费用未增加。

## 2026-07-18 Phase 14 Task 9 开始

- Task 9 复用 Phase 13 的 Candidate Store、PromotionPolicy、DecisionTrace 和 `retrieve_anchor_memory@1.0.0`，不重新实现 Agent 记忆逻辑。
- 公开安全边界固定为：候选先 stage，规则资格检查双独立 DecisionTrace、作用域、货盘白名单、冲突和敏感字段；只有 `ELIGIBLE_AWAITING_OPERATOR` 才能等待人工确认，PromotionPolicy 是唯一 active memory 写入口。
- 当前状态为 RED，尚未修改业务代码或调用真实模型；下一步先以失败测试固定资格事实、人工确认、拒绝强制晋升、CAS/幂等、PostgreSQL 重启和 PREPARE 读取闭环。

## 2026-07-18 Phase 14 Task 9 审查整改

- 规格审查阻断了首版实现：旧 PromotionPolicy 仍可由 STAGED 直接写 active memory，Trace 摘要可由调用方伪造，确认重放未绑定完整身份，active 写入/CAS/账本存在恢复窗口。
- 整改后 PromotionPolicy 只接受 `ELIGIBLE_AWAITING_OPERATOR`，从持久化资格 Store 读取真实 Trace ID，经 `InMemoryDecisionTraceResolver`/`PostgresDecisionTraceResolver` 重载事实，并要求同 command 的 operator intent；调用方不能携带 Trace 字典替代事实。
- 资格事实先落库再完成 Candidate CAS；确认 intent 先落库，active memory 使用确定性 key，候选 CAS 或命令账本中断后可由同一 command 重放；同作用域已有相同结构化模板冲突时保持 `ELIGIBLE_AWAITING_OPERATOR`。
- 审查补测覆盖直接 Policy 绕过、未知 Trace、active 冲突和 active-write/CAS 失败恢复；Task 9 相关 unit `34 passed`、integration `4 passed`，全量 unit/integration 均无失败。

## 2026-07-18 Phase 14 Task 9 最终复核

- 未返回可验证报告的 Task 9 只读复审线程已由主模型停止并接管；其结论未作为验收依据。
- 主模型复核确认资格事实、人工 intent、可信 Trace、作用域锁、候选版本 CAS、active-write/CAS 恢复和命令重放均有对应测试；未发现需要新增决策或放宽安全边界的问题。
- 复跑证据为 Task 9 相关 unit `20 passed`、相关 PostgreSQL integration `2 passed`、完整 unit `1301 passed, 4 warnings`、完整 integration `150 passed, 3 deselected, 5 warnings`；真实模型费用未增加。

## 2026-07-18 Phase 14 Task 9 提交与 Task 10 RED

- Task 9 已以 `dbd5768 feat: confirm governed memory promotion` 独立提交并推送，远端与本地 HEAD 一致；受保护用户脏文件未纳入。
- Task 10 开始前真实模型费用保持 `0.042344` 元；本任务先固定复合事故数据集、规则门禁、配对评估和人工对照协议，不调用真实模型。

## 2026-07-18 Phase 14 Task 10 规格审查与整改

- 只读审查发现初版未覆盖过期证据、CAS/版本冲突和未知副作用；已在四组 case 的固定 slot 中加入三类显式布尔事实及 JSON 状态，并将其纳入脱敏数据和 Manifest schema。
- 已让 `Phase14Dataset` 重新校验 case 顺序、分组和 JSONL `dataset_digest`；ScriptedModel 与人工评估入口只接受经重验的 Dataset，不能用 `model_construct` 篡改样本复用旧 Manifest。
- `schema_digest` 现在覆盖 case、facts、assignment 和 record 的全部字段；`generator_digest` 绑定当前评估模块源码；交叉评估验证两个条件使用同一 case；门槛使用未四舍五入的精确比例。
- 严重违规按 baseline/copilot 每条结果计数；新增嵌套敏感字段拒绝和场景组 case ID 回归。只读审查线程没有 Critical，但报告的 5 个 Important 已全部整改并由主模型复跑验证。

## 2026-07-18 Phase 14 Task 10 最终验证

- Task 10 专项 `9 passed`；数据/Phase 13 回归 `20 passed`；完整 unit `1310 passed, 4 warnings`；完整 integration `150 passed, 3 deselected, 5 warnings`。
- Phase 13 v2/v3 Manifest 已由官方生成器按新增源码闭包重建；Task 10 目标文件严格 UTF-8、无 BOM/replacement/mixed newline/trailing whitespace，compileall 和 `git diff --check` 通过；真实模型费用未增加。

## 2026-07-18 Phase 14 Task 10 提交与 Task 11 RED

- Task 10 已以 `3dc7f40 test: add human decision support evaluation` 独立提交并推送，远端与本地 HEAD 一致；用户脏文件未纳入。
- Task 11 开始前真实模型累计费用保持 `0.042344` 元；预检前禁止发送真实请求，先固定缺少 endpoint、公开价格、usage、Prompt/Schema/数据集/代码哈希或预算时的 fail-closed 证据。

## 2026-07-18 Phase 14 Task 11 验证

- 新增 `formal_evaluation.py`：精确核对 `deepseek-v4-flash`、endpoint、零温度、十例上限、1.00 元预算、Manifest 和 Prompt/Schema/价格/数据/代码摘要；缺失任一事实返回 `INCONCLUSIVE` 且 `can_send=false`。
- Scripted rehearsal 重用 Task 10 固定数据，零模型调用、零费用并明确 `REAL_MODEL_SMOKE_NOT_RUN`；external smoke 测试默认跳过，不自动联网。
- 未知 usage 按单例 reservation 上限结算；fallback 或严重安全违规为 `FAIL`；预检结果含内部可信标记，伪造 `model_construct(can_send=True)` 无法打开发送门。
- Task 11 专项 `7 passed`，Task 10/11/Manifest 回归 `27 passed`，完整 unit `1317 passed, 4 warnings`，完整 integration `150 passed, 3 deselected, 5 warnings`，external smoke `1 skipped`；真实模型费用仍为 `0.042344` 元。

## 2026-07-18 Phase 14 Task 11 提交与 Task 12 RED

- Task 11 已以 `6a79359 feat: evaluate human decision support formally` 独立提交并推送，远端与本地 HEAD 一致；用户脏文件未纳入。
- Task 12 开始前真实模型累计费用保持 `0.042344` 元；Demo 必须使用确定性内存/已有受控门面，不连接淘宝、LLM、Kafka 或生产数据库。

## 2026-07-18 Phase 14 Task 12 Demo 与 Acceptance

- Demo 固定回放 `PREPARE -> LIVE -> REVIEW`，三视图共享 `live-session-p001-sold-out-v1`；可信售罄保护底层事实为 `APPLIED`，第二次调用复用 Incident 且不重复保护。
- 经营恢复只由真实 Compiler 生成结构化 `MODIFY` 决定和候选 PlanCommand；Demo 不提交命令，证明没有人工决定就没有经营恢复写入。
- 两条独立 DecisionTrace 经过资格规则、人工确认和同 command 幂等重放，active memory 只出现一条结构化记忆。
- Task 12 专项 `3 passed`，Demo CLI 返回 0，Scripted rehearsal gate 为真；真实模型未重新运行，Acceptance 严格记为 `INCONCLUSIVE`，费用仍为 `0.042344` 元。
- Phase 14 完成后实时状态固定为 `AWAITING_PHASE_15_GATE`；Phase 15 不自动开始。

## 2026-07-18 Phase 15 Stage A 设计持久化

- Phase 15 重新冻结为技术发布与 Copilot 晋升双轨：Technical `PASS | FAIL | BLOCKED`，Promotion `PROMOTE | KEEP_DISABLED | BLOCKED`；技术发布通过不自动开启 `DECISION_SUPPORT`。
- 活跃 Golden Dataset 固定为 48 例，拆分 `12 development / 24 validation / 12 holdout`；Phase 13 的 240 例保留为历史 Manifest 完整性资产，不进入当前 Release 执行。
- 真人证据固定为 3-5 名真实参与者、每人 8 次、24-40 条记录；缺少真人记录时只能为 `BLOCKED`，不得用 ScriptedModel 伪造 Promotion。
- Phase 15 真实模型预算固定为 0.60 元，最多十个 `deepseek-v4-flash` smoke；endpoint、价格、usage、Prompt、Schema、数据和代码摘要任一缺失时禁止发送。
- Technical PASS 需要 line 90%/branch 85%、规则优先门禁、迁移和敏感扫描，以及精确 commit 上真实 GitHub Actions PR/Release run evidence；缺失外部证据不得伪造通过。
- 当前状态已改为 `PHASE_15_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`。Stage A 只持久化文档，Stage B Task 1-12 必须等待用户单独授权，Phase 15 Acceptance 后停止。

## 2026-07-18 Phase 15 Task 1 RED

- 用户已授权 Phase 15 Stage B；Task 1 范围固定为迁移清单、统一入口、敏感扫描和 Phase 15 迁移/入口契约测试。
- 主模型负责生产代码、迁移整合、最终验证、提交和推送；迁移只读 explorer 已完成并关闭，确认 17 步依赖顺序和最小 Phase 15 DDL；入口/扫描 explorer 派发受线程配额限制未启动，主模型已复核实际差异并接管。
- 真实模型、真人采集和 GitHub Actions 仍未运行；用户既有脏文件不纳入本 Task。

## 2026-07-18 Phase 15 Task 1 GREEN

- 新增 Phase 13 Memory、Phase 14 Decision Support/Memory Feedback 和 Phase 15 Release 的必需迁移注册；Phase 15 基础 ReleaseRun 表只保存最小事实，后续 Store 在 Task 4 扩展。
- 统一入口新增 Phase 13/14/15 Demo；Phase 15 当前 Demo 明确输出 `BLOCKED`，不伪造技术发布或 Copilot 晋升。
- 敏感扫描器修复语法错误，`--tracked` 只扫描 Git 跟踪文件并默认严格；已知测试夹具、示例占位符和正则规则文本不作为生产敏感载荷。
- 专项与历史契约回归 `24 passed`；真实模型、数据库实际迁移、真人采集和托管 CI 尚未运行。

## 2026-07-18 Phase 15 Task 1 REVIEW/VERIFY

- 规格复核确认迁移依赖、入口命令、Phase 15 `BLOCKED` 占位语义和 tracked 扫描边界符合 Task 1；未发现 Critical/Important 阻断。
- 代码质量复核移除专项测试未使用导入；没有扩大 Release Store、Golden Runner、预算或真实模型范围。
- 最终证据：unit `1324 passed, 4 warnings`；integration `150 passed, 3 deselected, 5 warnings`；专项/历史迁移与 Demo `24 passed`；迁移 dry-run 17 步、入口 help、Phase 13/15 Demo、tracked sensitive scan、compileall 和 `git diff --check` 通过。

## 2026-07-18 Phase 15 Task 1 READY TO PUSH

- Task 1 目标文件严格编码检查 `15` 个文件通过，`git diff --check` 通过，用户已有脏文件未纳入。
- 当前提交范围仅包含迁移注册/DDL、统一入口、扫描器、README、开发依赖、Task 1 测试和阶段留痕；Phase 15 Golden/Store/真实模型仍未实现。

## 2026-07-18 Phase 15 Task 2 RED

- Task 1 已以 `2a88224 build: align phase 15 release entrypoints` 提交并推送，用户脏文件未纳入。
- Task 2 固定活跃数据为 48 例：24 Runtime 安全、16 播中复合事故、8 PREPARE/REVIEW；split 为 `12/24/12`，Phase 13 240 例只做历史 Manifest 完整性校验。
- 当前尚未生成 Phase 15 数据、Manifest 或模型输入；下一步先读取既有 Phase 13/14 数据资产，建立预期失败测试。

## 2026-07-18 Phase 15 Task 2 GREEN

- 新增 `GoldenCase`、`GoldenManifest`、`Phase15Dataset` 和确定性生成器；活跃数据固定为 48 例，split `12/24/12`，domain 分布为 Runtime 三类各 8、LIVE 16、PREPARE/REVIEW 各 4。
- Phase 14 16 例以新 case ID 只读复用；Phase 13 v3 与 Phase 14 Manifest 只保存来源摘要；模型输入与 labels 分离，holdout label 不进入 case。
- Manifest 绑定文件级 artifact digest、48 个 case digest、Schema/规则/生成器/源码摘要和 source Manifest digest；连续生成逐字节稳定。
- Task 2 专项当前 `5 passed`，真实模型和外部服务未调用。

## 2026-07-18 Phase 15 Task 2 REVIEW

- 规格审查确认 48 例分布、Phase 14 只读复用、labels/holdout 隔离、Manifest case/artifact/Schema/规则/源码/来源摘要和 supersedes 均闭合。
- 审查发现 Phase 13 旧生成器会把新增 Phase 15 Schema 纳入历史 artifact；已将其收窄为明确的 Phase 13 Schema 文件，v2/v3 仅刷新源码闭包摘要，case/label 内容未变化。
- 增加 labels split/ID 重载校验和 Phase 13 历史 240 例完整性断言；未发现 Critical/Important 阻断，真实模型仍未调用。

## 2026-07-18 Phase 15 Task 2 VERIFY

- Task 2 专项 `5 passed`；全量 unit `1329 passed, 4 warnings`；全量 integration `150 passed, 3 deselected, 5 warnings`，退出码均为 `0`。
- `compileall`、Phase 13/14 数据聚合 `25 passed` 和生成器连续运行字节稳定性通过；真实模型、数据库写入和外部服务均未调用。
- 当前仅剩目标文件严格编码、敏感扫描、`git diff --check`、暂存边界、独立提交和推送；用户已有脏文件继续排除。

## 2026-07-18 Phase 15 Task 2 COMMIT/PUSH

- Task 2 已以 `eb31dd9 feat: version phase 15 golden dataset` 提交并推送，`origin/main=eb31dd9`。
- 提交仅包含 48 例 Golden/labels、Schema、Manifest、生成器、Phase 13 历史闭包修复、Task 2 测试和阶段留痕；用户已有脏文件未纳入。

## 2026-07-18 Phase 15 Task 3 RED

- Task 3 进入 RED，目标是统一 Subject Runner 与规则门禁；真实模型、数据库写入和外部服务仍禁止调用。
- 下一步覆盖 Skill 版本/权限、Plan/Event 状态、EvidenceRef、CAS/fencing、幂等、敏感信息、预算和 no-fallback 严重违规。

## 2026-07-18 Phase 15 Task 3 GREEN/REVIEW/VERIFY

- 新增 `SubjectManifest`、`SubjectObservation`、`EvaluationCaseResult` 和五类域绑定 Runner；规则优先检查精确 Skill 版本/权限、输出 Schema、EvidenceRef、Plan/Event 状态、CAS/fencing、幂等、敏感输出、预算、调用次数和 no-fallback。
- 严重规则码直接产生 `FAIL`；Subject 异常不回显异常文本并归一化为 `BLOCKED`；敏感输出不会写入结果 artifact；Subject 身份和 case 结果均绑定 SHA-256 摘要。
- 规格与质量审查未发现 Critical/Important；历史 Phase 13 源码闭包已明确排除后续 `src/release_gates`，v2/v3 重新生成并保持历史测试通过。
- Task 3/Task 2 聚合 `15 passed`；全量 unit `1337 passed, 4 warnings`；全量 integration `150 passed, 3 deselected, 5 warnings`；真实模型和外部服务未调用。

## 2026-07-18 Phase 15 Task 3 COMMIT/PUSH

- Task 3 已以 `9f9d835 feat: enforce release subject rules` 提交并推送，`origin/main=9f9d835`。
- 提交仅包含 Subject 模型、规则 Runner、历史 Phase 13 闭包隔离、Task 3 测试和阶段留痕；用户已有脏文件未纳入。

## 2026-07-18 Phase 15 Task 4 RED

- Task 4 进入 RED，目标是 ReleaseRun/CaseResult 持久化、Technical/Promotion 双轨结论和 Phase 15 `0.60` 元预算隔离。
- 真实模型、真人采集、数据库写入和外部服务仍禁止调用；先复用现有 Store/Budget 的幂等与重启语义建立本地红灯。

## 2026-07-18 Phase 15 Task 4 GREEN/REVIEW/VERIFY

- 新增 ReleaseRun/ReleaseCaseResult、Technical/Promotion/Final 三轨状态模型，以及内存/PostgreSQL Release Store；重复事实幂等，身份/digest/case 冲突和缺 case fail-closed。
- 技术 PASS 不能覆盖 Promotion `BLOCKED`；最终状态严格映射为 `RELEASED_DECISION_SUPPORT_ENABLED`、`RELEASED_DECISION_SUPPORT_DISABLED` 或 `NOT_RELEASED`。
- Phase 15 预算调整为独立 `src/release_gates/budget.py` 与独立 PostgreSQL 表，固定 0.60 元；未修改 Phase 13/14 共享预算模块，保证历史 code digest 不被后续阶段污染。
- 内存专项 `4 passed`、PostgreSQL Release Store `1 passed`、独立预算 PostgreSQL `1 passed`、既有预算/Runner 聚合 `85 passed`；全量 unit `1341 passed, 4 warnings`，integration `152 passed, 3 deselected, 5 warnings`。
- 复审修复 PostgreSQL 决策快照重启读取错误；未发现 Critical/Important 阻断，真实模型未调用。

## 2026-07-18 Phase 15 Task 4 COMMIT/PUSH

- Task 4 已以 `fefd926 feat: persist dual release decisions` 提交并推送，`origin/main=fefd926`。
- Phase 15 预算独立模块/DDL、Release Store、双轨结论和 Task 4 测试已提交；共享 Phase 13 预算与历史 Manifest 未纳入。

## 2026-07-18 Phase 15 Task 5 RED

- Task 5 进入 RED，目标是服务端控制的真人交叉对照采集器；没有真实参与者时只产生 `BLOCKED`，不生成 Promotion 证据。
- 下一步覆盖 3-5 人、每人 8 次、四组场景、平衡 assignment、封闭动作、服务端耗时、PII/自由文本拒绝、重复提交幂等和真实 smoke digest 绑定。

## 2026-07-18 Phase 15 Task 5 GREEN/REVIEW

- 内存与 PostgreSQL 真人 Study Store 已转绿；专项 unit `5 passed`、真实 PostgreSQL study `2 passed`，Phase 15 Store/预算聚合 `9 passed`，compileall 通过。
- 规格审查修复：PostgreSQL 初始化缺失 `Path` 导入；assignment/response 恢复必须通过 session join 返回权威 participant digest；跨 study 读取、同 study Manifest/artifact 漂移均 fail-closed；participant limit 按 study advisory lock 串行化；响应增加 session/assignment 联合外键。
- 明确并发 `next_trial` 语义为未响应前的同一 trial 幂等重放，不伪造一次性领取或客户端 lease；真实模型、真人数据和 Promotion 证据仍未产生。

## 2026-07-18 Phase 15 Task 5 VERIFY

- 完整 unit `1348 passed, 4 warnings`；完整 integration `154 passed, 3 deselected, 5 warnings`；Task 5 API `2 passed`、PostgreSQL `2 passed`，真实模型/真人费用为 `0`。
- 迁移 dry-run、compileall、目标文件严格 UTF-8/LF/BOM/replacement/trailing whitespace、敏感扫描和 `git diff --check` 均通过；全仓编码扫描的既有 4 errors/51 warnings 未计入目标文件结果。
- Phase 13 源码闭包明确排除 Phase 15 `release_gates` 与 `gateway/api_server.py` 集成面，重新生成 v2/v3 Manifest 仅修正闭包摘要，不改变 case、label、prompt、Schema、价格或历史评估结论。
- 最终只读规格审查未在等待窗口返回，已由主模型按 Design/Plan、实际 diff 和完整测试接管；没有剩余 Critical/Important 阻断。

## 2026-07-18 Phase 15 Task 5 COMMIT/PUSH 与 Task 6 RED

- Task 5 已以 `d181cd1 feat: capture blinded operator studies` 推送，Task 6 进入 RED；用户已有脏文件继续排除。
- Task 6 只建立真实 Copilot smoke 的预检与预算边界；endpoint、价格、usage、Manifest/代码哈希、Schema、fallback、严重违规、重复请求或预算不满足时必须阻止模型发送或 Promotion。
- 当前不访问外部模型；预检前真实模型费用保持 `0`。

## 2026-07-18 Phase 15 Task 6 GREEN

- `copilot_smoke.py` 已实现可信预检、模型身份/endpoint/价格/Manifest/代码摘要核对、零温度、10 例上限、单 case request ID 和 Phase 15 独立预算 reservation/settlement。
- Task 6 unit `7 passed`；PostgreSQL 预算重启/耗尽集成 `1 passed`；Phase 15 相关聚合 unit `18 passed`、integration `5 passed`；真实模型费用仍为 `0`。
- fallback、Schema 无效、严重违规、模型明确失败和 unknown usage 均不能成为 Promotion 资格；unknown usage 按完整 reservation 保守结算并返回 `BLOCKED`，已知 usage 超过单 case reservation 时封顶结算并阻断。

## 2026-07-18 Phase 15 Task 6 VERIFY

- Task 6 专项 unit `8 passed`，PostgreSQL `1 passed`；完整 unit `1356 passed, 4 warnings`，integration `155 passed, 3 deselected, 5 warnings`，退出码均为 0。
- compileall、Phase 15 迁移 dry-run、目标文件严格 UTF-8/LF/BOM/replacement/trailing whitespace、敏感扫描和 `git diff --check` 已通过；未访问真实模型或 endpoint。
- 只读审查线程未在等待窗口返回，已停止并由主模型按 Design/Plan、实际 diff 和完整回归接管；无剩余 Critical/Important 阻断。

## 2026-07-18 Phase 15 Task 7 GREEN

- `report.py` 将 Model smoke/Human Study evidence 编译为严格 `BLOCKED | KEEP_DISABLED | PROMOTE`；不接受调用方手工传入互相矛盾的最终状态。
- Technical `FAIL/BLOCKED` 始终优先生成 `NOT_RELEASED`；Technical `PASS` 下只有 Promotion `PROMOTE` 才生成 enabled，其余为 disabled。
- Task 7 专项 `5 passed`，与 Smoke/Store/Human 相关聚合 `22 passed`；真实模型费用仍为 `0`。

## 2026-07-18 Phase 15 Task 7 VERIFY

- 完整 unit `1361 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`；退出码均为 0。
- Task 7 报告 JSON/Markdown 稳定摘要、Technical/Promotion/Final 一致性和严格 AND 门全部通过；未访问真实模型或 endpoint。
- 目标编译、编码、敏感信息和 diff 门禁待最后一轮执行，用户已有脏文件继续排除。

## 2026-07-18 Phase 15 Task 8 RED

- Task 7 已由 `984b3ff` 推送。现有 `scripts/run_phase15_release_demo.py` 仍是只输出占位 `BLOCKED` 的 Task 1 骨架，缺少统一 mode、Manifest/Subject 校验、Release Store 编排、覆盖率门禁和 GitHub Actions evidence 入口。
- Task 8 固定复用 `SubjectManifest`、`BoundedSubjectRunner`、`ReleaseStore`、`report.py` 和 Phase 15 Dataset；不重新实现技术/晋升状态机。
- 本地演练的确定性观察只作为技术 Release 证据；没有真实模型、真人或外部 Actions 事实时不得把 Promotion 写成 `PROMOTE`。

## 2026-07-18 Phase 15 Task 8 REVIEW 整改

- 只读审查发现 Release 默认路径未自动要求 PostgreSQL、覆盖率和 GitHub Actions 证据，且 PR/Nightly 错误包含 holdout；另发现 Manifest/Dataset 身份绑定、EvidenceRef 保留和证据敏感回显缺口。
- 已修复：Release 强制三类外部门禁并把 gate facts 纳入 artifact；PR/Nightly 固定 36 个非 holdout，Release 固定 48 个；Subject Manifest/Dataset 绑定冻结摘要；ReleaseCaseResult 保留 EvidenceRef；Actions 读取器严格验证身份/摘要并只输出白名单；预算/覆盖率拒绝 NaN/非有限数。
- 审查没有放宽任何安全、预算或 Promotion 门槛；真实模型、GitHub API 和生产数据库仍未调用。

## 2026-07-18 Phase 15 Task 8 COMMIT/PUSH 与 Task 9 RED

- Task 8 已以 `d2d4c89 build: add local phase 15 release gates` 推送；PR/Nightly/Release 本地门禁入口已可复跑。
- Task 9 当前尚无 `.github/workflows/agent-runtime-pr.yml`、`agent-runtime-nightly.yml` 和 `.github/workflows/agent-runtime-release.yml`；RED 将固定 Python 3.12、PostgreSQL 15、36/48 case split、secret/trigger/artifact retention 边界。

## 2026-07-18 Phase 15 Task 9 REVIEW 整改

- 只读审查发现 Release workflow 未生成 coverage、未传 DSN/evidence，Nightly/Release 缺少 Kafka readiness 和 PostgresSaver 显式入口，三层权限/触发器测试不完整；这些问题均已修复。
- Release 现在通过受保护 environment secrets 注入 evidence JSON 与身份字段，调用 `fetch_github_actions_evidence.py --require-evidence` 后再调用 Release CLI；缺失 evidence 仍稳定阻断。
- `phase15-release-*` tag ruleset 的创建权限属于 GitHub 仓库外部配置，当前只记录为待真实 Actions/仓库设置验收的外部门禁，不伪造代码内强制能力。

## 2026-07-18 Phase 15 Task 9 VERIFY

- workflow contract `3 passed`，完整 unit `1375 passed, 4 warnings`，integration `155 passed, 3 deselected, 5 warnings`；目标 YAML 编码、敏感扫描、迁移 dry-run 和 diff check 通过。
- 真实托管 Actions、protected environment secrets 和 tag ruleset 没有在本地伪造；它们是后续真实 Release 验收所需的外部证据。

## 2026-07-18 Phase 15 Task 10 RED/GREEN

- RED 确认 Facade 文件、生产旧符号和 Executor `registry` 参数仍存在。
- GREEN 删除 Facade，Executor 统一读取 `SkillPolicyView`，旧测试、Security Hook Fixture 和 Phase 3A Demo 均迁移；生产源码旧符号命中数为 0。
- Task 10 专项 `104 passed`，下一步执行独立规格/安全复审与完整 unit/integration。

## 2026-07-18 Phase 15 Task 10 REVIEW/VERIFY

- 独立只读审查返回 0 Critical、4 Important：售罄幂等键未从业务参数移除、Legacy 异常回显、README 仍引用旧 Facade，以及旧 Flow 的同进程 PolicyView 注入边界。
- 已修复前三项并补回归；所有声明 `requires_idempotency_key` 的非兼容 Runtime Skill 统一把幂等键放入 `SkillExecutionContext`，Legacy 错误改为固定脱敏摘要，README 改为 Catalog/SkillPolicyView。
- PolicyView 注入不构成 HTTP/插件信任边界；按 D-121，生产 Runtime 装配的 `AgentToolExecutor` 与 `SkillExecutor` 已校验 Catalog/PolicyView 完整一致，旧 Flow 的 test-only 门禁快照保留用于零副作用回归，不新增生产 bypass。
- Task 10 专项 `21 passed`；完整 unit `1372 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`；真实模型费用新增 `0`。

## 2026-07-18 Phase 15 Task 11 RED/GREEN

- RED：显式 Release/Verified Defaults 路由契约因缺少 `src.release_gates.routing` 收集失败。
- GREEN：新增三路不可变 `ReleaseRouteProfile`；Settings 默认保持 `LEGACY_DEFAULT`，显式 profile 使用 `SKILL_RUNTIME`/`PLAN_ENGINE`，Verified Defaults 需要 Technical `PASS`，仅 Promotion `PROMOTE` 开启 Decision Support。
- 新增配置 Schema 与路由状态机已由 D-133 留痕；专项 `5 passed`，未调用真实模型、GitHub Actions 或外部 Release。

## 2026-07-18 Phase 15 Task 11 VERIFY

- Task 11 专项 `18 passed`；完整 unit `1379 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`；目标源码/入口 compileall、生产路由 import、`git diff --check` 和 16 个目标文件严格 UTF-8/LF/BOM/replacement/trailing whitespace 检查通过。
- 全仓 compileall 仍被用户已有 `scripts/patch_run_all.py` 与 `scripts/tmp_gen_story.py` 的历史语法错误阻断；两文件未修改、未暂存、未纳入 Task 11，正式源码编译证据独立通过。
- 规格与质量审查未发现 Critical/Important；真实模型、GitHub Actions 和外部 Release 仍未调用/伪造，费用保持 `0` 新增。

## 2026-07-18 Phase 15 Task 11 COMMIT/PUSH 与 Task 12 RED

- Task 11 已以 `efe16c5 feat: promote verified runtime defaults` 独立提交并推送，远端与本地 HEAD 一致；用户脏文件和临时脚本未纳入。
- Task 12 开始：先验证现有三场景 Demo、48 例报告、双轨结论、路由 profile、模型/真人/Actions 外部证据和最终停止状态；不调用真实模型，不伪造外部 Release 证据。

## 2026-07-18 Phase 15 Task 12 VERIFY

- Task 12 Demo 已组合 Phase 14 三视图业务闭环与 Phase 15 两次本地 profile Release；冻结 Manifest 为 48 例，本地 PR 运行 36 个非 holdout case，两次技术 dry-run 均 `PASS`，Promotion `BLOCKED`，默认路由 `DETERMINISTIC_ONLY`。
- 业务证据：同一 `live-session-p001-sold-out-v1` 穿过 `PREPARE/LIVE/REVIEW`，售罄自动保护 `APPLIED`，人工决定 `MODIFY`，经营恢复命令未提交，记忆晋升与重放均 `APPLIED`。
- 验证证据：Task 12 专项 `3 passed`、Phase 14/15 聚合 `33 passed`、完整 unit `1382 passed, 4 warnings`、integration `155 passed, 3 deselected, 5 warnings`；迁移 dry-run、正式源码 compileall、PR/Nightly 本地门禁和 `git diff --check` 通过。
- Release 模式因 coverage、PostgreSQL 和托管 GitHub evidence 缺失明确 `BLOCKED`；未调用真实模型，Phase 15 新增费用 `0`，Acceptance 与 Final Acceptance 诚实记录为 `INCONCLUSIVE`。

## 2026-07-18 Phase 15 Task 12 COMMIT/PUSH 与阶段完成

- Task 12 已以 `c01a5da docs: accept agent runtime release` 提交并推送，远端与本地一致；用户脏文件未纳入。
- Phase 15/Final Acceptance 已固定为 `INCONCLUSIVE`，Promotion `BLOCKED`，默认 `DETERMINISTIC_ONLY`；状态为 `PHASE_15_COMPLETE_INCONCLUSIVE`，不自动进入新阶段。

## 2026-07-18 Phase 16 Task 5 时钟权威整改

- PostgreSQL Analyst dispatch claim 的创建与过期由数据库事务时钟权威判定；Coordinator 若以 Worker 本地墙钟计算 `lease_until - now`，慢时钟会把两秒窗口错误放大，并采纳已过期的 Analyst 响应。
- 新增真实 PostgreSQL 慢 Worker 时钟 RED：旧实现得到 `1 failed`，迟到响应被写为 Analysis。GREEN 后由 Store 计算权威剩余秒数，Coordinator 只接收不超过冻结两秒的预算，回归为 `1 passed`。
- D-147 已同步为 Store/数据库权威剩余时间；不改变 at-most-once dispatch、`REVIEW` 闭合例外、默认 `DETERMINISTIC_ONLY` 或真实模型预算。

## 2026-07-18 Phase 16 Task 5 VERIFY

- D-147 的 `REVIEW` 例外必须同时要求既有 dispatch claim、`DEGRADED`、无 Analysis lineage 和无 Proposal lineage；只检查 claim 会把 LIVE 内 Planner/Validator 失败错误扩大到播后视图。
- 新增内存和真实 PostgreSQL RED，各 `1 failed`；收紧 Store 与 CAS trigger 后分别转绿。Task 5 最终专项为 unit `25 passed`、PostgreSQL `20 passed`，全量为 unit `1420 passed, 4 warnings`、integration `172 passed, 7 deselected, 5 warnings`。

## 2026-07-18 Phase 16 Task 6 VERIFY

- D-148 至 D-151 将双 Agent 的总预算、单次 Planner dispatch、LIVE/REVIEW 恢复和迟到事实全部收束到同一不可重置的五秒窗口；Planner 只读取精确 EvidenceBundle 与已验证 ConflictAnalysis。
- D-152 关闭了两条经营恢复旁路：通用 Proposal 写入/API 拒绝 `MULTI_AGENT`，Coordinator 是唯一写入入口；多 Agent `APPROVE/MODIFY` 必须精确绑定同一 Proposal、Analysis、Escalation 摘要的 `READY` Outcome。全局 deadline 耗尽也稳定归类为 `COORDINATOR_TIMEOUT`。
- 最终证据：Task 6 聚合 `83 passed`、真实 PostgreSQL Task 6 套件 `29 passed`、direct-SQL coordinator-context 拒绝 `1 passed`、完整 unit `1440 passed, 4 warnings`、完整 integration `181 passed, 7 deselected, 5 warnings`。规格审查和质量/安全整改复审均为 PASS，真实模型费用 `0.000000 CNY`。

## 2026-07-18 Phase 16 Task 7 GREEN / REVIEW

- D-153 将人工升级 HTTP 收窄为 Bundle ID、Workspace CAS 和规范 header 幂等键；服务端重新加载权威 Bundle，并装配 operator lease/fencing，客户端不能传 Profile、trigger、scope、Bundle snapshot 或 fencing。
- 首轮只读审查发现认证关闭时的默认管理员 Critical；D-154 令新端点在该配置下 `503` fail-closed，不复用旧本地兼容路径。审查还发现 lease 错误要映射 `409`、WebSocket 必须广播完整 Store 投影；均已补 RED/GREEN。
- 当前专项 API/Service 与 Phase 14 回归 `21 passed`；隔离 PostgreSQL 上的 Service -> Coordinator -> READY -> Workspace projection 为 `1 passed`，真实模型费用 `0.000000 CNY`。

## 2026-07-18 Phase 16 Task 7 D-155/D-156 整改

- D-155 修复规范 key 的 response-loss replay：同 Bundle 既有人工 escalation 使用当前 Store 版本恢复，仍要求当前 lease，两个 Runner 不会第二次执行；WebSocket 保持 `data.workspace` 而其中内容为完整权威投影。
- D-156 修复 Service 预读和 Coordinator 最终观察之间的自动升级竞态。`run_operator_requested` 只可恢复既有 `OPERATOR_REQUESTED` 事实；最终看到自动 escalation 时抛出冲突，零额外 Runner 调用。
- 全量 integration 首次仅有 Kafka 用例失败，根因是 Producer 未指定 key，跨分区 poll 没有总序。测试夹具已固定四条序列消息的 partition key，不改生产 Kafka/EventStore 语义；单项回归通过。

## 2026-07-18 Phase 16 Task 7 D-157 整改

- FastAPI 类型化 body 会在端点内的认证配置门禁前返回 `422`。D-157 改为原始 Request 在 D-154/认证之后手动验证，认证关闭的有效与畸形 JSON 都返回 `503`，认证启用后的无效 JSON 保持脱敏 `422`。
- Service HTTP 结果只返回 `accepted`、规范 key 与可选事实 ID；完整 Workspace/Agent 事实只在写后读取并按 `data.workspace` 广播。API/WebSocket 聚合 `31 passed`、PostgreSQL Service 集成 `1 passed`。

## 2026-07-18 Phase 16 Task 7 D-158 整改与最终验证

- 质量/安全复审发现自动调用会推进已有 pending `OPERATOR_REQUESTED` escalation。人工升级的单信号资格不能被自动三选二入口在失去当前 lease 后续跑；D-158 令自动入口只读恢复或返回 pending 身份。
- 新 RED/GREEN 证明自动观察不会产生 Runner 调用、Analysis 或 Outcome。完整验证在隔离 PostgreSQL 上为 unit `1457 passed, 4 warnings`、integration `182 passed, 7 deselected, 5 warnings`；真实模型费用保持 `0.000000 CNY`。独立整改复审为 PASS。

## 2026-07-18 Phase 16 Task 8 GREEN / REVIEW

- D-159 让 Workspace 只投影可升级 Bundle 的六项白名单摘要，工作台不要求运营输入 ID，也不显示六角色原始证据正文。
- D-160 至 D-163 依次修复浏览器认证头缺口、cookie browser binding、异步旧会话、同浏览器重新认证撤销、lineage 错配和客户端伪 DEGRADED。票据为 60 秒一次性、会话与 HttpOnly/SameSite binding 限定；Token 不进入 URL，票据不授予任何写权限。
- 工作台展示 escalation route/trigger、Analysis 和 Outcome，`DEGRADED` 只来自服务端稳定失败码与事实摘要，读取/写入失败为不可执行 `UNAVAILABLE`/提交失败；当前 multi-Agent escalation 缺少同 lineage `READY` Outcome 时，运营决定不回退到无关 Proposal。Task 8 聚合 `44 passed, 1 warning`，完整 unit `1473 passed, 4 warnings`、integration `182 passed, 7 deselected, 5 warnings`；最终复审 PASS。

## 2026-07-18 Phase 16 Task 9 RED

- 评估必须运行真实 `HighConflictEscalationCoordinator` 与 `ScriptedAgentModel`，并通过 Store API 重建
  Workspace、Incident 和 EvidenceBundle；测试专用 factory 不得进入生产/评估运行时。
- 标签与预期评分只保留在冻结数据资产，绝不写入 `AgentTask.input_snapshot`。Bundle TTL 使用受控 UTC
  时钟；每例独立 Store，防止短 TTL 与 append-only 幂等事实相互污染。
- D-143 继续要求共享 Runner 对 Phase 16 fail-closed。Task 9 只能提供独立、显式的 Scripted 预算组合，
  不得借用 Phase 14 的账本或运行真实 smoke。

## 2026-07-18 Phase 16 Task 9 GREEN / REVIEW

- `ScriptedAgentModel` 的输出会被协议冻结为只读 Mapping；传入 `AgentResult` 前必须经 Pydantic 的
  JSON 序列化边界恢复普通容器，否则协调器会将结构正确的脚本结果错误归类为 `ANALYST_MODEL_ERROR`。
- PostgreSQL 的 lease/freshness 使用事务时钟，评估不向其暴露内存 Store 专用 `now` 参数。陈旧 case
  先装配新鲜 Bundle，再把 Coordinator 时钟推进到 TTL 外，从而验证真实选择器的模型前拒绝而非 Assembler 失败。
- 评估只记录离线脚本合同成本，`real_model_calls` 固定为 0；该字段不能作为 Task 10 真实 smoke
  预算账本或任何 Phase 13-15 费用的来源。

## 2026-07-18 Phase 16 Task 9 REVIEW REMEDIATION

- ScriptedModel 的每一次发送先预约对应冻结 Profile 的 case ceiling；即使返回 `request_sent=True`
  的失败，也保守计入离线合同成本。24 READY 与 6 个发送后 DEGRADED 的合同合计为 `2.72 CNY`，但外部费用仍为 0。
- 模型 user message 现在携带完整 `task_id`、kind、`input_snapshot` 和 EvidenceRef。Analyst 只接受
  Bundle/trigger 输入，Planner 只接受 Bundle/validated Analysis；输出模板只能从已验证任务展开。
- 评估对每例比对 escalation、analysis、proposal、outcome 的 Bundle ID/digest、父链和 outcome digest；
  PostgreSQL 重放使用同 schema 的新 Store 实例，证明进程重建时不再发送 ScriptedModel。

## 2026-07-18 Phase 16 Task 9 REVIEW REMEDIATION TWO

- Manifest 源码闭包新增 `decision_support/store.py` 与 `proposal.py`；加载和执行前都重算 Generator
  与闭包摘要，任何同进程嵌套 case 篡改、生成器或执行路径变更都会 fail-closed。
- 24 条高冲突 case 先执行同一 Bundle 的确定性单 Copilot 基线，再运行双 Agent；基线不调用模型，
  记录 logical case 与 Bundle ID/digest 并与 READY lineage 对照。
- 运行时以 case ID 的 SHA-256 派生中性 Workspace/Incident/Evidence/request 身份，模型正文不含
  case ID、split 或 kind。每次请求使用冻结 `prompt_text`，验证 ModelSuccess identity 与 JSON Schema。

## 2026-07-18 Phase 16 Task 9 REVIEW REMEDIATION THREE

- paired baseline 现在调用既有 `PriorityLiveOpsPolicy`，以同一 Bundle 的库存、备品、弹幕和 EvidenceRef
  产生零模型调用的确定性建议，再与 controlled READY lineage 对照。
- ScriptedModel 现在返回冻结 Profile 规定的 `AgentAction FINAL` 信封；评估先验证 action/evidence，再校验
  `final_output` 的 JSON Schema，最后才构造 Coordinator 消费的 AgentResult。
- 每例的备品库存与节奏分数进入真实 Evidence payload，使三组 split 的 48 个输入互不重复；闭包加入
  Specialist `models.py`、`profiles.py`、`live_ops.py`，并在加载与执行前重新验证。

## 2026-07-18 Phase 16 Task 10 RED

- Task 10 的真实 smoke 不是默认回归能力：发送门必须在单次 `AgentModelPort` 调用前同时验证模型/endpoint、
  官方价格和 usage、冻结 Prompt/Schema、Phase 16 Manifest/源码闭包与独立 reservation；任一证据缺失时只允许
  `BLOCKED` 或 `INCONCLUSIVE`，不得探测网络。
- Phase 16 账本必须有独立 scope、表和 1.00 CNY ceiling，不能借用 Phase 13、14 或 15 的余额；ScriptedModel
  演练不产生真实预算消费，也不能被误作预检成功证据。

## 2026-07-18 Phase 16 Task 10 GREEN / REVIEW

- `PHASE16_MULTI_AGENT_SMOKE` 按业务 case 而非单个 Agent 请求预约：Analyst 与 Planner 共用 0.10 CNY，
  十例总 exposure 永不超过 1.00 CNY。PostgreSQL 先锁 ledger 行再重算 exposure，重启后仍保持同一硬上限。
- 预检缺少 endpoint、官方价格、usage 合同、Manifest/dataset/source closure、Profile Prompt/Schema 或 Task 10
  runtime digest 时为 `BLOCKED` 且零发送。已经进入 Model Port 后 usage 不明、异常或身份不可信时为
  `INCONCLUSIVE`，整例保守结算 0.10 CNY 并停止 Planner；这区分了发送门禁与外部证据不足。
- `ModelUsage` 无 cache 命中字段，所有 input token 按公开 cache-miss 价格保守结算。Task 10 只证明真实发送
  身份/成本门禁，Task 9 的 Coordinator/Validator ScriptedModel 重放仍是行为正确性的唯一离线证据。

## 2026-07-18 Phase 16 Task 10 REVIEW REMEDIATION

- scope 现为唯一精确 `PHASE16_MULTI_AGENT_SMOKE`。`RESERVED` 与 `SETTLED` 行都消费十例 slot，只有
  Analyst 调用明确未发送时才能 `RELEASED`；低实际价格不会让第十一例重新获得发送资格。
- Planner 未发送时会结算已经发生的 Analyst 可计价成本，不再错误 release 整例。内存与 PostgreSQL 都先
  执行相同 slot/金额门禁；PostgreSQL 测试覆盖 10 条并发 reservation、低成本 settle、重启和第十一例拒绝。
- 预检每次调用 `_validate_dataset_for_run`，重算 Task 9 的 generator、源码闭包、case 与 dataset digest；
  嵌套 `input` 篡改在接触 Model Port 前失败。D-165 明确 endpoint/价格/usage 是 D-121 可信启动装配内部
  事实，不存在 HTTP 预检端点，也不将 Python PrivateAttr 冒充插件隔离。

## 2026-07-18 Phase 16 Task 10 FINAL REVIEW REMEDIATION

- Reservation 现在持久化 `PASS | FAIL | INCONCLUSIVE` 终态和稳定 reason。重启重放只返回原结论：
  `SETTLED/INCONCLUSIVE` 不会提升为 PASS，`RELEASED` 只允许未发送 Analyst 的 `FAIL`。
- Task 9 generator/source closure/case/dataset 漂移包含文件丢失、编码错误和摘要不匹配，均在预检或每次
  发送前转换为零发送 `TASK9_DATASET_INVALID`。直接 SQL 同样被 DDL 拒绝 `RELEASED/PASS`，内存和 PostgreSQL
  状态机同构。
- Task 10 最终验证完成：unit `12 passed`，PostgreSQL `2 passed`，完整 unit/integration 退出码均为 0，
  18 步迁移实际执行无失败；真实模型费用保持 0。
