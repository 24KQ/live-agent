# Phase 12B Preemption and Incremental Replan Implementation Plan

> **For agentic workers:** Implement task-by-task with RED, GREEN, REFACTOR. Do not begin until Phase 12A Acceptance passes and the user authorizes continuous implementation.

**Goal:** 建立可信售罄事件的持久入站、局部抢占、紧急 child DAG 和最小增量 Replan。

**Architecture:** PostgreSQL Event Inbox 是事件权威源；PreemptionCoordinator 使用确定性 ImpactAnalyzer 冻结受影响节点并创建高优先级 child PlanRun；ReplanCoordinator 创建不可变新版本并通过输入指纹复用旧成功结果。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、psycopg 3、PostgreSQL、Kafka、LangGraph PostgresSaver、pytest。

---

## 实施边界

- 不实现 LiveOpsAgent、PlannerAgent 或 LLM ProposalProvider。
- 不接真实淘宝 API，不新增 HTTP/UI。
- 不做同次 Legacy fallback。
- 每个 Task 完成后更新 `continuous_execution_state.md` 和三个 worklog，使用独立 ASCII commit 并推送。

## Task 1：SkillPolicyView 与事件公共模型

**Files:**

- Create: `src/skill_runtime/policy_view.py`
- Create: `src/plan_engine/events.py`
- Modify: `src/skill_runtime/models.py`
- Modify: `src/skill_runtime/catalog.py`
- Test: `tests/unit/test_skill_policy_view.py`
- Test: `tests/unit/test_phase12b_event_models.py`

步骤：

1. 写红灯测试，覆盖 Catalog 只读投影、事件 canonical digest、时区、严格 JSON、普通构造伪造事件授权和互斥授权。
2. 运行 `pytest tests/unit/test_skill_policy_view.py tests/unit/test_phase12b_event_models.py -q`，确认因模块/模型缺失失败。
3. 实现 `SkillPolicyView`、`InventoryFactEvent`、`VerifiedIngressProvenance`、`EventAuthorizationContext`、`ImpactScope` 和 `authorization_requirement`。
4. 为 Manifest 增加 `authorization_requirement` 契约与测试夹具，但保持当前 `handle_sold_out_event` 单活版本不变；正式 `2.0.0` 切换必须和 Task 6 的 Handler 一起提交。
5. 运行专项与 Catalog/Executor 回归，确认事件模型、授权互斥、默认授权要求和 JSON 冻结契约通过，且现有售罄 Runtime 行为没有被提前切断。
6. 提交：`feat: add phase 12b event contracts`。

## Task 2：Event Inbox 内存 Store 与状态机

**Files:**

- Create: `src/plan_engine/event_store.py`
- Create: `src/plan_engine/event_state_machine.py`
- Test: `tests/unit/test_phase12b_event_store.py`

步骤：

1. 写红灯测试覆盖首次登记、同摘要重放、不同摘要冲突、lease、fencing、application 唯一性和非法状态迁移。
2. 实现 `EventStore` Protocol、线程安全 `InMemoryEventStore`、Inbox/Occurrence/Application 冻结视图。
3. 冲突必须保留首个事实并追加 occurrence，不覆盖 payload。
4. 状态变更使用显式白名单；旧 fencing token 不得提交处理结果。
5. 运行 `pytest tests/unit/test_phase12b_event_store.py -q`。
6. 提交：`feat: add phase 12b event inbox`。

## Task 3：PostgreSQL Event Store 与计划 lineage

**Files:**

- Create: `docker/init_phase12b_preemption.sql`
- Modify: `scripts/run_db_migrations.py`
- Modify: `src/plan_engine/event_store.py`
- Modify: `src/plan_engine/store.py`
- Test: `tests/unit/test_phase12b_migrations.py`
- Test: `tests/integration/test_phase12b_event_store_postgres.py`

步骤：

