# Phase 14 Human-Centered Decision Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` when an independent review or analysis task can be isolated; otherwise use `executing-plans` task-by-task. Steps use the repository RED, GREEN, REFACTOR, REVIEW, VERIFY, DOCS, COMMIT, PUSH protocol.

**Goal:** 交付三场景统一运营工作台和播中人机协同 Copilot，在不放宽 Runtime/PlanEngine 安全边界的前提下证明运营决策质量与效率收益。

**Architecture:** 复用已有 Harness、Skill Runtime、PlanEngine、Event Inbox、DecisionTrace、Replay、Profile Registry 和 WebSocket。新增不可变 Workspace 事实层、受限方案协议、人工决定编译链和三视图前端；确定性系统自动保护，运营确认经营恢复。

**Tech Stack:** Python 3.12、Pydantic v2、PostgreSQL 16、FastAPI、原生 HTML/JavaScript、WebSocket、pytest、现有 ScriptedModel/AgentModelPort。

---

## 固定执行协议

- 每个 Task 先更新 `continuous_execution_state.md`，记录目标、文件边界、禁止事项、当前 HEAD 和下一条命令。
- 每个 Task 必须完成 RED、GREEN、REFACTOR、规格审查、代码质量/安全审查、专项与相关回归、文档留痕、独立 ASCII commit 和 `origin/main` 推送。
- 公开接口、Schema、状态机、数据库不变量、安全/预算门槛发生变化时，先新增决策日志；不得以测试方便为由放宽门槛。
- 不提交用户已有脏文件；不运行真实模型直到 Task 11 的所有预检通过。

## Task 1：旧路径安全审计与路由骨架

**Files:** `src/core/on_live_harness_agent_graph.py`、`src/gateway/harness_dashboard_service.py`、新建 `tests/unit/test_phase14_harness_authority.py`。

1. RED：证明旧 Planner/Harness 在没有可信 `OperatorDecision` 时不能触发经营恢复写路径，且 `DECISION_SUPPORT` 不会同次 fallback 到 Legacy。
2. GREEN：新增启动冻结 `DETERMINISTIC_ONLY | DECISION_SUPPORT` 路由和明确的只读/经营写边界；保留既有自动保护路径。
3. VERIFY：运行 Harness、权限、Preemption 和路由回归；提交 `feat: gate decision support authority`。

## Task 2：Workspace 与不可变事实 Store

**Files:** 新建 `src/decision_support/models.py`、`src/decision_support/store.py`、`docker/init_phase14_decision_support.sql`、`tests/unit/test_phase14_workspace_store.py`、`tests/integration/test_phase14_workspace_store.py`。

1. RED：覆盖 `LiveSessionWorkspace` 的 `PREPARE | LIVE | REVIEW` 视图、不可变 Incident/EvidenceBundle/Proposal/Decision/Command、作用域、版本、幂等、操作员锁和 fencing。
2. GREEN：内存/PostgreSQL Store 使用同一状态机与 append-only 事实；重复提交复用原记录，陈旧版本/锁外操作拒绝。
3. VERIFY：真实 PostgreSQL 并发、重启、租约、外键与迁移 dry-run；提交 `feat: persist decision support workspace`。

## Task 3：确定性证据聚合与只读取证

**Files:** 新建 `src/decision_support/evidence.py`、`tests/unit/test_phase14_evidence.py`。

1. RED：覆盖可信事件、商品/计划快照、弹幕聚合、节奏信号、指纹和时间窗口；冲突、过期、跨房间和摘要错配必须拒绝方案。
2. GREEN：构建深冻结 `EvidenceBundle`，只允许白名单只读 Resolver；禁止直接 Store 查询和写 Skill。
3. VERIFY：运行 Event Inbox、PlanStore、SkillPolicyView 和 EvidenceRef 回归；提交 `feat: assemble governed live evidence`。

## Task 4：播中 Copilot 与结构化方案

**Files:** 新建 `src/decision_support/live_ops_copilot.py`、`src/decision_support/proposal.py`、`tests/unit/test_phase14_live_ops_copilot.py`。

1. RED：覆盖 Profile 精确身份、两次模型/三次只读 Skill/4000 token/五秒限制，一至三个 option、风险与证据闭合，及写 Skill、Agent 互调和自由动作拒绝。
2. GREEN：复用 `BoundedSpecialistRunner` 和 `AgentModelPort`，只生成 `LiveDecisionProposal`；模型失败返回显式 `DEGRADED` 事实摘要。
3. VERIFY：ScriptedModel、预算、取消、Schema、EvidenceRef 与现有 Specialist Runtime 回归；提交 `feat: add live decision support copilot`。

## Task 5：人工决定与受控执行编译

**Files:** 新建 `src/decision_support/commands.py`、`tests/unit/test_phase14_operator_decision.py`、`tests/integration/test_phase14_operator_decision.py`。

1. RED：覆盖批准、拒绝、受限修改、原因码、操作员身份、幂等、预期版本、锁冲突与重复命令。
2. GREEN：Validator 仅允许备品、提示语、优先级和时机的结构化修改；Compiler 将有效决定关联为 PlanCommand/SkillCall，永不覆盖 proposal。
3. VERIFY：Runtime 审批、PlanEngine Command Ledger、对账和 PostgreSQL 重启回归；提交 `feat: compile operator decisions safely`。

## Task 6：复合售罄自动保护与人工恢复

**Files:** 新建 `src/decision_support/sold_out_flow.py`、`tests/unit/test_phase14_sold_out_flow.py`、`tests/integration/test_phase14_sold_out_flow.py`。

