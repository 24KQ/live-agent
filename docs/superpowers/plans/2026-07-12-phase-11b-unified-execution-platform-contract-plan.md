# Phase 11B Unified Execution and Platform Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 把 13 个 Skill 收敛到具有 deadline、FailureFact、Attempt 证据、有状态 Fake 和三批可回滚路由的统一执行契约。

**Architecture:** SkillExecutor 继续是唯一门禁入口，但改为原生 async 单次尝试。业务状态通过商品与价格、直播会话、播中运营三个 Port 访问；Attempt Store 在 Adapter 调用前持久化意图，以唯一 Operation 阻止重复副作用。同步 Graph/Harness 只通过桥接器复用同一核心。

**Tech Stack:** Python 3.11、Pydantic v2、asyncio、psycopg 3、PostgreSQL、LangGraph、pytest、jsonschema。

---

## 实施边界

- 新增或修改 Python 代码使用 UTF-8，并添加说明职责、信任边界和失败语义的中文注释。
- 不实现 PlanEngine、自动重试、Replan、Command Ledger、真实淘宝 API、热加载或多 Agent。
- 不扩大 TRUSTED_COMPAT；HUMAN_INTERRUPT 继续只能由受控工厂构造。
- Handler、Adapter、Executor 和调用方均执行单次尝试。FailureFact 不得触发 sleep、循环、Legacy fallback 或隐式第二次调用。
- 每个 skill_id + version + room_id + idempotency_key 最多只有一个 Operation 和一个外部 Attempt；SIDE_EFFECT_UNKNOWN 不得自动重放。
- 保持 ToolRegistry 投影、播前 Graph 外观、checkpoint 和 interrupt 拓扑不变。

## Task 1: Phase 11B 模型与 Manifest 尝试上限

**Files:**
- Modify: src/skill_runtime/models.py
- Modify: src/skill_runtime/catalog.py
- Modify: src/skill_runtime/__init__.py
- Test: tests/unit/test_phase11b_models.py
- Test: tests/unit/test_skill_catalog.py

- [ ] **Step 1: 写失败事实和 deadline 红灯测试。**

~~~python
def test_failure_fact_is_frozen_and_has_no_recovery_action() -> None:
    fact = FailureFact(
        category=FailureCategory.RATE_LIMITED,
        external_code="fake.rate_limited",
        side_effect_state=SideEffectState.NOT_SENT,
        attempt_id="attempt-001",
        retry_after_seconds=3,
    )
    assert fact.category == FailureCategory.RATE_LIMITED
    with pytest.raises(ValidationError):
        fact.category = FailureCategory.TRANSIENT_INFRA  # type: ignore[misc]


def test_context_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        SkillExecutionContext.model_validate(_context(deadline_at="2026-07-12T10:00:00"))
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_models.py -q

Expected: FAIL，因为 FailureFact、deadline_at 和 Manifest 尝试上限尚不存在。

- [ ] **Step 3: 实现最小公共契约。**

新增 FailureCategory（D-023 的八类固定枚举）、SideEffectState（NOT_SENT、CONFIRMED、UNKNOWN）、冻结 FailureFact、AdapterRequest、AdapterSuccess。给 SkillExecutionContext 增加 UTC、时区感知的 deadline_at；给 SkillManifest 增加 max_attempt_seconds，范围 1 至 60，13 个首版 Manifest 显式固定为 15。给 SkillExecutionResult 增加可选 failure 和 attempt_id，成功结果禁止携带 FailureFact。

~~~python
class FailureFact(BaseModel, frozen=True):
    category: FailureCategory
    external_code: str
    side_effect_state: SideEffectState
    attempt_id: str
    retry_after_seconds: int | None = Field(default=None, ge=0)
~~~

- [ ] **Step 4: 扩展 Catalog 断言并运行绿灯。**

Run: pytest tests/unit/test_phase11b_models.py tests/unit/test_skill_catalog.py -q

Expected: PASS，13 个 Manifest 均为 1.0.0、根 Schema 闭合且尝试上限为 15。

- [ ] **Step 5: 提交。**

~~~bash
git add src/skill_runtime/models.py src/skill_runtime/catalog.py src/skill_runtime/__init__.py tests/unit/test_phase11b_models.py tests/unit/test_skill_catalog.py
git commit -m "feat: add phase 11b runtime contracts"
~~~

## Task 2: 独立 Attempt Store 与 PostgreSQL 迁移

