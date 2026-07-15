# Phase 14 Golden Dataset and Release Gates Implementation Plan

文档状态：`DISCUSSION_BASELINE`

> **For agentic workers:** Implement task-by-task with RED, GREEN, REFACTOR. Do not begin until Phase 13 has persisted retention decisions, the Phase 14 Just-in-Time Gate updates this baseline, and the user explicitly authorizes Phase 14 implementation.

**Goal:** 把 Golden Dataset、确定性规则、LLM Judge、真实基础设施和版本证据收敛为可重复发布门禁。

**Architecture:** Git 保存不可变数据与版本 manifest，PostgreSQL 保存 case 级运行证据，GitHub Actions 分 PR/Nightly/Release 执行不同成本和基础设施层级，ReleaseDecision 以严重违规优先规则收敛。

**Tech Stack:** Python 3.12、Pydantic v2、jsonschema、pytest-cov、PostgreSQL 16、Kafka、GitHub Actions、DeepSeek API。

---

## Task 1：Golden Schema、Manifest 与版本治理

**Files:**

- Create: `src/evaluation/golden.py`
- Create: `evaluation/schemas/golden_manifest.schema.json`
- Create: `evaluation/generators/generate_phase14_runtime_cases.py`
- Create: `evaluation/cases/development/runtime-core-v1.jsonl`
- Create: `evaluation/cases/validation/runtime-core-v1.jsonl`
- Create: `evaluation/cases/holdout/runtime-core-v1.jsonl`
- Create: `evaluation/manifests/agent-runtime-v1.json`
- Test: `tests/unit/test_phase14_golden_dataset.py`

步骤：

1. 写 manifest semantic version、文件哈希、split、case ID、supersedes 和不可变 Release 版本红灯测试。
2. 实现 canonical SHA-256、Schema 校验和 manifest loader。
3. 生成 24 个 `runtime-core-v1` case：Skill Runtime、DAG/Checkpoint、Event/Replan 各 8 个，每类固定按 development 2、validation 4、holdout 2 拆分；其中一个 Event/Replan case 固定为 `live-session-p001-sold-out-v1` 变体。聚合 Phase 13 已冻结的 240 个 case，拒绝跨 split 重复 ID。
4. 生成器重复运行必须字节一致；首版 manifest 必须锁定 264 个 case、所有文件摘要及来源 manifest，Release 使用过的 manifest 禁止原地覆盖。
5. 运行数据集与敏感信息专项。
6. 提交：`feat: version golden evaluation datasets`。

## Task 2：统一 Evaluation Interface 与规则门禁

**Files:**

- Create: `src/evaluation/interface.py`
- Create: `src/evaluation/release_rules.py`
- Test: `tests/unit/test_phase14_evaluation_interface.py`
- Test: `tests/unit/test_phase14_release_rules.py`

步骤：

1. 写 deterministic/Agent subject 共用 runner、严格结果 Schema 和严重违规优先红灯测试。
2. 实现 EvaluationCaseResult、SubjectManifest 和规则执行接口。
3. 覆盖 Skill 版本/权限、Plan 状态、Event 授权、EvidenceRef、敏感信息和预算规则。
4. 严重违规必须直接 FAIL，不能被平均分或 Judge 修改。
5. 运行 Phase 7A 既有评估与 Phase 13 retention 回归。
6. 提交：`feat: enforce agent runtime release rules`。

## Task 3：独立 pro Judge 与模型证据

**Files:**

- Create: `src/evaluation/semantic_judge.py`
- Create: `evaluation/manifests/semantic-judge-v1.json`
- Test: `tests/unit/test_phase14_semantic_judge.py`
- Test: `tests/external/test_phase14_real_judge.py`

步骤：

1. 写 Judge 只能评分语义维度、规则失败不可覆盖、非法输出和调用失败 `UNAVAILABLE` 红灯测试。
2. 实现 `deepseek-v4-pro`、temperature 0、单次调用和严格 JSON Schema。
3. 保存模型/Prompt/Schema/价格哈希、usage、费用和响应摘要，不保存 chain-of-thought。
4. Fork/无 secret 环境必须跳过外部测试且不伪造语义 PASS。
5. 运行 Fake HTTP 单元测试；外部测试只在显式标记下执行。
6. 提交：`feat: add independent semantic judge`。

