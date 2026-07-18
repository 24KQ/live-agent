# Phase 15 Golden Release Gates Implementation Plan

文档状态：`PHASE_15_TASK_11_READY_TO_PUSH`

本文件已完成 Stage A 持久化；用户已授权 Stage B。Task 1-10 已推送，Task 11 验证完成待提交；D-133 记录路由 profile Schema 扩展。

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` for isolated implementation and review tasks; otherwise use `executing-plans` task-by-task. Steps use the repository RED, GREEN, REFACTOR, REVIEW, VERIFY, DOCS, COMMIT, PUSH protocol.

**Goal:** 将三场景人机协同 Runtime 收敛为可复现的技术 Release，并在真实模型与真人证据完整时独立决定 Copilot 是否晋升默认路由。

**Architecture:** 新增 `src/release_gates/` 作为 Golden、规则、Release Store、双轨结论和本地 CLI 的统一内核；复用现有 Workspace、Decision Support、SkillPolicyView、PlanEngine、Event Inbox 和 Budget Ledger。GitHub Actions 只编排冻结的本地入口，规则门禁不依赖 LLM Judge。

**Tech Stack:** Python 3.12、Pydantic v2、PostgreSQL 15、Kafka、FastAPI、原生 HTML/JavaScript、pytest、pytest-cov、jsonschema、GitHub Actions、现有 DeepSeek Model Port。

---

## 固定执行协议

- Stage A 文档完成并推送后，必须重新获得用户授权才进入 Task 1。
- Task 1-12 每个独立执行 `RED -> GREEN -> REFACTOR -> REVIEW -> VERIFY -> DOCS -> COMMIT -> PUSH`。
- 主模型负责迁移、安全状态机、Release 聚合、最终验证、提交和推送；sub-agent 只处理明确边界的分析、实现或审查。
- 不提交红灯、半成品、已知失败代码或用户已有脏文件。
- 真实模型只允许在 Task 6 预检通过后执行；真人采集器没有真实参与者时只能产生 `BLOCKED`，不得生成伪造 Promotion 证据。
- Phase 15 完成后不进入新 Phase；最终状态由双轨 Release 结论决定。

## Task 1：发布入口、迁移清单与仓库事实

**Files:** `scripts/run_db_migrations.py`、`scripts/run_all.py`、`scripts/check_sensitive_payloads.py`、`pyproject.toml`、`README.md`、新建 Phase 15 迁移测试。

- RED：证明迁移 dry-run 遗漏 Phase 13 Memory、Phase 14 Decision Support/Memory Feedback；统一入口缺少 Phase 13/14/15 Demo；敏感扫描器不能编译。
- GREEN：按依赖顺序加入已有 DDL 与 Phase 15 DDL；增加 `phase13-demo`、`phase14-demo`、`phase15-demo`；修复 tracked-file 编码/敏感扫描参数；更新 README 和 dev 依赖。
- VERIFY：运行迁移 dry-run、入口 help、敏感扫描和 README 命令契约。
- COMMIT/PUSH：`build: align phase 15 release entrypoints`。

## Task 2：48 例 Golden Dataset 与 Manifest

**Files:** 新建 `src/release_gates/dataset.py`、`evaluation/schemas/phase15_golden_manifest.schema.json`、`evaluation/generators/generate_phase15_cases.py`、`evaluation/manifests/phase15-runtime-v1.json`、相关测试。

- RED：覆盖 48 case、12/24/12 split、三场景、归档 Phase 13 Manifest、重复 ID、敏感字段、Manifest 摘要和生成器字节稳定。
- GREEN：生成 24 Runtime、复用 16 Phase 14 live case、新增 8 PREPARE/REVIEW case；Manifest 记录来源文件、case 摘要、Schema/规则/源码摘要和 supersedes。
- VERIFY：生成器连续运行两次逐字节比较；Phase 13 240 例只做历史完整性检查；专项、unit/integration、编码和差异门禁通过。
- COMMIT/PUSH：`feat: version phase 15 golden dataset`。

## Task 3：统一 Subject Runner 与规则门禁

**Files:** 新建 `src/release_gates/models.py`、`src/release_gates/rules.py`、`src/release_gates/runner.py`、相关测试。

- RED：覆盖 Skill 版本/权限、Plan/Event 状态、EvidenceRef、CAS/fencing、幂等、敏感信息、费用和 no-fallback 严重违规。
- GREEN：实现 `GoldenCase`、`SubjectManifest`、`EvaluationCaseResult` 和五类受限 Runner；规则失败直接阻断 case，不允许模型或平均分覆盖。
- VERIFY：Task 3 专项、unit/integration、编译、敏感和编码门禁通过；PR/Release case 执行由后续 CLI Task 接管。
- COMMIT/PUSH：`feat: enforce release subject rules`。

## Task 4：Release Store、双轨决策与 Phase 15 预算

**Files:** 新建 `src/release_gates/store.py`、`src/release_gates/decisions.py`、`docker/init_phase15_release_gates.sql`，修改预算迁移和相关测试。

- RED：覆盖 ReleaseRun 幂等、case 结果唯一性、并发聚合、缺 case、digest 不匹配、Technical/Promotion 双结论和预算超限。
- GREEN：实现内存/PostgreSQL Store、`TechnicalReleaseDecision`、`DecisionSupportPromotionDecision`、`FinalReleaseStatus`；预算账本新增 `PHASE15_COPILOT_SMOKE=0.60` 且禁止借用 Phase 13/14。
- VERIFY：内存/PostgreSQL 重启、并发写入、重复运行、缺 case、digest 冲突和 Phase 15 独立预算边界测试通过。
- COMMIT/PUSH：`feat: persist dual release decisions`。

## Task 5：真人交叉对照采集器

**Files:** 新建 `src/release_gates/human_study.py`、扩展 `src/gateway/api_server.py`、`front/index.html`、Phase 15 study DDL 和测试。

- RED：缺参与者、重复 assignment、错误条件、客户端伪造耗时、自由文本、PII、缺行和重复提交必须拒绝。
- GREEN：提供 study session、next trial、response 三个受保护接口；服务端生成 3-5 人、每人 8 次的平衡 assignment，保存加盐参与者摘要、封闭动作、工作负担和服务端耗时。
- VERIFY：24/32/40 条记录、重启恢复、幂等、跨参与者作用域、Scripted diagnostic 与 Promotion-eligible digest 隔离测试。
- COMMIT/PUSH：`feat: capture blinded operator studies`。

## Task 6：真实 Copilot Smoke 与 Promotion 证据

**Files:** 新建 `src/release_gates/copilot_smoke.py`、修改预算/模型入口、外部与单元测试。

- RED：缺 endpoint/价格/usage/hash、fallback、Schema 错误、严重违规、重复请求和预算超限时阻止 Promotion。
- GREEN：在预检通过后最多调用 10 个 `deepseek-v4-flash` case，每例单次调用，总额不超过 0.60 元；usage 缺失按预留额结算并返回 `BLOCKED`。
- VERIFY：默认不联网；受保护环境才执行真实 smoke；检查 10/10、fallback 0、严重违规 0、usage 完整、安全正确率至少 90%。
- COMMIT/PUSH：`feat: evaluate phase 15 copilot smoke`。

## Task 7：PromotionDecision 与双轨 Acceptance

**Files:** `src/release_gates/decisions.py`、`src/release_gates/report.py`、相关测试和报告模板。

- RED：证明缺真人/模型证据返回 `BLOCKED`，完整但未达质量门返回 `KEEP_DISABLED`，全部严格 AND 满足才返回 `PROMOTE`。
- GREEN：生成技术发布、Copilot 晋升和最终状态三份稳定 JSON/Markdown 结果。
- VERIFY：分别覆盖 `RELEASED_DECISION_SUPPORT_ENABLED`、`RELEASED_DECISION_SUPPORT_DISABLED` 和 `NOT_RELEASED`。
- COMMIT/PUSH：`feat: decide decision support promotion`。

## Task 8：统一 Release CLI 与报告

**Files:** 新建 `scripts/run_release_gate.py`、`scripts/check_coverage_gate.py`、`scripts/fetch_github_actions_evidence.py`、测试。

- RED：非法 mode、Manifest/subject 不匹配、缺数据库、覆盖率不足和外部证据缺失必须返回非零或明确 `BLOCKED`。
- GREEN：实现 `--mode pr|nightly|release`、Manifest/subject/budget 输入、artifact digest、JSON/Markdown 报告和稳定退出码；`phase15-demo` 使用相同内核。
- VERIFY：运行本地 PR/Nightly/Release dry-run，确认真实模型不会在 `pr`/`nightly` 默认调用。
- COMMIT/PUSH：`build: add local phase 15 release gates`。

## Task 9：GitHub Actions 三层门禁

**Files:** 新建 `.github/workflows/agent-runtime-pr.yml`、`agent-runtime-nightly.yml`、`agent-runtime-release.yml`、workflow contract tests。

- RED：YAML 解析测试证明缺 Python 3.12、错误 PostgreSQL 版本、PR 泄露 secret、Release 可被普通 PR 触发或 artifact retention 错误。
- GREEN：PR 使用 PostgreSQL 15 和 36 case；Nightly 使用 PostgreSQL/Kafka/PostgresSaver；Release 使用 tag/手动触发、48 case 和 180 天 artifact。
- VERIFY：在精确 commit 上取得真实 PR Gate 与 Release Actions green run evidence；服务不可用时保持 `BLOCKED`，不伪造通过。
- COMMIT/PUSH：`ci: add hosted agent runtime gates`。

## Task 10：ToolRegistry Facade 退役

**Files:** 删除 `src/config/tool_registry.py`，修改 `src/core/agent_tool_executor.py`、`src/skill_runtime/__init__.py`、README、Phase 13/15 Manifest 和旧测试/治理测试。

- RED：证明生产导入已为零，但旧 Facade/兼容参数仍存在。
- GREEN：删除公共 Facade、注册表兼容参数和只验证旧 API 的测试；统一使用 Catalog/SkillPolicyView，所有要求幂等键的 Runtime Skill 将其放入 Context，Legacy 异常摘要固定脱敏，Legacy 只保留显式回滚路由。
- VERIFY：`rg -n "ToolRegistry|get_default_tool_registry|src\.config\.tool_registry" src` 无命中；Phase 13/15 Manifest 源码闭包重建；Task 10 专项 `21 passed`，完整 unit `1372 passed, 4 warnings`，完整 integration `155 passed, 3 deselected, 5 warnings`。
- COMMIT/PUSH：`refactor: retire tool registry facade`。

## Task 11：显式 Release、默认路由与第二次 Release

**Files:** `src/config/settings.py`、`src/skill_runtime/routing.py`、`src/plan_engine/routing.py`、Release 报告和默认路由测试。

- RED：显式新 Runtime、旧默认、Decision Support 独立默认和同次 no-fallback 的路由测试先失败。
- GREEN：第一次 Release 使用显式 `SKILL_RUNTIME`/`PLAN_ENGINE`；第一次 PASS 后切换确定性默认。只有 Promotion `PROMOTE` 才切换 `DECISION_SUPPORT`。
- VERIFY：推送 `phase15-explicit-v1.0.0-rc1`，再推送默认值提交和 `phase15-defaults-v1.0.0-rc1`；第二次失败必须用 revert commit 恢复，不重写历史。
- COMMIT/PUSH：确定性晋升使用 `feat: promote verified runtime defaults`；Acceptance 留痕拆分独立提交。

## Task 12：Demo、Phase 15 Acceptance 与 Final Acceptance

**Files:** `scripts/run_all.py`、`README.md`、新建 `docs/superpowers/reports/phase-15-golden-release-gates-acceptance.md`、`docs/superpowers/reports/agent-runtime-final-acceptance.md`、路线图和 worklog。

- RED：报告缺少 48 case、双轨决策、CI run、路由和三场景闭环证据时拒绝完成。
- GREEN：一键展示 PREPARE/LIVE/REVIEW、自动保护、人工决定、记忆和 Release 摘要；完整报告诚实记录 `PROMOTE | KEEP_DISABLED | BLOCKED`。
- VERIFY：unit/integration、coverage line90/branch85、migration dry-run、PR/Nightly/Release 命令、GitHub run evidence、编码/敏感扫描和 `git diff --check`。
- COMMIT/PUSH：`docs: accept agent runtime release`；最终不进入新 Phase。

## Sub-agent 与文档协议

- Task 2/3/4 可并行派发只读 Dataset、Schema、SQL 和测试分析；Task 5/6 可派发独立审查，但共享数据库迁移和 Release 聚合仍由主模型串行整合。
- 子智能体 20 分钟无可验证进展、连续两次同一阻塞、越界或建议放宽门槛时立即停止并由主模型接管。
- 每个 Task 开始、RED、GREEN、审查整改、验证、提交和推送都必须更新 `continuous_execution_state.md`、`task_plan.md`、`findings.md` 和 `progress.md`。

## 最终验证命令

```text
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python scripts/check_coverage_gate.py --line 90 --branch 85
python scripts/run_db_migrations.py --dry-run
python scripts/run_release_gate.py --mode pr
python scripts/run_release_gate.py --mode nightly
python scripts/run_release_gate.py --mode release
python scripts/check_doc_encoding.py --tracked
python scripts/check_sensitive_payloads.py --tracked
git diff --check
```

最终技术发布没有真人或真实模型证据时仍可为 `PASS`，但最终状态必须是 `RELEASED_DECISION_SUPPORT_DISABLED`，不能写成 Copilot 已晋升。