**Files:**
- Create: src/skill_runtime/attempt_store.py
- Create: docker/init_phase11b_skill_attempts.sql
- Modify: scripts/run_db_migrations.py
- Test: tests/unit/test_phase11b_attempt_store.py
- Test: tests/integration/test_phase11b_postgres_attempt_store.py

- [ ] **Step 1: 写 Operation 去重和意图先写测试。**

~~~python
def test_second_claim_reuses_attempt_without_new_external_work() -> None:
    store = InMemoryAttemptStore()
    first = store.claim_or_replay(_operation_request())
    second = store.claim_or_replay(_operation_request())
    assert first.created is True
    assert second.created is False
    assert second.record.attempt_id == first.record.attempt_id


def test_terminal_update_requires_intent_state() -> None:
    with pytest.raises(AttemptInvariantError):
        InMemoryAttemptStore().complete_success("missing", {"ok": True})
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_attempt_store.py -q

Expected: FAIL，缺少 Store 和原子 claim 语义。

- [ ] **Step 3: 实现 Store Protocol、内存实现与 SQL。**

定义 AttemptStore.claim_or_replay()、complete_success()、complete_failure()；状态只能从 INTENT_RECORDED 进入 SUCCEEDED、FAILED 或 SIDE_EFFECT_UNKNOWN。DDL 创建 skill_execution_operations 和 skill_execution_attempts：Operation 的 (skill_id, skill_version, room_id, idempotency_key) 唯一，Attempt 的 operation_id 唯一；存储 request digest、deadline、意图 JSON、终态 JSON、失败分类和副作用状态。

PostgreSQL claim 使用 INSERT ON CONFLICT DO NOTHING 后 SELECT；终态更新使用 WHERE attempt_id = ... AND state = INTENT_RECORDED，零行更新抛 AttemptInvariantError。Store 内显式使用 READ COMMITTED。

- [ ] **Step 4: 写 PostgreSQL 并发集成测试。**

两个连接同时 claim 同一 Operation，断言同一 attempt ID；成功、确定失败和未知副作用只能闭合一次；重复调用只读取原记录。

- [ ] **Step 5: 注册迁移、运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_attempt_store.py tests/integration/test_phase11b_postgres_attempt_store.py -q

Run: python scripts/run_db_migrations.py --dry-run

Expected: PASS；输出包含 phase11b。

~~~bash
git add src/skill_runtime/attempt_store.py docker/init_phase11b_skill_attempts.sql scripts/run_db_migrations.py tests/unit/test_phase11b_attempt_store.py tests/integration/test_phase11b_postgres_attempt_store.py
git commit -m "feat: persist phase 11b execution attempts"
~~~

## Task 3: 业务域 Port 与有状态 Fake Platform

**Files:**
- Create: src/skill_runtime/platform_ports.py
- Create: src/skill_runtime/fake_platform.py
- Test: tests/unit/test_phase11b_fake_platform.py

- [ ] **Step 1: 写 Fake 状态、CAS 与故障脚本红灯测试。**

~~~python
async def test_price_cas_conflict_does_not_mutate_state() -> None:
    platform = FakeLiveCommercePlatform.from_fixture(_fixture())
    before = platform.product("p001").price
    result = await platform.set_price(_request(expected_version=99))
    assert result.category == FailureCategory.VERSION_CONFLICT
    assert platform.product("p001").price == before


async def test_unknown_after_send_preserves_mutation_evidence() -> None:
    platform = FakeLiveCommercePlatform.from_fixture(_fixture_with_fault("set_price", "UNKNOWN_AFTER_SEND"))
    result = await platform.set_price(_request())
    assert result.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert platform.product("p001").price == Decimal("19.90")
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_fake_platform.py -q

Expected: FAIL，因为 Port、Fixture 和故障脚本未实现。

- [ ] **Step 3: 实现三个 async Port 和单实例 Fake。**

定义 ProductPricingPort（货盘读取、价格 CAS）、LiveSessionPort（建播准备/查询）、LiveOperationsPort（售罄、只读商品上下文解析、备选、上下文）。FakeLiveCommercePlatform 同时实现三个 Port，但状态只属于单个实例；Fixture 使用冻结 Pydantic 模型，故障规则按 operation_name + resource_key + call_index 匹配。

允许故障仅为 RATE_LIMITED、VERSION_CONFLICT、DEADLINE_BEFORE_SEND、UNKNOWN_AFTER_SEND。Fake 不使用随机数、sleep 或真实网络；发送前 deadline 失败为 TRANSIENT_INFRA/NOT_SENT，发送后未知为 SIDE_EFFECT_UNKNOWN/UNKNOWN。

