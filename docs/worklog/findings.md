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

# 2026-07-11 Phase 7A 发现

- 生产级 Agent 项目不能只证明“能跑”，还要能回放、评分和复核，否则很难解释 Agent 决策是否可靠。
- Replay 不能只依赖 LangGraph checkpoint；checkpoint 适合恢复状态，业务评估还需要 Harness session、ToolCallAudit 和 DecisionTrace 作为证据。
- 规则评分必须先于 LLM Judge。安全、人审和工具合规不能交给 LLM 改判。
- 外部模型测试需要显式标记，默认测试使用 fake HTTP，避免网络、额度和模型波动污染工程验收。
- 运维页面也要按生产标准处理持久化数据，不能因为是内部页面就用 `innerHTML` 直接拼接 replay 字段。
- 评估任务的汇总和维度明细必须事务一致，否则会产生“任务完成但证据缺失”的排障陷阱。

---
