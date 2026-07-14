# Phase 11B 统一执行与平台契约 Acceptance

- 状态：用户已审核并接受
- 验收日期：2026-07-14
- Design：[Phase 11B 统一执行与平台契约 Design](../specs/phase-11b-unified-execution-platform-contract-design.md)
- Plan：[Phase 11B Unified Execution and Platform Contract Implementation Plan](../plans/2026-07-12-phase-11b-unified-execution-platform-contract-plan.md)
- 验收代码基线：`778d52b`

## 1. 验收结论

Phase 11B 已取得可复核的技术验收证据：13 个 Skill 均由统一 Handler 工厂装配；三批启动冻结路由、原生 async 单次尝试、绝对 deadline、FailureFact、Attempt 意图先写与终态重放、有状态 Fake Platform 和六场景 Demo 均有自动化测试。相关系统回归与默认全量测试通过。

用户已于 2026-07-14 审核并明确接受本报告，Phase 11B 正式完成。现在可以按 Just-in-Time 原则进入 Phase 12A Design 讨论；本次接受不构成 Phase 12A 代码实施授权，在 Design 经用户审核前，不实现 PlanEngine、自动重试、Replan、真实淘宝 API 或多 Agent。

## 2. 实际交付

1. 建立 `AdapterRequest`、`AdapterSuccess`、`FailureFact`、绝对 `deadline_at` 和 Manifest 单次尝试上限等公共执行契约。
2. 建立商品与价格、直播会话、播中运营三个 async 业务域 Port，以及实例级有状态 `FakeLiveCommercePlatform` 和声明式故障脚本。
3. 建立独立 Operation/Attempt Store；带幂等键的调用先 claim 意图，再调用 Handler，成功、确定失败和副作用未知只闭合一次并可稳定重放。
4. 全部 13 个 Skill 进入 `build_skill_handlers()` 的实例局部装配；确定性能力不伪造外部调用，平台状态能力只经对应 Port。
5. 建立三批启动冻结路由，默认均为 `LEGACY`；Runtime 失败不会在同次调用中 fallback Legacy。
6. `setup_live_session` 与 `handle_sold_out_event` 进入批次二 Runtime，并保持播前 Graph、播中 Harness、checkpoint 与 interrupt 外观。
7. `set_product_price` 升级为单活 `1.1.0`，显式要求 `expected_version`、幂等键和可信审批；AgentToolExecutor 保持无审批能力，因此兼容入口只能返回 `pending`。
8. 增加真实生产 Legacy 建播与 Runtime 的隔离契约比较，以及六场景无外部依赖 Demo 和 `run_all.py phase11b-demo` 入口。

## 3. 13 个 Handler 与三批路由

- 批次一：`query_products`、`generate_live_plan`、`generate_product_card`、`suggest_price_change`、`create_live_plan_draft`、`recommend_backup_product`、`generate_on_live_prompt`、`aggregate_danmaku_questions`、`generate_danmaku_reply`、`on_live_context_collect`。
- 批次二：`setup_live_session`、`handle_sold_out_event`。
- 批次三：`set_product_price@1.1.0`。

三个批次均由进程启动时冻结的 `RoutePolicy` 控制。旧 Phase 11A generation/setup 配置继续作为前两批兼容别名；批次三必须显式启用。任何调用开始后不切换执行器，Runtime 失败、限流或副作用未知均不触发 Legacy fallback。

## 4. Attempt、FailureFact 与 deadline

- 同一 `skill_id + version + room_id + idempotency_key` 只允许一个 Operation；不同意图复用同一键时 fail-closed。
- 首次外部调用前写入 Attempt 意图；成功闭合为 `SUCCEEDED`，确定失败闭合为 `FAILED`，发送后未知闭合为 `SIDE_EFFECT_UNKNOWN`。
- 重复调用优先重放已持久化终态，不能因新的 deadline 已过期而覆盖首次事实。
- Handler 开始前 deadline 到期返回 `TRANSIENT_INFRA/NOT_SENT`；Handler 已开始后超时保守返回 `SIDE_EFFECT_UNKNOWN/UNKNOWN`。
- `FailureFact` 只报告外部失败事实，不包含重试、Replan 或人工动作；恢复决策仍属于未来 Phase 12 FailurePolicy。

