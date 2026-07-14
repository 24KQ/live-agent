# Phase 12A DAG PlanEngine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 为冻结排品后的手卡批次建立确定性、可持久化、可恢复的 DAG PlanEngine，并保持既有播前默认路径不变。

**Architecture:** 新增独立 src/plan_engine 包。Graph 只在显式 PLAN_ENGINE 路由下创建或恢复 PlanRun，PlanWorker 通过 PlanStore 的 claim、lease 和 fencing 执行规范 DAG；PlanStore 先提交执行事实，Graph 再写官方 checkpoint。Phase 12A 只使用固定候选 DAG Provider，不实现真实 LLM 规划、售罄抢占或 Replan。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、psycopg 3、PostgreSQL、LangGraph PostgresSaver、pytest。

---

## 实施边界

- 只处理冻结 LivePlanDraft 和商品快照后的最多三张 generate_product_card 节点；不改商品查询、排品和建播逻辑。
- 不实现真实 LLM Provider、真实淘宝 API、售罄事件、紧急 DAG、Replan、HTTP/UI 或多 Agent。
- 新增或修改 Python 代码使用 UTF-8，并添加说明职责、信任边界、并发语义和失败边界的详细中文注释。
- plan_engine_card_execution_route 默认 LEGACY，进程装配后冻结；PLAN_ENGINE 失败不得在同次调用中回退 Legacy 或双执行。
- PlanStore 是权威执行事实。不得直接读写 PostgresSaver 内部表，也不得把 PlanStore 与 PostgresSaver 伪装成同一事务。

## Task 1: PlanEngine 领域模型与固定候选 DAG

**Files:**

- Create: src/plan_engine/__init__.py
- Create: src/plan_engine/models.py
- Create: src/plan_engine/proposal.py
- Test: tests/unit/test_phase12a_plan_models.py
- Test: tests/unit/test_phase12a_proposal_validation.py

- [ ] **Step 1: 写领域模型和规范 DAG 的红灯测试。**

~~~python
def test_canonical_provider_materializes_prepare_cards_and_collect() -> None:
    proposal = CanonicalCardBatchProposalProvider().propose_sync(_planning_input())

    assert [node.logical_key for node in proposal.nodes] == [
        "prepare-card-batch",
        "card:p001",
        "card:p002",
        "card:p003",
        "collect-card-results",
    ]
    assert proposal.nodes[0].node_kind is PlanNodeKind.CONTROL
    assert proposal.nodes[1].skill_id == "generate_product_card"
    assert proposal.nodes[-1].depends_on == ("card:p001", "card:p002", "card:p003")


def test_planning_input_rejects_missing_or_duplicate_plan_product() -> None:
    with pytest.raises(ValueError, match="缺少商品快照"):
        CardBatchPlanningInput.model_validate(_input_with_missing_product())
    with pytest.raises(ValueError, match="重复"):
        CardBatchPlanningInput.model_validate(_input_with_duplicate_plan_item())
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_plan_models.py tests/unit/test_phase12a_proposal_validation.py -q

Expected: FAIL，原因是 src.plan_engine 和领域类型尚不存在。

- [ ] **Step 3: 实现不可变模型与 Provider。**

在 models.py 定义不可变 Pydantic 模型：PlanNodeKind、InputBindingKind、InputBinding、CardBatchPlanningInput、CandidatePlanNode、CandidatePlanProposal、PlanRunState、PlanNodeState、PlanCommandType 和 JSON-safe 的 PlanRun、Version、Node、NodeRun 视图。

CardBatchPlanningInput 保存 room_id、trace_id、完整 live_plan 和 products_by_id；使用规范 JSON 的 SHA-256 生成稳定 run_key。校验排品非空、商品 ID 不重复、每个排品商品都有快照。

在 proposal.py 定义：

~~~python
class PlanProposalProvider(Protocol):
    async def propose(self, request: CardBatchPlanningInput) -> CandidatePlanProposal: ...


class CanonicalCardBatchProposalProvider:
    provider_id = "canonical-card-batch"
    provider_version = "1.0.0"
~~~

Provider 从排品顺序选择 min(3, len(items)) 个商品，生成一个 PREPARE_CARD_BATCH 控制节点、每商品一个 generate_product_card Skill 节点和一个 COLLECT_CARD_RESULTS 控制节点。控制节点不携带 skill_id；手卡节点的 product 参数使用 PLAN_INPUT 绑定。

