# Phase 12A DAG PlanEngine Design

- 状态：用户已审核并接受
- 设计日期：2026-07-14
- 前置阶段：[Phase 11B Acceptance](../reports/phase-11b-unified-execution-platform-contract-acceptance.md)
- 决策依据：D-009 至 D-034、D-065 至 D-072

## 1. 设计目标

Phase 12A 建立确定性的 PlanEngine 基线：它接收已经冻结的播前排品和商品快照，将前三张单商品手卡表示为可查询、可恢复、可并发执行的 DAG。PlanEngine 是控制面组件，不是 Agent；它不负责商品排序、业务推荐、LLM 推理或售罄抢占。

首期必须证明五件事：不可变 PlanVersion、PlanStore 权威执行事实、受控并发与 fencing、失败后的持久化恢复、以及 PlanStore 与 LangGraph checkpoint 的有序一致性。

## 2. 非目标

- 不接管 `query_products`、`generate_live_plan`、`setup_live_session`，不改变既有播前默认路径。
- 不实现售罄事件、协作式冻结、紧急 DAG、依赖闭包失效或增量 Replan；这些属于 Phase 12B。
- 不调用真实 LLM 生成候选计划。保留 Provider 边界，但不为了展示 LLM 而在固定手卡批次中制造伪决策。
- 不新增 HTTP API、运营页面、真实淘宝 API、动态配置、自动 Agent 或多 Agent。

## 3. 首期 DAG 与能力边界

### 3.1 规划输入

`CardBatchPlanningInput` 是 PlanRun 创建时冻结的 JSON 安全快照，包含：

- `room_id` 与 `trace_id`。
- 完整 `LivePlanDraft` 快照。
- 以 `product_id` 索引的完整商品快照。

`PREPARE_CARD_BATCH` 从排品顺序选择最多前三项；输入缺少排品商品快照、排品为空或商品 ID 重复时，创建计划前以 `INVALID_INPUT` 拒绝。它不调用 Skill 或 Adapter。

### 3.2 规范 DAG

首期只有一个版本化、确定性的规范骨架：

```text
PREPARE_CARD_BATCH
        |
        +--> generate_product_card(product-1)
        +--> generate_product_card(product-2)
        +--> generate_product_card(product-3)
        |
COLLECT_CARD_RESULTS
```

实际排品不足三项时，只生成已有商品对应的手卡节点。`COLLECT_CARD_RESULTS` 依赖所有已生成的手卡节点，并输出按原排品顺序排列的手卡快照。准备和汇总是 `CONTROL` 节点；三张手卡是 `SKILL` 节点，精确钉住创建计划时 Catalog 中的 `generate_product_card` 单活版本。

`PlanCapabilityProfile` 只允许上述两个控制节点和 `generate_product_card`。Capability Profile 从 Catalog、FailurePolicy 和受控资源解析器注入风险、deadline、重试预算、门禁与版本；候选计划不得自行声明这些字段。

## 4. 候选计划与节点输入

### 4.1 ProposalProvider

定义只读异步接口 `PlanProposalProvider.propose(input) -> CandidatePlanProposal`。Phase 12A 唯一正式实现是 `CanonicalCardBatchProposalProvider`，它按 3.2 生成固定骨架，并带有稳定 `provider_id` 和 `provider_version`。测试与 Demo 使用版本化 Fixture 验证相同候选格式。

接口保留未来 LLM Provider 的替换点，但 Phase 12A 不提供 LLM Provider、LLM fallback 或模板自动降级。任一候选不通过校验时，PlanRun 不创建；不得以另一条执行路径掩盖规划错误。

### 4.2 受限类型化 DAG

`CandidatePlanProposal` 只能包含节点类型、允许的 Skill、依赖关系和 `InputBinding`。PlanEngine 校验能力白名单、无环依赖、唯一逻辑键、依赖闭包、生命周期与参数 Schema 后，才物化 PlanVersion。

