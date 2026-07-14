# LiveAgent Agent Runtime 演进决策日志

更新日期：2026-07-12

文档状态：持续更新

## 1. 使用方式

本文记录 Phase 11 及之后架构讨论中的正式决策。每项决策保留背景、候选方案、最终选择、选择理由、未选理由、影响和重新评估条件。后续决策可以将旧决策标记为 `SUPERSEDED` 或 `CONDITIONAL`，但不得删除历史。

状态说明：

- `ACCEPTED`：已确认，后续 Design 默认遵守。
- `CONDITIONAL`：只有满足明确条件时才启用。
- `SUPERSEDED`：曾经选择，后来被更严格或更准确的决策取代。
- `OPEN`：尚未完成讨论。

## D-001：项目推进目标

- **状态**：`ACCEPTED`
- **背景**：项目既希望体现 Agent 技术深度，也不能忽略可靠性、审计和恢复等生产工程。
- **候选方案**：技术标杆优先；可上线 MVP 优先；两条线平衡。
- **最终选择**：两条线平衡，架构主轴约 65%，与当前阶段直接相关的生产约束约 35%。
- **选择理由**：纯技术路线容易形成不可用演示，纯上线路线会被平台接入和运维工作吞没，无法补齐 PlanEngine、Skill Runtime 和评估闭环。
- **未选理由**：不选择机械 50/50，因为单人开发维护两条独立 backlog 会频繁切换上下文；不选择单线优先，因为不符合项目同时追求深度和工程性的目标。
- **影响**：每个阶段只设一个主要架构主题，同时附带该主题必需的生产验收项。
- **重新评估条件**：获得真实平台发布目标、正式团队资源或明确商业交付期限。

## D-002：周期与平台接入边界

- **状态**：`ACCEPTED`
- **背景**：路线需要与可用投入匹配，真实淘宝 API 还受资质、权限和凭据限制。
- **候选方案**：单人业余 12 周；单人全职 8 周；不绑定工期。平台侧可选择真实 API、契约优先 Fake Adapter 或暂不做 Adapter。
- **最终选择**：按单人业余 12 周估算；平台接入采用契约优先和高保真 Fake Adapter。
- **选择理由**：能够同时推进内核和可靠性，又不让不可控的外部资质阻塞架构验收。
- **未选理由**：不选择真实 API 作为验收条件，因为外部授权不受项目控制；不完全跳过 Adapter，因为统一执行契约是生产线的重要组成部分。
- **影响**：本周期验证接口、错误语义、幂等、超时和审计，不处理真实交易。
- **重新评估条件**：获得正式沙箱、稳定 API 权限和脱敏测试数据。

## D-003：长期文档组织方式

- **状态**：`ACCEPTED`
- **背景**：阶段多、讨论时间长，单一长文档会同时变成过时路线图和混乱执行日志。
- **候选方案**：总路线 + 每阶段三件套；总路线 + 每个大阶段一套；单一总计划。
- **最终选择**：总路线 + 每阶段 Design、Plan、Acceptance 三件套，三个 worklog 作为工作记忆。
- **选择理由**：总路线保持稳定，阶段文档可以独立审核和更新，Acceptance 能区分计划与实际结果。
- **未选理由**：不选择单一总计划，因为远期细节会迅速过时；不只按大阶段拆分，因为单份文件仍会过长。
- **影响**：远期阶段只在 Roadmap 中保留阶段目标、前置依赖、进入条件、退出条件和待决策项五类边界；待决策项保持开放，不得被执行者当作默认方案。详细 Design 和 Plan 在对应阶段开始前按最新事实 Just-in-Time 生成。
- **重新评估条件**：文档数量显著增加但没有提升恢复效率时，可按完成阶段归档。

## D-004：Skill Runtime 的建设方式

- **状态**：`ACCEPTED`
- **背景**：现有 ToolRegistry 具备白名单和风险元数据，但还不是统一、版本化、可执行和可评估的能力契约。
- **候选方案**：渐进升级 ToolRegistry；新建完整插件 Runtime；继续只使用 ToolRegistry。
- **最终选择**：渐进升级 ToolRegistry，首期不做外部插件安装、热加载和通用沙箱。
- **选择理由**：PlanEngine 和未来 Agent 需要的是稳定能力契约，不是当前没有需求的插件生态。
- **未选理由**：完整插件 Runtime 会引入发现、隔离、兼容和灰度成本；维持现状则无法钉住版本和统一执行语义。
- **影响**：首期聚焦 Manifest、Handler、Executor、版本、Schema、风险和审计。
- **重新评估条件**：出现仓库外 Skill、第三方安装或运行时灰度的真实需求。

## D-005：Skill 与 Tool 元数据的唯一事实源

- **状态**：`ACCEPTED`
- **背景**：SkillRegistry 与 ToolRegistry 分别维护风险和 Schema 会造成安全配置漂移。
- **候选方案**：SkillManifest 唯一事实源；两套注册表并存；ToolRegistry 继续为主。
- **最终选择**：SkillManifest 是唯一事实源，ToolRegistry 由 Manifest 生成兼容只读投影。
- **选择理由**：风险、生命周期、Schema、版本和幂等要求只能维护一次，所有执行入口必须看到相同元数据。
- **未选理由**：双注册表长期不可控；ToolRegistry 继续为主会让能力契约继续分散。
- **影响**：ToolRegistry 的兼容 API 保留，但不允许独立新增或修改元数据。
- **重新评估条件**：无；这是首期安全一致性的基础约束。

## D-006：现有工具迁移范围

- **状态**：`ACCEPTED`
- **背景**：当前共有 13 个注册工具，全部一次切换执行链会扩大回归范围，只迁移 4 个又会保留双事实源。
- **候选方案**：全量元数据 + 4 个 Handler；只迁移 4 个核心能力；13 个全部完整迁移。
- **最终选择**：13 个工具一次性迁移 Manifest 元数据，4 个核心 Handler 进入新 SkillExecutor，其余通过兼容适配执行。
- **选择理由**：同时获得唯一事实源和可控行为变更范围。
- **未选理由**：只迁移 4 个会形成混合元数据；全部切换 Handler 会把首期变成大规模业务重构。
- **影响**：必须增加 Manifest 到 ToolMetadata 的兼容投影测试，以及新旧执行路径的契约测试。
- **重新评估条件**：Phase 11A 验收后再逐批迁移剩余 Handler。

## D-007：Manifest 的首期存储形态

- **状态**：`ACCEPTED`
- **背景**：首期不需要外部插件或运行时配置，但需要类型安全、代码审查和中文说明。
- **候选方案**：Python + Pydantic Catalog；每个 Skill 一个 JSON；PostgreSQL 动态存储。
- **最终选择**：使用 Python + Pydantic Catalog，应用启动时统一校验。
- **选择理由**：与当前代码结构一致，便于重构、注释和静态引用检查，不需要额外加载协议。
- **未选理由**：JSON 会增加文件分散且缺少代码级引用；数据库方案超出当前动态配置需求。
- **影响**：Catalog 必须显式注册，不扫描任意目录或执行不可信代码。
- **重新评估条件**：需要仓库外分发、配置中心或非开发人员维护 Manifest。

## D-008：Skill 版本策略

- **状态**：`ACCEPTED`
- **背景**：长任务恢复时如果 Skill 已升级，直接使用新行为会破坏计划确定性。
- **候选方案**：单活版本 + 执行钉住；多版本并行灰度；只记录版本不校验。
- **最终选择**：Catalog 每个 `skill_id` 只允许一个活动版本；Plan 和 SkillCall 持久化精确版本，恢复时不匹配则暂停并触发 Replan 或人工处理。
- **选择理由**：用最小复杂度保证恢复一致性和审计可解释性。
- **未选理由**：多版本灰度需要额外路由和清理机制；只记录不校验等同于允许静默行为漂移。
- **影响**：版本升级必须明确处理尚未结束的 Plan。
- **重新评估条件**：确实需要 A/B、灰度或长期并行版本。

## D-009：SkillExecutor 执行模型

- **状态**：`ACCEPTED`
- **背景**：现有业务函数多数同步，但后续 DAG 需要并行、超时和协作式冻结。
- **候选方案**：异步接口 + 同步适配器；保持同步接口；直接 Kafka/Worker 队列化。
- **最终选择**：Runtime 对外采用异步执行接口，现有同步函数通过适配器接入。
- **选择理由**：避免 PlanEngine 阶段再次重构公共协议，同时控制首期改造范围。
- **未选理由**：纯同步接口不支持后续并行；全面队列化会过早引入分布式一致性问题。
- **影响**：Handler 必须有明确超时和错误映射，不能用无限等待模拟异步。
- **重新评估条件**：需要跨进程扩展或执行隔离时再引入 Worker。

## D-010：PlanEngine 与 LLM 的职责边界

- **状态**：`ACCEPTED`
- **背景**：直接让 LLM 执行或动态编译 LangGraph，会把业务计划、控制图和恢复状态混在一起。
- **候选方案**：LLM 提案 + 确定性执行；动态编译 LangGraph；ReAct 模拟 DAG。
- **最终选择**：LLM 只生成候选业务 DAG，PlanEngine 负责校验、调度、持久化和增量 Replan；LangGraph 负责 Harness 控制循环。
- **选择理由**：计划可以审计、拒绝、版本化和确定性恢复，LLM 仍保留规划价值。
- **未选理由**：动态编译图会增加图版本和热变更复杂度；ReAct 无法提供可靠依赖和局部恢复。
- **影响**：任何 LLM 计划都必须先经过 DAG、Skill、生命周期和风险校验。
- **重新评估条件**：LangGraph 后续提供成熟且可审计的动态业务图版本机制。

## D-011：PlanEngine 首期垂直场景

- **状态**：`ACCEPTED`
- **背景**：首期场景必须同时证明 DAG、并行、抢占、恢复和局部 Replan 的价值。
- **候选方案**：手卡生成 + 售罄抢占；仅播前手卡 DAG；仅播中事件调度。
- **最终选择**：批量生成手卡时收到售罄事件，处理紧急 DAG 后恢复未受影响任务。
- **选择理由**：一条链路可以验证长任务、紧急事件和增量恢复，演示结果也容易理解。
- **未选理由**：仅手卡 DAG 无法证明实时抢占；仅播中调度缺少清晰的长任务恢复对照。
- **影响**：Plan 模型必须支持并行节点、冻结、紧急计划和结果复用。
- **重新评估条件**：该场景无法稳定构造或缺少必要业务数据。

## D-012：抢占语义

- **状态**：`ACCEPTED`
- **背景**：外部 LLM/API 请求通常无法保证强制取消后平台侧没有执行。
- **候选方案**：协作式冻结；强制取消运行中任务；仅调整队列顺序。
- **最终选择**：停止派发新的低优节点，运行中节点在完成或超时后冻结，再执行紧急 DAG。
- **选择理由**：优先保证本地状态与外部副作用一致，并提供明确恢复点。
- **未选理由**：强杀可能产生平台已执行而本地未知；仅排队不构成真正抢占。
- **影响**：所有长任务 Handler 必须支持超时，调度器必须区分停止派发与取消执行。
- **重新评估条件**：Adapter 提供可证明幂等且可确认的取消协议。

## D-013：Plan 持久化事实源

- **状态**：`ACCEPTED`
- **背景**：LangGraph checkpoint 适合恢复控制循环，但不适合独立查询计划节点、版本和评估证据。
- **候选方案**：独立 PlanStore + checkpoint 引用；全部放 checkpoint；单份 JSONB 快照。
- **最终选择**：PostgreSQL PlanStore 保存 PlanRun、NodeRun 和版本；checkpoint 只保存计划引用和控制位置。
- **选择理由**：兼顾控制恢复、业务审计、节点查询、版本比较和批量评估。
- **未选理由**：checkpoint 单源会把业务证据绑死在图实例；单 JSONB 难以处理节点并发更新。
- **影响**：PlanStore 与 checkpoint 的写入顺序和恢复对账必须在 Design 中明确。
- **重新评估条件**：无；与现有 Replay 不应只依赖 checkpoint 的结论一致。

## D-014：Plan 版本模型

- **状态**：`ACCEPTED`
- **背景**：增量 Replan 需要保留旧计划和变化证据。
- **候选方案**：不可变版本快照；原地修改 + 版本号；仅事件日志。
- **最终选择**：每次 Replan 创建新的不可变 `plan_version`，复用结果显式引用旧 NodeRun。
- **选择理由**：能够直接回放每次计划变化，又不要求首期实现完整事件溯源系统。
- **未选理由**：原地修改难还原历史；纯事件日志实现和调试成本过高。
- **影响**：旧版本永不覆盖，新版本必须记录 retained、invalidated 和 added 节点。
- **重新评估条件**：计划规模导致快照存储成为可测量瓶颈。

## D-015：PlanNode 状态集

- **状态**：`ACCEPTED`
- **背景**：最小状态集无法准确表达人工审批、延迟重试、冻结和重规划失效。
- **候选方案**：受控生产状态集；最小五状态；完整工作流状态机。
- **最终选择**：使用 `PENDING`、`READY`、`RUNNING`、`WAITING_APPROVAL`、`WAITING_RECONCILIATION`、`RETRY_WAIT`、`SUCCEEDED`、`FAILED`、`FROZEN`、`INVALIDATED`、`SKIPPED`。其中 `WAITING_RECONCILIATION` 由 D-026 在失败语义讨论中追加。
- **选择理由**：覆盖当前垂直场景的真实语义，同时不引入补偿、人工接管等未验证状态。
- **未选理由**：五状态会把关键语义塞进附加字段；完整状态机超出首期范围。
- **影响**：必须定义合法状态迁移表并拒绝非法跳转。
- **重新评估条件**：出现补偿事务、除审批/对账外的新人工接管类型或跨天暂停需求。

## D-016：增量 Replan 的失效算法