- [ ] **Step 4: 实现候选 DAG 校验。**

校验器必须拒绝空 DAG、重复 logical_key、环依赖、指向不存在节点的依赖、控制节点携带 Skill、Skill 节点缺少 Skill、NODE_OUTPUT 未声明上游依赖和未知绑定类型。候选非法时不得创建 PlanRun，也不得 fallback 到其他路径。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase12a_plan_models.py tests/unit/test_phase12a_proposal_validation.py -q

Expected: PASS。

~~~bash
git add src/plan_engine/__init__.py src/plan_engine/models.py src/plan_engine/proposal.py tests/unit/test_phase12a_plan_models.py tests/unit/test_phase12a_proposal_validation.py
git commit -m "feat: add phase 12a plan models"
~~~

## Task 2: 能力配置、类型化输入绑定与状态机

**Files:**

- Create: src/plan_engine/capabilities.py
- Create: src/plan_engine/bindings.py
- Create: src/plan_engine/state_machine.py
- Test: tests/unit/test_phase12a_bindings.py
- Test: tests/unit/test_phase12a_state_machine.py

- [ ] **Step 1: 写绑定、资源键和状态迁移红灯测试。**

~~~python
def test_binding_resolver_only_reads_declared_sources() -> None:
    resolver = InputBindingResolver()
    value = resolver.resolve(
        InputBinding(kind="PLAN_INPUT", path=("products_by_id", "p001")),
        planning_input=_planning_input(),
        dependency_outputs={},
        declared_dependencies=frozenset(),
    )
    assert value["product_id"] == "p001"

    with pytest.raises(PlanValidationError, match="未声明依赖"):
        resolver.resolve(
            InputBinding(
                kind="NODE_OUTPUT",
                upstream_logical_key="prepare-card-batch",
                path=("products",),
            ),
            planning_input=_planning_input(),
            dependency_outputs={"prepare-card-batch": {"products": []}},
            declared_dependencies=frozenset(),
        )


def test_card_capability_derives_resource_key_and_catalog_version() -> None:
    resolved = PlanCapabilityProfile.default(catalog=_catalog()).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-1",
    )
    assert resolved.skill_version == "1.0.0"
    assert resolved.resource_keys == ("room:room-1:product:p001",)
    assert resolved.max_concurrency == 4
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_bindings.py tests/unit/test_phase12a_state_machine.py -q

Expected: FAIL，原因是 resolver、Capability Profile 和状态机尚不存在。

- [ ] **Step 3: 实现受控绑定和 Capability Profile。**

InputBindingResolver 只能按 tuple path 遍历 dict/list；拒绝空路径、越界、非 JSON 容器、未知来源和跨版本读取。派发前物化普通 JSON 参数，使用 json.dumps(sort_keys=True, separators=(",", ":")) 的 SHA-256 保存 input_fingerprint。

PlanCapabilityProfile 只允许 PREPARE_CARD_BATCH、COLLECT_CARD_RESULTS 和 generate_product_card。它从 Catalog 读取精确版本、生命周期、风险和单次 timeout；候选字段不能覆盖这些事实。手卡资源键固定为 room:{room_id}:product:{product_id}，控制节点没有外部资源锁。

- [ ] **Step 4: 固定 D-015 状态机。**

允许以下迁移，其他迁移抛出 PlanInvariantError：

~~~text
PENDING -> READY -> RUNNING
RUNNING -> SUCCEEDED | FAILED | RETRY_WAIT | WAITING_APPROVAL | WAITING_RECONCILIATION | FROZEN
RETRY_WAIT -> READY | FAILED
WAITING_APPROVAL -> READY | FAILED
WAITING_RECONCILIATION -> SUCCEEDED | FAILED
PENDING | READY | FROZEN -> INVALIDATED | SKIPPED
~~~

PlanRun 聚合状态只允许 ACTIVE、FROZEN、SUCCEEDED、FAILED，不得引入 PARTIAL_SUCCESS。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase12a_bindings.py tests/unit/test_phase12a_state_machine.py -q

Expected: PASS。

~~~bash
git add src/plan_engine/capabilities.py src/plan_engine/bindings.py src/plan_engine/state_machine.py tests/unit/test_phase12a_bindings.py tests/unit/test_phase12a_state_machine.py
git commit -m "feat: add phase 12a plan validation"
~~~

