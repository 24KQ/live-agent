# Phase 13 Specialist Agent Evaluation Implementation Plan

文档状态：`TASK_10_COMMIT_READY`

> 执行状态（2026-07-16）：Task 1-9 已提交并推送；Task 10 ReviewMemoryAgent 已完成专项、全量回归与审查，正在提交推送。Task 10-12 已获连续实施授权，Phase 13 Acceptance 后停止在 Phase 14 Gate。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立共享受限 Specialist Runtime，以配对数据和硬门槛独立判断三个新增 Agent 候选是否值得进入正式架构。

**Architecture:** Phase 13 按评估内核、LiveOps、Planner、ReviewMemory 和正式去留五个纵向阶段推进。候选先存在于 Evaluation Harness，只有 RETAINED 后才建立默认关闭的生产 Profile；统一 Registry 预留多 Agent 扩展，但不实现 Agent 互调。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、psycopg 3、PostgreSQL、DeepSeek OpenAI-compatible API、pytest。

---

## 实施规则

- 每个 Task 使用 `RED -> GREEN -> REFACTOR -> REVIEW -> VERIFY -> COMMIT -> PUSH`。
- 每个 Task 独立 ASCII commit 并立即推送 `origin/main`。
- 新增或修改代码使用详细 UTF-8 中文注释。
- 不运行未进入 Task 的真实模型；正式调用只能由 Task 11 的预算预检解封。
- sub-agent 只做独立只读规格、安全和质量审查，主模型负责实现、验证和提交。
- 不修改真实淘宝 API、Phase 14 默认路由、前端或 HTTP 管理面。

## Task 1：协议、Profile Registry 与确定性路由

**Files:**

- Create: `src/specialist_runtime/models.py`
- Create: `src/specialist_runtime/profiles.py`
- Create: `src/specialist_runtime/registry.py`
- Test: `tests/unit/test_phase13_specialist_models.py`

- [ ] 写红灯测试覆盖 AgentTask/Action/Result 互斥、严格 JSON、深度冻结、稳定摘要、task_kind/profile 匹配和额外字段拒绝。
- [ ] 写 Registry 红灯测试：同 profile/version 幂等、同身份不同摘要冲突、未知 task_kind fail-closed、多个 Profile 并存但单次只解析一个。
- [ ] 实现 `LIVE_OPS_ADVICE | PLAN_PROPOSAL | POST_LIVE_REVIEW`、结果状态集和 EvidenceRef 类型集。
- [ ] 实现冻结 `SpecialistProfile`，固定模型、Prompt/Schema 哈希、Skill 白名单、deadline、模型/Skill/Token/费用上限。
- [ ] 实现 `SpecialistProfileRegistry` 与确定性 `SpecialistOrchestrator`；不提供 Agent 调 Agent 或动态 handoff 方法。
- [ ] 运行 `python -m pytest tests/unit/test_phase13_specialist_models.py -q`，预期全部通过。
- [ ] 提交并推送：`feat: add specialist runtime contracts`。

## Task 2：原生 async 单次 AgentModelPort

**Files:**

- Create: `src/specialist_runtime/model_port.py`
- Create: `src/specialist_runtime/deepseek_adapter.py`
- Create: `src/specialist_runtime/scripted_model.py`
- Test: `tests/unit/test_phase13_agent_model_port.py`

- [ ] 写红灯测试覆盖一次请求、无隐藏重试、绝对 deadline、HTTP/限流/超时/非法 JSON 分类、模型身份和 usage 透传。
- [ ] 定义 async `AgentModelPort.complete(request)`、冻结 request/success/failure 和 usage 模型；request 固定 endpoint host、model、temperature、Prompt/Schema 哈希和 max tokens。
- [ ] 实现 OpenAI-compatible DeepSeek Adapter，每次调用只发送一个 `/chat/completions` 请求，不复用旧同步 LLMClient 的重试。
- [ ] 实现 ScriptedModel，按 case/action 序列返回成功、超时、越权动作和无 usage 结果。
- [ ] 断言 Adapter 不记录 API key、原始敏感 header 或 chain-of-thought，只保存响应摘要哈希。
- [ ] 运行专项测试与既有 LLMClient 回归；提交并推送：`feat: add single attempt agent model port`。

## Task 3：持久模型预算账本

**Files:**

- Create: `src/specialist_runtime/budget.py`
- Create: `docker/init_phase13_specialist_evaluations.sql`
- Modify: `scripts/run_db_migrations.py`
- Test: `tests/unit/test_phase13_model_budget.py`
- Test: `tests/integration/test_phase13_model_budget_postgres.py`