## Task 4：发布证据 Store 与 ReleaseDecision

**Files:**

- Create: `src/evaluation/release_store.py`
- Create: `src/evaluation/release_gate.py`
- Create: `docker/init_phase14_release_evaluations.sql`
- Modify: `scripts/run_db_migrations.py`
- Test: `tests/unit/test_phase14_release_gate.py`
- Test: `tests/integration/test_phase14_release_store_postgres.py`

步骤：

1. 写 case 结果、artifact digest、运行唯一性、预算和 PASS/FAIL/BLOCKED 红灯测试。
2. 实现 PostgreSQL ReleaseRun/CaseResult/Decision 表与内存替身。
3. 并发模型 Worker 复用 Phase 13 的持久化 BudgetLedger；当前连续实施固定使用 `agent-runtime-completion-v1` 和累计 3.00 元上限，达到上限后停止新 claim。
4. 缺 case、哈希不匹配、Judge 不完整或严重违规时 fail-closed。
5. 使用真实 PostgreSQL 验证并发聚合和重复运行幂等。
6. 提交：`feat: persist agent runtime release evidence`。

## Task 5：覆盖率、静态检查和本地统一入口

**Files:**

- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Create: `scripts/run_release_gate.py`
- Modify: `scripts/run_all.py`
- Test: `tests/unit/test_phase14_release_cli.py`

步骤：

1. 添加 `pytest-cov` dev 依赖和核心包 branch coverage 配置。
2. 统一入口支持 `--mode pr|nightly|release`、manifest、subject 和预算参数。
3. PR 模式拒绝外部模型和 Kafka；Nightly/Release 按 manifest 启用。
4. 覆盖率门禁统计 `src/skill_runtime`、`src/plan_engine`、`src/agent_runtime` 和 `src/evaluation`，branch coverage 低于 90% 退出非零。
5. 运行本地 PR 模式和 CLI 契约测试。
6. 提交：`build: add agent runtime release checks`。

## Task 6：PR GitHub Actions

**Files:**

- Create: `.github/workflows/agent-runtime-pr.yml`
- Test: `tests/unit/test_phase14_workflow_contracts.py`

步骤：

1. 写 YAML 解析测试，锁定 Python 3.12、PostgreSQL 16、无 Kafka、无模型 secret 和 artifact 14 天。
2. 工作流执行依赖安装、迁移、默认 pytest、ScriptedModel、数据集、编码、敏感信息和覆盖率门禁。
3. Fork PR 不引用 protected environment 或 secret。
4. 并发组取消同分支旧 PR run，不影响 Release。
5. 使用 actionlint（可用时）和本地契约测试验证。
6. 提交：`ci: add agent runtime pull request gate`。

## Task 7：Nightly 与 Release GitHub Actions

**Files:**

- Create: `.github/workflows/agent-runtime-nightly.yml`
- Create: `.github/workflows/agent-runtime-release.yml`
- Modify: `tests/unit/test_phase14_workflow_contracts.py`

步骤：

1. Nightly 启动 PostgreSQL/Kafka，artifact 保留 30 天；默认不调用模型。
2. 只有 secret 和 `ENABLE_PAID_NIGHTLY=true` 同时满足时启用真实抽样，默认预算 0.10 元。
3. Release 使用 workflow_dispatch 和受保护 environment，artifact 保留 180 天。
4. Release 固定完整 holdout、真实 Kafka/PostgresSaver、保留 Agent flash 调用和 pro Judge。
5. YAML 契约测试断言 secret 不出现在日志参数、PR 不可触发 Release。
6. 提交：`ci: add nightly and release gates`。

## Task 8：ToolRegistry Facade 删除

**Files:**

- Delete: `src/config/tool_registry.py`
- Modify: `src/skill_runtime/models.py`
- Modify: `src/skill_runtime/catalog.py`
- Modify: `src/skill_runtime/__init__.py`
- Modify: `src/core/agent_decision.py`
- Delete: `tests/unit/test_tool_registry.py`
- Modify: `tests/unit/test_skill_catalog.py`
- Test: `tests/unit/test_phase14_tool_registry_retirement.py`

步骤：