- **状态**：`ACCEPTED`
- **背景**：全部后继重跑浪费执行成本，让 LLM 选择失效节点又不可确定。
- **候选方案**：依赖闭包 + 输入指纹；全部后继失效；LLM 选择失效节点。
- **最终选择**：事件先定位直接受影响节点，再沿后继依赖传播；只有输入指纹变化的节点失效，其他成功节点复用。
- **选择理由**：在确定性和最小重算之间取得平衡，并能量化 Replan 效率。
- **未选理由**：全后继失效会扩大重跑；LLM 判定可能漏依赖或产生不稳定结果。
- **影响**：PlanNode 必须记录稳定输入指纹和依赖关系，Replan 必须生成复用证据。
- **重新评估条件**：输入无法稳定序列化或跨节点存在未声明隐式依赖。

## D-017：PlanEngine 并发策略

- **状态**：`ACCEPTED`
- **背景**：手卡 DAG 需要并行价值，但无界并发会触发模型限流、重复写和审批竞争。
- **候选方案**：有界并发 + 资源锁；全部串行；所有 READY 节点并发。
- **最终选择**：无依赖只读节点最大并发 4；写操作、高风险节点和同一商品资源串行。
- **选择理由**：能够展示 DAG 并行，又保留可预测的资源和安全边界。
- **未选理由**：全部串行无法证明 DAG 调度收益；无界并发不符合生产可靠性目标。
- **影响**：Manifest 或 PlanNode 需要提供资源键和副作用分类。
- **重新评估条件**：基准测试证明并发 4 明显不足，且 Adapter 限流允许提高。

## D-018：预设多 Agent 拓扑

- **状态**：`SUPERSEDED`
- **背景**：早期方案曾选择单进程 LangGraph 父图 + Specialist 子图，计划预设 Planner、Commerce、Review 等角色。
- **候选方案**：父图 + 子图；独立服务 Agent；暂缓多 Agent。
- **最终选择**：历史选择为父图 + 子图，原因是可以复用 checkpoint、interrupt 和审计；该选择已被后续 Agent 化评估门禁取代。
- **选择理由**：当时希望在单进程内复用现有 LangGraph、checkpoint、interrupt 和审计基础设施，避免过早引入独立服务。
- **未选理由**：独立服务 Agent 会扩大部署、状态一致性和审计成本；完全暂缓多 Agent 会错过验证 Specialist 边界的机会。
- **影响**：该历史选择过早承诺 Agent 数量，没有先证明职责确实需要独立推理循环。只有 Agent 化评估通过后，保留的 Agent 才允许讨论父图 + 子图；独立服务仍不在本周期范围。
- **重新评估条件**：见 D-019 和 D-020。

## D-019：Agent 化试验方法

- **状态**：`SUPERSEDED`
- **背景**：直接实现 LiveOpsAgent 无法证明它比固定流程更合适。
- **候选方案**：先建固定子图基线；直接试点 LiveOpsAgent；本周期完全暂缓多 Agent。
- **最终选择**：当时选择先用相同 Skill 实现确定性售罄处理子图，再与工具范围受限的 LiveOpsAgent 对照。
- **选择理由**：控制输入、能力和安全策略后，差异才能归因于 Agent 的多轮决策。
- **未选理由**：直接试点缺少基线；完全暂缓会失去用真实数据验证 Agent 边界的机会。
- **影响**：该决策仍作为播中 LiveOpsAgent 候选的历史依据，但不再代表 Phase 13 的全部范围；Phase 13 的当前正式范围见 D-052。
- **重新评估条件**：已被 D-052 取代；后续只在播中候选评估细节中引用本决策。

## D-020：LiveOpsAgent 保留门槛

- **状态**：`CONDITIONAL`
- **背景**：必须用可执行指标阻止“完成试点即保留 Agent”。
- **候选方案**：严格量化门槛；量化 + 架构评审；完成试点即保留。指标取向另比较平衡、质量优先和效率优先。
- **最终选择**：严格量化、平衡型门槛。
- **选择理由**：既要求实际质量收益，也限制额外模型延迟和成本，避免用主观扩展性为无收益 Agent 辩护。
- **未选理由**：架构评审会扩大主观空间；完成即保留直接违背“不为 Agent 而 Agent”；质量优先和效率优先都偏离双线平衡目标。
- **影响**：严重安全违规必须为 0；成功率至少提升 5 个百分点或恢复率提升 10 个百分点；延迟和 Token 成本增幅不得超过 20%。未达到即删除 Agent 方案。D-052 将该门槛泛化为所有 Specialist Agent 候选的默认保留门槛；如某候选 Agent 的指标不适用，必须先新增决策定义指标，不能直接保留。
- **重新评估条件**：Golden Dataset 规模过小，无法支持百分点比较时，先扩充数据集而不是降低门槛。

## D-021：Orchestrator 与 PlanEngine 是否属于 Agent

- **状态**：`ACCEPTED`
- **背景**：把调度器命名为 Agent 会模糊确定性控制与概率推理的责任边界。
- **候选方案**：确定性组件；LLM Orchestrator Agent；混合但统一称 Agent。
- **最终选择**：Orchestrator 和 PlanEngine 默认是确定性组件，不包装成 Agent。
- **选择理由**：路由、状态迁移、版本、风险和恢复必须可预测；LLM 只在候选计划或明确的 Specialist 内发挥作用。
- **未选理由**：LLM 主控会增加不可解释路由和安全风险；统一称 Agent 会削弱项目技术叙事的准确性。
- **影响**：后续架构图必须明确 deterministic control plane 与 agent reasoning plane。
- **重新评估条件**：有基准证明概率路由在复杂任务上显著优于确定性调度且不降低安全性。

## D-022：Review 能力的形态

- **状态**：`ACCEPTED`
- **背景**：当前已有 Lifecycle Hook、规则评估器和 LLM Judge，直接新增 ReviewAgent 可能只是把检查逻辑换成 Prompt。
- **候选方案**：Hook/Skill；ReviewAgent；取消独立 Review。
- **最终选择**：首期保留为确定性 Hook、规则评估和可选 LLM Judge Skill，不预设 ReviewAgent。
- **选择理由**：规则和安全检查不需要多轮自主规划，现有形态更稳定、可评估。
- **未选理由**：ReviewAgent 暂无独立工具循环、上下文或恢复需求；完全取消 Review 又会削弱评估闭环。
- **影响**：LLM Judge 不能把规则 FAIL 改成 PASS。
- **重新评估条件**：Review 需要主动检索多源证据、多轮质询并有独立失败恢复时，再进入 Agent 化门禁。

## D-023：PlanEngine 失败语义

- **状态**：`ACCEPTED`
- **背景**：现有执行器把参数错误、策略拦截、外部故障和未知异常压缩成自由文本 `error`，部分客户端又在内部自行重试，PlanEngine 无法统一审计和决定恢复动作。
- **候选方案**：结构化失败事实 + 集中式 FailurePolicy；每个 Skill 自行处理失败；只区分可重试与不可重试。
- **最终选择**：Skill 和 Adapter 只返回结构化失败事实，FailurePolicy 结合 Manifest、幂等性、副作用状态和节点状态决定恢复动作，PlanEngine 负责执行动作。
- **选择理由**：失败类别描述“发生了什么”，恢复动作描述“系统准备怎么办”，分层后同一种网络故障可以根据读写属性和副作用状态采取不同动作。
- **未选理由**：Skill 自行处理会分散重试、安全和审计策略；二元可重试模型无法表达业务冲突、版本冲突、副作用未知和人工对账。
- **影响**：失败类别固定为 `TRANSIENT_INFRA`、`RATE_LIMITED`、`INVALID_INPUT`、`BUSINESS_CONFLICT`、`POLICY_DENIED`、`VERSION_CONFLICT`、`SIDE_EFFECT_UNKNOWN`、`INTERNAL_INVARIANT`；恢复动作固定为 `RETRY`、`REPLAN`、`WAIT_HUMAN`、`SKIP`、`FAIL_PLAN`。`pending` 不再作为失败类别。
- **重新评估条件**：只有出现无法由“失败事实 + 策略上下文”表达的真实案例时，才扩展类别；不得为单个供应商错误码新增顶层类别。

## D-024：自动重试所有权与执行规则

- **状态**：`ACCEPTED`
- **背景**：当前 LLMClient 在内部执行多次退避重试；如果 PlanEngine 再重试节点，会形成隐藏的重试乘积，增加延迟、Token 成本和副作用风险。
- **候选方案**：PlanEngine 统一重试预算；Adapter 与 PlanEngine 两层共享预算；各客户端继续自行重试。
- **最终选择**：PlanEngine 是自动重试的唯一所有者，Skill、Adapter 和客户端执行单次尝试并返回结构化失败；等待通过持久化 `RETRY_WAIT` 调度，不在执行线程内 `sleep()`。
- **选择理由**：每次尝试都能形成 NodeRun、成本和审计证据，重启后也能恢复等待状态，并彻底避免重试次数叠乘。
- **未选理由**：两层预算仍需处理嵌套次数和审计归属；客户端自行重试会让 PlanEngine 看不到真实尝试次数和失败过程。
- **影响**：只读操作最多执行 3 次（首次 + 2 次重试）；具有可靠幂等键且 Adapter 能确认副作用状态的写操作最多执行 2 次（首次 + 1 次重试）；`SIDE_EFFECT_UNKNOWN`、`INVALID_INPUT`、`BUSINESS_CONFLICT`、`POLICY_DENIED`、`VERSION_CONFLICT` 和 `INTERNAL_INVARIANT` 不允许自动重试。退避采用指数增长和抖动，优先遵守 `Retry-After`，`next_retry_at` 不得越过节点 deadline。
- **重新评估条件**：外部 SDK 无法关闭内置重试时，必须把其次数纳入同一预算并显式上报；不能静默形成第二层重试。

## D-025：Replan 触发条件与循环预算

- **状态**：`ACCEPTED`
- **背景**：任何失败都调用 LLM Replan 会形成高成本循环，也会用新计划掩盖状态损坏或安全错误。
- **候选方案**：确定性触发矩阵；所有终态失败都 Replan；由 LLM 自行判断是否 Replan。
- **最终选择**：FailurePolicy 使用确定性矩阵决定是否允许局部 Replan，LLM 只有在策略已允许后才生成替代子图。
- **选择理由**：触发条件可以回归测试，LLM 仍负责有价值的替代计划生成，但不能决定是否绕过失败。
- **未选理由**：全失败 Replan 会掩盖内部故障并产生循环；LLM 自行判断不可预测且难以执行发布门禁。
- **影响**：首期仅对业务事实变化、Skill 版本冲突、重试耗尽且存在替代能力、人工拒绝后的安全替代路径触发 Replan。每个 root plan 最多创建 2 个新版本；相同 `failure_signature + input_fingerprint` 再次出现时立即停止 Replan，并转人工或终止计划。
- **重新评估条件**：Golden Dataset 证明两次预算显著降低可恢复成功率时，先分析失败类型和替代能力覆盖，不直接提高预算。

## D-026：审批等待与人工对账

- **状态**：`ACCEPTED`
- **背景**：执行前的高风险审批与执行后的副作用未知具有不同含义、恢复输入和安全后果，不能共用同一个等待状态。
- **候选方案**：使用两个独立状态；统一为 `WAITING_HUMAN` 并增加子类型；全部复用 `WAITING_APPROVAL`。
- **最终选择**：保留 `WAITING_APPROVAL` 表示执行前门禁，新增 `WAITING_RECONCILIATION` 表示执行结果未知后的人工对账。
- **选择理由**：状态本身即可支持查询、告警和合法迁移校验，不依赖容易遗漏的附加字段解释关键安全语义。
- **未选理由**：统一状态会让查询和状态迁移过度依赖 `human_task_type`；复用审批状态会混淆“批准执行”与“确认是否已经执行”。
- **影响**：审批 TTL 沿用 10 分钟，超时后不得执行原动作，并可按确定性矩阵尝试安全 Replan；对账 TTL 为 30 分钟，超时后保留 `SIDE_EFFECT_UNKNOWN` 证据并终止相关计划，禁止自动重试。两类恢复输入和审计事件必须分开。
- **重新评估条件**：真实运营数据证明 TTL 不适合直播响应时，可按人工作业 SLA 调整，但不得取消 fail-closed 收敛。

## D-027：紧急 DAG 失败后的恢复范围

- **状态**：`ACCEPTED`
- **背景**：售罄等紧急 DAG 可能自身失败。整张原计划永久冻结会扩大影响，直接恢复又可能继续处理已失效商品。
- **候选方案**：按风险影响范围部分恢复；整张原计划继续冻结；恢复原计划并仅发送告警。
- **最终选择**：按事件影响范围决定恢复：商品级风险继续阻断受影响商品分支，未受影响且输入指纹未变化的节点可以恢复；直播间或平台级风险未解除时整张计划保持冻结。
- **选择理由**：在 fail-closed 前提下保留无关任务的业务连续性，并与增量 Replan 的依赖闭包、输入指纹设计一致。
- **未选理由**：全量冻结会让局部故障阻塞全部只读和内容任务；直接恢复无法保证风险已解除。
- **影响**：紧急事件必须携带可校验的 impact scope 和资源键；无法确定影响范围时按全局风险处理。受影响分支进入人工处理或失败收敛，不得被原计划恢复逻辑重新激活。
- **重新评估条件**：事件源不能可靠提供 impact scope 时，先完善事件契约，不降低默认全局冻结策略。

## D-028：PlanStore 与 Checkpoint 的权威关系

- **状态**：`ACCEPTED`
- **背景**：PlanStore 与官方 PostgresSaver 虽然都使用 PostgreSQL，但由不同连接和提交边界管理，无法可靠加入同一个业务事务。
- **候选方案**：PlanStore 权威并有序写入；checkpoint 权威并异步补业务表；尝试跨存储原子事务。
- **最终选择**：PlanStore 是节点执行事实和结果的权威源。节点状态与结果必须先提交 PlanStore，graph 节点才允许返回，随后由 PostgresSaver 保存 checkpoint。
- **选择理由**：确保 checkpoint 不会声明一个尚无业务结果、审计和副作用证据的成功节点，同时保留官方 checkpointer 的升级边界。
- **未选理由**：checkpoint 权威会让业务证据滞后或缺失；跨连接伪原子事务依赖官方库内部连接，耦合高且无法真正保证原子性。
- **影响**：任何节点成功返回前必须完成 PlanStore 提交；checkpoint 只保存控制位置和计划引用，不取代 NodeRun。
- **重新评估条件**：官方 PostgresSaver 提供稳定、公开且可注入业务事务的接口时，才重新评估原子提交。