## Task 3: 内存 PlanStore、查询服务与 Command Ledger

**Files:**

- Create: src/plan_engine/store.py
- Create: src/plan_engine/commands.py
- Test: tests/unit/test_phase12a_plan_store.py
- Test: tests/unit/test_phase12a_command_service.py

- [ ] **Step 1: 写 PlanRun、NodeRun 和命令幂等红灯测试。**

~~~python
def test_create_or_resume_reuses_same_run_for_same_frozen_input() -> None:
    store = InMemoryPlanStore()
    first = store.create_or_resume(_materialized_plan())
    second = store.create_or_resume(_materialized_plan())

    assert second.plan_run_id == first.plan_run_id
    assert second.plan_version == 1


def test_command_id_replays_first_result_and_rejects_old_version() -> None:
    service = CommandService(store=InMemoryPlanStore())
    first = service.submit(_reconcile_command(version=1, status="WAITING_RECONCILIATION"))
    replay = service.submit(_reconcile_command(version=1, status="WAITING_RECONCILIATION"))
    stale = service.submit(_reconcile_command(command_id="stale", version=0, status="WAITING_RECONCILIATION"))

    assert replay == first
    assert stale.accepted is False
    assert stale.reason == "PLAN_VERSION_MISMATCH"
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_plan_store.py tests/unit/test_phase12a_command_service.py -q

Expected: FAIL，原因是 PlanStore 与 CommandService 尚不存在。

- [ ] **Step 3: 定义 Store Protocol 和内存实现。**

PlanStore 必须暴露 create_or_resume、get_plan_run、get_plan_version、list_nodes、claim_ready_nodes、heartbeat_node_run、reclaim_expired_node、record_node_result、schedule_retry、freeze_plan、list_node_runs、submit_command 和 reconcile_plan_reference。

create_or_resume 以 CardBatchPlanningInput.run_key 作为冻结输入幂等身份；同 run key 不同摘要 fail-closed。创建时写入 PlanRun、Version、Node 与依赖边；准备节点为 READY，其余节点为 PENDING。每次 claim 都创建新的 NodeRun，递增 attempt_number 与 claim_version。heartbeat_node_run 必须匹配 worker_id 与 claim_version 后才延长 lease；reclaim_expired_node 只在 lease_until 已过期时创建新的 NodeRun，并使旧 fencing token 永久失效。

PlanQueryService 只从 Store 返回 JSON-safe 的 PlanRun、Version、Node、NodeRun 和 Command 视图；不得读取 checkpoint 内部表。补充单元测试：心跳使用错误 token 必须拒绝，租约未过期不得 reclaim，成功 reclaim 后旧 NodeRun 不能写入终态。

- [ ] **Step 4: 实现通用 Command Ledger。**

定义 PlanCommandType.APPROVE、REJECT、RECONCILE、RESUME。命令包含 command_id、plan_run_id、expected_plan_version、node_id、expected_node_status 和 JSON-safe payload。重复 command ID 返回首次结果；版本或状态不匹配不修改节点。审批 TTL 10 分钟、对账 TTL 30 分钟，过期后 fail-closed。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase12a_plan_store.py tests/unit/test_phase12a_command_service.py -q

Expected: PASS。

~~~bash
git add src/plan_engine/store.py src/plan_engine/commands.py tests/unit/test_phase12a_plan_store.py tests/unit/test_phase12a_command_service.py
git commit -m "feat: add phase 12a plan store"
~~~

## Task 4: Worker、FailurePolicy 与协作式批次收敛

**Files:**

- Create: src/plan_engine/failure_policy.py
- Create: src/plan_engine/worker.py
- Test: tests/unit/test_phase12a_worker.py

- [ ] **Step 1: 写派发、重试、fencing 和批次失败红灯测试。**

~~~python
def test_worker_retries_rate_limited_card_with_persisted_retry_wait() -> None:
    worker, store, executor = _worker_with_rate_limit(retry_after_seconds=7)
    result = worker.run_once_sync(_plan_run_id(store))

    assert result.claimed == 1
    assert store.get_node("card:p001").state is PlanNodeState.RETRY_WAIT
    assert store.get_node("card:p001").next_retry_at is not None
    assert executor.calls == 1