`InputBinding` 仅允许三种来源：

- `PLAN_INPUT`：引用冻结规划输入中的受控路径。
- `NODE_OUTPUT`：引用已声明依赖节点的 JSON 安全输出路径。
- `LITERAL`：在候选 Schema 中验证通过的 JSON 值。

不支持 JSONPath、模板表达式、函数调用或跨版本隐式读取。Worker 在派发前解析绑定，生成不可变 `input_snapshot`，对目标 Skill 参数 Schema 校验后计算 `input_fingerprint`。

## 5. PlanStore 数据模型

PlanStore 与官方 PostgresSaver 使用同一 PostgreSQL 实例但独立连接、表和提交边界。PlanStore 是执行事实权威源；不得读取或修改 PostgresSaver 内部表结构。

| 表 | 职责 |
| --- | --- |
| `plan_runs` | 根计划、房间/追踪身份、当前版本、执行路由、冻结规划输入和聚合状态。 |
| `plan_versions` | 不可变 DAG 快照、版本号、父版本和候选 Provider 证据。 |
| `plan_nodes` | 版本内节点定义、稳定 `logical_key`、节点类型、Skill 钉住版本、绑定、资源键和来源关联。 |
| `plan_node_dependencies` | 版本内有向依赖边，供环校验、READY 查询和未来依赖闭包使用。 |
| `node_runs` | 每次 claim/执行的输入快照、指纹、状态、结果或 FailureFact、Skill Attempt、lease 与 fencing 证据。 |
| `plan_commands` | 人工命令账本、幂等结果、预期版本和预期节点状态。 |

`plan_versions` 使用 `(plan_run_id, version_number)` 唯一约束；`plan_nodes` 的 `node_id` 仅在当前版本有效，`logical_key` 则跨版本稳定。未来 Replan 必须创建新节点，并通过 `reused_from_node_id` 或 `invalidated_from_node_id` 指向旧节点，不能原地覆盖。DAG 快照、输入输出、FailureFact、命令结果和审计扩展信息使用 JSONB；可并发更新、查询和约束的身份、状态、版本、时间和 lease 使用关系列。

每个 `node_runs` 代表一次实际 Worker claim/执行。它保存单调递增的 `attempt_number`、可选 `skill_attempt_id`、`lease_owner`、`lease_until` 与 `claim_version`。重试或接管必须创建新的 NodeRun，不覆盖旧证据。

## 6. 调度、状态与失败处理

### 6.1 Worker 和资源锁

`PlanWorker` 是无状态 Worker 契约。Plan API 或 Graph 只创建/恢复 PlanRun；Worker 用 `FOR UPDATE SKIP LOCKED` claim READY 节点，写入 lease 和 fencing token。初始 lease、心跳和条件终态更新严格遵循 D-031 与 D-032；过期 Worker 使用旧 token 不能提交。

无依赖节点最多并发 4 个。`PlanCapabilityProfile` 中的 `ResourceKeyResolver` 依据可信 `room_id`、商品快照和节点类型计算资源键；本期每张手卡使用 `room:{room_id}:product:{product_id}`。控制节点没有外部资源锁。LLM 和候选输入不能覆盖资源键。

### 6.2 状态机与终态

PlanNode 使用 D-015 的受控状态集：`PENDING`、`READY`、`RUNNING`、`WAITING_APPROVAL`、`WAITING_RECONCILIATION`、`RETRY_WAIT`、`SUCCEEDED`、`FAILED`、`FROZEN`、`INVALIDATED`、`SKIPPED`。PlanRun 聚合为 `ACTIVE`、`FROZEN`、`SUCCEEDED` 或 `FAILED`，不引入“部分成功”终态。

控制节点发生未预期错误时按 `INTERNAL_INVARIANT` fail-closed。Skill/Adapter 只报告 FailureFact；FailurePolicy 按 D-023 至 D-027 决定动作。只读手卡节点对 `TRANSIENT_INFRA` 或 `RATE_LIMITED` 最多三次尝试，使用持久化 `RETRY_WAIT`、抖动、`Retry-After` 与节点 deadline；其他失败不自动重试。