## D-029：PlanStore 领先 Checkpoint 的恢复

- **状态**：`ACCEPTED`
- **背景**：进程可能在 PlanStore 已提交节点成功、checkpoint 尚未写入时崩溃。
- **候选方案**：从旧 checkpoint 重放并复用结果；后台直接修改 checkpoint 前移；完全从 PlanStore 重建 graph。
- **最终选择**：从旧 checkpoint 恢复 graph 并重新进入节点，执行器根据 NodeRun 和幂等标识命中已成功结果后直接返回，不再次调用 Skill，随后由 graph 正常写出新 checkpoint。
- **选择理由**：不依赖官方 checkpoint 内部表结构，也不会重复产生外部副作用，同时保留 messages、interrupt 等 LangGraph 状态。
- **未选理由**：直接修改 checkpoint 依赖私有结构；完全重建会丢失 LangGraph 控制上下文。
- **影响**：节点入口必须先查 PlanStore；复用结果也要写入恢复审计，区分真实执行与 replay reuse。
- **重新评估条件**：官方提供公开的 checkpoint 快进接口，并能证明不会破坏 interrupt 与消息状态。

## D-030：Checkpoint 领先 PlanStore 的处理

- **状态**：`ACCEPTED`
- **背景**：按 D-028 的顺序该状态不应发生，但旧代码、数据库故障或人工修改可能造成 checkpoint 显示完成而 PlanStore 缺少成功证据。
- **候选方案**：立即 fail-closed；用 checkpoint 自动回填 PlanStore；忽略 checkpoint 并重新执行。
- **最终选择**：分类为 `INTERNAL_INVARIANT`，冻结相关计划、发送告警并进入人工对账，不自动补造业务成功记录，也不重新执行节点。
- **选择理由**：checkpoint 不一定包含完整业务结果和副作用证据，自动回填或重跑都可能掩盖数据损坏或制造重复副作用。
- **未选理由**：自动回填会伪造权威证据；重新执行无法确认外部动作是否已经完成。
- **影响**：对账器必须显式检测 checkpoint 领先，并把证据差异写入审计。
- **重新评估条件**：只有人工或平台查询补齐可验证的外部执行证据后，才能通过 Command Ledger 恢复。

## D-031：PlanNode Worker 并发控制

- **状态**：`ACCEPTED`
- **背景**：仅使用租约时，旧 Worker 可能在租约过期、新 Worker 已接管后晚到提交，覆盖新结果。
- **候选方案**：数据库 lease + fencing token；只有 lease；PostgreSQL advisory lock。
- **最终选择**：使用 `FOR UPDATE SKIP LOCKED` 抢占可运行节点，分配 lease 并单调递增 `claim_version` 作为 fencing token；完成、失败和续租写入都必须匹配当前 token。
- **选择理由**：既能复用项目现有 Worker 抢占模式，又能阻止过期 Worker 晚到提交。
- **未选理由**：纯 lease 无法隔离迟到写；advisory lock 与连接生命周期绑定，不适合长任务和跨连接续租。
- **影响**：NodeRun 必须保存 `lease_owner`、`lease_until` 和 `claim_version`，所有终态更新使用条件 UPDATE。
- **重新评估条件**：执行迁移到具备等价 fencing 语义的外部任务平台时，可由平台 token 替代数据库 token。

## D-032：租约时长与心跳

- **状态**：`ACCEPTED`
- **背景**：固定短租约会误回收长任务，固定长租约又会拖慢直播场景中的崩溃恢复。
- **候选方案**：按 Skill timeout 派生并心跳续租；固定 60 秒；固定 5 分钟。
- **最终选择**：初始租约为 `max(60秒, skill_timeout + 30秒)`，上限 10 分钟；Worker 每 `lease_duration / 3` 发送心跳续租，停止心跳且租约到期后才允许回收。
- **选择理由**：让恢复速度与任务实际时长匹配，长任务也不需要占用不合理的固定租约。
- **未选理由**：固定 60 秒可能误抢占；固定 5 分钟使短任务崩溃恢复过慢。
- **影响**：心跳和完成写入都必须携带 fencing token；超过节点 deadline 时不得仅靠续租延长业务执行。
- **重新评估条件**：真实任务耗时分布表明派生公式产生系统性误回收或恢复延迟。

## D-033：人工恢复命令幂等

- **状态**：`ACCEPTED`
- **背景**：浏览器重发、网络重试和多个操作员可能对同一审批、对账或恢复任务重复提交 Command(resume)。
- **候选方案**：Command Ledger + 乐观版本；只使用 session idempotency key；只依赖 LangGraph thread_id。
- **最终选择**：所有人工命令先写入 Command Ledger，携带唯一 `command_id`、`expected_plan_version` 和 `expected_node_status`；唯一约束负责去重，版本或状态不匹配时拒绝执行。
- **选择理由**：同时解决重复提交、旧版本命令和并发操作员竞争，并能保留原始处理结果供重复请求返回。
- **未选理由**：单 idempotency key 无法阻止旧版本命令；thread_id 只能定位会话，不能表达业务命令幂等。
- **影响**：重复 `command_id` 返回首次处理结果；合法命令必须在同一 PlanStore 事务中登记并推进节点状态，之后才调用 LangGraph resume。
- **重新评估条件**：外部审批平台提供更强的命令账本时，仍需映射为本地 command_id 和预期版本。

## D-034：一致性对账触发方式

- **状态**：`ACCEPTED`
- **背景**：仅在 API 请求时对账会让无人访问的过期租约和不一致计划长期悬挂，仅周期扫描又会延迟启动恢复和人工操作。
- **候选方案**：启动 + 周期 + 按需；仅后台周期扫描；仅请求时检查。
- **最终选择**：服务启动时扫描一次，后台每 30 秒扫描非终态计划，审批、对账或恢复命令执行前再校验目标计划；三种入口复用同一个幂等对账服务。
- **选择理由**：兼顾启动恢复、无人值守收敛和人工操作前的一致性保证，不复制三套修复逻辑。
- **未选理由**：仅周期扫描存在窗口延迟；仅按需检查无法处理无人访问的异常计划。
- **影响**：对账服务只能使用公开 PlanStore 和 checkpointer 读取接口，不直接更新官方 checkpoint 表；重复扫描不得重复创建告警或恢复命令。
- **重新评估条件**：计划规模使 30 秒扫描产生可测量数据库压力时，改用事件唤醒加低频兜底扫描。

## D-035：ToolRegistry 兼容投影切换方式

- **状态**：`CONDITIONAL`
- **背景**：SkillManifest 成为唯一事实源后，现有硬编码 ToolRegistry 仍被 planner、Hook、policy 和多条 flow 读取，直接替换可能让风险、Schema 或生命周期在迁移中静默漂移。
- **候选方案**：冻结旧元数据并影子校验后切换；单次直接切换；运行时长期双读并在不一致时回退旧表。
- **最终选择**：先把旧 ToolRegistry 元数据冻结为只读对照快照。9 个未迁移 Handler 的 Manifest 必须逐字段一致；4 个核心 Skill 按 D-043 使用经过评审的显式输入 Schema，并对差异建立白名单断言。D-053 追加所有 Skill 根 Schema 的 `additionalProperties: false` 这一 fail-closed 安全约束，作为唯一受控例外。全部校验通过后统一切换到 Manifest 投影，切换后不保留旧实现回退。
- **选择理由**：既能发现无意迁移差异，又不会把评审已确认的旧 Schema 与真实执行语义错位固化进新 Runtime；切换后仍只有 SkillManifest 一个可编辑事实源。
- **未选理由**：直接切换缺少清晰的迁移诊断证据；长期双读会保留双事实源，并可能用旧表回退掩盖安全配置漂移。
- **影响**：旧元数据只能存在于迁移测试夹具或冻结快照中，不得继续接受业务修改；非白名单投影差异必须阻止切换，4 个受控 Schema 差异必须写明原因并由兼容适配器覆盖；D-053 的根对象额外字段拒绝必须更新冻结哈希并持续测试。
- **重新评估条件**：切换完成后如需兼容，只能兼容查询 API 或参数适配，不能恢复第二套可编辑元数据；4 个 Schema 修正范围变化时必须新增决策。

## D-036：Phase 11A 首批 Handler 范围

- **状态**：`ACCEPTED`
- **背景**：D-006 只确定首期迁移 4 个 Handler，但没有明确名称，实施者无法据此确定行为回归范围。
- **候选方案**：播前完整闭环；面向售罄抢占的跨生命周期纵切；仅迁移低风险读取与生成能力。
- **最终选择**：首批 4 个 Handler 固定为 `query_products`、`generate_live_plan`、`generate_product_card`、`setup_live_session`。
- **选择理由**：四个能力组成现有播前完整闭环，同时覆盖读取、确定性生成、审计、幂等写和人工 hard-gate，可复用成熟测试并独立验收 Skill Runtime。
- **未选理由**：跨生命周期纵切更贴近 Phase 12 场景，但在 Phase 11A 无法形成完整 DAG 链；只迁移低风险能力无法验证写操作、幂等和人工门禁。
- **影响**：其余 9 个工具只迁移 Manifest 元数据，执行继续经过现有兼容路径；不得把 Phase 12 售罄场景提前塞入本阶段。
- **重新评估条件**：只有现有播前闭环在实施前被删除或无法运行时，才重新选择首批 Handler。

## D-037：四个核心 Handler 的迁移批次

- **状态**：`ACCEPTED`
- **背景**：四个 Handler 同时切换难以隔离高风险建播写入，逐个建立完整灰度矩阵又会增加单人项目的实施成本。
- **候选方案**：分两批迁移；四个 Handler 逐个迁移；四个 Handler 原子切换。
- **最终选择**：第一批迁移 `query_products`、`generate_live_plan` 和 `generate_product_card`；第二批单独迁移 `setup_live_session`。
- **选择理由**：第一批可以集中验证只读与确定性生成语义，第二批独立验证审批、审计、幂等和写操作边界，风险隔离与实施成本较平衡。
- **未选理由**：逐个迁移会扩大开关和回归矩阵；原子切换无法区分通用执行器问题与高风险写入问题。
- **影响**：两个批次必须能够独立切换和回滚，第二批不得在第一批未通过验收时启用。
- **重新评估条件**：第一批三个能力出现无法解耦的共享事务时，才考虑进一步合并或拆分批次。

## D-038：新旧执行链的灰度方式

- **状态**：`SUPERSEDED`
- **背景**：需要获得新旧执行行为的等价证据，但写操作双执行可能制造重复副作用和冲突审计。
- **候选方案**：分组路由加有限影子执行；仅分组路由；使用单个全局执行器开关。
- **最终选择**：当时选择按 D-037 的两个批次设置显式执行路由，并在测试或 Fake Adapter 环境为第一批增加 `SHADOW_COMPARE` 路由；评审发现前三个现有服务方法同样写审计，因此该运行时双执行路由被 D-044 的测试专用比较器取代。
- **选择理由**：原选择希望同时获得行为等价证据与高风险写入隔离；后续代码审查证明，把双执行放进正式 Router 会让生产代码携带仅测试使用的分支，并可能误写正式审计。
- **未选理由**：只路由缺少切换前的结果对照；单个全局开关无法隔离第二批风险，也不支持两个批次独立验收。
- **影响**：正式 Router 只保留 `LEGACY` 与 `SKILL_RUNTIME`；行为双算仅存在于测试代码，使用隔离服务栈与独立内存审计 Store。
- **重新评估条件**：未来存在成熟流量复制、审计隔离和副作用沙箱时，才重新讨论运行时影子流量；写操作仍不得参与。

## D-039：执行链回滚语义

- **状态**：`ACCEPTED`
- **背景**：新执行器失败后在同一次调用中自动切换旧执行器，会隐藏缺陷，并可能对写操作产生重复副作用。
- **候选方案**：按批次显式回滚且调用路径钉住；只读能力自动降级；所有能力自动降级。
- **最终选择**：每个批次独立显式切回 legacy 路由；调用开始时钉住执行路径，执行中的调用不因配置变化而切换，也不得在失败后自动改走另一执行器重试。
- **选择理由**：每次调用只有一个执行所有者，结果、审计和副作用均可解释，批次级回滚仍能控制故障范围。
- **未选理由**：只读自动降级会掩盖新链可靠性问题并增加审计分支；全链自动降级还会给写操作带来重复副作用风险。
- **影响**：路由配置只影响尚未开始的新调用；第二批回滚不要求回滚已经通过验收的第一批。
- **重新评估条件**：只有调用协议具备跨执行器共享的强幂等证据，并且评估证明自动降级不会隐藏回归时才重新讨论。

## D-040：切换阻断与回滚触发条件

- **状态**：`ACCEPTED`
- **背景**：当前没有可信生产流量和样本量，使用错误率阈值会制造没有统计意义的发布标准。
- **候选方案**：关键不变量零容忍；按错误率或连续失败次数回滚；仅告警后人工综合判断。
- **最终选择**：Manifest 投影、参数 Schema、生命周期、风险门禁、版本钉住、审计或幂等语义任一不一致，都必须阻止切换或立即回滚当前批次；普通业务失败只有在新旧结果不等价时才判定为 Runtime 回归。
- **选择理由**：这些条件是 Skill Runtime 的正确性边界，不应被少量成功样本或平均错误率抵消。
- **未选理由**：错误率阈值缺少真实流量基线；纯人工判断不可重复，也容易容忍安全语义漂移。
- **影响**：验收报告必须区分业务本身失败与新执行链引入的语义差异，并给出触发不变量的具体证据。
- **重新评估条件**：未来拥有稳定生产流量后，可以为非安全类可用性增加统计阈值，但本条列出的不变量仍保持零容忍。

## D-041：Phase 11A 验收门槛

