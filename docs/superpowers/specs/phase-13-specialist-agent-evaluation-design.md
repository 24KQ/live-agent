# Phase 13 Specialist Agent Evaluation Design

文档状态：`IMPLEMENTATION_IN_PROGRESS`

实施前置：Phase 12B Acceptance 已通过；用户已授权 Phase 13 Task 1-12 连续实施。Task 1-9 已提交并推送，Task 10 正在完成最终验证。

## 1. 目标与阶段结构

Phase 13 不按数量交付 Agent，而是用相同输入、Skill、权限和数据集判断三个新增 Specialist Agent 是否值得接入正式架构。最终允许保留 0 至 3 个新增候选；现有播中 Agent Harness 不计入该数量。

阶段按纵向切片推进：

```text
13A 共享评估内核
-> 13B LiveOpsAgent
-> 13C PlannerAgent
-> 13D ReviewMemoryAgent
-> 13E 正式评估、条件接入与 Acceptance
```

每个候选独立判定。前一候选被拒绝不阻止后续候选；保留 0 个新增候选仍是有效的工程结论。

## 2. 非目标

- 不实现 Agent-to-Agent 直接调用、自由 handoff、共享 scratchpad 或概率式路由。
- 不让 Agent 执行售罄、改价、建播或正式记忆写入。
- 不让 Agent 决定 Skill 版本、授权、资源键、deadline、重试或发布门禁。
- 不使用 LLM 生成或标注正式数据集，不因预算不足缩小正式样本后宣称通过。
- 不新增前端、HTTP 管理接口、真实淘宝 API 或动态 Profile 热加载。

## 3. 共享 Specialist Runtime

### 3.1 AgentTask 与路由

`AgentTask` 是冻结严格 JSON，固定包含：

```text
task_id
task_kind: LIVE_OPS_ADVICE | PLAN_PROPOSAL | POST_LIVE_REVIEW
profile_id
profile_version
room_id
trace_id
objective
input_snapshot
initial_evidence_refs
evaluation_case_id
```

`SpecialistProfileRegistry` 可同时注册多个版本化 Profile。确定性 `SpecialistOrchestrator` 只按 `task_kind` 解析一个 Profile，不接受模型自选 Agent，也不允许 Agent 调用 Agent。未来多 Agent 扩展通过新增 Profile 和确定性路由完成。

### 3.2 AgentAction 与 AgentResult

动作只允许 `CALL_SKILL | FINAL | ABSTAIN`。`CALL_SKILL` 必须命中 Profile 白名单、Profile 冻结的精确 Skill 版本和 Catalog 严格 Schema；Catalog 版本与 Profile 不一致时必须在 Skill Port 前拒绝。`FINAL` 必须满足候选结果 Schema，完整 EvidenceRef 与嵌套 evidence IDs 都只能引用本轮已由 Resolver 验证的证据；`ABSTAIN` 必须提供稳定 reason code。

结果状态固定为：

```text
SUCCEEDED
ABSTAINED
FALLBACK
BUDGET_EXCEEDED
MODEL_ERROR
POLICY_DENIED
INVALID_OUTPUT
```

结果保存结构化输出、动作摘要、EvidenceRef、模型/Skill 调用数、tokens、延迟、人民币费用和结构化失败，不请求或保存 chain-of-thought。

### 3.3 BoundedSpecialistRunner

三个候选共用唯一 Runner。启动冻结 Profile 注入模型、Prompt 正文及摘要、Schema、Skill 白名单及精确版本、deadline、模型/Skill 调用上限、Token 和费用上限；Task 不得覆盖。Runner 必须把冻结 Prompt 正文作为真实 system message 发送，不能只把摘要写入审计。

Runner 每轮按固定顺序执行：deadline、Profile、预算预留、模型单次尝试、动作 Schema、EvidenceRef、Skill 白名单、Skill Runtime、结果 Schema。任何失败都形成 AgentResult，不静默增加步骤或调用次数。