1. 写 SQL 结构、索引、唯一约束、FK 和并发红灯测试。
2. 新增三张事件表，并扩展 `plan_runs`、`plan_versions`；迁移必须兼容既有 Phase 12A 数据。
3. 实现 `PostgresEventStore` 的 register、claim、record occurrence、create application 和 terminal update。
4. 使用真实 PostgreSQL 验证并发重复、摘要冲突、lease/fencing 和 event/root 唯一应用。
5. 运行迁移 dry-run 和 Phase 12A PostgreSQL 回归。
6. 提交：`feat: persist phase 12b event facts`。

## Task 4：Kafka 入站与 Trust Profile

**Files:**

- Create: `src/gateway/inventory_event_ingress.py`
- Modify: `src/gateway/kafka_consumer.py`
- Modify: `src/config/settings.py`
- Test: `tests/unit/test_phase12b_inventory_ingress.py`
- Test: `tests/integration/test_phase12b_kafka_event_inbox.py`

步骤：

1. 以记录型 consumer 写红灯测试，断言 Store commit 先于 offset commit，Store 失败不提交 offset。
2. 实现启动冻结 `IngressTrustProfile`，拒绝 topic/source/profile 不匹配和 payload 自报信任。
3. 实现手动 offset Kafka Adapter；重复和已持久化冲突均可提交，解析/持久化失败不得提交。
4. 使用真实 Kafka 验证重复投递、重启后重放、毒消息不阻塞后续 offset。
5. 运行既有 Kafka parser/consumer 回归。
6. 提交：`feat: ingest durable inventory events`。

## Task 5：ImpactAnalyzer 与协作式冻结

**Files:**

- Create: `src/plan_engine/impact.py`
- Modify: `src/plan_engine/store.py`
- Modify: `src/plan_engine/state_machine.py`
- Test: `tests/unit/test_phase12b_impact.py`
- Test: `tests/integration/test_phase12b_cooperative_freeze.py`

步骤：

1. 写 PRODUCT/ROOM/PLATFORM、依赖闭包和资源键红灯测试。
2. 实现纯确定性 `ImpactAnalyzer`，输出稳定 analysis digest。
3. 扩展 Store 支持版本内局部节点冻结、整计划冻结和在途节点协作式闭合。
4. 晚到结果保留 NodeRun；受影响结果标记 superseded，未受影响结果保持可复用。
5. 使用真实 PostgreSQL 验证 freeze 与 claim 的竞态、锁顺序和 fencing。
6. 提交：`feat: freeze impacted plan branches`。

## Task 6：售罄 CAS Skill 与严格对账

**Files:**

- Modify: `src/skill_runtime/handlers.py`
- Modify: `src/skill_runtime/catalog.py`
- Modify: `src/skill_runtime/fake_platform.py`
- Modify: `src/skill_runtime/platform_ports.py`
- Modify: `src/skill_runtime/executor.py`
- Create: `src/plan_engine/side_effect_reconciliation.py`
- Test: `tests/unit/test_skill_catalog.py`
- Test: `tests/unit/test_skill_executor.py`
- Test: `tests/unit/test_phase12b_sold_out_handler.py`
- Test: `tests/integration/test_phase12b_sold_out_reconciliation.py`

步骤：

1. 写 Catalog 只保留 `handle_sold_out_event@2.0.0`、旧版本拒绝、缺授权、CAS 成功、版本冲突、限流、未知副作用和授权互斥红灯测试。
2. 在同一 Task 中把 Manifest 升级为 `2.0.0`、Schema 收敛为 `product_id + expected_version`，并把 Handler 收敛为一次 `mark_sold_out`；Fake 增加 expected_version CAS，成功输出版本事实。
3. 实现严格读后对账；只有商品已售罄且版本证据闭合时确认原 Attempt。
4. 证据不足保持 `WAITING_RECONCILIATION`，不得创建第二个写 Operation。
5. 运行 Skill Runtime、Attempt Store 和售罄专项回归。
6. 提交：`feat: execute versioned sold out writes`。

## Task 7：高优先级紧急 child DAG

**Files:**