1. RED：证明可信售罄会自动冻结/CAS/阻断陈旧执行；备品、提示和时机没有 OperatorDecision 不得执行。
2. GREEN：把 Phase 12B Preemption、严格对账和 Replan 接入 Workspace 事实；未知副作用保持等待对账。
3. VERIFY：并发事件、版本冲突、资源锁、SIDE_EFFECT_UNKNOWN 和恢复回归；提交 `feat: coordinate human guided sold out recovery`。

## Task 7：统一 API 与 WebSocket 协议

**Files:** `src/gateway/api_server.py`、新建 `src/gateway/decision_support_service.py`、`tests/unit/test_phase14_decision_support_api.py`。

1. RED：覆盖 Workspace 查询、proposal 创建、OperatorDecision 提交、主播只读提示、鉴权、幂等和 WebSocket 顺序。
2. GREEN：新增受操作员认证保护的 Workspace/Proposal/Decision API；新增 `decision_support_workspace_update`，不破坏旧 `agent_harness_update`。
3. VERIFY：FastAPI、operator auth、session lock 和 WebSocket 回归；提交 `feat: expose decision support workspace api`。

## Task 8：三视图运营工作台

**Files:** `front/index.html`、`tests/unit/test_phase14_dashboard_contract.py`、必要的无外部依赖浏览器/接口测试。

1. RED：固定 Prepare、Live、Review 三视图和同一 session 身份；Live 必须展示事实、风险、1-3 个方案、修改控件和执行状态。
2. GREEN：将现有 demo 审批面升级为运营主控工作台；主播只接收确认后的精简提示，不拥有审批控件。
3. VERIFY：桌面/移动 viewport、文本溢出、WebSocket 重连、错误/DEGRADED/等待对账状态；提交 `feat: build operator decision workspace`。

## Task 9：播后反馈与人工确认记忆晋升

**Files:** 新建 `src/decision_support/review_feedback.py`、`tests/unit/test_phase14_memory_confirmation.py`、`tests/integration/test_phase14_memory_confirmation.py`。

1. RED：覆盖双 Trace、作用域、白名单、敏感字段、冲突与人工确认；规则不合格候选不能被人工强制晋升。
2. GREEN：复用 Task 9 的 Candidate Store/PromotionPolicy，增加 `ELIGIBLE_AWAITING_OPERATOR` 事实和确认命令；下一次 PREPARE 可读取已确认记忆。
3. VERIFY：Memory Store、DecisionTrace、Replay、幂等与 PostgreSQL 回归；提交 `feat: confirm governed memory promotion`。

## Task 10：冻结数据集与离线协同评估

**Files:** 新建 `evaluation/phase14_human_support/`、`src/decision_support/evaluation.py`、`tests/unit/test_phase14_human_support_evaluation.py`。

1. RED：覆盖复合售罄、备品冲突、弹幕噪声、节奏、过期证据、冲突、CAS 冲突和未知副作用的字节稳定 case/manifest。
2. GREEN：实现 ScriptedModel 基准和三至五名代理运营、四组等价场景的随机交叉记录格式；记录耗时、正确性、关键漏报、覆盖和工作负担。
3. VERIFY：生成器重跑、敏感信息扫描、配对指标计算和恢复后重算；提交 `test: add human decision support evaluation`。

## Task 11：真实模型 smoke 与严格结论

**Files:** 新建 `src/decision_support/formal_evaluation.py`、`tests/unit/test_phase14_formal_evaluation.py`、`tests/external/test_phase14_real_smoke.py`。

1. RED：预检缺 endpoint、公开价格、usage、Prompt/Schema/数据/代码哈希或预算时必须阻止发送。
2. GREEN：冻结 `deepseek-v4-flash`、温度零、最多十个 smoke case、1.00 元预算；未知 usage 按预留上限结算，正式 fallback 记为失败。
3. VERIFY：先完整 ScriptedModel 演练；真实 smoke 只在用户提供的受控环境满足预检后运行；提交 `feat: evaluate human decision support formally`。

## Task 12：Demo、Acceptance 与 Phase 15 Gate

**Files:** 新建 `scripts/run_phase14_human_support_demo.py`、`docs/superpowers/reports/phase-14-human-centered-decision-support-acceptance.md`、路线图和 worklog。

1. RED：固定 Demo 必须输出播前准备、播中自动保护/人工决定、播后确认记忆和可回放结果。
2. GREEN：实现无外部依赖 Demo 和报告，汇总自动化、人工对照、真实模型费用及 `PASS | INCONCLUSIVE | FAIL`。
3. VERIFY：完整 unit/integration、compileall、迁移 dry-run、严格编码、`git diff --check`、规格审查、质量/安全审查；提交 `docs: accept phase 14 human decision support`。
4. 完成后把实时状态设置为 `AWAITING_PHASE_15_GATE`，不得自动实施 Phase 15。

## Sub-agent 使用与收敛协议

- 主模型独自负责安全边界、共享迁移、核心状态机、决策日志、集成、最终验证、提交和推送。
- 可并行派发只读 sub-agent 处理互不重叠的 Store/迁移分析、测试缺口、规格审查和安全审查；写入任务仅在文件所有权与测试数据库作用域隔离时派发。
- 派发前将任务目标、文件边界、预期测试、开始时间和交付物写入 `continuous_execution_state.md`。
- 首次回报、关键 GREEN 和提交前，主模型必须检查实际 diff 与测试输出。sub-agent 结论不能替代主模型验收。
- 二十分钟内无可验证进展、连续两次同一阻塞、越过文件边界、或建议放宽安全/指标/预算时，主模型停止该任务、保留只读发现并接管；每个 Task 提交前必须确认没有运行中的 sub-agent。
