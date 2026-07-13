# Phase 11B 统一执行与平台契约 Design

更新日期：2026-07-13

文档状态：Task 1-7 已完成；Task 8 契约纠偏已审核，实施待执行

## 1. 设计目标

Phase 11B 在已验收的 Phase 11A Skill Runtime 之上，统一全部 13 个 Skill 的执行 deadline、外部失败事实、幂等尝试证据和批次路由。平台接入继续采用契约优先的有状态 Fake Adapter，不接入真实淘宝 API。

本阶段的完成标准不是新增 Agent，而是让播前、播中已有 Skill 能以相同的单次尝试、失败传播和审计证据规则执行，为 Phase 12A PlanEngine 提供稳定基础设施。

## 2. 非目标

- 不实现 PlanStore、DAG、自动重试、Replan、人工 Command Ledger 或多 Agent。
- 不接入真实淘宝 API、生产交易、热加载、外部插件或动态路由配置。
- 不把播前、播中、播后三个业务场景机械改造成三个 Agent。
- 不改变 Phase 11A 的 ToolRegistry 兼容投影、审批信任边界、Graph checkpoint 或 interrupt 拓扑；不为改价新增 Graph、Facade 或 AgentToolExecutor 批准入口。

## 3. 总体架构

```text
SkillCall + SkillExecutionContext
  (room_id / trace_id / deadline_at / route / idempotency_key / approval)
        |
        v
SkillExecutor
  (Manifest -> version -> lifecycle -> Schema -> gate -> single attempt)
        |
        v
SkillHandler
  (领域输入输出编排，不隐藏重试)
        |
        +--> 商品与价格 Port
        +--> 直播会话 Port
        +--> 播中运营 Port
                  |
                  v
        有状态 Fake Adapter + 版本化 Fixture + 故障脚本
                  |
                  v
        AdapterSuccess | FailureFact

Attempt Store: intent -> Adapter 调用 -> terminal fact
ToolCallAudit: 兼容结果审计，使用 attempt_id 关联 Attempt Store
```

三个业务域 Port 的职责如下：

- 商品与价格：提供可信商品快照、价格建议所需状态、带资源版本的价格写入。
- 直播会话：提供建播准备与会话状态查询，支持带幂等键的建播结果确认。
- 播中运营：提供售罄状态变更、只读商品上下文解析、备选商品、库存告警和弹幕上下文相关状态。

排品、手卡、主播提示、弹幕聚合和回复等确定性业务计算仍可留在 Handler 或领域服务中；它们不应为了形式统一而调用虚假的外部 API。但凡读取或修改平台状态，必须通过对应 Port。

2026-07-13 Task 5 纠偏：批次一的 `recommend_backup_product` 与 `generate_on_live_prompt` 公开输入只携带房间和商品标识，但复用既有确定性领域函数需要售罄商品与备选商品的完整可信快照。因此 `LiveOperationsPort` 增加只读 `resolve_product_context(request)`。该方法只读取当前平台状态，返回 `sold_out_product` 与可选 `backup_product` 快照；不产生副作用、不新增 Skill、不改变公开参数 Schema、不升级当前 `1.0.0` 版本，也不得被实现为旧 Graph State 直读或 Legacy fallback。

## 4. 公共执行与失败契约

`SkillExecutionContext` 增加可信的绝对 `deadline_at`。业务 arguments、LLM 输出和兼容参数规范化都不得覆盖该字段。Manifest 只声明单次尝试上限，实际 timeout 为该上限与剩余 deadline 的较小值。

`set_product_price@1.1.0` 的业务 arguments 固定为 `product_id: string`、`price: string`、`expected_version: integer` 且 `minimum: 1`，三项全部必填，根对象 `additionalProperties: false`。`idempotency_key`、`approval`、`room_id`、`trace_id`、deadline（模型字段为 `deadline_at`）和 route（模型字段为 `execution_route`）只属于 `SkillExecutionContext`，不得出现在该业务 Schema 中。

版本错误与资源冲突必须在不同层返回。调用已退出 Catalog 的 `set_product_price@1.0.0` 时，Executor 在 Handler 与 Attempt 前返回 `SkillErrorCode.VERSION_MISMATCH`；调用有效 `1.1.0` 后，如果 `expected_version` 已落后于商品当前版本，`ProductPricingPort` Adapter 返回 `FailureFact`，其 `category=FailureCategory.VERSION_CONFLICT`。Schema 缺少 `expected_version` 属于 `INVALID_ARGUMENTS`，不得伪装成资源冲突。