- **状态**：`ACCEPTED`
- **背景**：现有测试全绿和单次演示成功不能单独证明 Manifest 唯一事实源、新旧执行语义等价或高风险写入可安全重放。
- **候选方案**：契约与行为双门禁；仅要求全量测试和闭环演示；追加吞吐、P95 延迟与资源阈值。
- **最终选择**：采用契约与行为双门禁：13 个 Manifest 与 ToolMetadata 投影逐字段一致；旧 ToolRegistry 查询契约保持兼容；4 个 Handler 的新执行链契约测试通过；第一批影子结果等价；`setup_live_session` 的批准、拒绝和幂等重放通过；相关既有回归测试全部通过。
- **选择理由**：该门槛直接覆盖 Phase 11A 的架构目标和主要回归风险，并能在 Fake Adapter 与现有测试条件下重复验证。
- **未选理由**：仅测试加演示缺少专项等价证据；当前 Fake Adapter 和样本无法给出可信容量结论，性能发布门应基于后续真实基线。
- **影响**：任一门禁未通过时 Phase 11A 不得标记完成，也不得开始第二批或进入 Phase 11B。
- **重新评估条件**：Phase 11B 建立稳定执行遥测后，再为后续发布增加延迟、吞吐和资源指标。

## D-042：ToolRegistry 兼容 API 保留期限

- **状态**：`SUPERSEDED`
- **背景**：planner、Hook、policy 和多条 flow 当前直接依赖 ToolRegistry 查询 API，在 Phase 11A 全部强拆会显著扩大回归面，但永久保留又会形成两套公共概念。
- **候选方案**：保留至 Phase 12 验收后重审；Phase 11A 后立即删除；长期作为稳定公共 API 保留。
- **最终选择**：ToolRegistry 作为 SkillManifest 的只读兼容门面保留至 Phase 12 验收，并标记为 deprecated；届时根据直接调用清单单独决定删除计划。
- **选择理由**：允许当前消费者稳定迁移，同时给 Skill Runtime 和 PlanEngine 留出统一调用入口的验证周期。
- **未选理由**：立即删除会把消费者重构混入 Phase 11A；永久保留会让 SkillCatalog 与 ToolRegistry 长期并列为两套公共接口。
- **影响**：兼容期内 ToolRegistry 不得拥有独立注册或写入能力；新增代码默认使用 SkillCatalog 或 SkillExecutor，不得扩大旧 API 调用面。Phase 12A Task 5 后已完成生产调用清单复核，后续退役安排由 D-074 取代。
- **重新评估条件**：本决策已由 D-074 取代；历史上用于解释为什么 Phase 11A 没有立即删除兼容 Facade。

## D-043：四个核心 Skill 的显式输入契约

- **状态**：`ACCEPTED`
- **背景**：代码评审发现旧 ToolRegistry Schema、AgentToolExecutor dispatch 和播前 Graph 数据流并不一致：手卡工具声明单商品但旧执行器生成三张卡，建播工具声明幂等键但旧服务从 trace_id 内部生成。
- **候选方案**：使用不可变完整快照重新定义显式输入；严格保持现有 Manifest Schema；按旧执行器实际行为定义 Skill。
- **最终选择**：控制字段放入可信 `SkillExecutionContext`；`query_products` 无业务参数，`generate_live_plan` 接收商品快照列表，`generate_product_card` 接收单商品快照，`setup_live_session` 接收计划快照。13 个 Skill 首个正式版本均为 `1.0.0`。
- **选择理由**：显式快照使执行依赖可审计、可重放，也为 Phase 12 输入指纹和增量 Replan 提供稳定输入，不依赖执行期间重新查询的隐藏状态。
- **未选理由**：保留旧 Schema 会继续隐藏数据依赖；按旧 dispatch 定义会固化忽略参数、重复查询和隐式重建计划等缺陷。
- **影响**：9 个未迁移工具保持旧投影；4 个核心 Skill 使用受控 Schema 修正，旧调用必须在兼容边界规范化后才能进入 Runtime。
- **重新评估条件**：领域快照过大或包含不可持久化字段时，先引入可版本化引用协议，再讨论从完整快照改为 ID + 版本。

## D-044：行为等价比较的位置

- **状态**：`ACCEPTED`
- **背景**：现有查询、排品和手卡服务方法都会写工具审计，不能被当作真正无副作用能力直接在运行时双算。
- **候选方案**：测试专用比较器；保留测试环境 `SHADOW_COMPARE` 运行时路由；取消新旧行为双算。
- **最终选择**：生产代码的 RoutePolicy 只允许 `LEGACY` 与 `SKILL_RUNTIME`。测试比较器使用相同不可变输入、两套隔离服务栈和独立内存审计 Store 运行新旧路径，再比较规范化业务结果。
- **选择理由**：保留迁移等价证据，同时从生产 Router 移除双执行分支，避免影子调用污染正式审计或被误启用。
- **未选理由**：运行时影子路由需要额外的审计隔离和配置防误用；完全取消双算会削弱迁移前行为证据。
- **影响**：`setup_live_session` 不参与双算；比较器忽略随机 audit_id 和执行时间，只比较业务结果、状态与审计事件语义。
- **重新评估条件**：出现真实流量复制需求且已经具备副作用沙箱、隔离审计和强制环境门禁。

## D-045：高风险 Skill 的可信审批上下文

- **状态**：`CONDITIONAL`
- **背景**：普通 Skill arguments 可能来自 LLM 或外部调用方，不能用其中的 `confirmed=true` 证明 hard-gate 已经获得人工批准。
- **候选方案**：独立可信 ApprovalContext 并兼容旧入口；新 Runtime 只接受 LangGraph interrupt 人审；继续使用普通 confirmed 参数。
- **最终选择**：审批证据属于 `SkillExecutionContext`，不属于业务 arguments。`HUMAN_INTERRUPT` 来源必须包含已校验 decision、operator_id 和 approval_audit_id；旧 `confirmed_setup` 只能由内部 Facade 映射为明确标记的 `TRUSTED_COMPAT` 来源。
- **选择理由**：把权限证据与 LLM 可控参数隔离，同时保持现有播前演示和非 interrupt 测试在兼容期可运行。
- **未选理由**：立即只接受 interrupt 会破坏现有兼容入口；普通 confirmed 参数可以被不可信调用方伪造。
- **影响**：SkillExecutor 在调用 Handler 前验证审批来源；缺少可信证据返回 pending，拒绝证据不得执行 Handler。审批证据与业务 arguments 分离的核心约束继续有效；`TRUSTED_COMPAT` 兼容条款由 D-075 取代。
- **重新评估条件**：只有真实平台出现不同于人工中断和可信事件的授权机制时，才能新增受控授权类型；不得恢复普通参数批准。

## D-046：Skill Runtime 接入播前 Graph 的方式

- **状态**：`ACCEPTED`
- **背景**：现有播前 Graph 使用同步节点和 `PreLiveBusinessServiceProtocol`，直接改成异步节点会连带修改 invoke、checkpoint、interrupt、API 和大量测试。
- **候选方案**：兼容 Facade + 同步桥接器；Graph 节点全面异步化；只迁移 AgentToolExecutor。
- **最终选择**：新增 `RoutedPreLiveBusinessService` 实现现有 Service Protocol，按两个批次选择 legacy 或 Skill Runtime；标准 SkillExecutor 保持异步接口，同步 Graph 通过内部 `SyncSkillExecutorAdapter` 复用同一单次执行核心。
- **选择理由**：可以证明播前完整闭环进入 Runtime，同时保持 Graph 拓扑、checkpoint 和 interrupt 行为不变，把异步图迁移留给有真实收益的后续阶段。
- **未选理由**：全面异步化显著扩大 Phase 11A 回归面；只改 AgentToolExecutor 无法覆盖播前主闭环。
- **影响**：Facade 负责领域对象与 JSON 安全结果转换；同步桥接器不是新的执行实现，不得复制校验、门禁或 Handler dispatch。
- **重新评估条件**：播前 Graph 主调用链整体迁移到 `ainvoke()`，或同步桥接成为可测量性能瓶颈。

## D-047：Skill 参数 Schema 校验实现

- **状态**：`ACCEPTED`
- **背景**：现有 AgentToolExecutor 把 jsonschema 作为可选依赖，库不存在时跳过参数校验，不满足新 Runtime 的 fail-closed 要求。
- **候选方案**：正式依赖 jsonschema；为 13 个 Skill 全量建立 Pydantic 参数模型；延续可选校验。
- **最终选择**：将 `jsonschema` 纳入正式项目依赖，Catalog 启动时按 Draft 2020-12 检查全部 Schema，SkillExecutor 每次调用在 Handler 前执行确定性校验。
- **选择理由**：统一覆盖全部 13 个 Manifest，不必为尚未迁移的 9 个工具提前建立完整输入模型，并消除环境相关的校验跳过。
- **未选理由**：全量 Pydantic 模型会扩大未迁移能力的重构范围；可选校验会造成 fail-open。
- **影响**：缺库、非法 Schema 或参数不匹配均不得调用 Handler；旧执行器的兼容路径也必须使用同一校验结果。
- **重新评估条件**：所有 Skill 都具备稳定 Pydantic 输入模型后，可评估由模型生成 Schema 并减少手写 JSON Schema。

## D-048：执行路由配置生命周期

- **状态**：`ACCEPTED`
- **背景**：两个迁移批次需要独立切换和回滚，但 Phase 11A 已排除数据库动态配置和热加载。
- **候选方案**：Settings 启动配置 + 构造注入；进程内动态切换；仅在代码中硬编码。
- **最终选择**：Settings 提供两个 `LEGACY | SKILL_RUNTIME` 配置，装配时创建不可变 RoutePolicy 并注入 Facade；变更通过重启或重新装配服务实例生效。
- **选择理由**：提供明确、可测试的部署回滚机制，同时不引入热配置的并发可见性、权限和审计问题。
- **未选理由**：动态切换超出首期范围；硬编码无法表达部署级灰度和回滚。
- **影响**：两个配置默认均为 `LEGACY`；调用开始后使用已解析路由，不读取可变全局状态。
- **重新评估条件**：出现分钟级无重启回滚 SLA，并具备受控配置服务、审计和并发一致性要求。

## D-049：AgentToolExecutor 的兼容收敛

- **状态**：`ACCEPTED`
- **背景**：ToolRegistry 投影采用新显式 Schema 后，如果 AgentToolExecutor 继续保留四个工具的旧 dispatch，同一 skill_id 会存在两套参数和执行语义。
- **候选方案**：增加旧参数规范化适配并委托 Runtime；只迁移播前 Graph；删除四个工具的旧入口。
- **最终选择**：保留 AgentToolExecutor 同步外观，在兼容边界把旧参数补全为显式快照并委托统一 Runtime；四个核心工具不再维护独立 dispatch 分支。
- **选择理由**：统一执行语义而不迫使现有 planner、测试和调用方立即切换公共 API。
- **未选理由**：只迁移 Graph 会形成双行为；立即删除旧入口会扩大 Phase 11A 的调用方改造范围。
- **影响**：版本、Schema、生命周期、审批和幂等失败统一映射为 AgentObservation；兼容适配产生的隐藏查询必须记录，不能进入未来 PlanEngine 的显式调用路径。
- **重新评估条件**：旧 Agent 调用全部改用 SkillCall 后，删除参数规范化层和 AgentToolExecutor 的四工具兼容分支。

## D-050：三场景业务范围与当前技术形态

- **状态**：`ACCEPTED`
- **背景**：早期路线图用“播前 Workflow + 播中单体 Agent Harness”概括当前形态，容易让后续执行者误以为项目业务范围只有播中，忽略播前和播后链路。
- **候选方案**：继续使用播中单体 Harness 作为项目定位；改为三场景全链路主播 Agent Runtime；直接承诺三场景多 Agent。
- **最终选择**：项目定位修正为面向淘宝直播播前、播中、播后三场景的全链路主播 Agent Runtime 项目；当前实现形态分别为播前偏 Workflow / Graph、播中已有单体 Agent Harness、播后偏 Replay / Evaluation / 复盘流程。
- **选择理由**：该表述同时保留代码事实和业务范围，不把当前最成熟的播中 Harness 误当成项目全部，也不提前承诺尚未评估的多 Agent 架构。
- **未选理由**：继续使用旧表述会压窄项目叙事；直接承诺三场景多 Agent 会违反“不为 Agent 数量而 Agent”的原则。
- **影响**：路线图、恢复提示词和后续 Phase 13 讨论都必须以三场景为边界；历史文档中的旧表述只能作为阶段性判断引用。
- **重新评估条件**：项目正式缩减为单一直播阶段，或新增超出播前、播中、播后的长期业务场景。

## D-051：Agent、Skill、Tool、PlanEngine 与 Orchestrator 分层边界

- **状态**：`ACCEPTED`
- **背景**：多轮讨论中曾混用业务场景、Agent 数量、Skill 能力和 Tool 执行入口，容易把“播前三场景”机械映射成“三个 Agent”。
- **候选方案**：按业务场景命名 Agent；按技术分层定义职责；继续沿用 ToolRegistry 和 skills 目录命名。
- **最终选择**：固定五层边界：Tool 是底层动作和外部副作用；Skill 是可治理、可版本化、可审计的业务能力单元；Agent 是有目标、上下文、工具选择权和局部推理循环的决策者；PlanEngine 是确定性 DAG 调度、恢复和 Replan 组件；Orchestrator 是确定性协调与路由组件，不默认包装成 Agent。
- **选择理由**：该分层能解释为什么 Phase 11A 先做 Skill Runtime、Phase 12 做 PlanEngine、Phase 13 再评估 Agent，而不是先堆 Agent 名称。
- **未选理由**：按业务场景命名 Agent 会把“播前、播中、播后”等同于三个 Agent；继续沿用现有目录命名会把普通业务模块误认为真正 Skill Runtime。
- **影响**：新增设计必须先说明它属于 Tool、Skill、Agent、PlanEngine 还是 Orchestrator；Orchestrator 和 PlanEngine 仍默认是确定性组件。
- **重新评估条件**：后续评估证明概率式 Orchestrator 或 Agent 化 PlanEngine 在安全、恢复和成本上均优于确定性基线。