def test_expired_claim_cannot_commit_late_result() -> None:
    store = _store_with_ready_node()
    first = store.claim_ready_nodes(_plan_run_id(store), worker_id="worker-a", limit=1)[0]
    second = store.reclaim_expired_node(first.node_id, worker_id="worker-b")

    with pytest.raises(PlanInvariantError, match="fencing"):
        store.record_node_result(first.node_run_id, first.claim_version, output={"card": {}})
    assert second.claim_version == first.claim_version + 1
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_worker.py -q

Expected: FAIL，原因是 Worker 和 FailurePolicy 尚不存在。

- [ ] **Step 3: 实现集中 FailurePolicy。**

FailurePolicy.decide() 只接收 FailureFact、节点能力、NodeRun 次数、deadline 与当前时间，返回 RETRY、WAIT_HUMAN、SKIP 或 FAIL_PLAN。只读手卡的 TRANSIENT_INFRA 与 RATE_LIMITED 最多三次尝试；限流优先使用 retry_after_seconds，其他退避使用指数增长加确定性抖动。deadline 前无法再次尝试时 FAIL_PLAN。其他 FailureFact 不自动重试。

- [ ] **Step 4: 实现异步 Worker 与同步桥接。**

PlanWorker.run_once() 最多派发四个资源不冲突节点。控制节点执行确定性准备或汇总函数；Skill 节点通过 Phase 11B 的统一 Skill 执行核心调用精确钉住的 SkillCall。派发前解析绑定、冻结参数、校验 Schema 并保存输入指纹。

不可恢复失败后停止派发新节点，已运行节点仅在完成或 deadline 到期后收敛；PlanRun 最终 FAILED，但已成功结果不删除。所有成功结果先写 PlanStore，再允许上层返回。同步 Graph 只能通过 SyncPlanWorkerAdapter 复用同一 async 核心。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase12a_worker.py tests/unit/test_phase12a_plan_store.py tests/unit/test_phase12a_bindings.py -q

Expected: PASS。

~~~bash
git add src/plan_engine/failure_policy.py src/plan_engine/worker.py tests/unit/test_phase12a_worker.py
git commit -m "feat: add phase 12a plan worker"
~~~

## Task 5: PostgreSQL PlanStore、DDL 与并发证据

**Files:**

- Create: docker/init_phase12a_plan_engine.sql
- Modify: scripts/run_db_migrations.py
- Modify: src/plan_engine/store.py
- Test: tests/integration/test_phase12a_plan_store_postgres.py
- Test: tests/unit/test_phase12a_migrations.py

- [ ] **Step 1: 写 PostgreSQL Schema、并发 claim 和迁移顺序红灯测试。**

~~~python
def test_two_connections_claim_one_ready_node_once(settings: Settings) -> None:
    store_a = PostgresPlanStore(settings)
    store_b = PostgresPlanStore(settings)
    run = store_a.create_or_resume(_materialized_plan())

    first = store_a.claim_ready_nodes(run.plan_run_id, worker_id="a", limit=1)
    second = store_b.claim_ready_nodes(run.plan_run_id, worker_id="b", limit=1)

    assert len(first) == 1
    assert second == []


def test_migration_registers_phase12a_after_phase11b() -> None:
    phases = [step.phase for step in MIGRATIONS]
    assert phases.index("phase12a") == phases.index("phase11b") + 1
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_migrations.py tests/integration/test_phase12a_plan_store_postgres.py -q

Expected: FAIL，原因是 Phase 12A SQL、迁移注册和 PostgresPlanStore 尚不存在。

- [ ] **Step 3: 编写 DDL 与注册迁移。**

SQL 创建 plan_runs、plan_versions、plan_nodes、plan_node_dependencies、node_runs、plan_commands。必须包含 plan_run_id/version_number、plan_version_id/logical_key、node_id/attempt_number 和 command_id 唯一约束；Node/Run 状态 CHECK；JSONB 快照；UTC 时间；READY 查询、lease 和依赖索引。

在 MIGRATIONS 的 phase11b 后追加 required phase12a。不得给 PlanStore 表添加 PostgresSaver 私有表外键，也不得把 skill_execution_attempts 当作 PlanStore；skill_attempt_id 仅为可空关联字段。

- [ ] **Step 4: 实现 PostgresPlanStore。**

复用 PostgresAttemptStore 的 psycopg、dict_row、Jsonb、READ COMMITTED 和条件 UPDATE WHERE claim_version = %s 模式。READY claim 使用 FOR UPDATE SKIP LOCKED，并排除与 RUNNING 节点资源键冲突的任务；终态、心跳和重试必须匹配 worker ID 与 fencing token。