任一节点不可恢复失败后，PlanRun 不再派发新的节点，已经运行的节点仅在完成或 deadline 到期后收敛。PlanRun 最终为 `FAILED`，但已成功手卡、NodeRun 和输入指纹全部保留，供 Phase 12B 的 Replan 复用。Phase 12A 不创建 Replan 版本。

### 6.3 Command Ledger

`CommandService` 通过 `plan_commands` 实现 `APPROVE`、`REJECT`、`RECONCILE` 和 `RESUME` 四类命令。每个命令必须提供唯一 `command_id`、`expected_plan_version` 与 `expected_node_status`；重复命令返回首次结果，版本或状态不匹配时拒绝。

首期手卡 DAG 不会自然进入高风险审批，但四类命令必须通过合成节点和状态机测试。审批 TTL 为 10 分钟，对账 TTL 为 30 分钟；到期按 D-026 fail-closed。命令登记和节点状态推进在同一 PlanStore 事务中完成，之后才允许 Graph resume。

## 7. Checkpoint 与播前 Graph 路由

成功或复用结果必须先提交 PlanStore，Graph 节点才返回；checkpoint 只保存 `plan_run_id`、`plan_version` 和控制位置。PlanStore 领先 checkpoint 时，恢复 Graph 后 Worker/节点入口复用已成功 NodeRun，不重复调用 Skill；checkpoint 领先 PlanStore 时写入 `INTERNAL_INVARIANT`，冻结计划并进入人工对账。

引入启动冻结的 `PlanExecutionRoute`：`LEGACY | PLAN_ENGINE`，配置字段为 `plan_engine_card_execution_route`，默认 `LEGACY`。`PLAN_ENGINE` 启用后，播前 Graph 在已取得冻结排品与商品快照后创建 PlanRun，并通过 Worker 驱动手卡批次；同一调用不得回退 Legacy，也不做生产双执行。现有 `generate_product_cards` 路径继续可用，直至 Phase 12A 验收后另行决定迁移范围。

`PlanQueryService` 提供领域级 PlanRun、PlanVersion、PlanNode、NodeRun 与命令查询，供 Graph、Replay、Evaluation 和测试复用。本期不增加 FastAPI endpoint 或运营页面。

## 8. 测试与验收

- 单元测试：候选 DAG 白名单、环依赖、绑定解析、输入指纹、状态迁移、FailurePolicy、资源键、控制节点、Command Ledger 幂等和过期命令。
- PostgreSQL 集成测试：PlanStore Schema/索引、并发 claim、lease、fencing、NodeRun 历史、重试、PlanStore 领先 checkpoint 的结果复用、checkpoint 领先 PlanStore 的 fail-closed 与幂等对账。
- Graph 路由测试：默认 Legacy、显式 PLAN_ENGINE、启动冻结、禁止同次 fallback、checkpoint 只保留计划引用。
- 无外部依赖 Demo：三张手卡并行、可恢复失败、不可恢复失败、进程重启恢复和重复人工命令。Demo 使用 Fake Adapter、固定 Fixture 和隔离 Store，不连接真实 LLM、淘宝 API、Kafka。
- 验收必须覆盖真实 PostgreSQL 与官方 PostgresSaver；内存 Store 只用于快速单元测试，不能替代并发和一致性证据。

## 9. 后续步骤

用户已于 2026-07-14 审核并接受本 Design。实施依据为 [Phase 12A DAG PlanEngine Implementation Plan](../plans/2026-07-14-phase-12a-dag-plan-engine-plan.md)；在用户确认执行该计划前，不修改 PlanEngine 业务代码。Phase 12B 再设计售罄事件、协作式冻结、紧急 DAG、依赖闭包失效和增量 Replan。