Adapter 使用原生 async 单次尝试接口，接收包含 `operation_id`、`attempt_id`、资源版本、幂等键、deadline 和已验证业务快照的 `AdapterRequest`。Adapter 只能返回：

- `AdapterSuccess`：包含 JSON 安全业务输出和可确认副作用事实。
- `FailureFact`：包含 D-023 固定失败类别、稳定外部码、可选 `retry_after`、`attempt_id` 与副作用确认状态。

`FailureFact` 的类别固定为 `TRANSIENT_INFRA`、`RATE_LIMITED`、`INVALID_INPUT`、`BUSINESS_CONFLICT`、`POLICY_DENIED`、`VERSION_CONFLICT`、`SIDE_EFFECT_UNKNOWN` 和 `INTERNAL_INVARIANT`。它只表达已经发生的事实，不表达 `RETRY`、`REPLAN` 或人工处理动作。

同步 Graph 和 Harness 继续通过受限同步桥接器调用同一 async 执行核心。桥接器不得复制校验、路由、重试或 Handler dispatch。若请求发送前 deadline 到期，记录未发送失败；发送后超时或连接中断且无法确认外部结果，必须记录 `SIDE_EFFECT_UNKNOWN` 并 fail-closed。

## 5. Attempt Store 与审计顺序

Attempt Store 独立于既有 `tool_call_audit`，避免改变已验证的工具审计唯一键和重放比较语义。

一个 Operation 由 `skill_id + version + room_id + idempotency_key` 唯一定位。需要幂等键的 Skill 缺少该键时，仍由 Phase 11A Runtime 在 Handler 前拒绝。首次合法调用的顺序固定为：

1. 校验 Manifest、版本、生命周期、Schema、门禁和 deadline。
2. 在 Attempt Store 写入不可变调用意图和 `attempt_id`。
3. 调用对应 Adapter 的单次尝试。
4. 写入唯一终态：成功、确定失败或副作用未知。
5. 写入或关联既有兼容 ToolCallAudit 结果，结果审计携带 `attempt_id`。

同一 Operation 的重复或并发调用必须返回原有事实：已成功、已确定失败、进行中或副作用未知都不得触发第二次 Adapter 调用。副作用未知的重放不能自动重试，留给未来的对账和 Command Ledger 协议处理。

高风险改价缺少批准时，Runtime 返回 `pending`；明确拒绝时返回 `APPROVAL_REJECTED`。这两类结果以及缺参数、缺幂等键和旧 Skill 版本错误都发生在 Attempt 与 Port 调用前，不得创建外部尝试证据。

## 6. Fake Adapter 与迁移路由

Fake Adapter 的状态仅属于单个装配实例。每个测试或 Demo 都使用版本化 Fixture 显式构造独立状态，包含商品、库存、价格版本、直播会话、播中状态和声明式故障脚本。故障脚本按操作、资源键和调用序号匹配，可确定性给出限流、版本冲突、deadline 或副作用未知。

Fake Adapter 必须实现与生产 Port 相同的 `resolve_product_context` 只读语义：按 `sold_out_product_id` 解析售罄商品，按显式 `backup_product_id` 或当前可用商品顺序解析备选商品。找不到售罄商品返回 `INVALID_INPUT`；只读解析不得修改库存、价格、版本或会话状态。

Phase 11B 新增三项启动冻结的 `LEGACY | SKILL_RUNTIME` 批次路由。路由在装配时解析，调用开始时钉住；变更只能经重启或重新装配生效，回滚只影响新调用，Runtime 失败不允许自动回退 Legacy。

迁移顺序固定为：

| 批次 | Skill |
| --- | --- |
| 1 | `query_products`、`generate_live_plan`、`generate_product_card`、`suggest_price_change`、`create_live_plan_draft`、`recommend_backup_product`、`generate_on_live_prompt`、`aggregate_danmaku_questions`、`generate_danmaku_reply`、`on_live_context_collect` |
| 2 | `setup_live_session`、`handle_sold_out_event` |
| 3 | `set_product_price` |

批次一先验证读取、建议、生成和上下文契约；批次二验证具有可靠幂等与可确认状态的建播和售罄；批次三单独验证高风险改价。写操作的新旧比较只能在隔离 Fake、独立审计和明确故障脚本中进行。

