# Phase 13 Specialist Agent Evaluation Implementation Plan

> **For agentic workers:** Implement task-by-task with RED, GREEN, REFACTOR. Do not begin until Phase 12B Acceptance passes and continuous implementation is authorized.

**Goal:** 使用统一受限 Harness 和版本化数据集，独立判断三个 Specialist Agent 是否值得进入正式架构。

**Architecture:** 三个候选共用 BoundedSpecialistRunner、Skill Runtime 和 Evaluation Interface；确定性基线与 Agent 使用相同输入、能力、Hook 和证据；最终由严格规则生成条件化去留结论。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、psycopg 3、PostgreSQL、DeepSeek 兼容 API、pytest。

---

## Task 1：Agent 协议、Profile 与预算

**Files:**

- Create: `src/agent_runtime/__init__.py`
- Create: `src/agent_runtime/models.py`
- Create: `src/agent_runtime/profiles.py`
- Create: `src/agent_runtime/budget.py`
- Test: `tests/unit/test_phase13_agent_models.py`
- Test: `tests/unit/test_phase13_agent_budget.py`

步骤：

1. 写 AgentTask/Action/Result/EvidenceRef、动作互斥、严格 JSON 和 Profile 冻结红灯测试。
2. 写模型/Skill 调用数、Token、deadline、费用和人民币全局预算并发红灯测试。
3. 实现深度冻结值对象、稳定摘要和 fail-closed BudgetLedger。
4. 明确不接受 thought/chain_of_thought 字段；额外字段导致 Schema 失败。
5. 运行专项测试。
6. 提交：`feat: add bounded specialist contracts`。

## Task 2：AgentModelPort 与共享 Runner

**Files:**

- Create: `src/agent_runtime/model_port.py`
- Create: `src/agent_runtime/deepseek_model.py`
- Create: `src/agent_runtime/runner.py`
- Create: `src/agent_runtime/scripted_model.py`
- Test: `tests/unit/test_phase13_specialist_runner.py`

步骤：

1. 写单次模型调用、非法 JSON、越权 Skill、EvidenceRef 伪造、循环预算和结构化 fallback 红灯测试。
2. 实现原生 async `AgentModelPort`，并在独立 `deepseek_model.py` 中实现无隐藏重试的 DeepSeek Adapter。
3. 实现 ScriptedModel Fixture，用固定动作序列覆盖全部规则和 CI。
4. Runner 每轮先校验预算，再调用模型或 Skill；任何失败写结构化结果，不能静默扩展步骤。
5. 记录 usage、延迟、费用和响应摘要哈希，不保存原始 chain-of-thought。
6. 提交：`feat: add bounded specialist runner`。

## Task 3：Evaluation Manifest、Store 与规则接口

**Files:**

- Create: `src/evaluation/__init__.py`
- Create: `src/evaluation/specialist_models.py`
- Create: `src/evaluation/specialist_store.py`
- Create: `docker/init_phase13_specialist_evaluations.sql`
- Modify: `scripts/run_db_migrations.py`
- Test: `tests/unit/test_phase13_evaluation_store.py`
- Test: `tests/integration/test_phase13_evaluation_store_postgres.py`

步骤：

1. 写 manifest 哈希、run/case 唯一性、attempt 历史、正式结果选择、retention decision 和预算并发预留红灯测试。
2. 新增三张评估结果表与一张 `model_budget_ledgers` 表及索引，结果 JSONB 必须通过 Pydantic 重新校验。
3. 实现内存与 PostgreSQL Store；同一正式 case 不允许两个结果进入聚合，预算使用行锁和乐观版本阻止并发超额。
4. 价格表缺失、usage 缺失或哈希不匹配时阻止正式评估；usage 不可确认时按预留上限结算。
5. 使用真实 PostgreSQL 验证 case claim、预算 reserve/settle、并发上限和稳定重放。
6. 提交：`feat: persist specialist evaluations`。