## 5. 高风险改价契约

`set_product_price@1.1.0` 的业务 arguments 为 `product_id`、`price` 和 `expected_version`。`idempotency_key`、审批证据、room、trace、deadline 和 route 只存在于可信 `SkillExecutionContext`。

- 旧 `1.0.0` 在 Handler 和 Attempt 前返回 `VERSION_MISMATCH`。
- 缺参数、缺幂等键、缺审批或审批拒绝均在 Port 前终止。
- `expected_version` 过期由 Adapter 返回 `FailureCategory.VERSION_CONFLICT`，不与 Skill 版本错误混淆。
- `price` 只接受非负普通十进制字符串；`Infinity`、`NaN`、负数、指数写法和空值在 Attempt 前返回 `INVALID_ARGUMENTS`。
- 限流保留 `retry_after_seconds`；发送后未知保留已发生的价格变化证据，并禁止同一 Operation 第二次调用 Port。

默认 AgentToolExecutor 的兼容 Port 继续拒绝实际改价。这不是缺失实现：D-064 明确规定 AgentToolExecutor 不新增审批 API；本阶段可信批准路径只通过内部 `SkillCall + ApprovalContext + Fake Runtime` 验证，真实 Graph/Facade 批准入口另行设计。

## 6. 隔离比较与六场景 Demo

成功建播比较器调用真实 `PreLiveBusinessFlowService.setup_live_session` 作为 Legacy 路径，并以独立 Runtime Fake、Attempt Store 和平台审计执行新路径。Legacy 本来没有 Fake/Attempt，测试保留该架构差异，只比较双方共同公开的门禁结果和业务事实，不手工复制 Runtime 状态机。

旧改价路径没有可比较的平台失败语义，因此限流、版本冲突和发送后未知被明确标记为 Runtime-only 契约测试，不伪装成新旧等价。

Demo 固定输出：

1. `setup_success`
2. `sold_out`
3. `rate_limited`
4. `version_conflict`
5. `deadline`
6. `side_effect_unknown`

每个场景重新装配独立 Fake 和 Attempt Store，不连接 PostgreSQL、Kafka、LLM 或真实淘宝 API。直接脚本输出恰好六行 JSON；`run_all.py phase11b-demo` 是人类可读统一入口，会额外输出既有 `[INFO] running...` 包装日志。

## 7. 测试与静态检查证据

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| Runtime 专项 10 文件 | `76 passed in 1.54s` | `0` |
| 相关系统回归 13 文件 | `124 passed in 6.61s` | `0` |
| `pytest -q` | `636 passed, 3 deselected, 9 warnings in 63.48s` | `0` |
| `python scripts/run_phase11b_platform_contract_demo.py` | 六场景按固定顺序完整输出 | `0` |
| `python scripts/run_all.py phase11b-demo` | 统一入口复现六场景 | `0` |
| `git diff --check` | 无空白错误；仅显示工作树既有行尾转换提示 | `0` |
| `python scripts/check_doc_encoding.py` | `4 errors/59 warnings`，未通过 | `1` |

全量测试的 9 条 warning 为既有 FastAPI/Starlette TestClient 与 Kafka Serializer/Deserializer 弃用告警；3 个 deselected 为默认配置排除项，未计入 passed。

### 7.1 Runtime 专项复现命令

```bash
pytest tests/unit/test_phase11b_models.py tests/unit/test_phase11b_attempt_store.py tests/unit/test_phase11b_fake_platform.py tests/unit/test_phase11b_executor.py tests/unit/test_phase11b_handlers_batch1.py tests/unit/test_phase11b_routing.py tests/unit/test_phase11b_handlers_batch2.py tests/unit/test_phase11b_handlers_batch3.py tests/unit/test_phase11b_equivalence.py tests/unit/test_phase11b_demo.py -q
```

### 7.2 系统回归复现命令

