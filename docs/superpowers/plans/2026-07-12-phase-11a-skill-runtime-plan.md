# Phase 11A Skill Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Manifest 唯一事实源、统一 SkillExecutor、两批可回滚路由和四个播前核心 Handler，并保持 ToolRegistry、播前 Graph 与 AgentToolExecutor 兼容。

**Architecture:** 新建 `src/skill_runtime/` 包承载模型、Catalog、Executor、Handler、路由和兼容适配。播前 Graph 保持同步拓扑，通过 `RoutedPreLiveBusinessService` 和内部同步桥接器进入异步标准 Runtime；正式路由只允许 `LEGACY` 与 `SKILL_RUNTIME`，新旧双算只存在于隔离测试。

**Tech Stack:** Python 3.11、Pydantic 2、jsonschema Draft 2020-12、LangGraph 1.2.8、pytest 8、现有 PostgreSQL/Fake Store。

---

## 实施规则

- 开始前读取 Design、D-035 至 D-049、三个 worklog 和当前 `git status`。
- 在现有工作区存在用户修改时只追加或兼容，不得回退无关文件。
- 每个任务严格执行红灯 -> 最小实现 -> 绿灯；不能先写实现再补测试。
- 新增或修改代码必须添加解释职责、信任边界、幂等或兼容原因的 UTF-8 中文注释。
- 每个任务独立提交；提交前仅暂存该任务文件，不包含无关未跟踪文件。
- 不实现 Phase 11B 的真实平台 Adapter、统一重试、PlanEngine 或多 Agent。

## 文件结构

新增运行时模块：

```text
src/skill_runtime/__init__.py              # 稳定导出面
src/skill_runtime/models.py                # Manifest、调用、审批、结果和路由模型
src/skill_runtime/catalog.py               # 13 个 Manifest、Schema 校验和 ToolMetadata 投影
src/skill_runtime/executor.py              # 单次执行核心、异步入口和同步桥接器
src/skill_runtime/pre_live_handlers.py     # 四个播前 Handler
src/skill_runtime/routing.py               # 不可变 RoutePolicy
src/skill_runtime/pre_live_facade.py       # 播前 Graph 兼容 Facade
src/skill_runtime/compatibility.py         # AgentToolExecutor 旧参数规范化
```

新增主要测试：

```text
tests/unit/test_skill_runtime_models.py
tests/unit/test_skill_catalog.py
tests/unit/test_skill_executor.py
tests/unit/test_pre_live_skill_handlers.py
tests/unit/test_skill_runtime_routing.py
tests/unit/test_agent_tool_executor_skill_compat.py
tests/unit/test_skill_runtime_equivalence.py
tests/integration/test_pre_live_graph_skill_runtime_flow.py
scripts/run_phase11a_skill_runtime_demo.py
```

## Task 1：Runtime 模型与正式依赖

**Files:**
- Create: `src/skill_runtime/__init__.py`
- Create: `src/skill_runtime/models.py`
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Test: `tests/unit/test_skill_runtime_models.py`

- [ ] **Step 1：先写模型失败测试**

测试必须覆盖：13 个首版版本格式、route 枚举拒绝未知值、HUMAN_INTERRUPT 缺 operator/audit 时失败、TRUSTED_COMPAT 不能从业务 arguments 构造、SkillCall 冻结、结果状态和错误码受控。

```python
def test_human_approval_requires_operator_and_audit_evidence() -> None:
    """人工批准缺少操作员或审批审计时必须 fail-closed。"""
    with pytest.raises(ValidationError):
        ApprovalContext(source="HUMAN_INTERRUPT", decision="APPROVED")


def test_skill_call_is_immutable() -> None:
    """调用开始后不得替换路由或版本。"""
    call = build_query_products_call()
    with pytest.raises(ValidationError):
        call.context.execution_route = SkillExecutionRoute.SKILL_RUNTIME
```

- [ ] **Step 2：确认测试红灯**