- [ ] 写红灯测试覆盖 3.00 元总额、2.40 元 Phase 13 上限、0.60 元 Phase 14 预留、候选额度、reserve/settle/release 和未知 usage 保守结算。
- [ ] 在迁移中创建 budget ledger、reservation 和 model-call 表，使用唯一 request_id、状态约束、金额非负约束和版本列。
- [ ] 实现内存与 PostgreSQL Store；预留在行锁事务中同时校验总额、阶段预留和候选额度。
- [ ] 验证两个连接并发预留只能有一个越过临界余额；崩溃后未结 reservation 可按持久状态对账。
- [ ] 验证提前拒绝只释放未消费候选额度，不能动用 Phase 14 的 0.60 元。
- [ ] 运行迁移 dry-run、专项和真实 PostgreSQL 测试；提交并推送：`feat: persist agent model budgets`。

## Task 4：BoundedSpecialistRunner 与 Evidence Resolver

**Files:**

- Create: `src/specialist_runtime/evidence.py`
- Create: `src/specialist_runtime/runner.py`
- Test: `tests/unit/test_phase13_specialist_runner.py`

- [ ] 写红灯测试覆盖 deadline、调用预算、Profile 覆盖攻击、非法动作、越权 Skill、伪造 EvidenceRef、结果 Schema 和禁止 chain-of-thought。
- [ ] 实现 Event、Plan、PlanNode、SkillAttempt、Audit、Replay、Memory、Evaluation resolver，并交叉校验 source version、digest、anchor 和 room。
- [ ] 实现固定 Runner 顺序：预算预留、模型单次尝试、动作校验、Evidence 校验、SkillExecutor、结果校验、费用结算。
- [ ] 模型失败、预算不足、策略拒绝和非法输出返回对应 AgentResult；正式评估不得调用 baseline。
- [ ] 实现生产建议门面：只有已保留 Profile 可在失败后调用确定性 baseline，并返回 `FALLBACK`，不得把 fallback 计为 Agent 成功。
- [ ] 运行 Runner、Skill Runtime、PlanEngine/Harness 权限回归；提交并推送：`feat: add bounded specialist runner`。

## Task 5：Evaluation Store、配对比较与迁移

**Files:**

- Create: `src/specialist_evaluation/models.py`
- Create: `src/specialist_evaluation/store.py`
- Create: `src/specialist_evaluation/comparison.py`
- Modify: `docker/init_phase13_specialist_evaluations.sql`
- Test: `tests/unit/test_phase13_evaluation_store.py`
- Test: `tests/integration/test_phase13_evaluation_store_postgres.py`

- [x] 写红灯测试覆盖 Manifest 哈希、run/case/subject 唯一性、attempt 历史、正式结果选择、retention decision 和重复聚合拒绝。
- [x] 扩展迁移，创建 evaluation manifest/run、case attempt、selected result、paired metric 和 retention decision 表。
- [x] 实现内存/PostgreSQL Store；同 manifest/candidate/case/subject 只能有一个 selected 结果，重跑保留 attempt。
- [x] 实现配对聚合：绝对指标、百分点差、paired wins/losses 和 Wilson 区间；严重违规独立聚合且不可被平均分抵消。
- [x] 实现 `RETAINED | REJECTED | INCONCLUSIVE`，并验证 INCONCLUSIVE 只用于外部证据不足。
- [x] 运行真实 PostgreSQL 并发 claim/选择测试及 Phase 7A Evaluation 回归；提交并推送：`feat: persist paired specialist evaluations`。

## Task 6：240 例数据集与 Evaluation Manifest

**Files:**

- Create: `evaluation/schemas/phase13_case.schema.json`
- Create: `evaluation/generators/generate_phase13_cases.py`
- Create: `evaluation/cases/phase13/*.jsonl`
- Create: `evaluation/manifests/phase13-v2.json`
- Test: `tests/unit/test_phase13_dataset.py`

- [x] 为 LiveOps、Planner、ReviewMemory 各定义 20 development、40 validation、20 holdout 模板、变量范围和确定性 label。
- [x] 写红灯测试覆盖 240 个唯一 ID、固定 split、严格 Schema、无敏感字段、稳定排序和 SHA-256。
- [x] 实现固定 seed 生成器并固化 JSONL；重复生成必须字节一致，三个候选和 split 不得共享 case ID。
- [x] Manifest 固定 endpoint host、model、temperature、generator/Schema/Prompt 版本与哈希；正式价格字段要求来源 URL、日期、币种和输入/输出 token 单价。
- [x] Profile 同时绑定真实 Prompt 正文/摘要和精确 Skill 版本；case loader 在消费时校验 Manifest、原始字节摘要、Schema 与 case 身份，源码摘要覆盖全部 `src/**/*.py`。
- [x] EvaluationManifest 区分数据集基线与正式评估；基线不得创建 Run，正式 Manifest 必须绑定最终 40 位 Git commit。
- [x] 禁止候选 Prompt 代码读取 holdout labels；development 允许 ScriptedModel 和每候选最多 5 个真实 smoke case。
- [x] 运行生成器两次、字节比较和敏感信息扫描；提交并推送：`test: add phase 13 paired datasets`。