```bash
pytest tests/unit/test_skill_executor.py tests/unit/test_pre_live_skill_handlers.py tests/unit/test_skill_runtime_routing.py tests/unit/test_agent_tool_executor.py tests/unit/test_agent_tool_executor_skill_compat.py tests/unit/test_on_live_agent_graph_real.py tests/unit/test_on_live_harness_agent_graph.py tests/integration/test_pre_live_graph_skill_runtime_flow.py tests/integration/test_on_live_flow.py tests/integration/test_danmaku_flow.py tests/integration/test_phase11b_postgres_attempt_store.py tests/integration/test_phase11b_price_flow.py tests/integration/test_agent_evaluation_flow.py -q
```

## 8. Design 与 Plan 偏差

1. Task 5 为 `LiveOperationsPort` 增加只读 `resolve_product_context`，用于根据商品 ID 取得可信快照；该变更记录为 D-063，不新增 Skill、不改变公开 Schema 或副作用边界。
2. Task 8 新增必填 `expected_version`，因此 `set_product_price` 从 `1.0.0` 升级为 `1.1.0`；该契约修正记录为 D-064。
3. 质量审查发现任意字符串价格可让 `Infinity` 写入 Fake，或将 `NaN` 错误闭合为未知副作用；最终 Schema 在 Attempt 前拒绝非普通十进制值。
4. Task 9 初版把手写 Attempt/Adapter 基线命名为 Legacy，无法证明生产新旧路径差异；最终改为真实 Legacy 建播入口，改价失败明确为 Runtime-only。
5. 原冻结 Implementation Plan 把 PostgreSQL Attempt Store 集成测试写为不存在的 `test_phase11b_attempt_store.py`。首次系统回归因此退出码 `4` 且未收集测试；核对仓库后使用真实文件 `test_phase11b_postgres_attempt_store.py` 重跑，结果为 `124 passed`，并在本次留迹中修正 Plan 的复现命令。

以上偏差均已在代码、决策或验收证据中显式记录，没有扩大到 Phase 12 功能。

## 9. 编码扫描与历史问题

全仓编码扫描不能声明通过。4 个 error 均来自 `scripts/check_doc_encoding.py` 自身用于检测 U+FFFD 的示例；59 个 warning 为仓库既有 BOM 或混合换行命中。本阶段目标代码、测试、Demo 和本报告通过严格 UTF-8 解码、字节往返、无 BOM、无 replacement character、无混合换行和无尾随空白检查，目标命中为 0。

历史告警不在 Phase 11B 范围内，本阶段没有为追求全仓扫描绿色而修改无关文件。

## 10. 有效提交

- `3e33ec3`：Runtime 公共契约。
- `5033dcf`：Attempt Store 与 PostgreSQL 持久化。
- `770ba8f`：有状态 Fake Platform 与业务域 Port。
- `8eff0b2`：async Executor、deadline 与 Attempt 传播。
- `f348290`、`c0c11f2`：只读商品上下文契约与批次一 Handler。
- `edb27d6`：三批启动冻结路由。
- `6908f41`：批次二建播、售罄与播中 Harness 接入。
- `5ca05cf`、`76afbdf`、`3feab86`：版本化高风险改价契约与实现。
- `778d52b`：真实 Legacy 对照、六场景 Demo 与统一入口。

## 11. Phase 12A 进入条件

1. 用户已审核并明确接受本 Acceptance；该条件于 2026-07-14 满足。
2. Phase 11B 的 Skill 契约、FailureFact、Attempt Store、Fake Adapter、三批路由和兼容债务保持稳定。
3. 下一步重新读取 Phase 12A 高层大纲、D-009 至 D-034 的 PlanEngine 决策和本报告，按 Just-in-Time 原则讨论并生成独立 Phase 12A Design。
4. 在 Phase 12A Design 审核前，不实现数据库物理 Schema、DAG 校验器、调度进程、PlanStore 查询 API、自动重试或 Replan。

## 12. 用户审核

- [x] 用户已审核技术验收证据。
- [x] 用户已确认接受 Phase 11B Acceptance。
- [x] 用户已授权进入 Phase 12A Design 讨论。

当前审核结论：用户已接受 Phase 11B Acceptance，Phase 11B 正式完成。