正式评估中的 fallback 计为 Agent 失败。只有已保留候选的生产建议路径允许调用确定性 baseline，并显式返回 `FALLBACK`；高风险 Runtime/PlanEngine 写路径继续禁止同次 fallback。

### 3.4 EvidenceResolverRegistry

EvidenceRef 类型固定为 `EVENT | PLAN | PLAN_NODE | SKILL_ATTEMPT | AUDIT | REPLAY | MEMORY | EVALUATION`，包含 evidence ID、source version 和 digest。Resolver 必须核对来源、版本、摘要及 anchor/room 作用域。无法解析、摘要不符或跨作用域引用返回 `POLICY_DENIED`，不能降级为无证据建议。

## 4. AgentModelPort 与模型审计

新增原生 async 单次尝试 `AgentModelPort` 和 OpenAI-compatible DeepSeek Adapter。每次调用只发送一个请求，不隐藏重试；旧同步 `LLMClient` 不进入正式评估。

正式 Evaluation Manifest 固定：

- endpoint host：`api.deepseek.com`
- model：`deepseek-v4-flash`
- temperature：0
- Prompt、结果 Schema、数据集、代码和价格表 SHA-256
- Profile 调用、Token、deadline 和费用上限
- 官方价格来源 URL、抓取日期和人民币换算版本

价格事实保存独立只读快照并由 Manifest 绑定原始字节摘要。数据集基线 Manifest 在 Task 6 生成；Task 7-10 改动候选实现后，Task 11 必须基于最终 Git commit 重新生成新的正式 Manifest，校验全部 `src/**/*.py`、数据、Prompt、Schema、Profile 与价格快照后才能注册 EvaluationRun。旧数据集 Manifest 不得冒充正式运行身份。

API 返回模型身份不匹配、模型没有公开价格或 usage 缺失时，不开始新的正式 case。请求已发送但 usage 不明时按预留上限结算，不能低估费用。

## 5. 持久预算

预算作用域继续使用 `agent-runtime-completion-v1`，总上限 3.00 元：

- Phase 13 最多 2.40 元。
- Phase 14 首次 Release 保留 0.60 元，不得被 Phase 13 借用。
- LiveOpsAgent 初始额度 0.60 元。
- PlannerAgent 初始额度 1.00 元。
- ReviewMemoryAgent 初始额度 0.80 元。

额度覆盖 development 真实 smoke、validation、holdout 和诊断 Judge。候选提前拒绝后的未消费余额可回到 Phase 13 公共池，但总额和 Phase 14 预留不变。

`ModelBudgetLedger` 与 reservation/model-call 记录持久化限额、预留、已消费、结算状态和版本。每次请求前在 PostgreSQL 行锁内预留，完成后按 usage 结算并释放差额；并发 Worker 不能越过总额、阶段预留或候选上限。

## 6. 新增受治理 Skill

### 6.1 retrieve_anchor_memory@1.0.0

播前只读 Skill。输入 anchor_id、room_id 和受控 limit，输出脱敏 active MemoryRef。不得返回 embedding、私密原文或其他主播记忆。Planner 正式评估不重新调用 `query_products`；baseline 和 Agent 使用 case 准备阶段冻结的同一商品和记忆快照。

### 6.2 collect_post_live_evidence@1.0.0

播后只读 Skill。按 trace/room 收集 Replay、DecisionTrace、Audit、Plan/Event 证据，输出脱敏不可变快照和 EvidenceRef。

### 6.3 calculate_post_live_attribution@1.0.0

纯确定性 Skill。只消费显式证据快照，输出归因标签、不可归因项和 reason codes，不读取隐藏 Store。

### 6.4 stage_memory_candidates@1.0.0

幂等 staging Skill。只接受白名单字段与 EvidenceRef，写 MemoryCandidateStore；不写 active MemoryStore，不更新 trust score，不持久化 Agent 自由文本。

## 7. LiveOpsAgent

输入为可信售罄 EvidenceRef、商品快照、库存告警、弹幕聚合和未解除风险。输出只允许：

