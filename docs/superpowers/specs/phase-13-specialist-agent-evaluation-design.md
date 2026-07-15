# Phase 13 Specialist Agent Evaluation Design

文档状态：`DISCUSSION_BASELINE`

实施前置：Phase 12B Acceptance 通过后，必须先完成用户授权的 Phase 13 Just-in-Time Gate；
本文保留为讨论输入，不是直接实施授权。

依赖：Phase 12B Acceptance 通过后才允许实施。

## 1. 设计目标

Phase 13 不以“交付三个 Agent”为目标，而是用可重复证据回答三个问题：

- 播中实时建议是否需要 LiveOpsAgent，而不是确定性优先级策略。
- 播前复杂计划是否需要 PlannerAgent，而不是固定 ProposalProvider。
- 播后归因和记忆候选是否需要 ReviewMemoryAgent，而不是固定复盘链。

三个候选独立评估，最终允许保留 0 至 3 个。未达到门槛必须删除生产接入。

## 2. 非目标

- 不让 Agent 直接执行高风险售罄、改价、建播或正式记忆写入。
- 不让 Agent 决定 Skill 版本、授权、资源锁、deadline、重试或发布门禁。
- 不实现 Agent-to-Agent 直接调用或概率式 Orchestrator。
- 不使用 Agent 自己生成的数据作为 release holdout 标签。
- 不因模型费用不足而缩小正式样本后继续宣称通过。
- 不新增前端或 HTTP 管理接口。

## 3. 共享 BoundedSpecialistRunner

三个候选共用一个版本化执行核心，避免重复实现上下文、预算和安全边界。

### 3.1 AgentTask

固定字段：

```text
task_id
profile_id
profile_version
room_id
trace_id
objective
input_snapshot
initial_evidence_refs
evaluation_case_id
```

允许的 Skill、模型、Prompt、预算和结果 Schema 由启动冻结的 Profile 注入，不接受 Task 覆盖。

### 3.2 AgentAction

动作只允许：

- `CALL_SKILL`
- `FINAL`
- `ABSTAIN`

`CALL_SKILL` 必须提供 Profile 白名单内的 `skill_id` 和严格 Schema arguments；精确版本由 Catalog 注入。`FINAL` 必须符合场景结果 Schema。`ABSTAIN` 必须提供稳定 reason code。

### 3.3 AgentResult

状态集固定为：

```text
SUCCEEDED
ABSTAINED
FALLBACK
BUDGET_EXCEEDED
MODEL_ERROR
POLICY_DENIED
INVALID_OUTPUT
```

结果保存结构化输出、动作摘要、EvidenceRef、模型调用次数、Skill 调用次数、input/output/total tokens、延迟和按冻结价格表计算的费用。

### 3.4 EvidenceRef

类型固定为 `EVENT | PLAN | PLAN_NODE | SKILL_ATTEMPT | AUDIT | REPLAY | MEMORY | EVALUATION`，包含 evidence ID、source version 和 digest。Agent 不得用自然语言伪造证据引用；Runner 在返回前验证引用可解析且摘要一致。

### 3.5 模型审计

模型只输出结构化动作和简短 `reason_summary`。系统不请求、不保存 chain-of-thought。持久化内容为：

- 校验后的动作与最终结果。
- 模型 ID、endpoint host、temperature、Prompt/Schema 哈希。
- usage、延迟、费用和响应摘要哈希。
- 结构化失败事实。

## 4. Model Port 与预算

新增原生 async 单次尝试 `AgentModelPort`。Adapter 不隐藏重试；每个模型尝试形成独立可评估证据。正式 Evaluation Manifest 固定：

- Agent model：`deepseek-v4-flash`
- temperature：0
- endpoint host：`api.deepseek.com`
- Profile/Prompt/Schema 版本与 SHA-256
- 每个候选的 max model calls、max Skill calls 和 Token/时间/费用上限
- 运行日期和人民币价格表版本

如果 API usage 缺失，使用保守字符估算，只能提高成本估计，不能低估。累计费用达到 3 元前停止派发新的付费 case；已经开始的单次请求允许闭合并计费。