Run: `pytest tests/unit/test_skill_runtime_models.py -q`

Expected: FAIL，原因是 `src.skill_runtime.models` 尚不存在。

- [ ] **Step 3：加入正式 jsonschema 依赖**

在 `pyproject.toml` 和 `requirements.txt` 同时加入 `jsonschema>=4.23.0`，不再保留“安装时才校验”的可选路径。

- [ ] **Step 4：实现冻结模型与枚举**

`models.py` 必须定义：

```python
class SkillExecutionRoute(StrEnum):
    LEGACY = "LEGACY"
    SKILL_RUNTIME = "SKILL_RUNTIME"


class ApprovalSource(StrEnum):
    HUMAN_INTERRUPT = "HUMAN_INTERRUPT"
    TRUSTED_COMPAT = "TRUSTED_COMPAT"


class SkillExecutionStatus(StrEnum):
    SUCCESS = "success"
    PENDING = "pending"
    ERROR = "error"


class SkillErrorCode(StrEnum):
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    LIFECYCLE_MISMATCH = "LIFECYCLE_MISMATCH"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    IDEMPOTENCY_REQUIRED = "IDEMPOTENCY_REQUIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    HANDLER_NOT_FOUND = "HANDLER_NOT_FOUND"
    HANDLER_FAILED = "HANDLER_FAILED"
```

同时实现冻结的 `SkillManifest`、`ApprovalContext`、`SkillExecutionContext`、`SkillCall`、`SkillExecutionResult`。ApprovalContext 的 model validator 固定规则：HUMAN_INTERRUPT 强制 operator_id 和 approval_audit_id；TRUSTED_COMPAT 只允许 APPROVED，且只能由内部 Facade 构造。

- [ ] **Step 5：导出稳定模型 API 并运行测试**

Run: `pytest tests/unit/test_skill_runtime_models.py -q`

Expected: PASS。

- [ ] **Step 6：提交模型任务**

```bash
git add pyproject.toml requirements.txt src/skill_runtime/__init__.py src/skill_runtime/models.py tests/unit/test_skill_runtime_models.py
git commit -m "feat: add phase 11a skill runtime models"
```

## Task 2：SkillCatalog 与 ToolRegistry 只读投影

**Files:**
- Create: `src/skill_runtime/catalog.py`
- Modify: `src/config/tool_registry.py`
- Modify: `tests/unit/test_tool_registry.py`
- Test: `tests/unit/test_skill_catalog.py`

- [ ] **Step 1：写 Catalog 与投影失败测试**

测试固定以下不变量：

- 默认 Catalog 恰好包含当前 13 个 tool name。
- 全部版本为 `1.0.0`，ID 唯一。
- 非法 JSON Schema 和重复 ID 在构造时失败。
- 9 个未迁移工具七字段严格等于冻结 ToolMetadata。
- 4 个核心工具仅 parameter_schema 允许白名单差异，其他字段严格一致且 compatibility_note 非空。
- ToolRegistry 继续按名称排序并拒绝未知工具。

```python
CORE_SCHEMA_OVERRIDES = {
    "query_products",
    "generate_live_plan",
    "generate_product_card",
    "setup_live_session",
}


def test_only_four_core_skills_have_schema_deltas() -> None:
    """迁移白名单之外不得出现元数据漂移。"""
    catalog = get_default_skill_catalog()
    changed = compare_with_frozen_tool_metadata(catalog)
    assert set(changed) == CORE_SCHEMA_OVERRIDES
    assert all(fields == {"parameter_schema"} for fields in changed.values())
```

- [ ] **Step 2：确认测试红灯**

Run: `pytest tests/unit/test_skill_catalog.py tests/unit/test_tool_registry.py -q`

Expected: FAIL，Catalog 尚不存在或 ToolRegistry 仍硬编码元数据。

- [ ] **Step 3：实现 13 个 Manifest 和显式 Schema**

从当前 ToolRegistry 搬迁 13 个 description、lifecycle、risk、gate 和 idempotency 字段，不改文案。四个核心 Schema 固定为：