- [ ] **Step 4: 补充隔离与重放断言。**

验证不同 Fake 实例互不污染；售罄后备选跳过失效商品；同一建播幂等键返回相同会话；限流携带 retry_after_seconds。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_fake_platform.py -q

Expected: PASS。

~~~bash
git add src/skill_runtime/platform_ports.py src/skill_runtime/fake_platform.py tests/unit/test_phase11b_fake_platform.py
git commit -m "feat: add stateful fake platform ports"
~~~

## Task 4: async Executor、deadline 与 FailureFact 传播

**Files:**
- Modify: src/skill_runtime/executor.py
- Test: tests/unit/test_phase11b_executor.py
- Test: tests/unit/test_skill_executor.py

- [ ] **Step 1: 写执行顺序和 timeout 红灯测试。**

~~~python
async def test_executor_writes_intent_before_handler_call() -> None:
    events: list[str] = []
    result = await _executor(RecordingAttemptStore(events), RecordingHandler(events)).execute(_call())
    assert result.status == SkillExecutionStatus.SUCCESS
    assert events == ["claim", "handler", "success"]


async def test_timeout_after_handler_started_is_unknown() -> None:
    result = await _executor(handler=UnknownAfterSendHandler()).execute(_expired_call())
    assert result.failure.category == FailureCategory.SIDE_EFFECT_UNKNOWN
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_executor.py -q

Expected: FAIL，现有 Handler 为同步函数且 Executor 无 Attempt/FailureFact。

- [ ] **Step 3: 实现唯一 async 单次尝试核心。**

将 _SkillHandler.execute() 改为 async，返回 _SkillHandlerResult、AdapterSuccess 或 FailureFact。执行顺序固定为 Manifest、版本、生命周期、Schema、幂等、审批、deadline、Attempt claim、Handler、Attempt 终态和 Result 映射。

~~~python
remaining = (call.context.deadline_at - datetime.now(timezone.utc)).total_seconds()
timeout = min(remaining, manifest.max_attempt_seconds)
if timeout <= 0:
    return self._finish_not_sent_deadline(call)
outcome = await asyncio.wait_for(
    handler.execute(call.skill_id, call.arguments, call.context),
    timeout=timeout,
)
~~~

已返回的 FailureFact 原样闭合并映射；Handler 异常仍脱敏为 HANDLER_FAILED。asyncio.TimeoutError 仅在 Handler 已开始后闭合为 SIDE_EFFECT_UNKNOWN。禁止 asyncio.to_thread、隐藏重试和 Legacy fallback。

SyncSkillExecutorAdapter 仅用 asyncio.run() 桥接；若当前线程已有事件循环，fail-closed 并要求调用方直接 await async 接口，不能创建嵌套 loop 或线程池。

- [ ] **Step 4: 迁移 Phase 11A 替身与回归。**

把旧测试 Handler 替身改为 async，保留门禁顺序、脱敏摘要、非 JSON 输出、同步/异步入口和 Handler 映射快照断言。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_executor.py tests/unit/test_skill_executor.py -q

Expected: PASS。

~~~bash
git add src/skill_runtime/executor.py tests/unit/test_phase11b_executor.py tests/unit/test_skill_executor.py
git commit -m "feat: enforce async deadline execution"
~~~

## Task 5: 统一 Handler 装配与批次一迁移

**Files:**
- Create: src/skill_runtime/handlers.py
- Modify: src/skill_runtime/pre_live_handlers.py
- Modify: src/skill_runtime/pre_live_facade.py
- Test: tests/unit/test_phase11b_handlers_batch1.py
- Test: tests/unit/test_pre_live_skill_handlers.py

- [ ] **Step 1: 写批次一 10 个 Handler 装配红灯测试。**

~~~python
@pytest.mark.parametrize("skill_id", BATCH_ONE_SKILL_IDS)
async def test_batch_one_handlers_are_registered(skill_id: str) -> None:
    assert skill_id in build_skill_handlers(_dependencies())


async def test_query_products_uses_product_port_only() -> None:
    ports = RecordingPorts(products=[_product_snapshot()])
    outcome = await build_skill_handlers(_dependencies(ports=ports))["query_products"].execute(...)
    assert ports.calls == ["list_products"]
    assert outcome.output["products"][0]["product_id"] == "p001"
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_handlers_batch1.py -q

Expected: FAIL，当前只有四个同步播前 Handler。