- Create: `src/plan_engine/emergency.py`
- Modify: `src/plan_engine/capabilities.py`
- Modify: `src/plan_engine/proposal.py`
- Modify: `src/plan_engine/store.py`
- Modify: `src/plan_engine/worker.py`
- Test: `tests/unit/test_phase12b_emergency_plan.py`
- Test: `tests/integration/test_phase12b_emergency_priority.py`

步骤：

1. 写规范 DAG、root/parent/trigger lineage、priority 100 和资源锁红灯测试。
2. 实现固定 `SoldOutEmergencyProposalProvider` 与 Capability Profile 扩展。
3. 新增不破坏既有 `claim_ready_nodes(plan_run_id=...)` 的跨 PlanRun claim 原语；调度查询按 priority 降序、READY 时间和 node ID 稳定排序。
4. 验证紧急计划不能绕过 Skill 版本、授权、FailurePolicy 或 fencing。
5. 使用真实 PostgreSQL 证明紧急节点优先于普通 READY 节点且同商品串行。
6. 提交：`feat: run sold out emergency plans`。

## Task 8：增量 Replan 与结果复用

**Files:**

- Create: `src/plan_engine/replan.py`
- Modify: `src/plan_engine/store.py`
- Modify: `src/plan_engine/bindings.py`
- Test: `tests/unit/test_phase12b_replan.py`
- Test: `tests/integration/test_phase12b_replan_postgres.py`

步骤：

1. 写多事件合并、依赖闭包、指纹相同复用、指纹变化重算和版本预算红灯测试。
2. 实现 root 级 Replan 锁，锁内读取最新版本并 claim 当前待应用事件。
3. 每个新版本创建新 node ID；复用节点写 `SUCCEEDED + reused_from_node_id`，不复制 NodeRun。
4. 相同 failure signature 与 input fingerprint 阻止循环；版本 3 后冻结转人工。
5. 使用真实 PostgreSQL 验证两个 Replan Worker 只能创建一个下一版本。
6. 提交：`feat: incrementally replan card batches`。

## Task 9：SkillPolicyView 生产消费者迁移

**Files:**

- Modify: `src/skills/on_live_harness_planner.py`
- Modify: `src/memory/tool_mask_policy.py`
- Modify: `src/core/agent_tool_executor.py`
- Modify: `src/skill_runtime/executor.py`
- Modify: `src/core/danmaku_flow.py`
- Modify: `src/core/on_live_flow.py`
- Modify: `src/core/pre_live_business_flow.py`
- Modify: `src/core/pre_live_flow.py`
- Modify: `src/core/agent_lifecycle_hooks.py`
- Modify: `src/core/security_hooks.py`
- Test: `tests/unit/test_skill_policy_view.py`
- Test: `tests/unit/test_on_live_harness_planner.py`
- Test: `tests/unit/test_tool_mask_policy.py`
- Test: `tests/unit/test_agent_tool_executor.py`
- Test: `tests/unit/test_agent_tool_executor_skill_compat.py`
- Test: `tests/unit/test_skill_executor.py`
- Test: `tests/unit/test_agent_lifecycle_hooks.py`
- Test: `tests/unit/test_security_hooks.py`
- Test: `tests/integration/test_pre_live_flow.py`
- Test: `tests/integration/test_on_live_flow.py`
- Test: `tests/integration/test_danmaku_flow.py`

步骤：

1. 用 `rg` 固化当前 `src/` ToolRegistry 导入清单，并写 Planner、Policy、Hook、Flow、Executor 通过 `SkillPolicyView` 查询的红灯测试。
2. 让构造器接收启动冻结的只读 Policy View；未知 Skill、生命周期、风险、门禁、Schema 和精确版本仍保持 fail-closed。
3. `SkillExecutor` 直接使用 Catalog/Policy View，不得再通过 ToolRegistry 反向查询 Manifest 已拥有的元数据；AgentToolExecutor 保留同步兼容 API，但其治理查询改用同一 View。
4. 运行 `rg -n "from src\.config\.tool_registry|import src\.config\.tool_registry" src --glob '!src/config/tool_registry.py'`，结果必须为空；注释中的历史名词不算生产依赖。
5. 运行上述单元、播前/播中 Flow 与 Harness 回归；ToolRegistry Facade 自身和兼容测试暂时保留到 Phase 14。
6. 提交：`refactor: migrate tool policy consumers`。