```text
query_products        -> object，无业务字段，additionalProperties=false
generate_live_plan    -> required products: CatalogProduct snapshot array
generate_product_card -> required product: CatalogProduct snapshot
setup_live_session    -> required plan: LivePlanDraft snapshot
```

其余 9 个 Schema 原样搬迁。Catalog 使用 `Draft202012Validator.check_schema()` 启动校验，并以不可变映射保存 Manifest。

- [ ] **Step 4：让 ToolRegistry 从 Catalog 投影**

保留 ToolMetadata、ToolRegistry 和 ToolNotFoundError 公共 API。`get_default_tool_registry()` 使用函数内导入获取默认 Catalog，避免模块循环；不保留硬编码运行时 fallback。

- [ ] **Step 5：运行专项与现有消费者测试**

Run:

```bash
pytest tests/unit/test_skill_catalog.py tests/unit/test_tool_registry.py tests/unit/test_tool_mask_policy.py tests/unit/test_security_hooks.py tests/unit/test_on_live_harness_planner.py -q
```

Expected: PASS。

- [ ] **Step 6：提交 Catalog 任务**

```bash
git add src/skill_runtime/catalog.py src/config/tool_registry.py tests/unit/test_skill_catalog.py tests/unit/test_tool_registry.py
git commit -m "feat: make skill manifests the tool metadata source"
```

## Task 3：SkillExecutor、门禁与同步桥接器

**Files:**
- Create: `src/skill_runtime/executor.py`
- Test: `tests/unit/test_skill_executor.py`

- [ ] **Step 1：写执行顺序与失败结果测试**

使用记录调用次数的 FakeHandler，分别覆盖未知 Skill、错误版本、错误生命周期、Schema 错误、缺幂等键、缺审批、拒绝审批、Handler 缺失和 Handler 异常。每个前置失败都断言 `handler.calls == 0`。

```python
def test_hard_gate_without_approval_returns_pending_before_handler() -> None:
    """高风险 Skill 缺少可信批准时不得触发业务 Handler。"""
    result = sync_executor.execute(build_setup_call(approval=None))
    assert result.status == SkillExecutionStatus.PENDING
    assert result.error_code == SkillErrorCode.APPROVAL_REQUIRED
    assert setup_handler.calls == 0
```

- [ ] **Step 2：确认测试红灯**

Run: `pytest tests/unit/test_skill_executor.py -q`

Expected: FAIL，Executor 尚不存在。

- [ ] **Step 3：实现唯一单次执行核心**

`SkillExecutor._execute_once()` 按 Design 固定顺序校验，使用 `Draft202012Validator(...).validate(arguments)`。门禁通过 Catalog 投影出的 ToolMetadata 调用现有 `evaluate_tool_gate()`：只有可信 APPROVED ApprovalContext 才令 hard-gate 的 confirmed 为 True。

所有预期校验失败返回结构化 SkillExecutionResult；Handler 未知异常转换为 `HANDLER_FAILED`，摘要只包含异常类型和安全文案，不回显完整参数。

- [ ] **Step 4：实现异步入口与同步桥接器**

```python
async def execute(self, call: SkillCall) -> SkillExecutionResult:
    """在线程中执行同步业务 Handler，避免阻塞调用方事件循环。"""
    return await asyncio.to_thread(self._execute_once, call)


class SyncSkillExecutorAdapter:
    """仅供现有同步 Graph 使用，不复制任何校验或路由逻辑。"""

    def execute(self, call: SkillCall) -> SkillExecutionResult:
        return self._executor._execute_once(call)
```

异步测试使用 `asyncio.run()`，不新增 pytest-asyncio 依赖。

- [ ] **Step 5：运行 Executor 测试**

Run: `pytest tests/unit/test_skill_executor.py -q`

Expected: PASS，且 FakeHandler 前置失败调用次数均为 0。

- [ ] **Step 6：提交 Executor 任务**