批次三 Runtime 路由只负责把 `set_product_price` 钉住到 Catalog 单活版本 `1.1.0`，并把兼容 arguments 中的 `idempotency_key` 移入 Context 后从业务 arguments 删除。AgentToolExecutor 不新增 `approval` 参数或 `execute_approved` 方法，构造的 Context 中 `approval` 始终为 `None`；因此有效改价调用只能得到 `pending`，不得创建 Attempt、调用 ProductPricingPort 或 fallback Legacy。可信批准路径由内部 `SkillCall`、受控 `ApprovalContext` 和 Fake Platform 集成测试覆盖，未来真实 Graph / Facade 接入不属于 Task 8。批次三回滚仍是启动冻结 `LEGACY` 路由，只影响重新装配后的新调用。

`AgentToolExecutor` 中不受 Catalog 注册的 `switch_product` legacy dispatch 将删除。Reducer 的切品领域原语保留，但它不是本阶段第十四个 Skill；重新暴露该能力必须经过新的 Manifest、审批和 Adapter 决策。

## 7. 版本与兼容边界

Skill 版本代表公开调用契约，而不是内部实现批次。仅改造 Adapter、Attempt Store、审计或路由时保持当前版本；参数 Schema、输出语义、幂等/副作用承诺或门禁语义变化时，受影响 Skill 才升级。D-061 因此继续有效：D-043 所述 13 个首版均为 `1.0.0` 是历史事实；Task 8 新增必填 `expected_version` 后，Catalog 的单活集合变为 12 个 `1.0.0` 与一个 `set_product_price@1.1.0`，不保留可执行的改价 `1.0.0`。旧版本调用受控返回 `VERSION_MISMATCH`，商品 CAS 冲突返回 `VERSION_CONFLICT`。

ToolRegistry 继续是 Catalog 的只读兼容投影，不增加 version 字段，只投影当前单活 `1.1.0` 的改价 Schema。D-035 对 9 个未迁移工具的逐字段冻结是 Phase 11A 历史约束；`set_product_price` 在 D-064 后退出该集合，其余 8 个仍严格保持。Phase 11A 的 `HUMAN_INTERRUPT` 受控工厂、`TRUSTED_COMPAT` 兼容边界、根 Schema fail-closed 和同步外观均保持有效；Phase 11B 不扩大 `TRUSTED_COMPAT` 的使用范围。

## 8. 测试与验收

Phase 11B 的后续实施必须至少证明：

1. 全部 13 个 Handler 可由统一装配得到；所有平台状态能力仅经业务域 Port。
2. Catalog 恰有 12 个 `1.0.0` 和一个 `set_product_price@1.1.0`；改价 Schema 精确要求 `product_id`、`price`、`expected_version >= 1`，拒绝额外字段；ToolRegistry 只投影该 Schema，不新增 version 字段。
3. `set_product_price@1.0.0` 在 Handler 与 Attempt 前返回 `VERSION_MISMATCH`；有效 `1.1.0` 的过期商品版本由 Adapter 返回 `VERSION_CONFLICT`，测试不得混淆两者。
4. 缺业务参数、缺幂等键、缺批准和明确拒绝均不创建 Attempt、不调用 Port；AgentToolExecutor 钉住 `1.1.0`、只搬移改价幂等键、保持批准为 `None`，返回 `pending` 且不 fallback Legacy。
5. 受控批准的内部改价路径覆盖成功、资源冲突、限流、发送后未知和 Operation 重放；SetProductPriceHandler 每次首次 Operation 只调用一次 `ProductPricingPort.set_price`，重放不产生第二次调用。
6. Fake 状态、价格 CAS、建播、售罄、备选商品和故障脚本可确定性重放；所有层均无隐藏重试。
7. 意图写入先于 Adapter 调用；成功、确定失败和副作用未知只闭合一次；重复和并发 Operation 只调用一次 Adapter。
8. 三个批次分别覆盖 Legacy/Runtime、启动冻结、调用钉住、显式回滚和无 fallback。写操作比较只在隔离 Fake 中发生。
9. 播前 Graph、播中 Harness、Replay/Evaluation 相关回归通过；无外部依赖 Demo 覆盖成功建播、售罄、限流、版本冲突、deadline 和副作用未知六种场景。
10. 相关专项、集成和默认全量测试通过；`git diff --check` 通过；中文文档和新增代码满足 UTF-8、无 BOM、无替换字符、无混合换行和无尾随空白。全仓编码扫描的既有脚本样例与历史告警必须单独报告，不得虚报通过。

## 9. 后续步骤

Task 1-7 已完成，Task 8 契约纠偏已由用户审核；下一步只执行修订后的 Task 8，不提前展开 Task 9/10。实施必须遵循独立的 Phase 11B Implementation Plan，并按 TDD 分批执行。Phase 12A 的 PlanEngine、自动重试、FailurePolicy 动作、PlanStore 和 Command Ledger 均不在本阶段提前实现。