## Task 7：LiveOpsAgent 纵向切片

**Files:**

- Create: `src/specialist_runtime/live_ops.py`
- Create: `src/specialist_evaluation/live_ops.py`
- Test: `tests/unit/test_phase13_live_ops.py`
- Test: `tests/integration/test_phase13_live_ops_evaluation.py`

- [x] 写 PriorityLiveOpsPolicy baseline 红灯测试，覆盖未解除风险、已闭合售罄、弹幕优先级和 no action。
- [x] 按 D-110 生成版本化 LiveOps 修正版 case/label/Manifest；保留 v2 审计基线，不降低冻结门槛。
- [x] 写 Agent 输出枚举、EvidenceRef、2 模型/3 Skill/4000 tokens/5 秒和禁止高风险写测试。
- [x] 实现 baseline 与 Agent adapter，保证两者消费同一冻结 case；允许五个播中只读/生成 Skill。
- [x] 从 `acceptable_actions`、`incident_recovery_actions` 和共同门禁计算 action success、incident recovery 与严重违规；严格门为 90%/+5pp 与 85%/+10pp 的 AND。
- [x] 使用 ScriptedModel 跑完整 80 例，验证四个 validation shard、早停数学和 holdout 解封。
- [x] 运行 Harness、Preemption Evidence 和 Skill 权限回归；提交并推送：`feat: evaluate live ops specialist`。

## Task 8：PlannerAgent 与记忆读取切片

**Files:**

- Modify: `src/skill_runtime/catalog.py`
- Modify: `src/skill_runtime/handlers.py`
- Create: `src/specialist_runtime/planner.py`
- Create: `src/specialist_evaluation/planner.py`
- Test: `tests/unit/test_phase13_planner.py`
- Test: `tests/integration/test_phase13_planner_evaluation.py`

- [ ] 写 `retrieve_anchor_memory@1.0.0` 严格 Schema、作用域、脱敏、active-only 和 limit 红灯测试。
- [ ] 实现只读 Handler/Port；Catalog 从 13 增至 14 个单活 Manifest，不返回 embedding 或跨主播记忆。
- [ ] 写 CandidatePlanProposal 白名单、循环、绑定、执行控制字段和禁止 query_products/建播测试。
- [ ] 实现 Planner baseline、Agent adapter 和 Validator/Compiler；Agent 正式运行 Skill 上限为 0，只读冻结商品、记忆和计划证据。
- [ ] 实现 executable >=95% 与 constraint recovery >=85%/+10pp 门；非法候选不得用 baseline 替代后计成功。
- [ ] 使用 ScriptedModel 跑 80 例并运行 PlanEngine/Memory 回归；提交并推送：`feat: evaluate planner specialist`。

## Task 9：播后 Skill、MemoryCandidate 与 PromotionPolicy

**Files:**

- Modify: `src/skill_runtime/catalog.py`
- Modify: `src/skill_runtime/handlers.py`
- Create: `src/memory/candidate_store.py`
- Create: `src/memory/promotion_policy.py`
- Create: `docker/init_phase13_memory_candidates.sql`
- Test: `tests/unit/test_phase13_post_live_skills.py`
- Test: `tests/integration/test_phase13_memory_candidates_postgres.py`

- [ ] 写三个播后 Manifest、显式证据输入、脱敏输出、幂等 staging 和禁止 active write 红灯测试。
- [ ] 实现 evidence collection、纯确定性 attribution 和 staging Handler；Catalog 从 14 增至 17 个单活 Manifest。
- [ ] 创建 MemoryCandidate 与 PromotionCommand 表，状态为 STAGED/APPROVED/REJECTED/APPLIED，命令校验唯一 ID、expected version/status。
- [ ] 实现双 DecisionTrace、同作用域、无冲突、货盘白名单和确定性模板 PromotionPolicy。
- [ ] 验证单证据、冲突、跨主播/房间、敏感字段和白名单不匹配均不能自动晋升。
- [ ] 使用真实 PostgreSQL 验证幂等晋升和下一次 `retrieve_anchor_memory` 可读取；提交并推送：`feat: govern post live memory promotion`。

## Task 10：ReviewMemoryAgent 纵向切片

**Files:**

- Create: `src/specialist_runtime/review_memory.py`
- Create: `src/specialist_evaluation/review_memory.py`
- Test: `tests/unit/test_phase13_review_memory.py`
- Test: `tests/integration/test_phase13_review_memory_evaluation.py`