## D-052：Phase 13 三场景 Agent 化评估范围

- **状态**：`ACCEPTED`
- **背景**：D-019 只覆盖播中售罄场景与 LiveOpsAgent 对照，无法代表播前规划和播后复盘 / 记忆沉淀中的 Agent 化可能性。
- **候选方案**：维持只评估 LiveOpsAgent；Phase 13 升级为三场景 Agent 化评估；当前就承诺 PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent 全部落地。
- **最终选择**：Phase 13 升级为三场景 Agent 化评估与试点，候选包括 PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent；每个候选都必须先有确定性基线，再用相同 Skill、Hook、权限和评估样本对照。
- **选择理由**：三场景评估能体现项目全链路 Agent Runtime 的技术深度，同时保留评估驱动取舍，不把候选 Agent 变成默认架构。
- **未选理由**：只评估 LiveOpsAgent 会继续压窄项目范围；直接承诺三个 Agent 会重新落入“为了多 Agent 而多 Agent”的问题。
- **影响**：PlannerAgent、LiveOpsAgent 和 ReviewMemoryAgent 都只是评估候选。默认沿用 D-020 的严重安全违规为 0、成功率或恢复类指标提升、延迟和 Token 成本增幅不超过 20% 的保留门槛；不适用时必须新增决策定义指标。
- **重新评估条件**：Phase 12B 验收数据表明某个场景没有足够复杂度、样本量或收益空间时，可取消对应 Agent 候选。

## D-053：Phase 11A 审批来源与 Schema 闭合安全修正

- **状态**：`ACCEPTED`
- **背景**：最终代码审查发现，完整字段的 `HUMAN_INTERRUPT` 可被直接构造为可信审批，且 9 个未迁移 Manifest 的根 Schema 默认接受额外参数。这两项都与 Phase 11A 的 fail-closed 安全边界冲突。
- **候选方案**：只检查审批字段存在性并保持旧 Schema；在 Executor 中临时补充额外字段检查；使用内部审批工厂并让全部根 Schema 显式拒绝额外字段。
- **最终选择**：`HUMAN_INTERRUPT` 只能由 Graph 在审批响应校验、审批审计写入成功后调用内部工厂构造；13 个 Manifest 的根 Schema 全部显式 `additionalProperties: false`。9 个旧 Schema 的该安全收紧是 D-035 的唯一受控投影例外，并更新冻结哈希。
- **选择理由**：字段形状不是来源证明，高风险门禁不能接受调用方伪造的证据；Schema 校验若放过未知字段，会让治理边界被静默绕过。
- **未选理由**：仅在 Executor 临时补充会使 Manifest 不再是完整契约；维持旧 Schema 或仅校验字段完整性均为 fail-open。
- **影响**：所有人工审批测试和 Demo 必须使用受控工厂；任何新增 Manifest 根 Schema 都必须拒绝未声明字段；Schema 快照测试必须覆盖 13 个 Skill。
- **重新评估条件**：未来引入可验证的审批签名或独立 Approval Store 后，可将内部工厂替换为对签名或审计事实的验证，但不得降低当前 fail-closed 行为。

## D-054：Phase 11B 业务域 Adapter 边界

- **状态**：`ACCEPTED`
- **背景**：Phase 11A 的四个核心 Handler 仍直接依赖播前服务，而余下九个能力尚未进入 Runtime。若只为遗留 Handler 新增 Adapter，会留下两套平台、超时和错误边界。
- **候选方案**：只为余下九个 Handler 建 Adapter；使用单一巨型平台 Adapter；按商品与价格、直播会话、播中运营拆分业务域 Port。
- **最终选择**：使用商品与价格、直播会话、播中运营三个业务域 Port。全部 13 个 Skill 统一使用 Runtime 的 deadline、失败事实、尝试审计和路由契约；纯确定性排品、手卡、文案与聚合能力不伪装成外部调用，但其平台状态输入只可经对应 Port 获得。
- **选择理由**：既避免巨型接口和两套执行语义，也不为没有外部状态交互的确定性逻辑虚构平台 API。
- **未选理由**：只迁移九个 Handler 不符合统一平台契约；单一 Adapter 会混合价格、建播和播中状态职责。
- **影响**：Phase 11B 会重构四个既有 Handler 的依赖边界，但不改变其公开业务输入或 Phase 11A Graph 外观。
- **重新评估条件**：真实平台 API 显示三个 Port 的资源所有权或认证边界明显不一致时，可在不改变 Skill 契约的前提下调整 Port 划分。

## D-055：有状态 Fake Adapter 与 Fixture 生命周期

- **状态**：`ACCEPTED`
- **背景**：固定返回 Fixture 无法验证价格版本冲突、建播重放、售罄状态和副作用未知等跨调用语义；直接接入真实淘宝 API 不属于本周期验收范围。
- **候选方案**：固定响应 Fixture；实例级内存状态加版本化 Fixture；复用 PostgreSQL 或全局单例状态。
- **最终选择**：Fake Adapter 采用实例级、可重置的内存状态，初始直播间、商品、会话和故障脚本来自版本化 Fixture。故障采用按操作、资源键和调用序号匹配的声明式脚本。
- **选择理由**：能够确定性复放状态变化和错误顺序，同时不把平台模拟与 PlanStore、新数据库迁移或测试间全局污染耦合。
- **未选理由**：固定 Fixture 无法证明状态一致性；PostgreSQL 和全局单例会扩大阶段范围或污染测试。
- **影响**：每个测试和 Demo 必须显式装配独立 Fake 实例；随机故障不得作为验收输入。
- **重新评估条件**：获得稳定的正式平台 Sandbox 后，可比较 Sandbox 契约测试与 Fake Fixture 的偏差，但不以真实凭据替代本地回归。

## D-056：Deadline、异步 Adapter 与同步桥接

- **状态**：`ACCEPTED`
- **背景**：各层各自使用 timeout 会累积超时预算；同步函数放入线程池后，即使上层超时也可能继续产生外部副作用。
- **候选方案**：调用方绝对 deadline 加原生异步 Adapter；仅 Manifest timeout；同步线程池包装全部 Adapter。
- **最终选择**：可信执行上下文携带不可延长的绝对 `deadline_at`，Manifest 只声明单次尝试上限，Executor 和 Adapter 使用两者剩余时间的较小值。Adapter 统一提供原生 async 单次尝试与协作取消；现有同步 Graph/Harness 仅通过受限桥接等待同一核心，不新增公共同步 Runtime API。
- **选择理由**：避免嵌套 timeout 透支预算，并使取消语义能够与后续 PlanEngine 的租约和并发控制对齐。
- **未选理由**：只有 Manifest timeout 无法约束全链路耗时；线程池包装不能可靠停止已发出的外部写操作。
- **影响**：发送前 deadline 到期记录未发送失败；发送后超时或断连且无法确认结果时必须返回 `SIDE_EFFECT_UNKNOWN`，不得伪装为可安全重试。
- **重新评估条件**：某个真实 SDK 无法协作取消时，必须先证明其副作用查询或幂等能力，再决定是否允许接入。

## D-057：外部失败事实边界

- **状态**：`ACCEPTED`
- **背景**：Phase 11A 仍会把多数 Handler 异常压缩为 `HANDLER_FAILED`，这不足以表达限流、版本冲突和副作用未知，且会迫使后续 PlanEngine 解析自由文本。
- **候选方案**：Adapter 返回 FailureFact；Adapter 抛分类型异常；仅统一错误码。
- **最终选择**：Adapter 返回 `AdapterSuccess` 或受控 `FailureFact`。FailureFact 只描述发生事实，使用 D-023 的八类失败分类，并可携带稳定外部码、`retry_after`、`attempt_id` 和副作用确认状态；不携带恢复动作。
- **选择理由**：结果式契约可直接做组合和回归测试，并保留 FailurePolicy 对恢复动作的唯一决策权。
- **未选理由**：异常路径不利于普通契约测试；只统一错误码会丢失限流、发送边界和对账所需证据。
- **影响**：Adapter、Handler、Executor 和客户端均不得隐藏重试；Phase 11B 只传播 FailureFact，不实现自动重试、Replan 或人工 Command Ledger。
- **重新评估条件**：出现真实平台错误无法由既有八类描述时，先补充失败证据并按 D-023 的重审规则讨论，不为单一供应商新增顶层类别。

## D-058：独立 Attempt Store 与单一 Operation 重放

- **状态**：`ACCEPTED`
- **背景**：外部写调用在请求发送后超时，只有成功后写一条工具审计无法证明是否已经产生副作用；直接扩展既有 `tool_call_audit` 会改变已验证的幂等唯一键语义。
- **候选方案**：独立 Attempt Store；扩展 `tool_call_audit`；只在 Fake 内存中记录。
- **最终选择**：新增独立 Attempt Store。Runtime 以 `skill_id + version + room_id + idempotency_key` 建立唯一 Operation；首次调用先持久化不可变意图和 `attempt_id`，再调用 Adapter，最后写成功、确定失败或副作用未知终态。重复或并发调用只返回原 Operation/Attempt，绝不产生第二次 Adapter 请求。
- **选择理由**：同时保留外部执行证据、避免重复副作用，并保持现有工具审计和重放语义兼容。
- **未选理由**：扩表会扩大 Phase 11A 审计回归面；只存内存无法处理重启后的对账与证据回放。
- **影响**：`tool_call_audit` 继续保存兼容结果审计，且可用 `attempt_id` 关联 Attempt Store；副作用未知的重放必须 fail-closed，等待未来对账协议处理。
- **重新评估条件**：Phase 12 的 PlanStore 已经具有等价且经过验证的 Operation/Attempt 证据模型时，可评估合并查询视图，但不得丢失独立事实或改变写入顺序。

## D-059：Phase 11B 三批迁移与路由

- **状态**：`ACCEPTED`
- **背景**：一次性把十三个能力切到新平台契约会把低风险读取、可确认状态变更和高风险改价的回归风险混在一起。
- **候选方案**：两批按播前/播中划分；一次性迁移；按风险和副作用分三批并使用启动冻结批次路由。
- **最终选择**：采用三项 `LEGACY | SKILL_RUNTIME` 启动冻结路由。批次一为 `query_products`、`generate_live_plan`、`generate_product_card`、`suggest_price_change`、`create_live_plan_draft`、`recommend_backup_product`、`generate_on_live_prompt`、`aggregate_danmaku_questions`、`generate_danmaku_reply`、`on_live_context_collect`；批次二为 `setup_live_session`、`handle_sold_out_event`；批次三为 `set_product_price`。
- **选择理由**：先验证低风险和确定性能力，再验证可确认的建播/售罄状态变化，最后单独承受价格写入的最高风险；每批都能部署级回滚。
- **未选理由**：按场景划分会让同批风险差异过大；一次性切换不利于定位和回滚。
- **影响**：路由在进程装配时冻结，调用开始时钉住对应批次；回滚只影响新调用，Runtime 失败绝不自动回退 Legacy。写操作只允许在隔离 Fake 中进行新旧结果比较。
- **重新评估条件**：真实运行证据证明某批次吞吐、风险或依赖明显不适合当前组合时，可新增决策调整批次，但不得把动态逐 Skill 开关作为默认方案。

## D-060：不可达 switch_product 分支的清理边界

- **状态**：`ACCEPTED`
- **背景**：`AgentToolExecutor` 保留 `switch_product` legacy dispatch，但该名称不在 13 个 Manifest、ToolRegistry 投影、Runtime 测试或实际调用入口中，形成未治理且不可达的历史代码。
- **候选方案**：保留并标记弃用；补为第十四个 Skill；删除不可达 dispatch，保留 Reducer 领域原语。
- **最终选择**：删除 `AgentToolExecutor` 中不可达的 `switch_product` dispatch，不把它纳入 Phase 11B 的十三个 Skill。Reducer 中的切品领域原语保留；未来若重新暴露切品能力，必须先新增 Manifest、风险门禁、Adapter 契约和独立决策。
- **选择理由**：消除未受 Catalog 治理的误导性路径，同时不在本阶段无证据地扩大工具范围。
- **未选理由**：保留死代码会让后续执行者误判迁移范围；直接新增第十四个 Skill 超出已接受的迁移和验收边界。
- **影响**：播后锁对领域原语的保护不受影响；Phase 11B 测试应证明 Executor 只暴露 Catalog 的十三个能力。
- **重新评估条件**：业务需求确实要求播中切品，且能够定义可信状态、审批、幂等和平台副作用契约时。

## D-061：Phase 11B Skill 版本规则

- **状态**：`ACCEPTED`
- **背景**：Adapter、审计和路由的内部迁移不一定改变调用契约，但参数、结果、幂等或副作用承诺发生变化时，继续沿用版本会损害后续恢复和回放的精确钉住能力。
- **候选方案**：全部统一升至 1.1.0；版本永远不变；只有公开契约变化才升级对应 Skill。
- **最终选择**：纯内部 Adapter、审计和实现重构保持当前 `1.0.0`。参数 Schema、输出语义、幂等/副作用承诺或门禁语义变化时，受影响 Skill 才升级到 `1.1.0`，并明确旧版本的受控拒绝或兼容路径。
- **选择理由**：让版本代表可观察契约而不是代码重构批次，维持 D-008 的单活精确版本语义。
- **未选理由**：全部升级会制造无意义的恢复不兼容；版本永远不变会使精确钉住失去价值。
- **影响**：Phase 11B Design 和后续 Implementation Plan 必须逐项记录是否发生契约变化，不能隐式升级或隐式兼容。
- **重新评估条件**：所有 Skill 形成独立发布制品或外部消费者需要语义版本范围协商时。

## D-062：Phase 11B 验收门槛