```bash
git add src/skill_runtime/executor.py tests/unit/test_skill_executor.py
git commit -m "feat: add validated skill executor"
```

## Task 4：原子播前能力与四个 Handler

**Files:**
- Create: `src/skill_runtime/pre_live_handlers.py`
- Modify: `src/core/pre_live_business_flow.py`
- Modify: `tests/unit/test_pre_live_business_flow_idempotency.py`
- Test: `tests/unit/test_pre_live_skill_handlers.py`

- [ ] **Step 1：写原子手卡与显式幂等测试**

测试要求：单商品方法只生成一张卡和一条该商品审计；批量旧方法仍生成前三张；调用方提供的 setup idempotency_key 被写入审计并用于重放，不再静默替换为 trace 派生值。

- [ ] **Step 2：确认服务测试红灯**

Run:

```bash
pytest tests/unit/test_pre_live_skill_handlers.py tests/unit/test_pre_live_business_flow_idempotency.py -q
```

Expected: FAIL，缺少原子方法或显式幂等参数。

- [ ] **Step 3：最小重构 PreLiveBusinessFlowService**

新增 `generate_card(room_id, product, trace_id) -> ProductCard`，让原 `generate_cards()` 循环调用该方法。为 `setup_live_session()` 增加仅关键字参数 `idempotency_key: str | None = None`；None 时保留旧的 `f"{trace_id}:setup_live_session"` 兼容值，Runtime 必须显式传值。

- [ ] **Step 4：实现四个 Handler**

Handler 从 arguments 恢复 CatalogProduct 或 LivePlanDraft，调用业务服务并返回 JSON 安全输出：

```text
query_products        -> {"products": [...]}
generate_live_plan    -> {"plan": {...}}
generate_product_card -> {"card": {...}}
setup_live_session    -> {"allowed": true, "setup_status": "prepared"}
```

Handler 不查询未声明输入、不检查路由、不重试，也不重新实现工具审计。

- [ ] **Step 5：运行 Handler 与业务回归测试**

Run:

```bash
pytest tests/unit/test_pre_live_skill_handlers.py tests/unit/test_pre_live_business_flow_idempotency.py tests/integration/test_pre_live_business_flow.py -q
```

Expected: PASS。若 PostgreSQL 集成环境不可用，只记录环境阻断，单元测试必须全绿。

- [ ] **Step 6：提交 Handler 任务**

```bash
git add src/core/pre_live_business_flow.py src/skill_runtime/pre_live_handlers.py tests/unit/test_pre_live_skill_handlers.py tests/unit/test_pre_live_business_flow_idempotency.py
git commit -m "feat: add explicit pre-live skill handlers"
```

## Task 5：不可变路由与播前 Facade

**Files:**
- Create: `src/skill_runtime/routing.py`
- Create: `src/skill_runtime/pre_live_facade.py`
- Modify: `src/config/settings.py`
- Modify: `.env.example`
- Test: `tests/unit/test_skill_runtime_routing.py`

- [ ] **Step 1：写配置、批次隔离和无 fallback 测试**

测试固定默认路由都是 LEGACY、非法字符串使 Settings 校验失败、generation 与 setup 可独立配置、Facade 在 Runtime 失败时不调用 legacy、构造后修改环境变量不影响已有 RoutePolicy。

- [ ] **Step 2：确认测试红灯**

Run: `pytest tests/unit/test_skill_runtime_routing.py -q`

Expected: FAIL，缺少路由配置和 Facade。

- [ ] **Step 3：增加启动配置**

Settings 增加：

```python
skill_route_prelive_generation: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
    default="LEGACY",
    validation_alias="SKILL_ROUTE_PRELIVE_GENERATION",
)
skill_route_prelive_setup: Literal["LEGACY", "SKILL_RUNTIME"] = Field(
    default="LEGACY",
    validation_alias="SKILL_ROUTE_PRELIVE_SETUP",
)
```

`.env.example` 同步两个默认值。