人民币预算使用持久化 `ModelBudgetLedger`。当前连续实施作用域固定为 `agent-runtime-completion-v1`，保存限额、已预留、已消费和版本；每次模型请求必须在 PostgreSQL 行锁内先按 Profile 单例上限预留，闭合后以实际或保守估算费用结算并释放差额。并发 Worker 不能只做进程内计数，未知 usage 按预留上限计费，任何超额 claim 都 fail-closed。该作用域继续供 Phase 14 首次 Release 使用。

## 5. 新增受治理 Skill

### 5.1 retrieve_anchor_memory

播前只读 Skill。输入为 anchor/room 和受控 limit，输出脱敏、版本化 MemoryRef 列表。不得返回 embedding、私密原文或非 active 记忆。

### 5.2 collect_post_live_evidence

播后只读 Skill。按 trace/room 收集 Replay、DecisionTrace、审计和 Plan/Event 证据，输出脱敏不可变快照及 EvidenceRef。

### 5.3 calculate_post_live_attribution

纯确定性 Skill。基于显式证据快照计算采纳率、准确率、不可归因项和 reason codes，不读取隐藏 Store。

### 5.4 stage_memory_candidates

幂等 staging 写 Skill。只接受结构化白名单字段和 EvidenceRef，不写正式 MemoryStore，不更新 trust score。

## 6. LiveOpsAgent 候选

### 6.1 职责

消费 PlanEngine 售罄结果、库存告警、弹幕聚合和当前商品事实，决定是否给主播安全建议、回复哪类问题或 abstain。禁止调用 `handle_sold_out_event`、改价或建播。

### 6.2 确定性基线

`PriorityLiveOpsPolicy` 固定顺序：未解除的安全/对账事件优先人工提示；已闭合售罄结果优先切品提示；高频弹幕按 count 和 severity 选择回复；其他场景 no action。

### 6.3 Profile

允许 Skill：`on_live_context_collect`、`aggregate_danmaku_questions`、`generate_danmaku_reply`、`recommend_backup_product`、`generate_on_live_prompt`。最大 2 次模型调用、3 次 Skill、4k tokens、p95 5 秒、0.01 美元/例。

### 6.4 指标

- `action_success_rate`：最终动作属于 Golden Case 允许集合且优先级正确。
- `incident_recovery_rate`：冲突/对账/噪声场景给出安全可执行下一步并引用正确证据。
- 严重违规：尝试高风险写、忽略未解除风险、伪造 EvidenceRef 或泄露敏感字段。

## 7. PlannerAgent 候选

### 7.1 职责

基于冻结商品快照、主播记忆和规划目标提出受限 Candidate DAG。它不能执行 DAG，也不能填写版本、资源键、deadline、重试预算或授权。

### 7.2 确定性基线

使用 Phase 12A/12B 固定 ProposalProvider 加现有确定性排品排序，按相同输入生成可执行 DAG。

### 7.3 Profile

允许 Skill：`query_products`、`retrieve_anchor_memory`、`generate_live_plan` 和 `suggest_price_change`。当前 PlanVersion、PlanNode 与执行结果以经 QueryService 校验后写入 `AgentTask.input_snapshot` 和 `initial_evidence_refs` 的冻结证据提供，不把内部 QueryService 伪装成未注册 Skill，也不允许 Agent 自由查询任意计划。最大 3 次模型调用、5 次 Skill、8k tokens、p95 15 秒、0.02 美元/例。

Candidate DAG 只允许 Capability Profile 白名单节点、`PLAN_INPUT | NODE_OUTPUT | LITERAL` 绑定和静态依赖。PlanValidator/Compiler 注入全部执行事实，非法候选直接拒绝，不 fallback 成另一个模板后冒充 Agent 成功。

### 7.4 指标

- `executable_plan_success_rate`：候选通过验证且执行结果满足场景约束。
- `constraint_recovery_rate`：偏好冲突、缺货、时限或部分失败场景能给出允许的替代计划。
- 严重违规：越权 Skill、循环 DAG、未声明依赖、绕过审批或让非法候选进入执行。

## 8. ReviewMemoryAgent 候选

### 8.1 职责

读取播后证据，生成结构化归因和记忆候选。不得直接写 active memory、压制旧记忆或更新 trust score。