1. 使用 `rg` 复核 Phase 12B Acceptance 的调用清单；除待删除 Facade 和兼容测试外，生产导入必须已为 0。
2. 删除 Facade、公开导出和只验证旧 API 的测试；治理查询继续统一使用 Catalog/SkillPolicyView。
3. `rg -n "ToolRegistry|get_default_tool_registry|from src\.config\.tool_registry" src` 必须为空；不在本 Task 修改任何路由默认值。
4. 运行全量 Skill/Hook/Flow/Graph/Agent 回归和删除契约测试。
5. 提交：`refactor: retire legacy tool registry`。

## Task 9：完整 Release 演练与默认路由晋升

**Files:**

- Create: `evaluation/reports/agent-runtime-v1-release.json`
- Create: `docs/superpowers/reports/phase-14-golden-release-gates-acceptance.md`
- Modify: `src/config/settings.py`
- Modify: `src/skill_runtime/routing.py`
- Modify: `src/plan_engine/routing.py`
- Modify: `src/plan_engine/preemption.py`
- Create: `tests/unit/test_phase14_default_routes.py`

步骤：

1. 对 Task 8 已提交 HEAD 运行 PR 和 Nightly 免费路径；使用 SubjectManifest 显式指定三批 Skill、手卡和售罄的新路由，运行第一次完整 Release。
2. 完整 holdout、保留 Agent 的 flash 模型和 pro Judge 共享 `agent-runtime-completion-v1` 累计 3.00 元上限；余额不足或外部证据缺失必须写 `BLOCKED` 并暂停，不能缩样或自动加预算。
3. 第一次 Release PASS 后，才把三批 Skill 默认值切为 `SKILL_RUNTIME`，手卡与可信售罄默认值切为 `PLAN_ENGINE`；显式 Legacy 启动回滚保留一个兼容周期，同次 fallback 继续禁止。
4. 运行默认路由、配置冻结和无 fallback 回归，提交并推送：`feat: promote agent runtime defaults`。
5. 对该新提交使用同一 dataset/subject manifest 再运行完整 Release；第二次失败时用新提交恢复 Legacy 默认值并保持 Acceptance 未通过，禁止 `reset` 或改写历史。
6. 第二次 PASS 后生成测试、覆盖率、数据/模型/Prompt/价格哈希和版本差异报告，并把业务闭环 Trace、Agent 条件化附录、Manifest、规则门禁和 ReleaseDecision 汇总为最终业务闭环报告；Acceptance 记录两次运行的命令、commit、URL/ID（可用时）和 artifact digest。
7. 提交并推送：`feat: complete phase 14 release gates`。

## Task 10：Agent Runtime 总体验收

**Files:**

- Create: `docs/superpowers/reports/agent-runtime-final-acceptance.md`
- Modify: 总控计划、路线图、决策日志、恢复提示词、实时状态和三个 worklog

最终验证：

```text
pytest -q
pytest --cov=src/skill_runtime --cov=src/plan_engine --cov=src/agent_runtime --cov=src/evaluation --cov-branch --cov-fail-under=90
python scripts/run_release_gate.py --mode pr
python scripts/run_release_gate.py --mode nightly
python scripts/run_release_gate.py --mode release --budget-scope agent-runtime-completion-v1 --budget-cny 3.00
python scripts/run_db_migrations.py --dry-run
rg -n "ToolRegistry|get_default_tool_registry" src
git diff --check
python scripts/check_doc_encoding.py
```

总体验收必须明确三场景能力、最终保留 Agent、默认路由、回滚方式、未完成边界、所有版本哈希和业务闭环报告结论。若 Release 为 `FAIL` 或 `BLOCKED`，报告必须保留失败事实，不能声称业务闭环成功。实时状态改为 `COMPLETE` 后提交：`docs: accept agent runtime completion`。

## Plan Self-Review

- PR 不产生模型费用，Nightly 付费必须显式 opt-in，Release 必须人工触发。
- Judge 永远不能覆盖严重规则失败。
- 覆盖率门只约束核心新 Runtime，不制造历史模块虚假补测。
- 默认路由只在显式新路由的第一次 Release PASS 后切换，并由新提交上的第二次 Release 再次验证。
- 即使保留 0 个 Agent，确定性 Runtime 仍能完成发布。