- [ ] **Step 4：实现 RoutePolicy 与 Facade**

RoutePolicy 是冻结模型。Facade 实现现有业务服务接口，并按方法选择固定批次；Runtime 的 `generate_cards()` 从计划前三项映射商品快照，逐次调用单商品 Skill，缺少对应商品时明确失败。

旧 `confirmed_setup=True` 由 Facade 构造 `TRUSTED_COMPAT` ApprovalContext；False 不构造审批证据。Facade 不捕获 Runtime 错误后调用 legacy。

- [ ] **Step 5：运行配置和路由测试**

Run: `pytest tests/unit/test_skill_runtime_routing.py tests/unit/test_settings.py -q`

Expected: PASS。

- [ ] **Step 6：提交路由任务**

```bash
git add .env.example src/config/settings.py src/skill_runtime/routing.py src/skill_runtime/pre_live_facade.py tests/unit/test_skill_runtime_routing.py
git commit -m "feat: route pre-live batches through skill runtime"
```

## Task 6：可信人审证据接入 LangGraph

**Files:**
- Modify: `src/core/pre_live_graph.py`
- Modify: `tests/unit/test_pre_live_graph_interrupt.py`
- Modify: `tests/integration/test_pre_live_graph_interrupt_flow.py`
- Test: `tests/integration/test_pre_live_graph_skill_runtime_flow.py`

- [ ] **Step 1：写 HUMAN_INTERRUPT 证据测试**

批准恢复后断言传给 Facade 的 ApprovalContext 含 APPROVED、operator_id 和 approval_resume_audit_id；拒绝恢复时断言 setup Handler 从未执行。现有 checkpoint 恢复测试继续使用相同 thread_id。

- [ ] **Step 2：确认测试红灯**

Run:

```bash
pytest tests/unit/test_pre_live_graph_interrupt.py tests/integration/test_pre_live_graph_skill_runtime_flow.py -q
```

Expected: FAIL，Graph 尚未传递可信审批上下文。

- [ ] **Step 3：扩展兼容 Protocol 和调用**

为 `setup_live_session()` 增加可选关键字 `approval_context`。普通旧服务可以忽略该值；RoutedPreLiveBusinessService 消费它。`_setup_live_session_with_human_approval()` 仅在 response 通过现有 validator 且批准审计写入成功后构造 HUMAN_INTERRUPT ApprovalContext。

拒绝分支保持原状态输出并且不调用 setup service。

- [ ] **Step 4：运行 Graph、checkpoint 和 interrupt 回归**

Run:

```bash
pytest tests/unit/test_pre_live_graph.py tests/unit/test_pre_live_graph_checkpoint.py tests/unit/test_pre_live_graph_interrupt.py tests/integration/test_pre_live_graph_checkpoint_flow.py tests/integration/test_pre_live_graph_interrupt_flow.py tests/integration/test_pre_live_graph_skill_runtime_flow.py -q
```

Expected: PASS；没有可用 PostgreSQL 时单独报告集成环境阻断。

- [ ] **Step 5：提交 Graph 人审任务**

```bash
git add src/core/pre_live_graph.py tests/unit/test_pre_live_graph_interrupt.py tests/integration/test_pre_live_graph_interrupt_flow.py tests/integration/test_pre_live_graph_skill_runtime_flow.py
git commit -m "feat: pass trusted approval evidence to skills"
```

## Task 7：AgentToolExecutor 兼容收敛

**Files:**
- Create: `src/skill_runtime/compatibility.py`
- Modify: `src/core/agent_tool_executor.py`
- Modify: `tests/unit/test_agent_tool_executor.py`
- Test: `tests/unit/test_agent_tool_executor_skill_compat.py`

- [ ] **Step 1：写旧参数规范化和单一 dispatch 测试**

覆盖：旧 room_id 参数被移到 context；products 被转换为快照；product_id 被解析为单商品快照；setup 的 plan_item_ids 和 idempotency_key 被转换为计划快照与上下文字段；四个工具只调用 SkillExecutor 一次；兼容补全被标记；Runtime 错误映射为 AgentObservation。