```text
NO_ACTION
HUMAN_ATTENTION
SWITCH_PRODUCT_SUGGESTION
DANMAKU_REPLY_SUGGESTION
```

输出必须包含稳定 reason code、主播建议和可解析 EvidenceRef。允许 `on_live_context_collect`、`aggregate_danmaku_questions`、`generate_danmaku_reply`、`recommend_backup_product`、`generate_on_live_prompt`；禁止售罄写、改价和建播。

Profile 上限：2 次模型、3 次 Skill、4000 total tokens、5 秒。确定性 baseline 为 `PriorityLiveOpsPolicy`。

保留门：`action_success_rate >= 90%` 且比 baseline 提升至少 5pp；`incident_recovery_rate >= 85%` 且提升至少 10pp。

## 8. PlannerAgent

输入为冻结商品快照、目标约束、已检索记忆和当前 PlanVersion/节点结果。输出为受限 `CandidatePlanProposal`，只能声明白名单节点、依赖与 `PLAN_INPUT | NODE_OUTPUT | LITERAL` 绑定。

Planner 正式运行时 Skill 上限为 0，不重新查询商品或计划。PlanValidator/Compiler 注入版本、资源键、deadline、授权和重试；未知节点、循环、非法绑定或执行控制字段直接拒绝。候选可声明计划生成、手卡和价格建议能力，禁止建播和任何写操作。

Profile 上限：3 次模型、0 次 Skill、8000 total tokens、15 秒。确定性 baseline 为 Phase 12A/12B 固定 Provider 加现有排品排序。

保留门：`executable_plan_success_rate >= 95%`；`constraint_recovery_rate >= 85%` 且比 baseline 提升至少 10pp。

## 9. ReviewMemoryAgent 与记忆闭环

输入为脱敏 Replay、规则 Evaluation、DecisionTrace、货盘白名单和 active memory 摘要。输出为结构化 attribution 与 memory candidate，不包含可直接进入长期记忆的自由文本。

Profile 允许三个播后 Skill，最多 3 次模型、4 次 Skill、8000 total tokens、20 秒。确定性 baseline 固定执行 evidence collection、attribution、白名单 candidate 生成和 staging。

MemoryCandidate 状态为 `STAGED | APPROVED | REJECTED | APPLIED`。确定性 PromotionPolicy 自动晋升必须同时满足：

- 至少两个独立 DecisionTrace。
- anchor/room 作用域一致。
- 无相反证据或 active memory 冲突。
- 类目、标签和商品 ID 命中当前货盘白名单。
- 正式记忆正文由确定性模板生成。

不满足时等待幂等 `MemoryPromotionCommand` 或拒绝。重复 command ID 返回首次结果，错误版本或状态拒绝。

保留门：`grounded_attribution_accuracy >= 90%` 且提升至少 5pp；`memory_candidate_macro_f1 >= 0.85` 且提升至少 0.10。

闭环验收固定为：

```text
两条独立 DecisionTrace
-> stage MemoryCandidate
-> PromotionPolicy
-> 幂等模板记忆晋升
-> 下一次播前 retrieve_anchor_memory 可读取
```

## 10. 数据集与安全早停

每个候选固定 80 例：20 development、40 validation、20 holdout，共 240 例。人工场景模板与固定 seed 生成脱敏 JSONL；LLM 不参与生成或标注。Manifest 保存 generator、seed、Schema、case IDs、split 和 SHA-256。候选 case loader 每次读取都校验 Manifest 摘要、Schema 与 case 身份，文件被替换后必须 fail-closed。

holdout labels 只由受审计 Evaluator 读取，不进入 case loader、AgentTask、Prompt、模型消息或 Skill。当前不可信边界是只能接收结构化消息的远端模型，它没有本地文件系统或任意代码执行能力；本阶段不支持第三方插件式候选代码。未来若允许不受信候选实现，必须把 label store 放入独立进程/权限域后再开放，不能沿用当前同仓库治理边界。

development 使用 ScriptedModel，并允许每候选最多 5 个真实 smoke case；不进入去留指标。baseline 对全部 80 例运行。