- **状态**：`ACCEPTED`
- **背景**：仅证明 Handler 能返回成功，无法证明 deadline、失败事实、外部副作用和批次回滚是否符合统一执行契约。
- **候选方案**：只跑全量测试；只做 Fake Adapter 单测；契约、状态、失败、迁移和系统回归共同验收。
- **最终选择**：采用共同验收：十三个 Handler/Port 装配；可重置 Fake 与声明式故障；deadline、限流、版本冲突和副作用未知传播；意图先写与单一 Operation 幂等；每批独立路由/回滚；播前 Graph、播中 Harness、Replay/Evaluation 回归；成功建播、售罄、限流、版本冲突、deadline、副作用未知六种无外部依赖 Demo；专项、相关与默认全量测试、`git diff --check` 和编码检查。
- **选择理由**：该门槛同时覆盖 Runtime、平台契约、生产约束和三场景已有链路，且不把尚未开始的 PlanEngine 和真实淘宝 API 作为验收条件。
- **未选理由**：单独全量或 Fake 单测无法证明写入顺序、运行时路由和系统集成行为。
- **影响**：任一关键不变量失败，Phase 11B 不得生成 Acceptance 或进入 Phase 12A。
- **重新评估条件**：获得真实 Sandbox、稳定延迟基线或正式平台限流契约后，可增加性能和契约兼容门槛，但不得弱化当前安全与幂等门禁。

## D-063：LiveOperationsPort 只读商品上下文解析

- **状态**：`ACCEPTED`
- **背景**：Phase 11B Task 5 准备迁移批次一 Handler 时发现，`recommend_backup_product` 只有 `room_id` 与 `sold_out_product_id`，`generate_on_live_prompt` 只有售罄 / 备选商品 ID；但既有确定性领域函数需要完整 `Product` 快照。现有 `LiveOperationsPort` 只有售罄写入和播中上下文读取，无法在不绕过 Port 的情况下提供可信商品状态。
- **候选方案**：直接读取旧 Graph State 或旧服务；伪造最小商品对象；新增第十四个 Skill 查询商品上下文；给 `LiveOperationsPort` 增加只读商品上下文解析方法。
- **最终选择**：给 `LiveOperationsPort` 增加 `resolve_product_context(request)`，返回 `sold_out_product` 与可选 `backup_product` 的可信快照。该方法只读、不产生副作用、不新增 Skill、不修改既有 Skill 公开参数 Schema，也不升级当前 `1.0.0` Skill 版本。
- **选择理由**：它补齐了批次一迁移所需的平台状态读取边界，同时保持 D-054 的三 Port 架构、D-059 的十三个 Skill 迁移范围和 D-061 的版本规则。Handler 仍复用确定性领域函数，只是把可信输入来源固定到 Port。
- **未选理由**：直接读取旧 Graph State 或旧服务会绕过冻结的 Port 边界；伪造商品对象会制造不可审计的业务事实；新增第十四个 Skill 会扩大 Phase 11B 范围并破坏既有 Catalog 门禁。
- **影响**：FakeLiveCommercePlatform 必须实现同名只读方法；`recommend_backup_product` 与 `generate_on_live_prompt` 的 Runtime Handler 必须经该 Port 获取商品快照，禁止隐式 Legacy fallback。若售罄商品不存在，应返回结构化 `INVALID_INPUT` 事实；只读解析不得修改库存、版本、价格或会话状态。
- **重新评估条件**：真实平台提供独立且更细粒度的商品上下文查询 API，或 Phase 12 PlanEngine 需要把商品上下文解析拆为独立可计划节点时，可重新评估是否引入新的 Manifest 和版本。

## D-064：set_product_price 资源版本契约与审批入口边界

- **状态**：`ACCEPTED`
- **背景**：Phase 11B Task 8 实施前审核发现，`ProductPricingPort.set_price` 已按商品资源版本执行 CAS，但 `set_product_price@1.0.0` 的 Manifest 只声明 `product_id` 与 `price`，调用方无法显式提供 `expected_version`。同时，批次三若让 AgentToolExecutor 接受批准参数，会把不可信兼容入口扩大为高风险审批来源，并混淆 Skill 版本错误与商品资源版本冲突。
- **候选方案**：显式参数 + `1.1.0`，即把 `expected_version` 加入业务 arguments 并升级单活版本；资源版本放 `SkillExecutionContext`，保持业务 Schema 与 `1.0.0`；暂缓批次三，等待未来 Graph / Facade 审批入口一起设计。
- **最终选择**：`set_product_price` 从单活 `1.0.0` 升级为单活 `1.1.0`；业务 arguments 固定为 `product_id: string`、`price: string`、`expected_version: integer` 且最小值为 `1`，根对象 `additionalProperties: false`。`idempotency_key`、`approval`、`room_id`、`trace_id`、deadline 和 route 只属于 `SkillExecutionContext`。调用旧 `set_product_price@1.0.0` 时，Runtime 必须在 Handler 与 Attempt 创建前返回 `SkillErrorCode.VERSION_MISMATCH`；调用 `1.1.0` 后发现商品 `expected_version` 过期时，由 Adapter 返回 `FailureFact`，其 `category=FailureCategory.VERSION_CONFLICT`，两者不得互换。AgentToolExecutor 不新增 `approval` 参数或 `execute_approved` 方法，只在构造时从 Catalog 冻结精确版本，并仅对 `set_product_price` 把兼容 arguments 中的 `idempotency_key` 搬入 Context；其 `approval` 保持 `None`，因此有效高风险调用只返回 `pending`，不创建 Attempt，也不调用 Port。可信批准路径本阶段只通过内部 `SkillCall`、受控 `ApprovalContext` 与 Fake Platform 集成测试证明，未来真实 Graph / Facade 接入另行设计。Runtime 失败不 fallback Legacy，批次三回滚仍通过启动冻结的 `LEGACY` 路由完成。
- **选择理由**：`expected_version` 是改价 CAS 的可持久化业务前置条件，显式放入 arguments 才能参与 Schema 校验、审计、重放和输入摘要；新增必填参数是可观察契约变化，按 D-061 升级到 `1.1.0` 能保持精确版本钉住。AgentToolExecutor 保持无批准能力，可以继续复用既有兼容入口而不扩大 Phase 11A 已验收的审批信任边界。
- **未选理由**：把资源版本放入 Context 会把业务并发条件混入跨 Skill 控制字段，并使重放证据依赖隐藏状态；继续使用 `1.0.0` 会违反 D-061 的可观察契约版本规则；暂缓批次三会在 Port、Fake 和单次尝试基础已经具备时阻断本阶段闭环，却不能替代未来真实批准入口的独立设计。
- **影响**：D-061 继续有效，本决策是其在改价契约上的具体应用。D-035 所述 9 个未迁移工具逐字段冻结是 Phase 11A 的历史约束；D-064 生效后 `set_product_price` 退出该冻结集合，其余 8 个仍严格保持。D-043 所述“13 个首版均为 `1.0.0`”继续作为历史事实保留。Catalog 同时只注册 12 个 `1.0.0` 与一个 `set_product_price@1.1.0`；ToolRegistry 不增加 version 字段，只投影 `1.1.0` 的新 Schema。AgentToolExecutor 只能证明未批准调用返回 `pending`，不能成为批准入口。本阶段不实现重试、PlanEngine、真实淘宝 API、新 Graph 或多 Agent。
- **重新评估条件**：未来需要真实 Graph / Facade 承接高风险改价批准、外部消费者需要版本范围协商，或平台 CAS 不再使用整数资源版本时，必须新增决策重新定义入口、版本兼容和资源冲突语义；在此之前不得扩大 AgentToolExecutor 的审批能力。

## D-065：Phase 12A 首期垂直边界

- **状态**：`ACCEPTED`
- **背景**：D-011 选择“手卡生成 + 售罄抢占”作为 PlanEngine 首期价值场景，但把商品查询、排品、建播审批和售罄事件一次性纳入会让 PlanStore、调度、checkpoint 和抢占问题彼此遮蔽。
- **候选方案**：冻结排品后的手卡批次；查询到手卡的完整播前链；包含建播审批的完整链。
- **最终选择**：Phase 12A 只接管冻结 `LivePlanDraft` 与商品快照后的前三张单商品手卡。查询、排品和建播保留既有路径；售罄抢占、紧急 DAG 与增量 Replan 延后到 Phase 12B。
- **选择理由**：先以最小真实 DAG 验证不可变版本、并发、失败恢复和一致性协议，避免把高风险审批和事件抢占混入基础设施验收。
- **未选理由**：完整播前链需要运行时动态展开和更多输入依赖；建播审批会扩大人审/副作用变量，无法集中验证 PlanEngine 基线。
- **影响**：Phase 12A 的规划输入必须是不可变排品和商品快照；默认播前 Graph 不改变，PlanEngine 只在显式路由下处理手卡批次。
- **重新评估条件**：冻结排品无法提供生成手卡所需的完整商品快照，或首期 DAG 不能证明并发与恢复价值时，先补齐输入契约再扩大边界。

## D-066：候选 DAG 与 ProposalProvider 边界

- **状态**：`ACCEPTED`
- **背景**：D-010 要求 LLM 只提出候选计划，不能执行或控制恢复；但冻结排品后的手卡批次没有足够业务分歧，强行接入 LLM 只会制造形式化规划。
- **候选方案**：受限类型化 DAG + 固定 Provider；业务子目标由编译器展开；完整执行 DAG 或强制 LLM Provider。
- **最终选择**：保留 `PlanProposalProvider` Port，Phase 12A 只使用版本化 `CanonicalCardBatchProposalProvider` 和 Fixture 生成 `PREPARE_CARD_BATCH -> generate_product_card x N -> COLLECT_CARD_RESULTS` 的规范 DAG。候选只声明节点、依赖和输入绑定；LLM Provider 延后。
- **选择理由**：保留未来可替换的规划边界，同时避免让无价值的概率输出控制固定业务骨架。控制参数继续由确定性 PlanEngine 注入。
- **未选理由**：子目标编译器会隐藏 DAG 契约；完整执行 DAG 会把版本、超时、资源和恢复权限错误交给 LLM；现在强制 LLM Provider 不能证明业务收益。
- **影响**：Phase 12A 不实现 LLM Provider、fallback 或生产双执行；候选非法时拒绝创建 PlanRun，不走隐式模板替代。
- **重新评估条件**：Phase 12B 或后续场景出现可量化的计划分歧，例如售罄后的替代商品/子图选择，且可在 Golden Dataset 中验证时再新增 LLM Provider 决策。

## D-067：PlanStore 物理模型

- **状态**：`ACCEPTED`
- **背景**：D-013 已确定 PlanStore 是权威事实源，但首期仍需选择能支持版本、节点依赖、NodeRun 并发和审计查询的物理表达。
- **候选方案**：关系行 + JSONB 快照；整张计划 JSONB；仅追加事件流。
- **最终选择**：使用 `plan_runs`、`plan_versions`、`plan_nodes`、`plan_node_dependencies`、`node_runs` 与 `plan_commands` 关系表；完整 DAG、输入输出、FailureFact 和审计扩展信息保存 JSONB。
- **选择理由**：关系列支持索引、唯一约束、READY 查询、lease 和条件更新；JSONB 保留完整快照和可扩展业务证据，不需要首期实现事件溯源投影。
- **未选理由**：整张 JSONB 难以安全处理节点并发更新；仅事件流会把查询和恢复投影复杂度提前带入首期。
- **影响**：PlanStore 与 PostgresSaver 独立连接和事务；不直接操作官方 checkpoint 表。所有版本和节点状态变化必须由 PlanStore 的公开接口完成。
- **重新评估条件**：计划规模证明显式依赖边或 JSONB 快照造成可测量瓶颈，或需要跨系统事件溯源时再评估。

## D-068：节点身份与输入绑定

- **状态**：`ACCEPTED`
- **背景**：D-014 与 D-016 要求版本不可变、输入可指纹化、未来 Replan 可说明复用/失效来源；自由路径表达式或跨版本复用同一节点 ID 会破坏这些证据。
- **候选方案**：版本内节点 ID + 稳定逻辑键 + 受限类型化绑定；跨版本复用同一节点 ID；自然键或通用 JSONPath。
- **最终选择**：每个 PlanVersion 创建新的 `node_id`，同时保存稳定 `logical_key`；未来复用和失效通过显式旧节点来源关联表示。输入只允许 `PLAN_INPUT`、`NODE_OUTPUT` 与 Schema 校验后的 `LITERAL`，派发前物化不可变输入快照并计算指纹。
- **选择理由**：版本快照不会被后续状态污染，依赖和输入来源可在创建时校验，输入指纹可用于未来最小失效。
- **未选理由**：跨版本复用 ID 会混淆旧证据；自然键不能表达同商品不同角色；JSONPath/表达式难以做静态安全校验和重命名保护。
- **影响**：NodeRun 必须保存实际输入快照与指纹；绑定目标不存在、未声明依赖、输出未 JSON 安全或不符合目标 Schema 时，节点不能进入 READY。
- **重新评估条件**：实际业务需要结构化转换且不能由确定性控制节点完成时，新增受限转换类型，不开放通用表达式执行。

## D-069：资源锁元数据与 NodeRun 审计粒度

- **状态**：`ACCEPTED`
- **背景**：D-017 要求资源锁，D-031 要求 fencing；现有 SkillManifest 没有通用资源键字段，且只依赖 Phase 11B Skill Attempt 无法记录控制节点、claim 和调度失败历史。
- **候选方案**：Capability Profile 确定性资源解析器 + 每次独立 NodeRun；扩展全部 Manifest；让 LLM 提供资源键或只复用 Skill Attempt。
- **最终选择**：Phase 12A 以 `PlanCapabilityProfile` 和 `ResourceKeyResolver` 从可信 room、商品快照和节点类型确定资源键，候选不可覆盖。每次 claim/执行创建独立 NodeRun，可选关联 Skill Attempt。
- **选择理由**：无需扩大已经稳定的 13 个 Manifest；Worker、控制节点和 Skill 调用都有完整的 lease、fencing、重试和结果证据。
- **未选理由**：现在迁移全部 Manifest 会扩大范围；LLM 提供资源键会削弱并发安全；只有 Skill Attempt 会遗漏 PlanEngine 调度层事实。
- **影响**：首期手卡节点资源键固定为 `room:{room_id}:product:{product_id}`，控制节点无外部资源锁；NodeRun 终态更新必须匹配当前 fencing token。
- **重新评估条件**：Phase 12B 新能力需要跨商品或直播间级锁时，扩展 Capability Profile，不在未经评估时修改全局 Manifest。