## Task 4：四个跨场景 Skill 与 Memory Candidate Store

**Files:**

- Modify: `src/skill_runtime/catalog.py`
- Modify: `src/skill_runtime/handlers.py`
- Create: `src/memory/candidate_store.py`
- Create: `src/memory/promotion_policy.py`
- Create: `src/skill_runtime/post_live_ports.py`
- Modify: `src/skill_runtime/models.py`
- Create: `docker/init_phase13_memory_candidates.sql`
- Modify: `scripts/run_db_migrations.py`
- Test: `tests/unit/test_skill_catalog.py`
- Test: `tests/unit/test_phase13_cross_scene_skills.py`
- Test: `tests/integration/test_phase13_memory_candidates_postgres.py`

步骤：

1. 写四个 Manifest、显式输入、脱敏输出、幂等 staging 和禁止 active write 红灯测试。
2. 定义只读主播记忆与播后证据 Port；I/O Handler 只经注入 Port/Store 读取，deterministic attribution 只消费显式证据快照，不读取隐藏全局状态。
3. 实现 memory retrieval、evidence collection、deterministic attribution 和 stage handlers；Catalog 从 13 个单活 Manifest 扩展为 17 个，并同步严格 Schema、生命周期和投影回归。
4. 实现 Candidate Store 状态机和 `MemoryPromotionCommand` 幂等账本，并把 SQL 加入统一迁移清单。
5. 实现双独立证据、作用域、冲突和货盘白名单 Promotion Policy。
6. 运行 Catalog、Memory、Replay、DecisionTrace 与真实 PostgreSQL 回归后提交：`feat: govern post live memory candidates`。

## Task 5：数据集生成与冻结

**Files:**

- Create: `evaluation/schemas/specialist_case.schema.json`
- Create: `evaluation/generators/generate_phase13_cases.py`
- Create: `evaluation/cases/development/*.jsonl`
- Create: `evaluation/cases/validation/*.jsonl`
- Create: `evaluation/cases/holdout/*.jsonl`
- Create: `evaluation/manifests/phase13-v1.json`
- Test: `tests/unit/test_phase13_dataset.py`

步骤：

1. 为三个候选分别定义场景模板、变量范围、确定性 labels 和固定 seed。
2. 写 case ID 唯一、20/40/20 split、严格 Schema、无敏感字段和 SHA-256 红灯测试。
3. 生成并固化 240 个 case；生成器重复运行必须字节一致。
4. development/validation/holdout 目录不得共享 case ID；Prompt 代码不能读取 holdout labels。
5. 运行数据集专项和敏感信息扫描。
6. 提交：`test: add specialist evaluation datasets`。

## Task 6：LiveOps 基线、候选与评估

**Files:**

- Create: `src/agent_runtime/live_ops.py`
- Create: `src/evaluation/live_ops_rules.py`
- Test: `tests/unit/test_phase13_live_ops_agent.py`
- Test: `tests/integration/test_phase13_live_ops_evaluation.py`

步骤：

1. 写 PriorityLiveOpsPolicy 的安全/售罄/弹幕/no-action 基线测试。
2. 写 Profile 白名单、2 模型/3 Skill、无写权限和 final result Schema 红灯测试。
3. 实现相同输入和 Skill 下的 baseline/Agent runner adapter。
4. 实现 action success、incident recovery、severe violation、延迟和成本聚合。
5. 先用 ScriptedModel 跑 80 例，确保评估管线和 fail-closed 规则稳定。
6. 提交：`feat: evaluate live ops agent`。

## Task 7：Planner 基线、候选 DAG 与评估

**Files:**

- Create: `src/agent_runtime/planner.py`
- Create: `src/evaluation/planner_rules.py`
- Modify: `src/plan_engine/proposal.py`
- Test: `tests/unit/test_phase13_planner_agent.py`
- Test: `tests/integration/test_phase13_planner_evaluation.py`