- [ ] **Step 3: 实现 SkillRuntimeDependencies 和 10 个 Handler。**

build_skill_handlers(dependencies) 返回 13 个局部 Handler 映射，禁止运行期全局依赖替换。批次一固定为 query_products、generate_live_plan、generate_product_card、suggest_price_change、create_live_plan_draft、recommend_backup_product、generate_on_live_prompt、aggregate_danmaku_questions、generate_danmaku_reply、on_live_context_collect。

平台状态读取仅通过 Port；`recommend_backup_product` 与 `generate_on_live_prompt` 使用 LiveOperationsPort.resolve_product_context 获取可信商品快照后复用确定性领域函数，不读取旧 Graph State、不伪造商品对象、不触发 Legacy fallback。排品、手卡、提示、聚合和回复继续使用确定性领域函数，不伪造外部请求。旧 build_pre_live_handlers() 只做兼容装配并委托新工厂，不保留第二套 Handler 逻辑。

- [ ] **Step 4: 保持播前 Facade 外观。**

Facade 继续返回 CatalogProduct、LivePlanDraft、ProductCard、GateResult；只进行领域对象/JSON 快照转换，不重查上游数据、不创建额外 Attempt。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_handlers_batch1.py tests/unit/test_pre_live_skill_handlers.py tests/unit/test_skill_runtime_equivalence.py -q

Expected: PASS。

~~~bash
git add src/skill_runtime/handlers.py src/skill_runtime/pre_live_handlers.py src/skill_runtime/pre_live_facade.py tests/unit/test_phase11b_handlers_batch1.py tests/unit/test_pre_live_skill_handlers.py
git commit -m "feat: migrate batch one skill handlers"
~~~

## Task 6: 三批启动冻结路由与 AgentToolExecutor 收敛

**Files:**
- Modify: src/config/settings.py
- Modify: src/skill_runtime/routing.py
- Modify: src/core/agent_tool_executor.py
- Test: tests/unit/test_phase11b_routing.py
- Test: tests/unit/test_settings.py
- Test: tests/unit/test_agent_tool_executor_skill_compat.py

- [ ] **Step 1: 写批次独立与无 fallback 红灯测试。**

~~~python
def test_phase11b_routes_default_to_legacy() -> None:
    policy = RoutePolicy.from_settings(Settings(_env_file=None))
    assert (policy.batch1, policy.batch2, policy.batch3) == (RouteConfig.LEGACY,) * 3


def test_runtime_failure_never_runs_legacy_for_same_call() -> None:
    executor = _executor_with_runtime_failure(batch1=RouteConfig.SKILL_RUNTIME)
    result = executor.execute("suggest_price_change", _args(), "room-1", "trace-1")
    assert result.status == "error"
    assert executor.legacy_calls == []
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_routing.py tests/unit/test_settings.py -q

Expected: FAIL，现有 RoutePolicy 只有 generation/setup。

- [ ] **Step 3: 实现三批 Settings 与兼容映射。**

新增 SKILL_ROUTE_PHASE11B_BATCH1、BATCH2、BATCH3，默认 LEGACY。旧 SKILL_ROUTE_PRELIVE_GENERATION/SETUP 保留到 Phase 12；新 batch 环境变量未设置时 generation 映射 batch1、setup 映射 batch2、batch3 保持 LEGACY。RoutePolicy.from_settings() 一次解析后冻结。

AgentToolExecutor 按 Skill 所属批次选择 Legacy 或 Runtime。所有 Runtime 调用都经同步桥接；删除不可达 switch_product 分支和旧 jsonschema 可选跳过分支。FailureFact 映射到脱敏 AgentObservation，附带稳定类别和 attempt/audit ID。

- [ ] **Step 4: 回归旧播前配置。**

验证旧 Settings 仍能控制 batch1/batch2；调用开始后环境变化不影响已装配 Policy；Runtime 失败和副作用未知从不回退 Legacy。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_routing.py tests/unit/test_settings.py tests/unit/test_agent_tool_executor.py tests/unit/test_agent_tool_executor_skill_compat.py -q

Expected: PASS。

~~~bash
git add src/config/settings.py src/skill_runtime/routing.py src/core/agent_tool_executor.py tests/unit/test_phase11b_routing.py tests/unit/test_settings.py tests/unit/test_agent_tool_executor.py tests/unit/test_agent_tool_executor_skill_compat.py
git commit -m "feat: add phase 11b batch routing"
~~~