## D-070：Worker、Graph 路由与查询边界

- **状态**：`ACCEPTED`
- **背景**：把调度循环写入 LangGraph 会重耦合业务图与 PlanStore，直接替换默认播前路径又会放大首期风险；同时 PlanRun 证据必须可被 Graph、Replay 和测试查询。
- **候选方案**：独立无状态 Worker + 启动冻结可选路由 + 领域服务；仅进程内服务；LangGraph 主图调度或直接替换现有节点。
- **最终选择**：Plan API/Graph 只创建或恢复 PlanRun，`PlanWorker` 以 `FOR UPDATE SKIP LOCKED`、lease 和 fencing 执行节点。新增默认 `LEGACY` 的 `PlanExecutionRoute`，仅显式 `PLAN_ENGINE` 才接管手卡节点；提供 `PlanQueryService`，本期不提供 HTTP/UI。
- **选择理由**：Worker 可在测试内联或生产独立进程运行，PlanStore 保持唯一协调点；默认 Legacy 保留 Phase 11B 已验收行为，路由切换不影响在途调用。
- **未选理由**：仅进程内模型弱化崩溃恢复和多 Worker 边界；Graph 主图调度违反 D-010；直接替换或生产双执行会扩大兼容和审计风险。
- **影响**：路由在进程装配时冻结，Runtime 失败不 fallback Legacy；checkpoint 只保存计划引用和控制位置，查询通过 PlanQueryService 读取 PlanStore。
- **重新评估条件**：Phase 12A 验收显示独立 Worker 的装配复杂度无法被当前服务生命周期承载，或需要公开人工查询时再讨论 HTTP/API。

## D-071：Command Ledger 与批次失败收敛

- **状态**：`ACCEPTED`
- **背景**：D-033 要求人工命令幂等，但首期只读手卡 DAG 不会自然触发审批；同时手卡批次必须避免把“部分成功”误报为业务成功。
- **候选方案**：现在实现通用 Command Ledger 且整批失败；仅实现对账命令；允许部分成功或首错强制取消。
- **最终选择**：现在实现 `APPROVE`、`REJECT`、`RECONCILE`、`RESUME` 四类通用命令，通过合成节点测试。任一不可恢复节点失败后停止派发新节点，让在途节点协作式收敛，PlanRun 最终 `FAILED`，但保留全部成功结果和 NodeRun 证据。
- **选择理由**：满足未来高风险节点的命令基础设施要求，也保证失败批次不被错误作为完整手卡包消费；保留结果可供 Phase 12B Replan 复用。
- **未选理由**：只做对账会让 D-033 的协议残缺；部分成功需要新的业务消费语义；强制取消违反 D-012 的副作用安全原则。
- **影响**：命令必须携带 `command_id`、`expected_plan_version`、`expected_node_status`；审批和对账 TTL 分别为 10/30 分钟，超时 fail-closed。Phase 12A 不创建 Replan 版本。
- **重新评估条件**：真实业务确认可安全消费不完整手卡包，或出现新的人工动作类别时，先新增状态/消费语义决策再改变终态。

## D-072：Phase 12A 验收门槛

- **状态**：`ACCEPTED`
- **背景**：仅用内存替身无法证明 PostgreSQL 并发 claim、lease、fencing 与 PlanStore/checkpoint 的有序恢复；Phase 12A 需要一套不依赖真实外部平台的可重复验收证据。
- **候选方案**：单元测试 + 真实 PostgreSQL/PostgresSaver；仅内存替身；仅端到端 PostgreSQL。
- **最终选择**：单元测试覆盖模型和策略，真实 PostgreSQL 与官方 PostgresSaver 集成测试覆盖并发和一致性；Demo 使用 Fixture、Fake Adapter 与隔离 Store。
- **选择理由**：既能快速定位模型/状态机问题，也能证明真实数据库语义和官方 checkpoint 边界。
- **未选理由**：仅内存替身不能证明 SQL 并发和写入顺序；只端到端会牺牲快速、精确的失败定位。
- **影响**：验收必须包含 DAG/绑定/状态/FailurePolicy/Command 单元测试、PostgreSQL  lease/fencing/恢复集成测试、Graph 路由回归和五场景无外部依赖 Demo。真实 LLM、淘宝 API、Kafka 与 Phase 12B 功能不进入验收。
- **重新评估条件**：CI 无法提供 PostgreSQL 时，先补齐可重复数据库服务，不以全内存测试替代一致性验收。

## D-073：连续实施的文档记忆与自动技术门禁

- **状态**：`ACCEPTED`
- **背景**：Phase 12A-14 跨越大量任务和多次上下文压缩，只有长路线图无法恢复当前子步骤、最近测试和偏差原因；用户未来会单独授权长时间无人监控实施。
- **候选方案**：总控计划 + 实时执行状态 + 阶段三件套；只依赖三个既有 worklog；每次压缩后重新分析仓库。
- **最终选择**：新增全程总控计划和固定格式 `continuous_execution_state.md`，Task 开始、RED、GREEN、审查、验证和推送节点实时写盘；阶段技术门禁通过后，在正式连续实施已获授权的前提下自动进入下一阶段。
- **选择理由**：实时游标保持短小，长期决策和历史仍由 Design/Plan/Acceptance 与 worklog 承担，能够从磁盘和 Git 精确恢复而不依赖对话记忆。
- **未选理由**：既有 worklog 已较长且没有唯一当前游标；重新分析会重复消耗上下文并可能改变已经批准的选择。
- **影响**：本轮只持久化文档，实时状态固定为等待正式实施授权；未来每个 Task 与状态更新一同提交，不向 main 推送红灯或半成品。
- **重新评估条件**：实时状态长期与 Git 不一致或维护成本高于恢复收益时，可改为机器可校验格式，但不能取消持久恢复游标。

## D-074：ToolRegistry 的分阶段退役

- **状态**：`ACCEPTED`
- **背景**：Phase 12A Task 5 后，ToolRegistry 仍被 Hook、Policy、Planner、Flow 和 Executor 消费，但它只是不含独立数据的 Manifest 投影；永久保留会维持两套公共概念，立即删除又会扩大当前验收面。
- **候选方案**：SkillPolicyView 分阶段迁移并在 Phase 14 删除；长期保留只读 Facade；并入 Phase 12A 立即删除。
- **最终选择**：Phase 12B 新增窄化只读 SkillPolicyView 并迁移全部生产消费者；Phase 14 删除 ToolRegistry 公共 Facade。新增代码不得依赖 ToolRegistry。
- **选择理由**：先稳定迁移查询职责，再在发布门禁下删除兼容 API，既降低当前风险又保证最终架构只有 Catalog 一个事实源。
- **未选理由**：长期 Facade 会使 Catalog/Registry 概念并存；立即删除会把无关大范围回归混入 Phase 12A checkpoint 和 Graph 验收。
- **影响**：D-042 标记为 SUPERSEDED；Phase 12B Acceptance 必须给出生产调用清单，Phase 14 删除 Facade 后全仓生产调用必须为 0。
- **重新评估条件**：发现仓库外稳定消费者且无法在 Phase 14 迁移时，可以延长显式兼容周期，但不得恢复独立元数据注册。

## D-075：TRUSTED_COMPAT 的退役时间

- **状态**：`ACCEPTED`
- **背景**：播前 Graph 已能通过真实 interrupt 写入 pending/resume 审计并构造 HUMAN_INTERRUPT，TRUSTED_COMPAT 只剩把旧 `confirmed_setup=True` 升级为 Runtime 批准这一条兼容路径。
- **候选方案**：Phase 12A Acceptance 前删除；Phase 12B 后删除；长期内部保留。
- **最终选择**：在 Phase 12A Graph 路由完成后、最终 Acceptance 前设置独立 Task 删除 TRUSTED_COMPAT。Runtime 建播只接受 HUMAN_INTERRUPT。
- **选择理由**：此兼容来源已无必要，并会让普通旧参数继续拥有升级权限；独立 Task 能在不混入 checkpoint 实现的情况下完成回归。
- **未选理由**：延后会让特殊权限进入抢占和 Agent 阶段；长期保留违反可信审批边界。
- **影响**：D-045 改为 CONDITIONAL；`confirmed_setup` 只能服务 Legacy Protocol，不能生成 Runtime ApprovalContext。
- **重新评估条件**：无；未来新增授权机制必须使用新的显式证据模型，不能恢复 TRUSTED_COMPAT。

## D-076：Phase 12A checkpoint 对账事故的持久化

- **状态**：`ACCEPTED`
- **背景**：原 Task 6 对 checkpoint 领先只要求冻结，但没有持久保存原因、signature 和恢复历史，重启后无法解释为什么冻结或阻止普通命令。
- **候选方案**：扩展 plan_runs；新增第七张 incident 表；只写日志/内存结果。
- **最终选择**：扩展 `plan_runs` 保存 reconciliation_required、结构化失败事实、signature、恢复次数和最近时间，不新增 incident 表。
- **选择理由**：每个 PlanRun 首期只需要一个当前对账事故聚合，直接扩展权威行便于命令前 fail-closed，并避免为单一场景增加表和投影。
- **未选理由**：独立 incident 表在首期没有多事故查询需求；日志和内存无法支持重启恢复与审计。
- **影响**：checkpoint 引用只含 plan ID、version 和批次成功/失败控制位置；Store 领先复用结果，checkpoint 领先不得补造 NodeRun。
- **重新评估条件**：同一 PlanRun 需要保留多个独立事故生命周期和复杂查询时，再拆分 append-only incident history。

## D-077：Event Inbox 与 Kafka 的权威关系

- **状态**：`ACCEPTED`
- **背景**：现有 Kafka Consumer 只是一次性解析演示，没有 offset 与业务提交边界，不能证明重复投递和崩溃恢复。
- **候选方案**：PostgreSQL Inbox + Kafka Adapter；只实现 Inbox；Kafka 直接驱动 PlanEngine。
- **最终选择**：PostgreSQL Event Inbox 是事件权威源；Kafka Adapter 必须先幂等落库，再提交 offset，并提供 Fake 与真实 Kafka 集成证据。
- **选择理由**：把传输重放和业务事实分开后，可用数据库唯一约束、lease 和审计处理重复、冲突与崩溃。
- **未选理由**：只实现 Inbox 会留下现有 Kafka 到抢占链路断点；直接驱动无法可靠闭合 offset 与外部副作用。
- **影响**：数据库失败不得提交 offset；相同事件已落库时允许安全重放并提交。
- **重新评估条件**：真实平台提供具备等价持久幂等语义的事件服务时，可替换 Kafka Adapter，Inbox 权威不变。

## D-078：可信售罄事件的来源验证

- **状态**：`ACCEPTED`
- **背景**：售罄需要实时自动处理，但 payload 中的 trusted/approved 字段可被普通生产者伪造，Kafka topic 访问也不应自动等于业务批准权。
- **候选方案**：启动冻结 Trust Profile 验证后自动授权；所有 Kafka 自动授权；全部人工审批。
- **最终选择**：Ingress Trust Profile 验证 transport/topic/source，并持久化 provenance；只有可信来源、完整 observed_version 和摘要一致时可自动构造事件授权，其他事件转人工。
- **选择理由**：保留实时性，同时把权限放在可信入站边界而非业务 payload。
- **未选理由**：所有 Kafka 自动授权扩大 topic 权限；全部人工会失去实时抢占和无人值守恢复价值。
- **影响**：事件 payload 不能声明信任等级；Profile 在进程启动时冻结，provenance 进入 Event Inbox 审计。
- **重新评估条件**：接入真实签名 webhook 或平台凭证时，扩展 Trust Profile 验证器，不改变权限不来自 payload 的原则。

## D-079：同一事件 ID 的摘要冲突处理

- **状态**：`ACCEPTED`
- **背景**：同一 event_id 不同 payload 可能表示生产者错误、篡改或 ID 复用；不提交 offset 会永久阻塞 Kafka 分区。
- **候选方案**：保留首个事实、追加冲突 occurrence 并冻结；拒绝且不提交 offset；last-write-wins。
- **最终选择**：保留首个不可变事实，追加 CONFLICT occurrence，冻结受影响计划并转人工；冲突可靠落库后提交 offset。
- **选择理由**：同时保留审计可信度和分区可用性，重复毒消息不会制造无限循环。
- **未选理由**：不提交 offset 会阻塞后续事件；覆盖首次事实破坏幂等和审计。
- **影响**：Event Store 必须区分相同摘要重复与不同摘要冲突，并记录每次投递元数据。
- **重新评估条件**：上游提供可证明的事件更正协议时，新增显式 correction event，仍不得原地覆盖。

## D-080：Phase 12B 事件与计划的物理模型

- **状态**：`ACCEPTED`
- **背景**：事件幂等、投递冲突、child plan 和 Replan lineage 需要关系约束，不能只嵌入 Plan JSONB。
- **候选方案**：独立三张事件表 + 扩展计划 lineage；事件嵌入 Plan JSONB；重写为通用事件溯源。
- **最终选择**：新增 event inbox、occurrences、applications 三表；扩展 PlanRun kind/priority/root/parent/trigger 和 PlanVersion change reason/source events。
- **选择理由**：关系表能约束 event/root 唯一应用和 child lineage，又不推翻 Phase 12A 六表模型。
- **未选理由**：JSONB 难以表达并发唯一性；通用事件溯源会重写已验收的 PlanStore。
- **影响**：普通 CARD_BATCH priority 0，EMERGENCY_SOLD_OUT priority 100；历史版本不可变。
- **重新评估条件**：事件类型和跨计划关系显著扩展后，再评估通用 event relation 表，不重写既有事实。

## D-081：Impact scope、局部冻结与在途结果