- [ ] **Step 5: 运行绿灯、迁移 dry-run 并提交。**

Run: pytest tests/unit/test_phase12a_migrations.py tests/integration/test_phase12a_plan_store_postgres.py -q

Run: python scripts/run_db_migrations.py --dry-run

Expected: PASS；dry-run 输出包含 phase12a。

~~~bash
git add docker/init_phase12a_plan_engine.sql scripts/run_db_migrations.py src/plan_engine/store.py tests/unit/test_phase12a_migrations.py tests/integration/test_phase12a_plan_store_postgres.py
git commit -m "feat: persist phase 12a plans"
~~~

## Task 6: Checkpoint 一致性与人工命令恢复

**Files:**

- Create: src/plan_engine/reconciliation.py
- Modify: src/plan_engine/store.py
- Modify: src/plan_engine/commands.py
- Create: src/plan_engine/service.py
- Modify: docker/init_phase12a_plan_engine.sql
- Test: tests/unit/test_phase12a_reconciliation.py
- Test: tests/unit/test_phase12a_migrations.py
- Test: tests/integration/test_phase12a_plan_checkpoint_consistency.py

- [ ] **Step 1: 写两种不一致方向的红灯测试。**

~~~python
def test_planstore_success_replays_without_second_skill_call(postgres_plan_store, checkpointer) -> None:
    plan = _persist_success_before_checkpoint(postgres_plan_store)
    calls = CountingCardExecutor()

    result = _resume_graph_from_old_checkpoint(plan.plan_run_id, checkpointer, calls)

    assert result.cards_snapshot[0]["product_id"] == "p001"
    assert calls.count == 0


def test_checkpoint_reference_without_planstore_evidence_freezes_plan(postgres_plan_store, checkpointer) -> None:
    reference = _write_checkpoint_reference_without_node_run(checkpointer)

    outcome = PlanCheckpointReconciler(postgres_plan_store).reconcile(reference)

    assert outcome.category == "INTERNAL_INVARIANT"
    assert outcome.plan_state == "FROZEN"
    persisted = postgres_plan_store.get_plan_run(reference.plan_run_id)
    assert persisted.reconciliation_required is True
    assert persisted.reconciliation_failure["category"] == "INTERNAL_INVARIANT"
    assert persisted.reconciliation_signature


def test_command_service_reconciles_before_mutating_node() -> None:
    reconciler = RecordingReconciler()
    service = CommandService(store=InMemoryPlanStore(), reconciler=reconciler)

    service.submit(_reconcile_command(version=1, status="WAITING_RECONCILIATION"))

    assert reconciler.calls == ["before_command"]
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_reconciliation.py tests/integration/test_phase12a_plan_checkpoint_consistency.py -q

Expected: FAIL，原因是 reconciliation 服务尚不存在。

- [ ] **Step 3: 实现公开接口上的一致性对账。**

PlanCheckpointReference 只保存 `plan_run_id`、`plan_version` 和 `CARD_BATCH_SUCCEEDED | CARD_BATCH_FAILED`。PlanStore 已有成功 NodeRun、checkpoint 落后时，对账返回 replay reuse，Worker 不调用 Skill，并写 replay reuse 审计摘要。checkpoint 声称完成但 Store 缺少证据时，写入 `INTERNAL_INVARIANT`、冻结 PlanRun 并进入 WAITING_RECONCILIATION；不补造 NodeRun、不重跑 Skill。

扩展 `plan_runs`，持久化 `reconciliation_required`、JSONB `reconciliation_failure`、`reconciliation_signature`、`reconciliation_attempt_count` 和 `last_reconciled_at`。重复扫描相同 signature 只增加受控恢复事实，不重复创建事故或命令。普通 APPROVE/REJECT/RESUME 命令在 reconciliation_required 时拒绝，只有符合预期版本和状态的 RECONCILE 可以推进。

实现 PlanReconciliationService：服务装配时调用 reconcile_startup()；后台入口每 30 秒调用 reconcile_active_plans_once()；CommandService.submit() 在写入命令前调用 reconcile_before_command()。三个入口都委托同一个幂等 reconcile()，且只使用 PlanStore 和官方 checkpointer 的公开读取 API。为三个入口分别添加单元测试，断言它们复用同一 reconciler、重复扫描不重复冻结或创建命令。