- [ ] 写 Review 输入/输出 Schema、3 模型/4 Skill/8000 tokens/20 秒、禁止自由文本记忆和禁止 active write 红灯测试。
- [ ] 实现确定性 evidence/attribution/stage baseline 与 Agent adapter；两者使用同一三个播后 Skill 和冻结 case。
- [ ] 实现 grounded attribution >=90%/+5pp 与 macro-F1 >=0.85/+0.10 门，以及无证据、跨主播、敏感信息和绕过 PromotionPolicy 严重违规。
- [ ] 使用 ScriptedModel 跑 80 例，验证规则、早停、holdout 解封和 Candidate Store 隔离。
- [ ] 运行 Replay、Evaluation、DecisionTrace、MemoryStore 和 Planner 记忆读取回归。
- [ ] 提交并推送：`feat: evaluate review memory specialist`。

## Task 11：正式评估、早停与条件生产接入

**Files:**

- Create: `src/specialist_evaluation/runner.py`
- Create: `scripts/run_phase13_evaluation.py`
- Modify: `src/config/settings.py`
- Test: `tests/unit/test_phase13_retention.py`
- Test: `tests/integration/test_phase13_formal_evaluation.py`

- [ ] 写价格/模型/endpoint/usage/哈希预检、10 例 shard、严重违规早停、数学早停和一次 holdout 红灯测试。
- [ ] 在 Task 7-10 代码冻结后生成新的正式 Manifest，绑定最终 Git commit、全部运行源码、Prompt、Schema、数据和价格快照；Task 6 数据集基线 Manifest 不得直接注册正式 Run。
- [ ] 实现正式 CLI：先 baseline，再按 LiveOps、Planner、ReviewMemory 顺序运行 validation；只有资格候选运行 holdout。
- [ ] 每个模型请求使用 Task 3 的 reservation；候选额度不足时尝试 Phase 13 公共剩余，禁止使用 Phase 14 预留。
- [ ] 对规则通过候选最多抽样 10 对 holdout 调用 Judge；Judge 结果只写诊断字段，不改 retention decision。
- [ ] `RETAINED` 后才注册默认关闭的 DETERMINISTIC/SPECIALIST_AGENT 路由；REJECTED/INCONCLUSIVE 不创建生产 Profile。
- [ ] 使用 ScriptedModel 完整演练所有结论；真实模型只在官方价格、manifest、endpoint 和预算预检全部通过后运行。
- [ ] 逐候选独立提交去留证据，最后提交并推送：`feat: decide specialist retention`。

## Task 12：多 Agent 接口、业务附录与 Acceptance

**Files:**

- Create: `scripts/run_phase13_specialist_demo.py`
- Create: `tests/unit/test_phase13_demo.py`
- Create: `docs/superpowers/reports/phase-13-specialist-agent-evaluation-acceptance.md`
- Modify: 路线图、总控计划、实时状态和三个 worklog

- [ ] 写 Demo 红灯测试，覆盖 0/1/2/3 retained Profile 的确定性路由、Agent 禁止互调和 EvidenceRef 跨阶段传递。
- [ ] 实现无付费 Demo，展示三个 baseline、候选结论、可审计 FALLBACK 和记忆双证据闭环。
- [ ] 为 `live-session-p001-sold-out-v1` 只读生成 `agent-decision-appendix.json` 与 Markdown，不改写 Phase 12B Trace。
- [ ] Acceptance 逐候选记录样本、早停、绝对指标、paired delta、Wilson 区间、p95、tokens、人民币费用和结论。
- [ ] 运行 Phase 13 专项、完整 unit/integration、真实 PostgreSQL、Demo、迁移 dry-run、严格 UTF-8、编码扫描和 `git diff --check`。
- [ ] 将实时状态更新为 `AWAITING_PHASE_14_GATE`，不开始 Phase 14。
- [ ] 提交并推送：`feat: complete phase 13 specialist evaluation`。

## Acceptance 硬门

- 三个候选都有 `RETAINED | REJECTED | INCONCLUSIVE` 结论，且 0 表示 0 个新增 Specialist，不影响现有播中 Agent Harness。
- 所有 RETAINED 候选完成 40 validation + 20 holdout、严重违规 0，并满足各自严格 AND 门。
- 240 个 case、Manifest、Prompt/Schema/价格哈希和配对结果可重复。
- Phase 13 消费不超过 2.40 元，Phase 14 的 0.60 元预留仍可用。
- 未保留候选从未进入生产 Registry；保留候选路由默认关闭。
- 多 Profile 可并存，但 Agent 不直接调用 Agent；记忆反馈只通过正式 MemoryStore。