- **状态**：`ACCEPTED`
- **背景**：一律全局冻结会阻断无关商品，强制取消在途任务又无法撤销已发送调用。
- **候选方案**：PRODUCT 局部分支冻结、ROOM/PLATFORM 全局冻结；全部全局冻结；每商品独立 PlanRun。
- **最终选择**：确定性 ImpactAnalyzer 推导 scope。PRODUCT 冻结依赖闭包内未开始节点，ROOM/PLATFORM 冻结整计划；在途节点允许 deadline 内闭合，受影响旧结果标记 superseded。
- **选择理由**：满足 D-027 的影响范围语义，并保留完整外部调用证据和最小重算价值。
- **未选理由**：全局冻结削弱增量计划；强拆每商品 PlanRun 会重构 Phase 12A 汇总和 checkpoint。
- **影响**：晚到结果不得进入新版本汇总；未受影响结果仍可复用。
- **重新评估条件**：出现无法通过节点依赖闭包隔离的跨商品原子业务时，提升 scope，不隐式扩大 PRODUCT。

## D-082：售罄紧急 DAG 与 Skill 契约

- **状态**：`ACCEPTED`
- **背景**：现有 handle_sold_out_event 同时写状态、推荐和生成提示，使 PlanEngine 看不到内部失败与恢复边界。
- **候选方案**：拆成可审计紧急 DAG；保留单个复合 Skill；由控制节点直接执行业务。
- **最终选择**：紧急 child DAG 使用验证控制节点、`handle_sold_out_event@2.0.0` CAS 写、备选推荐、主播提示和汇总控制节点。写 Skill 只接受 product_id 与 expected_version。
- **选择理由**：每一步都有 Skill/NodeRun 证据，写副作用仍通过 SkillExecutor 和 Attempt Store，后续只读步骤可独立恢复。
- **未选理由**：复合 Skill 恢复粒度过粗；控制节点执行业务会绕过治理。
- **影响**：room、trace、幂等和授权保留在 Context；旧 1.x 调用精确拒绝，不 fallback。
- **重新评估条件**：真实平台只能提供不可拆分事务 API 时，可以在 Port 内原子执行，但 DAG 仍必须保留可观察结果边界。

## D-083：可信事件授权的 Runtime 表达

- **状态**：`ACCEPTED`
- **背景**：事件自动授权不是人工 Approval，复用 ApprovalContext 会混淆来源；放入 arguments 又可被 LLM 伪造。
- **候选方案**：独立 EventAuthorizationContext；统一授权 discriminated union；写入业务参数。
- **最终选择**：新增独立冻结 EventAuthorizationContext 和 Manifest authorization_requirement；与 ApprovalContext 同时出现时拒绝。
- **选择理由**：保持两条信任链语义独立，减少现有人工审批迁移面，并防止普通参数升级权限。
- **未选理由**：统一重构会扩大已验收审批链；业务参数不可信。
- **影响**：authorization requirement 支持 NONE、HUMAN_APPROVAL、TRUSTED_EVENT_OR_HUMAN；受控内部工厂验证 Inbox provenance。
- **重新评估条件**：授权来源增加到三种以上且共享规则显著时，再评估统一联合类型。

## D-084：售罄写副作用未知的恢复

- **状态**：`ACCEPTED`
- **背景**：SIDE_EFFECT_UNKNOWN 可能表示平台已执行，盲目重发有重复副作用；全部人工会降低可恢复性。
- **候选方案**：严格只读对账；一律等待人工；自动重试写。
- **最终选择**：进入 WAITING_RECONCILIATION，通过只读 Port 验证商品已售罄且版本事实与原 CAS 闭合；证据充分确认原 Attempt，否则转人工。
- **选择理由**：用业务事实消除不确定性，不产生第二次外部写。
- **未选理由**：全部人工浪费可证明事实；自动重发违反副作用未知边界。
- **影响**：相同幂等键重放仍返回原未知结果；对账服务不得创建新 Operation。
- **重新评估条件**：平台提供官方 operation query 接口时，可作为更强对账证据接入。

## D-085：增量 Replan 的事件合并、复用和版本预算

- **状态**：`ACCEPTED`
- **背景**：每个事件独立创建版本会快速耗尽预算，复制成功 NodeRun 又会伪造执行证据。
- **候选方案**：root 锁内合并当前待应用事件并引用旧结果；每事件一个版本；覆盖当前版本。
- **最终选择**：child 成功后在 root Replan 锁内吸收当前 pending 事件；新版本创建新 node ID，指纹相同成功节点写 reused_from_node_id 并读取旧 NodeRun，不复制 NodeRun。每个 root 最多两个新版本。
- **选择理由**：保持不可变版本和真实执行历史，同时减少近同时事件的版本膨胀。
- **未选理由**：每事件一版消耗预算；覆盖版本破坏审计；复制 NodeRun 伪造执行。
- **影响**：版本冲突时重新读取最新版本；重复 failure signature + input fingerprint 冻结转人工。
- **重新评估条件**：Golden Cases 证明两版预算显著损害安全恢复率时，先分析事件合并和替代能力，再决定是否调整。

## D-086：PlanEngine 与播中 Harness 的主从关系

- **状态**：`ACCEPTED`
- **背景**：Kafka 与 Agent Harness 都执行售罄写会产生重复副作用，先让 LLM 决定安全事件又会增加实时路径不确定性。
- **候选方案**：PlanEngine 唯一执行、Harness 消费证据；Harness 触发 PlanEngine；两条入口并存。
- **最终选择**：可信 EventIngress/PreemptionCoordinator 是唯一写入口；Harness 只消费 EventApplication、PlanRun 和 EvidenceRef 生成建议。
- **选择理由**：安全事实由确定性路径及时处理，Agent 仍可对如何提示和控场提供价值。
- **未选理由**：Harness 触发让实时动作依赖模型；双入口存在竞争和重复。
- **影响**：PlanEngine 路由启用时 Harness 禁止调用售罄写 Skill；默认切换延后到 Phase 14 Release。
- **重新评估条件**：无；即使未来保留 LiveOpsAgent，它也不能成为可信库存写的第二入口。

## D-087：ReviewMemoryAgent 的播后 Skill 基线

- **状态**：`ACCEPTED`
- **背景**：现有 13 个 Manifest 只覆盖播前/播中，播后 Agent 直接调用 Replay/Memory Service 会绕过 Skill Runtime，无法公平对照。
- **候选方案**：新增最小播后 Skill 集；直接调用旧服务；取消播后候选。
- **最终选择**：新增 retrieve_anchor_memory、collect_post_live_evidence、calculate_post_live_attribution 和 stage_memory_candidates，确定性基线与 Agent 使用相同能力。
- **选择理由**：三场景都经过相同版本、Schema、权限和审计边界，同时避免把全部 MemoryStore API 暴露给 Agent。
- **未选理由**：直接旧服务绕过治理；取消候选会使三场景评估不完整。
- **影响**：stage 只写候选 Store，不写 active memory，不更新 trust score。
- **重新评估条件**：播后能力进一步拆分时按真实复用需求新增 Manifest，不为数量扩张。

## D-088：记忆候选的晋升门槛

- **状态**：`ACCEPTED`
- **背景**：Agent 单次推断直接写长期记忆会影响未来播前决策；全部人工又无法展示受控反馈闭环。
- **候选方案**：双证据自动、其余人工；全部人工；Agent 直接写。
- **最终选择**：至少两个独立 DecisionTrace、无冲突、作用域一致且命中货盘白名单时由确定性 Policy 幂等晋升；其他候选等待人工命令。
- **选择理由**：只自动化可由多证据和白名单证明的低风险结构化事实，保留闭环且限制污染。
- **未选理由**：全部人工削弱自动反馈；直接写风险不可接受。
- **影响**：Agent 自由文本不进入正式记忆，内容由确定性模板生成；基线与 Agent 使用同一 Promotion Policy。
- **重新评估条件**：有真实标注证明单证据精度达到发布门时，可讨论调整证据数，严重污染门仍为 0。

## D-089：共享 Specialist Harness 与 Planner 输出

- **状态**：`ACCEPTED`
- **背景**：三个独立 Agent 图会复制预算、安全和证据逻辑；Planner 只给排序意图又不足以验证候选 DAG 能力。
- **候选方案**：共享受限 Harness Core + Profile；三个独立 LangGraph；扩展播中单体 Agent。Planner 输出比较受限 DAG、规划意图和直接工具序列。
- **最终选择**：三个候选共用 BoundedSpecialistRunner，Profile 限定目标、Skill、预算和结果 Schema；PlannerAgent 输出受限 Candidate DAG，由确定性 PlanValidator/Compiler 注入执行事实。
- **选择理由**：共享核心保证评估可比，受限 DAG 又能验证真实规划差异而不让模型控制权限和调度。
- **未选理由**：独立图容易漂移；跨生命周期单体 Agent 边界过大；直接工具序列恢复性差。
- **影响**：AgentTask/Action/Result/EvidenceRef 成为版本化协议；不保存 chain-of-thought。
- **重新评估条件**：某候选无法由共享动作模型表达真实必要行为时，可扩展 Profile-specific result，不复制安全核心。

## D-090：Phase 13 样本、模型、循环与费用

- **状态**：`ACCEPTED`
- **背景**：Agent 去留需要足够样本、固定模型和成本门；单人项目又不能手写 240 条重复数据或无限调用模型。
- **候选方案**：模板生成并固化 JSONL；全部人工；LLM 生成标注。模型配置比较版本化 manifest、代码硬编码和普通 Settings。费用比较 3 元硬门、提高预算或只跑 scripted。
- **最终选择**：每候选 20 development/40 validation/20 holdout，人工模板与标签、固定 seed 生成并固化 JSONL。Agent 使用 deepseek-v4-flash、temperature 0 和版本化 Evaluation Manifest。LiveOps 2/3、Planner 3/5、Review 3/4 模型/Skill 上限。真实模型总费用 3 元。
- **选择理由**：样本可重复、标签不依赖被测模型，配置和成本可审计；严格循环满足实时和预算约束。
- **未选理由**：全手写工作量大且易漂移；LLM 标注污染 holdout；普通 Settings 无法公平比较；无费用门无法无人监控执行。
- **影响**：按 LiveOps、Planner、Review 顺序消费预算；未完成 60 个正式样本即 INCONCLUSIVE，不保留。当前连续实施使用持久化预算作用域 `agent-runtime-completion-v1`，Phase 14 首次 Release 的保留 Agent 与 Judge 调用只能使用 3 元总上限中的剩余余额。
- **重新评估条件**：官方价格变化导致 3 元无法完成正式集时，保持 INCONCLUSIVE，不自动缩样或提预算。

## D-091：Judge、三级 CI 与 Nightly 付费开关

- **状态**：`ACCEPTED`
- **背景**：同模型自评有偏差，有 secret 就每天付费又会产生不可控长期成本；仓库当前没有 GitHub Actions。
- **候选方案**：flash Agent + pro Judge；同模型 Judge；无 Judge。Kafka 比较 Nightly/Release、每 PR 和仅本地。Nightly 付费比较显式 opt-in、有 secret 自动和永不运行。
- **最终选择**：deepseek-v4-pro 只评语义，规则优先且 Judge 不得覆盖严重违规。PR 使用 PostgreSQL/ScriptedModel；真实 Kafka 放 Nightly/Release。Nightly 真实模型需 secret 与 ENABLE_PAID_NIGHTLY=true，默认每次 0.10 元。
- **选择理由**：降低同源偏差，保持 PR 快速免费，并把持续费用变成显式选择。
- **未选理由**：同模型自评偏置；每 PR Kafka 成本和稳定性差；有 secret 自动付费不可控。
- **影响**：Release 手动运行完整 holdout；Judge 不可用时不能伪造语义高分。本次连续实施不运行付费 Nightly，首次 Release 复用 D-090 的剩余预算；项目完成后的未来付费 Nightly/Release 必须由受保护环境重新提供显式预算授权。
- **重新评估条件**：CI 资源和模型价格变化时可调整抽样/基础设施层级，但 PR 免费和严重规则优先不变。

## D-092：最终默认路由、ToolRegistry 删除与覆盖率

- **状态**：`ACCEPTED`
- **背景**：长期默认 Legacy 会让新架构只存在于 Demo，立即删除所有 Legacy 又缺少回滚；全仓覆盖率会把历史 UI/脚本补测混入主线。
- **候选方案**：Release 后默认新 Runtime并保留显式回滚；只保留新 Runtime；继续默认 Legacy。覆盖率比较核心包 90%、全仓 80% 和不设门。
- **最终选择**：Phase 14 Release 通过后，Skill 三批、手卡和售罄默认新 Runtime/PlanEngine，保留启动期显式 Legacy 一个兼容周期，禁止同次 fallback；删除 ToolRegistry Facade；核心 Runtime branch coverage 至少 90%。
- **选择理由**：项目默认展示真实新架构，同时保留受控回滚；覆盖率集中约束高风险新代码。
- **未选理由**：只留新路径没有快速回滚；继续 Legacy 弱化交付；全仓数字会激励低价值补测。
- **影响**：PR CI 增加 pytest-cov；ToolRegistry 生产调用最终为 0。
- **重新评估条件**：一个兼容周期结束且 Release 证据稳定后，可单独决定删除 Legacy 路由。

## D-093：最终操作面与 Git 执行方式

- **状态**：`ACCEPTED`
- **背景**：全程实施可选择扩展 UI/HTTP 或专注内部 Runtime，也可选择长期分支或延续当前 main 原子提交链。
- **候选方案**：内部 Service/CLI；只读 Dashboard；完整控制台。Git 比较 main 原子提交、单一长期分支和每阶段分支。
- **最终选择**：本轮只提供内部 Query/Command Service、JSON Demo 和报告，不新增前端/HTTP 管理面。正式连续实施获得授权后沿用 main，每 Task 验证后独立提交并立即推送。
- **选择理由**：把时间用于 Agent Runtime、恢复和评估深度，延续已建立的可追踪提交链；用户明确接受 main 执行方式。
- **未选理由**：Dashboard/控制台扩大前后端验收；长期分支增加漂移和最终合并风险。
- **影响**：不派发子智能体，由主模型直接实施与审查；用户已有脏文件不纳入提交。
- **重新评估条件**：项目进入真实运营或多人协作时，再设计操作面和 PR 分支策略。