- [ ] **Step 4: 使用真实 PostgreSQL 和官方 PostgresSaver 跑绿灯并提交。**

Run: pytest tests/unit/test_phase12a_reconciliation.py tests/integration/test_phase12a_plan_checkpoint_consistency.py -q

Expected: PASS。

~~~bash
git add src/plan_engine/reconciliation.py src/plan_engine/store.py src/plan_engine/commands.py src/plan_engine/service.py docker/init_phase12a_plan_engine.sql tests/unit/test_phase12a_reconciliation.py tests/unit/test_phase12a_migrations.py tests/integration/test_phase12a_plan_checkpoint_consistency.py
git commit -m "feat: reconcile phase 12a plan checkpoints"
~~~

## Task 7: 启动冻结路由与播前 Graph 局部接入

**Files:**

- Create: src/plan_engine/routing.py
- Modify: src/plan_engine/service.py
- Modify: src/config/settings.py
- Modify: src/core/pre_live_graph.py
- Test: tests/unit/test_phase12a_routing.py
- Test: tests/integration/test_phase12a_pre_live_graph_route.py

- [ ] **Step 1: 写默认 Legacy、PlanEngine 路由和无 fallback 红灯测试。**

~~~python
def test_default_route_keeps_existing_generate_cards_call() -> None:
    graph, legacy = _graph_with_route("LEGACY")

    result = graph.invoke(_initial_state())

    assert legacy.generate_cards_calls == 1
    assert result["plan_run_id"] is None


def test_plan_engine_route_records_reference_without_legacy_fallback() -> None:
    graph, legacy, plan_service = _graph_with_route("PLAN_ENGINE")

    result = graph.invoke(_initial_state())

    assert legacy.generate_cards_calls == 0
    assert result["plan_run_id"] == plan_service.created_plan_run_id
    assert result["cards_snapshot"] == plan_service.cards_snapshot
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_routing.py tests/integration/test_phase12a_pre_live_graph_route.py -q

Expected: FAIL，原因是独立 PlanEngine 路由和 Graph bridge 尚不存在。

- [ ] **Step 3: 实现独立路由和 CardBatchPlanService。**

在 Settings 增加：

~~~python
plan_engine_card_execution_route: Literal["LEGACY", "PLAN_ENGINE"] = Field(
    default="LEGACY",
    validation_alias="PLAN_ENGINE_CARD_EXECUTION_ROUTE",
)
~~~

PlanExecutionPolicy.from_settings() 在装配期冻结该值；不得复用 skill_route_phase11b_batch1。

CardBatchPlanService 固定提供：

~~~python
class CardBatchPlanService(Protocol):
    def create_or_resume(self, request: CardBatchPlanningInput) -> CardBatchPlanRef: ...
    def drive_to_terminal(self, plan_run_id: str) -> CardBatchExecutionResult: ...
~~~

pre_live_graph.py 只修改 generate_product_cards：LEGACY 时保持 service.generate_cards()；PLAN_ENGINE 时从既有 plan_snapshot 与 products_snapshot 创建或恢复 PlanRun，再通过同步 Worker bridge 获取终态。Graph state 新增 JSON-safe 的 plan_run_id、plan_version、plan_execution_status；成功时仍写既有 cards_snapshot 与 card_count。不改 query、排品、合规或建播节点。

- [ ] **Step 4: 覆盖 checkpoint 与旧路径回归。**

测试路由在 Graph 创建后冻结；运行中修改 Settings 不影响既有服务。PLAN_ENGINE 注入失败时断言不调用 legacy generate_cards。LEGACY 保留既有 state、审计和 checkpoint 契约。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase12a_routing.py tests/integration/test_phase12a_pre_live_graph_route.py tests/unit/test_pre_live_graph.py tests/integration/test_pre_live_graph_skill_runtime_flow.py -q

Expected: PASS。

~~~bash
git add src/plan_engine/routing.py src/plan_engine/service.py src/config/settings.py src/core/pre_live_graph.py tests/unit/test_phase12a_routing.py tests/integration/test_phase12a_pre_live_graph_route.py
git commit -m "feat: route pre-live cards through plan engine"
~~~

## Task 8: 移除 TRUSTED_COMPAT 审批兼容

**Files:**