- [ ] **Step 2：确认测试红灯**

Run:

```bash
pytest tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_agent_tool_executor.py -q
```

Expected: FAIL，旧执行器仍维护独立分支。

- [ ] **Step 3：实现 CompatibilityArgumentNormalizer**

规范化器可以使用旧 service 补全缺失快照，但输出必须包含 `compatibility_enriched=True` 证据。它不能被 RoutedPreLiveBusinessService 或未来 PlanEngine 使用。

setup 缺少可信审批时保持 pending；不能沿用旧代码中强制 `confirmed_setup=True` 的行为。

- [ ] **Step 4：重构 AgentToolExecutor**

保留现有构造和 `execute()` 同步签名。四个核心工具交给规范化器和 SyncSkillExecutorAdapter；其余旧工具暂时保留现有兼容 dispatch。删除四个核心工具原分支，避免同一 skill_id 双实现。

- [ ] **Step 5：运行 Agent 与 Hook 回归**

Run:

```bash
pytest tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_agent_tool_executor.py tests/unit/test_agent_lifecycle_hooks.py tests/unit/test_on_live_harness_agent_graph.py -q
```

Expected: PASS。

- [ ] **Step 6：提交兼容入口任务**

```bash
git add src/skill_runtime/compatibility.py src/core/agent_tool_executor.py tests/unit/test_agent_tool_executor.py tests/unit/test_agent_tool_executor_skill_compat.py
git commit -m "refactor: unify core agent tools on skill runtime"
```

## Task 8：隔离行为比较器与 Phase 11A Demo

**Files:**
- Test: `tests/unit/test_skill_runtime_equivalence.py`
- Create: `scripts/run_phase11a_skill_runtime_demo.py`
- Modify: `scripts/run_all.py`

- [ ] **Step 1：写隔离等价测试**

测试内创建两套独立 FakeRepository、PreLiveBusinessFlowService 和 InMemoryAuditStore。相同快照分别经过 legacy 与 Runtime，比较：商品列表、计划 item 顺序、三张手卡完整内容、审计 tool_name/action/operator 语义；明确断言两个 Store 不是同一对象且事件互不污染。

- [ ] **Step 2：确认等价测试红灯**

Run: `pytest tests/unit/test_skill_runtime_equivalence.py -q`

Expected: FAIL，隔离装配或规范化结果尚未完整接通。

- [ ] **Step 3：完成测试专用装配，不增加生产 SHADOW 路由**

比较器只放在测试模块或 test helper 中。搜索生产代码必须没有 `SHADOW_COMPARE`。

- [ ] **Step 4：新增无外部依赖 Demo**

Demo 使用固定商品快照和内存审计，依次输出：

```text
scenario=all_legacy
scenario=generation_runtime_setup_legacy
scenario=all_runtime
scenario=setup_rollback_to_legacy
```

每个场景输出 generation_route、setup_route、product_count、plan_item_count、card_count、setup_status、audit_count；不得输出密钥或完整环境配置。

- [ ] **Step 5：运行等价测试和 Demo**

Run:

```bash
pytest tests/unit/test_skill_runtime_equivalence.py -q
python scripts/run_phase11a_skill_runtime_demo.py
```

Expected: 测试 PASS；四个 scenario 都完成，Runtime 失败不会出现自动 legacy fallback。

- [ ] **Step 6：提交比较器和 Demo**

```bash
git add tests/unit/test_skill_runtime_equivalence.py scripts/run_phase11a_skill_runtime_demo.py scripts/run_all.py
git commit -m "test: add phase 11a migration evidence"
```

## Task 9：全量验收与阶段留迹