## Task 10：PreemptionCoordinator、Harness 证据接入与路由

**Files:**

- Create: `src/plan_engine/preemption.py`
- Modify: `src/config/settings.py`
- Modify: `src/core/on_live_harness_agent_graph.py`
- Modify: `src/core/on_live_harness_audit.py`
- Test: `tests/unit/test_phase12b_preemption.py`
- Test: `tests/integration/test_phase12b_harness_evidence.py`

步骤：

1. 写默认 Legacy、显式 PlanEngine、启动冻结、无 fallback 和 Harness 不重复写红灯测试。
2. 实现 `PreemptionCoordinator` 串联 Inbox claim、impact、freeze、child plan、reconciliation 和 replan。
3. Harness 只读取结构化 EvidenceRef 和最终建议事实，不执行 `handle_sold_out_event` 写 Skill。
4. 断言 PlanEngine 路由启用时，Harness 的可用动作白名单不含售罄写；Legacy 路由仍只能作为下一次启动的显式回滚。
5. 运行播中 Harness、Replay、Evaluation、SkillPolicyView 和 Phase 11B 路由回归。
6. 提交：`feat: coordinate sold out preemption`。

## Task 11：Demo、验收和阶段留迹

**Files:**

- Create: `scripts/run_phase12b_preemption_demo.py`
- Modify: `scripts/run_all.py`
- Create: `tests/unit/test_phase12b_demo.py`
- Create: `docs/superpowers/reports/phase-12b-preemption-replan-acceptance.md`
- Modify: 路线图、决策日志、实时状态和三个 worklog

Demo 固定输出：

1. 可信售罄局部冻结并成功 Replan。
2. Kafka 重复事件幂等。
3. 同 ID 不同摘要冲突留证。
4. 在途结果晚到并 superseded。
5. `SIDE_EFFECT_UNKNOWN` 严格对账成功。
6. 对账证据不足转人工。
7. 多事件合并与旧结果复用。
8. 版本预算耗尽后冻结。

此外，`live-session-p001-sold-out-v1` 必须支持
`--scenario live-session-p001-sold-out-v1 --output-dir <dir>`。该场景输出
`business-loop-trace.json` 与 `business-loop-report.md`，字段与展示边界以
`docs/project_guidance/agent_runtime_business_closed_loop_track.md` 为准。输出只能
序列化既有事件、Attempt、NodeRun、PlanVersion 与复用事实；不得为生成报告重发写
操作、创建新 Operation 或使用 Legacy fallback。

验证命令：

```text
pytest tests/unit/test_phase12b_*.py -q
pytest tests/integration/test_phase12b_*.py -q
pytest -q
python scripts/run_phase12b_preemption_demo.py
python scripts/run_all.py phase12b-demo
python scripts/run_db_migrations.py --dry-run
git diff --check
python scripts/check_doc_encoding.py
```

Acceptance 必须记录数据库结构、授权边界、Kafka offset 顺序、冻结竞态、紧急 DAG、失败矩阵、Replan 复用、ToolRegistry 调用清单、精确测试结果、业务闭环 Trace/报告摘要和 Phase 13 Gate 进入条件。

提交：`feat: complete phase 12b preemption`。

## Plan Self-Review

- 每项外部写都经 SkillExecutor、Attempt Store、授权和 CAS，不由控制节点直接执行。
- Kafka、Event Inbox、PlanStore 和 checkpoint 没有伪造跨连接原子事务。
- Replan 不覆盖旧版本，不复制 NodeRun，不让 LLM决定失效范围。
- Harness 与事件执行只有一个写入口。
- SkillPolicyView 迁移有独立 Task；Phase 12B 结束时生产代码不再导入 ToolRegistry，Facade 删除仍由 Phase 14 完成。
- Phase 13 Agent 和真实淘宝 API不在任何 Task 中。