步骤：

1. 写固定 Provider 基线和受限 Candidate DAG 红灯测试。
2. Profile 只允许只读/建议 Skill，最大 3 模型/5 Skill。
3. PlanValidator 必须拒绝未知节点、循环、非法绑定和候选注入执行控制字段。
4. 实现 executable plan success、constraint recovery 和 severe violation 聚合。
5. 使用 ScriptedModel 跑 80 例，确认非法候选不会被模板替代后计为成功。
6. 提交：`feat: evaluate planner agent`。

## Task 8：ReviewMemory 基线、候选与评估

**Files:**

- Create: `src/agent_runtime/review_memory.py`
- Create: `src/evaluation/review_memory_rules.py`
- Test: `tests/unit/test_phase13_review_memory_agent.py`
- Test: `tests/integration/test_phase13_review_memory_evaluation.py`

步骤：

1. 写固定复盘链基线、Profile 预算和禁止直接正式写红灯测试。
2. 实现 Agent 结构化 attribution 和 MemoryCandidate 输出。
3. EvidenceRef 不存在、跨主播、敏感字段或自由文本写入必须 fail-closed。
4. 实现 grounded attribution accuracy、memory candidate F1 和 severe violation 聚合。
5. 使用隔离 Candidate Store 与 ScriptedModel 跑 80 例。
6. 提交：`feat: evaluate review memory agent`。

## Task 9：真实模型正式评估与条件化裁剪

**Files:**

- Create: `scripts/run_phase13_specialist_evaluation.py`
- Create: `src/evaluation/retention.py`
- Test: `tests/unit/test_phase13_retention_policy.py`
- Test: `tests/external/test_phase13_real_model_evaluation.py`

步骤：

1. 冻结 flash 模型、temperature 0、三个 Prompt/Schema 哈希和人民币价格表。
2. 先运行 development，仅允许形成新版本后重跑 validation；holdout 每个正式版本只运行一次。
3. 按 LiveOps、Planner、ReviewMemory 顺序消费 3 元总预算。
4. 每候选只有完整 40 validation + 20 holdout 才能进入 `RETAINED | REJECTED`，否则 `INCONCLUSIVE`。
5. 应用零严重违规、收益、延迟、Token 和费用门槛。
6. 删除未保留候选的生产 Profile/Prompt/装配；保留评估代码、数据和报告。
7. 若两个以上保留，实现确定性 SpecialistOrchestrator；否则不增加多 Agent 协调层。
8. 提交：`feat: decide specialist agent retention`。

## Task 10：阶段验收

**Files:**

- Create: `docs/superpowers/reports/phase-13-specialist-agent-evaluation-acceptance.md`
- Modify: 路线图、决策日志、实时状态和三个 worklog

验证命令：

```text
pytest tests/unit/test_phase13_*.py -q
pytest tests/integration/test_phase13_*.py -q
pytest -q
python scripts/run_phase13_specialist_evaluation.py --mode scripted
python scripts/run_phase13_specialist_evaluation.py --mode real --budget-cny 3.00
python scripts/run_db_migrations.py --dry-run
git diff --check
python scripts/check_doc_encoding.py
```

Acceptance 必须逐候选列出 baseline/Agent 样本数、成功率、领域指标、严重违规、p95、Token、费用、最终结论和删除/保留代码证据。真实模型失败或预算不足不得用 ScriptedModel 代替。

提交：`feat: complete phase 13 agent evaluation`。

## Plan Self-Review

- 每个候选先有确定性基线，且使用相同 Skill、Hook、权限和 case。
- 没有 Task 让 Agent 直接执行高风险写或正式记忆写。
- 去留结果由数据条件化，计划没有预先承诺多 Agent 数量。
- 3 元费用是并发安全硬门，不是验收后统计信息。
- 未通过候选最终不会留在生产装配中。