**Files:**
- Create: `docs/superpowers/reports/phase-11a-skill-runtime-acceptance.md`
- Modify: `docs/project_guidance/agent_runtime_evolution_roadmap.md`
- Modify: `docs/project_guidance/phase_execution_log.md`
- Modify: `docs/worklog/task_plan.md`
- Modify: `docs/worklog/findings.md`
- Modify: `docs/worklog/progress.md`

- [ ] **Step 1：运行 Runtime 专项测试**

```bash
pytest tests/unit/test_skill_runtime_models.py tests/unit/test_skill_catalog.py tests/unit/test_skill_executor.py tests/unit/test_pre_live_skill_handlers.py tests/unit/test_skill_runtime_routing.py tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_skill_runtime_equivalence.py -q
```

Expected: 全部 PASS。

- [ ] **Step 2：运行相关回归测试**

```bash
pytest tests/unit/test_tool_registry.py tests/unit/test_tool_mask_policy.py tests/unit/test_security_hooks.py tests/unit/test_agent_tool_executor.py tests/unit/test_pre_live_graph.py tests/unit/test_pre_live_graph_checkpoint.py tests/unit/test_pre_live_graph_interrupt.py tests/unit/test_on_live_harness_agent_graph.py -q
```

Expected: 全部 PASS。

- [ ] **Step 3：运行全量默认测试**

Run: `pytest -q`

Expected: 默认排除 external 的测试全部 PASS。若 PostgreSQL、Redis 或 Kafka 环境导致集成测试失败，Acceptance 必须逐项记录命令、失败原因和未验证风险，不能写“全量通过”。

- [ ] **Step 4：运行 Demo 与静态检查**

```bash
python scripts/run_phase11a_skill_runtime_demo.py
python scripts/check_doc_encoding.py
git diff --check
```

Expected: Demo 四场景完成；`git diff --check` 返回 0。编码扫描需将仓库既有命中与本次新增命中分开，本次文件命中必须为 0。

- [ ] **Step 5：执行严格 UTF-8 专项检查**

对本阶段新增/修改文件验证：严格 UTF-8 解码、字节往返一致、无 BOM、无 U+FFFD、无混合换行、无尾随空白。任何失败先修复再写 Acceptance。

- [ ] **Step 6：生成 Acceptance 并同步事实源**

Acceptance 必须记录：实际交付、测试命令与计数、两批路由证据、审批/幂等证据、与 Design 的偏差、历史编码问题和 Phase 11B 进入条件。只有全部双门禁通过，路线图才把 Phase 11A 标记完成。

- [ ] **Step 7：最终范围审查**

Run:

```bash
git status --short
git diff --stat
rg -n "SHADOW_COMPARE|hot.reload|PlanEngine|LiveOpsAgent" src/skill_runtime src/core/agent_tool_executor.py
```

Expected: 没有生产 `SHADOW_COMPARE`、动态热配置、PlanEngine 或多 Agent 实现；无关未跟踪文件未被暂存。

- [ ] **Step 8：提交验收留迹**

```bash
git add docs/superpowers/reports/phase-11a-skill-runtime-acceptance.md docs/project_guidance/agent_runtime_evolution_roadmap.md docs/project_guidance/phase_execution_log.md docs/worklog/task_plan.md docs/worklog/findings.md docs/worklog/progress.md
git commit -m "docs: record phase 11a skill runtime acceptance"
```

## 完成条件

Phase 11A 只有同时满足以下条件才算完成：

- SkillManifest 是 13 个工具元数据唯一事实源。
- 四个核心 Skill 使用显式快照并通过统一 Executor。
- ToolRegistry 和 AgentToolExecutor 兼容测试通过且无四工具双 dispatch。
- 第一批隔离行为等价，第二批审批与幂等零违规。
- 两个批次独立路由、默认 legacy、无运行时影子执行和隐式 fallback。
- 相关回归、默认全量测试、Demo、diff 和本次编码检查有可复核证据。
- Acceptance、路线图、决策日志和 worklog 状态一致。

完成后停止继续编码，先由用户审核 Acceptance，再按 Just-in-Time 原则讨论 Phase 11B Design。