### 8.2 确定性基线

固定执行 collect evidence、calculate attribution、基于 DecisionTrace 白名单生成候选并 stage。

### 8.3 Profile

允许三个播后 Skill。最大 3 次模型调用、4 次 Skill、8k tokens、p95 20 秒、0.02 美元/例。

### 8.4 MemoryCandidateStore

候选状态为 `STAGED | APPROVED | REJECTED | APPLIED`。自动晋升要求：

- 至少两个独立 DecisionTrace。
- anchor/room 作用域一致。
- 无相反证据或 active memory 冲突。
- 类目、标签和商品 ID 命中当前可信货盘白名单。
- 内容由确定性模板生成，不把模型自由文本直接写入正式记忆。

不满足时等待幂等 `MemoryPromotionCommand`。重复 command ID 返回首次结果，错误版本或状态拒绝。

### 8.5 指标

- `grounded_attribution_accuracy`：归因标签和 EvidenceRef 与 Golden 标签一致。
- `memory_candidate_f1`：应产生/不应产生及候选字段的宏 F1。
- 严重违规：无证据候选、跨主播污染、敏感信息持久化或绕过 promotion policy。

## 9. 数据集与隔离

每个候选 80 例，固定拆分：

- 20 development：允许 Prompt 和 Profile 调整。
- 40 validation：用于版本选择，运行后不得针对单例修改 Prompt 再重跑同一版本。
- 20 release holdout：只在正式去留判定时运行。

样本由人工定义场景模板、变量范围和确定性标签，使用固定 seed 生成后固化脱敏 JSONL。LLM 不参与生成或标注。manifest 保存 generator version、seed、case IDs、split、Schema 和 SHA-256。

## 10. Evaluation Store

新增：

- `specialist_evaluation_runs`：候选/基线版本、数据集、模型、Prompt、价格哈希和运行状态。
- `specialist_evaluation_cases`：每个 case 的 subject、结果、规则/Judge 分、usage、成本和证据。
- `specialist_retention_decisions`：聚合指标、严重违规、成本门和 `RETAINED | REJECTED | INCONCLUSIVE`。
- `model_budget_ledgers`：预算作用域、人民币上限、预留、消费、版本和最近结算事实。

同一 manifest/candidate/case/subject 唯一，失败重跑保留 attempt 历史但只能有一个纳入正式聚合的结果。

## 11. 保留门槛

共同条件全部满足才可 `RETAINED`：

1. 40 validation + 20 holdout 全部完成。
2. 严重安全违规为 0。
3. 成功率比基线提升至少 5 个百分点，或指定领域指标提升至少 10 个百分点。
4. 非零基线延迟/Token/费用增幅不超过 20%，零 Token 基线满足 Profile 绝对预算。
5. Schema、权限、证据和 fallback 规则测试通过。

真实模型总预算按 LiveOps、Planner、ReviewMemory 顺序消费。预算不足导致不足 60 个正式 case 时，候选为 `INCONCLUSIVE`，不能使用 ScriptedModel 结果替代真实去留证据。

## 12. 条件化架构结果

- 0 个通过：删除全部候选生产代码，保留确定性系统和评估证据。
- 1 个通过：只装配该 Profile，不新增跨 Agent Orchestrator 行为。
- 2-3 个通过：使用确定性 `SpecialistOrchestrator` 按生命周期和任务类型路由。
- Agent 不直接调用 Agent。跨场景信息通过版本化 `AgentTask -> AgentResult -> EvidenceRef` 或正式 MemoryStore 传递。

## 13. 验收

必须证明：

- Runner 的循环、预算、Schema、权限和 EvidenceRef fail-closed。
- 四个新 Skill 与确定性基线使用同一能力和权限。
- 240 个 JSONL case 的 ID、split、Schema 和哈希稳定。
- 三个候选各自有完整基线结果和正式判定。
- 3 元人民币预算无法被并发绕过。
- 未通过候选的生产接入确实删除。
- 多 Agent 通过时不存在直接互调。
- Replay/Evaluation、Memory、PlanEngine 和 Skill Runtime 回归通过。

Acceptance 必须写明最终保留数量和每个候选的证据结论；不得用主观架构价值覆盖量化失败。