validation 按稳定 case_id 顺序分成四个 10 例 shard。每个 shard 后执行早停：

- 任一严重安全违规立即 `REJECTED`。
- 即使剩余 case 全部成功也无法达到严格 AND 门时，立即 `REJECTED`。
- 只有完成 40 validation 且仍满足资格的候选，才能解封并运行一次 20 holdout。

endpoint、价格、预算或基础设施导致证据不足为 `INCONCLUSIVE`；已经由规则证明无法通过的是 `REJECTED`。

## 11. 配对评估与 Judge

baseline 与 Agent 使用相同 case 和冻结输入。每个 case 保存配对结果，聚合报告绝对指标、百分点差、paired wins/losses 和比例的 Wilson 区间。

规则标签决定安全、权限、结构化正确性和正式去留。Judge 只在候选规则门通过后抽样最多 10 对 holdout，补充语义观察，不参与 `RETAINED | REJECTED | INCONCLUSIVE`，Judge 不可用不阻断规则结论。

共同保留硬门：完成 40 validation + 20 holdout、严重安全违规为 0、无 fallback 冒充成功、Schema/权限/证据门禁通过，并同时满足候选绝对指标和相对提升指标。

## 12. Evaluation Store

新增关系事实：

- evaluation manifest/run。
- case attempt 与唯一正式选中结果。
- paired metric 与 retention decision。
- model budget ledger、reservation 和 model-call 审计。
- memory candidate 与 promotion command。

Evaluation Manifest 必须区分 `DATASET_BASELINE | FORMAL_EVALUATION`。基线允许注册以供审计和 Loader 摘要校验，但不能创建 EvaluationRun；正式 Manifest 必须绑定 40 位最终 Git commit，且由 Task 11 在候选实现冻结后生成。正式注册及每个执行进程的 create_run 都需要可信授权；授权只在源码闭包无 symlink、磁盘 Python 文件与 Git tracked 闭包相等、目录清洁、Git HEAD、source commit 和重算 code digest 全部一致后签发。Plan/脚本中的阶段文字不能代替 Store 的该项强制门禁。

同一 manifest/candidate/case/subject 只能有一个结果进入正式聚合；失败重跑保留历史。Store 不复用 Phase 7A 通用回放表表达预算和去留，但可通过适配器读取既有 Replay、规则 Evaluation 和 Judge。

## 13. 条件生产接入与多 Agent 预留

候选在 Evaluation Harness 中完成正式判定前没有生产路由。`RETAINED` 后才建立默认关闭、启动冻结的 `DETERMINISTIC | SPECIALIST_AGENT` 路由；Phase 13 只验证显式 Specialist 路由，默认值是否晋升由 Phase 14 Release Gate 决定。

多个候选通过时，Registry 可同时保存 Profile，但 Orchestrator 仍按生命周期和任务类型确定性选择一个。跨场景事实只通过 `AgentTask -> AgentResult -> EvidenceRef` 或正式 MemoryStore 传递。

## 14. Acceptance

Acceptance 必须逐候选记录 baseline/Agent 样本数、早停位置、严重违规、绝对指标、paired delta、Wilson 区间、p95、tokens、人民币费用和最终结论。固定场景 `live-session-p001-sold-out-v1` 追加只读 `agent-decision-appendix.json` 与 Markdown 摘要，不改写 Phase 12B 主 Trace。

必须证明：

- Runner、预算、Schema、权限和 EvidenceRef fail-closed。
- 240 个 case 的 ID、split、Schema 和哈希稳定。
- 2.40 元 Phase 13 上限与 0.60 元 Phase 14 预留不能被并发绕过。
- 未保留候选从未进入生产装配；保留候选只通过默认关闭路由接入。
- 记忆双证据晋升和下一次播前读取闭环可重复。
- 多 Profile 并存不产生 Agent 互调或概率式路由。

Phase 13 Acceptance 后状态进入 `AWAITING_PHASE_14_GATE`，用户重新审核 Phase 14 后才能实施。