## Task 7: 批次二建播/售罄与播中 Harness 接入

**Files:**
- Modify: src/skill_runtime/handlers.py
- Modify: src/core/on_live_agent_graph.py
- Modify: src/core/on_live_harness_agent_graph.py
- Test: tests/unit/test_phase11b_handlers_batch2.py
- Test: tests/unit/test_on_live_agent_graph_real.py
- Test: tests/unit/test_on_live_harness_agent_graph.py

- [ ] **Step 1: 写人审、售罄幂等和 Graph 兼容红灯测试。**

~~~python
async def test_setup_without_trusted_approval_is_pending_without_attempt() -> None:
    result = await _runtime().execute(_setup_call(approval=None))
    assert result.status == SkillExecutionStatus.PENDING
    assert _store().records == []


async def test_sold_out_replay_invokes_port_once() -> None:
    first = await _runtime().execute(_sold_out_call("idem-sold-out-1"))
    second = await _runtime().execute(_sold_out_call("idem-sold-out-1"))
    assert second.attempt_id == first.attempt_id
    assert _platform().sold_out_call_count == 1
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_handlers_batch2.py -q

Expected: FAIL，建播直接调用播前服务，售罄仍走 _LocalServiceExecutor。

- [ ] **Step 3: 实现批次二 Handler 与 RuntimeOnLiveExecutor。**

setup_live_session 只在 Executor 已验证可信审批和幂等键后调用 LiveSessionPort.prepare_session。handle_sold_out_event 调用 LiveOperationsPort.mark_sold_out 并返回售罄、备选和提示事实。

新增 RuntimeOnLiveExecutor，保留现有 execute(tool_name, arguments, room_id, trace_id, state=...) -> dict 形状，内部创建 ON_LIVE SkillCall 并走同步桥接。_LocalServiceExecutor 仅保留 Legacy 路径；不改变 Harness Graph 边、interrupt payload 或 JSON state。

- [ ] **Step 4: 运行批准/拒绝回归。**

拒绝时不得创建 Attempt 或调用 Port；批准时只产生一个 Attempt；售罄结果仍能进入 Hook observation 和既有 Harness Audit writer。

- [ ] **Step 5: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_handlers_batch2.py tests/unit/test_on_live_agent_graph_real.py tests/unit/test_on_live_harness_agent_graph.py tests/integration/test_on_live_flow.py -q

Expected: PASS。

~~~bash
git add src/skill_runtime/handlers.py src/core/on_live_agent_graph.py src/core/on_live_harness_agent_graph.py tests/unit/test_phase11b_handlers_batch2.py tests/unit/test_on_live_agent_graph_real.py tests/unit/test_on_live_harness_agent_graph.py
git commit -m "feat: migrate session and sold-out skills"
~~~

## Task 8: 批次三高风险改价

**Files:**
- Modify: src/skill_runtime/catalog.py
- Modify: src/skill_runtime/handlers.py
- Modify: src/core/agent_tool_executor.py
- Modify: tests/unit/test_skill_catalog.py
- Modify: tests/unit/test_phase11b_routing.py
- Modify: tests/unit/test_skill_executor.py
- Modify: tests/unit/test_phase11b_executor.py
- Create: tests/unit/test_phase11b_handlers_batch3.py
- Create: tests/integration/test_phase11b_price_flow.py
- Regression: tests/unit/test_agent_tool_executor_skill_compat.py

- [ ] **Step 1: 写 Catalog 版本与改价 Schema 红灯测试。**

~~~python
def test_catalog_has_twelve_v1_skills_and_price_v1_1() -> None:
    versions = {manifest.skill_id: manifest.version for manifest in get_default_skill_catalog()}
    assert list(versions.values()).count("1.0.0") == 12
    assert versions["set_product_price"] == "1.1.0"