- Modify: src/skill_runtime/models.py
- Modify: src/skill_runtime/pre_live_facade.py
- Modify: src/core/pre_live_graph.py
- Modify: src/skill_runtime/__init__.py
- Test: tests/unit/test_skill_runtime_models.py
- Test: tests/unit/test_skill_runtime_routing.py
- Test: tests/unit/test_pre_live_graph_interrupt.py
- Test: tests/integration/test_pre_live_graph_skill_runtime_flow.py

- [ ] **Step 1: 写 TRUSTED_COMPAT 不再存在的红灯测试。**

测试必须证明：`ApprovalSource` 只保留 `HUMAN_INTERRUPT`；普通构造仍不能伪造人工批准；Runtime 路由下 `confirmed_setup=True` 且没有 approval_context 时返回 pending，不创建 Attempt、不调用建播 Port；真实 interrupt approve 仍成功，reject 仍不执行。

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_skill_runtime_models.py tests/unit/test_skill_runtime_routing.py tests/unit/test_pre_live_graph_interrupt.py tests/integration/test_pre_live_graph_skill_runtime_flow.py -q

Expected: FAIL，原因是 TRUSTED_COMPAT 枚举、token、工厂和 Facade 映射仍存在。

- [ ] **Step 3: 删除兼容审批来源。**

删除 `_TRUSTED_COMPAT_TOKEN`、`ApprovalSource.TRUSTED_COMPAT`、`_build_trusted_compat_approval()` 和 Facade `_create_compat_approval()`。`setup_live_session()` 在 Runtime 路由下只转发调用方提供的受控 `approval_context`；`confirmed_setup` 仅保留旧 Protocol/Legacy 调用兼容，不能影响 Runtime 权限。

现有 Graph 人审路径继续在写入 pending/resume 审计后使用 `_build_human_interrupt_approval()`。不得把 `confirmed_setup`、普通 arguments 或 PlanEngine 状态改造成审批证据。

- [ ] **Step 4: 跑绿灯与回归并提交。**

Run: pytest tests/unit/test_skill_runtime_models.py tests/unit/test_skill_runtime_routing.py tests/unit/test_pre_live_graph_interrupt.py tests/integration/test_pre_live_graph_skill_runtime_flow.py tests/integration/test_pre_live_graph_interrupt_flow.py -q

Expected: PASS。

~~~bash
git add src/skill_runtime/models.py src/skill_runtime/pre_live_facade.py src/core/pre_live_graph.py src/skill_runtime/__init__.py tests/unit/test_skill_runtime_models.py tests/unit/test_skill_runtime_routing.py tests/unit/test_pre_live_graph_interrupt.py tests/integration/test_pre_live_graph_skill_runtime_flow.py
git commit -m "refactor: remove trusted compatibility approval"
~~~

## Task 9: 无外部依赖 Demo、全量验收与 Acceptance

**Files:**

- Create: scripts/run_phase12a_dag_plan_engine_demo.py
- Modify: scripts/run_all.py
- Create: tests/unit/test_phase12a_demo.py
- Create: docs/superpowers/reports/phase-12a-dag-plan-engine-acceptance.md
- Modify: docs/project_guidance/agent_runtime_evolution_roadmap.md
- Modify: docs/worklog/task_plan.md
- Modify: docs/worklog/findings.md
- Modify: docs/worklog/progress.md

- [ ] **Step 1: 写五场景 Demo 契约红灯测试。**

~~~python
def test_demo_emits_five_isolated_scenarios() -> None:
    records = [json.loads(line) for line in run_demo_lines()]

    assert [record["scenario"] for record in records] == [
        "three_cards_parallel",
        "rate_limited_retry",
        "unrecoverable_failure",
        "planstore_ahead_recovery",
        "duplicate_command",
    ]
    assert records[2]["plan_status"] == "FAILED"
    assert records[3]["skill_calls_after_restart"] == 0
    assert records[4]["replayed"] is True
~~~

- [ ] **Step 2: 运行红灯测试。**

Run: pytest tests/unit/test_phase12a_demo.py -q

Expected: FAIL，原因是 Demo 脚本与运行入口尚不存在。

- [ ] **Step 3: 实现 Demo 和统一入口。**

每个场景重新装配 InMemoryPlanStore、固定 Proposal Fixture、Fake Skill executor 和隔离时钟；不得连接 PostgreSQL、Kafka、LLM 或真实平台。直接脚本只输出五行 JSON。run_all.py 新增 phase12a-demo 子命令，保留统一入口的 [INFO] 日志包装。