def test_price_schema_requires_explicit_resource_version() -> None:
    manifest = _manifest("set_product_price")
    assert manifest.parameter_schema == {
        "type": "object",
        "required": ["product_id", "price", "expected_version"],
        "properties": {
            "product_id": {"type": "string"},
            "price": {"type": "string", "pattern": "^[0-9]+(?:\\.[0-9]+)?$"},
            "expected_version": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }
~~~

- [ ] **Step 2: 写版本、前置门禁和 Adapter 结果红灯测试。**

`tests/unit/test_phase11b_handlers_batch3.py` 使用记录型 ProductPricingPort 和内存 Attempt Store。先固定所有 Handler / Attempt 前失败，再覆盖一次且仅一次的 Port 调用：

~~~python
async def test_price_v1_is_rejected_before_handler_and_attempt() -> None:
    runtime, port, store = _runtime()
    result = await runtime.execute(_price_call(version="1.0.0"))
    assert result.error_code == SkillErrorCode.VERSION_MISMATCH
    assert port.calls == []
    assert store.claims == 0


@pytest.mark.parametrize(
    ("call", "status", "error_code"),
    [
        (_price_call(arguments={"product_id": "p001", "price": "39.90"}), SkillExecutionStatus.ERROR, SkillErrorCode.INVALID_ARGUMENTS),
        (_price_call(idempotency_key=None), SkillExecutionStatus.ERROR, SkillErrorCode.IDEMPOTENCY_REQUIRED),
        (_price_call(approval=None), SkillExecutionStatus.PENDING, SkillErrorCode.APPROVAL_REQUIRED),
        (_price_call(approval=_rejected_approval()), SkillExecutionStatus.ERROR, SkillErrorCode.APPROVAL_REJECTED),
    ],
)
async def test_price_preconditions_never_create_attempt_or_call_port(
    call: SkillCall,
    status: SkillExecutionStatus,
    error_code: SkillErrorCode,
) -> None:
    runtime, port, store = _runtime()
    result = await runtime.execute(call)
    assert (result.status, result.error_code) == (status, error_code)
    assert port.calls == []
    assert store.claims == 0
~~~

`CountingAttemptStore` 只在测试内继承 `InMemoryAttemptStore` 并覆写
`claim_or_replay()` 递增 `claims`；生产 Store 不增加测试专用的列表查询 API。

非法价格（`Infinity`、`NaN`、负数、指数写法和空值）必须由 Schema 在 Attempt / Port 前返回 `INVALID_ARGUMENTS`；不得写入非有限 Decimal，也不得把未发送请求错误闭合为 `SIDE_EFFECT_UNKNOWN`。

`tests/integration/test_phase11b_price_flow.py` 通过内部 `SkillCall`、受控 `ApprovalContext` 和独立 Fake Platform / Attempt Store 覆盖成功、商品资源版本冲突、限流、发送后未知及同一 Operation 重放。冲突必须断言 `FailureCategory.VERSION_CONFLICT`，重放必须断言 Fake `set_price` 调用总数仍为 1。

`tests/unit/test_phase11b_routing.py` 固定 AgentToolExecutor 的批次三兼容边界：Runtime SkillCall 钉住 `1.1.0`，`idempotency_key` 只进入 Context，业务 arguments 只保留 `product_id`、`price`、`expected_version`；`approval is None`，结果为 `pending`，没有 Attempt / Port 调用，也没有 Legacy fallback。另测启动冻结 `LEGACY` 路由仍能显式回滚新调用。

- [ ] **Step 3: 运行完整 RED，确认失败原因都来自尚未纠偏的版本与 Handler。**

Run: pytest tests/unit/test_skill_catalog.py tests/unit/test_phase11b_handlers_batch3.py tests/integration/test_phase11b_price_flow.py tests/unit/test_phase11b_routing.py tests/unit/test_agent_tool_executor_skill_compat.py -q

Expected: FAIL；Catalog 仍注册 `set_product_price@1.0.0` 且缺少 `expected_version`，AgentToolExecutor 仍硬编码 `1.0.0` 并保留兼容幂等字段，统一工厂仍装配未支持的批次三 Handler。

- [ ] **Step 4: 实现 Catalog Schema、精确版本钉住与兼容参数搬移。**

在 `catalog.py` 只把 `set_product_price` 单活版本升级为 `1.1.0`，并把 `expected_version: {"type": "integer", "minimum": 1}` 加入必填业务 Schema；根对象继续 `additionalProperties: false`。其余 12 个 Manifest 版本保持 `1.0.0`，D-035 冻结集合中剩余 8 个未迁移工具的元数据哈希严格保持。ToolRegistry 不新增 version 字段，只投影 Catalog 当前 Schema。

AgentToolExecutor 在构造时从 Catalog 复制 `skill_id -> 精确单活 version` 的不可变映射，Runtime 调用不得再硬编码 `1.0.0`。兼容参数搬移只对白名单 `set_product_price` 生效：从副本中移除 `idempotency_key` 并写入 `SkillExecutionContext.idempotency_key`；其他 Skill 的 arguments 行为保持不变。不得新增 `approval` 参数、`execute_approved` 方法或任何可由 Agent 入口构造的批准证据，Context 的 `approval` 保持 `None`。

- [ ] **Step 5: 实现 SetProductPriceHandler 的单次 CAS 调用。**

用 `SetProductPriceHandler` 替换统一工厂中的 `_UnsupportedPhase11BHandler("set_product_price")`。Handler 只根据已校验 arguments 与 Context 构造一次 AdapterRequest，并恰好调用一次 `ProductPricingPort.set_price`；不得预读商品、内部重试、sleep、再次调用 Port 或 Legacy fallback。AdapterSuccess 原样映射业务输出；`VERSION_CONFLICT`、`RATE_LIMITED` 和 `SIDE_EFFECT_UNKNOWN` 原样作为 FailureFact 传播。

失败语义固定如下：旧 Skill `1.0.0` 在 Handler / Attempt 前返回 `SkillErrorCode.VERSION_MISMATCH`；缺少 `expected_version` 返回 `INVALID_ARGUMENTS`；缺幂等键返回 `IDEMPOTENCY_REQUIRED`；缺批准返回 `PENDING + APPROVAL_REQUIRED`；拒绝返回 `APPROVAL_REJECTED`，以上均无 Attempt / Port。只有受控批准且通过前置校验的 `1.1.0` 调用创建 Attempt；商品 `expected_version` 过期由 Adapter 返回 `FailureFact`，其 `category=FailureCategory.VERSION_CONFLICT`。限流保留 `retry_after`，发送后未知保留 `SIDE_EFFECT_UNKNOWN`，Operation 重放返回首次终态且不产生第二次 Port 调用。

- [ ] **Step 6: 运行 GREEN 与兼容回归。**

Run: pytest tests/unit/test_phase11b_handlers_batch3.py tests/integration/test_phase11b_price_flow.py tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_skill_executor.py tests/unit/test_phase11b_executor.py -q

Expected: PASS。

Run: pytest tests/unit/test_skill_catalog.py tests/unit/test_phase11b_routing.py -q

Expected: PASS；Catalog 版本分布、Schema 投影、AgentToolExecutor `1.1.0` / `pending` / no fallback 与批次三 `LEGACY` 回滚全部锁定。

- [ ] **Step 7: 只暂存 Task 8 业务与测试文件并提交。**

~~~bash
git add src/skill_runtime/catalog.py src/skill_runtime/handlers.py src/skill_runtime/compatibility.py src/core/agent_tool_executor.py tests/unit/test_skill_catalog.py tests/unit/test_phase11b_routing.py tests/unit/test_skill_executor.py tests/unit/test_phase11b_executor.py tests/unit/test_phase11b_handlers_batch3.py tests/integration/test_phase11b_price_flow.py tests/unit/test_agent_tool_executor_skill_compat.py
git commit -m "feat: migrate high-risk price skill"
~~~

## Task 9: 测试比较器与六场景无外部依赖 Demo

**Files:**
- Create: tests/unit/test_phase11b_equivalence.py
- Create: tests/unit/test_phase11b_demo.py
- Create: scripts/run_phase11b_platform_contract_demo.py
- Modify: scripts/run_all.py

- [ ] **Step 1: 写隔离比较器和 Demo 红灯测试。**

~~~python
def test_write_comparison_uses_isolated_fake_stacks() -> None:
    legacy, runtime = run_isolated_comparison(_approved_setup_case())
    assert legacy.audit_ids != runtime.audit_ids
    assert normalize(legacy.output) == normalize(runtime.output)


def test_demo_emits_six_fixed_scenarios() -> None:
    names = [row["scenario"] for row in run_demo_scenarios(emit=False)]
    assert names == [
        "setup_success",
        "sold_out",
        "rate_limited",
        "version_conflict",
        "deadline",
        "side_effect_unknown",
    ]
~~~

- [ ] **Step 2: 验证红灯。**

Run: pytest tests/unit/test_phase11b_equivalence.py tests/unit/test_phase11b_demo.py -q

Expected: FAIL，缺少比较器和 Demo。

- [ ] **Step 3: 实现比较器和 Demo。**

比较器仅用于测试，Legacy/Runtime 分别装配独立 Fake、Attempt Store 和内存审计；比较业务结果、FailureFact 类别和可观察状态，不比较随机 audit/attempt ID。Demo 不连接 PostgreSQL、Kafka、LLM 或真实平台，固定输出成功建播、售罄、限流、版本冲突、deadline、副作用未知六个场景。

在 scripts/run_all.py 新增 phase11b-demo，只委托新脚本。

- [ ] **Step 4: 运行绿灯并提交。**

Run: pytest tests/unit/test_phase11b_equivalence.py tests/unit/test_phase11b_demo.py -q

Run: python scripts/run_phase11b_platform_contract_demo.py

Expected: PASS；六个场景全部输出，退出码为 0。

~~~bash
git add tests/unit/test_phase11b_equivalence.py tests/unit/test_phase11b_demo.py scripts/run_phase11b_platform_contract_demo.py scripts/run_all.py
git commit -m "feat: add phase 11b contract demo"
~~~

## Task 10: 最终验收与阶段留迹

**Files:**
- Create: docs/superpowers/reports/phase-11b-unified-execution-platform-contract-acceptance.md
- Modify: docs/project_guidance/agent_runtime_evolution_roadmap.md
- Modify: docs/superpowers/plans/2026-07-12-phase-11b-unified-execution-platform-contract-plan.md
- Modify: docs/worklog/task_plan.md
- Modify: docs/worklog/findings.md
- Modify: docs/worklog/progress.md

- [ ] **Step 1: 运行 Runtime 专项。**

~~~bash
pytest tests/unit/test_phase11b_models.py tests/unit/test_phase11b_attempt_store.py tests/unit/test_phase11b_fake_platform.py tests/unit/test_phase11b_executor.py tests/unit/test_phase11b_handlers_batch1.py tests/unit/test_phase11b_routing.py tests/unit/test_phase11b_handlers_batch2.py tests/unit/test_phase11b_handlers_batch3.py tests/unit/test_phase11b_equivalence.py tests/unit/test_phase11b_demo.py -q
~~~

Expected: PASS。

- [ ] **Step 2: 运行相关系统回归。**

~~~bash
pytest tests/unit/test_skill_executor.py tests/unit/test_pre_live_skill_handlers.py tests/unit/test_skill_runtime_routing.py tests/unit/test_agent_tool_executor.py tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_on_live_agent_graph_real.py tests/unit/test_on_live_harness_agent_graph.py tests/integration/test_pre_live_graph_skill_runtime_flow.py tests/integration/test_on_live_flow.py tests/integration/test_danmaku_flow.py tests/integration/test_phase11b_postgres_attempt_store.py tests/integration/test_phase11b_price_flow.py tests/integration/test_agent_evaluation_flow.py -q
~~~

Expected: PASS。

- [ ] **Step 3: 运行全量、Demo 与静态检查。**

~~~bash
pytest -q
python scripts/run_phase11b_platform_contract_demo.py
python scripts/run_all.py phase11b-demo
git diff --check
python scripts/check_doc_encoding.py
~~~

Expected: pytest、两个 Demo 和 git diff --check 为 0。若编码扫描仍因扫描器自身 replacement-character 样例或历史 BOM/混合换行退出 1，Acceptance 必须列明该事实和本阶段目标文件零命中，不能虚报全仓通过。

- [ ] **Step 4: 编写 Acceptance 并同步状态。**

报告必须记录 13 个 Handler 最终装配、三批路由、Attempt 写入顺序、FailureFact/deadline 证据、六场景 Demo、精确测试计数、Design 偏差、编码历史问题和 Phase 12A 进入条件。路线图仅更新为“Phase 11B 技术验收完成，Acceptance 待用户审核”，不得写成用户已接受。

- [ ] **Step 5: 提交文档。**

~~~bash
git add docs/superpowers/reports/phase-11b-unified-execution-platform-contract-acceptance.md docs/project_guidance/agent_runtime_evolution_roadmap.md docs/superpowers/plans/2026-07-12-phase-11b-unified-execution-platform-contract-plan.md docs/worklog/task_plan.md docs/worklog/findings.md docs/worklog/progress.md
git commit -m "docs: record phase 11b acceptance"
~~~

## Plan Self-Review

- D-054 至 D-064 由 Task 1 至 Task 10 及 Task 8 前置契约纠偏覆盖：三 Port、状态化 Fake、deadline/async、FailureFact、Attempt Store、三批路由、switch_product 清理、版本规则、验收门槛、播中只读商品上下文解析和高风险改价版本契约。
- 所有外部写都先经过 Attempt claim；所有自动重试、PlanEngine、真实平台和多 Agent 均被排除。
- 每个任务先 RED、再 GREEN、再回归并单独提交。若现有公开契约与本 Plan 冲突，必须先更新 Design/Decisions 并获得用户确认，不能自行扩展范围。