- [ ] **Step 4: 运行专项、集成、全量和静态检查。**

Run: pytest tests/unit/test_phase12a_plan_models.py tests/unit/test_phase12a_proposal_validation.py tests/unit/test_phase12a_bindings.py tests/unit/test_phase12a_state_machine.py tests/unit/test_phase12a_plan_store.py tests/unit/test_phase12a_command_service.py tests/unit/test_phase12a_worker.py tests/unit/test_phase12a_migrations.py tests/unit/test_phase12a_reconciliation.py tests/unit/test_phase12a_routing.py tests/unit/test_phase12a_demo.py -q

Run: pytest tests/integration/test_phase12a_plan_store_postgres.py tests/integration/test_phase12a_plan_checkpoint_consistency.py tests/integration/test_phase12a_pre_live_graph_route.py tests/integration/test_pre_live_graph_checkpoint_flow.py tests/integration/test_pre_live_graph_skill_runtime_flow.py -q

Run: pytest -q

Run: python scripts/run_phase12a_dag_plan_engine_demo.py

Run: python scripts/run_all.py phase12a-demo

Run: python scripts/run_db_migrations.py --dry-run

Run: git diff --check

Run: python scripts/check_doc_encoding.py

Expected: 所有 pytest、Demo、迁移 dry-run 和 diff 检查通过。编码扫描如仍因扫描器自身 U+FFFD 样例和历史 BOM/混合换行退出 1，Acceptance 必须记录本阶段目标文件零命中，不能虚报全仓通过。

- [ ] **Step 5: 编写 Acceptance 并提交。**

Acceptance 必须记录规范 DAG、能力白名单、PlanStore 表和索引、NodeRun/Skill Attempt 关系、FailurePolicy、Command Ledger 四命令及 TTL、路由默认值、两种 checkpoint 不一致处理、持久化 reconciliation 事故字段、服务启动/周期/命令前三类对账触发、`TRUSTED_COMPAT` 删除证据、五场景 Demo、精确测试结果、设计偏差和 Phase 12B 进入条件。验收必须分别证明 D-071 的命令幂等、旧版本拒绝和整批失败收敛，以及 D-072 的单元加真实 PostgreSQL/PostgresSaver 证据。只有全程实施获得单独授权且该技术门禁通过后，才自动进入已冻结的 Phase 12B Implementation Plan。

~~~bash
git add scripts/run_phase12a_dag_plan_engine_demo.py scripts/run_all.py tests/unit/test_phase12a_demo.py docs/superpowers/reports/phase-12a-dag-plan-engine-acceptance.md docs/project_guidance/agent_runtime_evolution_roadmap.md docs/worklog/task_plan.md docs/worklog/findings.md docs/worklog/progress.md
git commit -m "feat: add phase 12a plan engine demo"
~~~

## Plan Self-Review

- D-065 的冻结手卡批次由 Task 1、2、4、7 实现；Task 8 只收紧既有建播审批兼容，不让 PlanEngine 接管建播或售罄。
- D-066 的固定 Provider、D-068 的受限绑定、D-069 的 Capability Profile 由 Task 1、2 实现；没有任务实现真实 LLM Provider。
- D-067 的六表 PlanStore 与 D-031/D-032 的并发证据由 Task 3、5、6 实现；Task 6 只扩展 `plan_runs` 对账字段，不新增第七张事故表。Phase 11B Attempt Store 只作为可选关联，不被扩展为 DAG Store。
- D-023 至 D-034 的 FailurePolicy、命令账本、checkpoint 权威和对账由 Task 3、4、6 实现；自动重试只属于 Worker。D-071 由 Task 3、6、9 的四命令、TTL、旧版本拒绝和批次失败验收覆盖；D-072 由 Task 5、6、9 的单元加真实 PostgreSQL/PostgresSaver 验收覆盖。
- D-070 的默认 Legacy 路由与局部 Graph 接入由 Task 7 实现；没有 HTTP/UI、动态配置、生产双执行或 Legacy fallback。
- D-045 的兼容审批重新评估由 Task 8 收口：删除 TRUSTED_COMPAT，真实 HUMAN_INTERRUPT 保持唯一 Runtime 建播批准来源。
- Task 9 以真实 PostgreSQL/PostgresSaver 集成证据、无外部依赖 Demo、全量回归和 Acceptance 结束阶段；本计划本轮只持久化，未获正式实施授权前不得执行 Task 6。
