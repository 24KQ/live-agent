# Phase 12B Preemption and Incremental Replan Design

文档状态：`FROZEN_NOT_AUTHORIZED_FOR_IMPLEMENTATION`

依赖：Phase 12A Acceptance 通过后才允许实施。

## 1. 设计目标

Phase 12B 以“前三张手卡并行生成期间收到可信售罄事件”为唯一主场景，建立：

- 可审计、可去重、可恢复的事件入站边界。
- 商品级局部冻结与房间/平台级全局冻结。
- 高优先级紧急 child PlanRun。
- 带资源版本的售罄 CAS 写和副作用未知对账。
- 基于依赖闭包与输入指纹的不可变增量 Replan。
- 售罄执行事实与播中 Agent 建议的职责隔离。

## 2. 非目标

- 不接真实淘宝 API。
- 不实现通用事件总线或事件溯源架构。
- 不让 LLM 决定 impact scope、授权、冻结、重试或 Replan 资格。
- 不实现 LiveOpsAgent；Phase 12B 只提供它未来需要消费的确定性基线和证据。
- 不新增 HTTP API、运营页面或动态路由配置。
- 不删除 ToolRegistry Facade；本阶段只迁移生产消费者，最终删除属于 Phase 14。

## 3. 事件事实与传输边界

### 3.1 InventoryFactEvent

不可变事件事实固定包含：

```text
event_id: 非空稳定 ID
event_type: SOLD_OUT
room_id: 直播间 ID
product_id: 商品 ID
observed_version: 大于等于 1 的商品资源版本
occurred_at: 带时区 UTC 时间
source: 业务来源标识，不表达信任等级
payload_digest: canonical JSON 的 SHA-256
```

`payload_digest` 由入站边界根据除摘要自身外的规范 JSON 计算。字符串 key 排序，紧凑分隔符，拒绝 NaN、Infinity、非字符串 key 和非 JSON 类型。调用方提供的摘要若不匹配，事件在进入 Inbox 前拒绝。

### 3.2 Kafka Adapter

Kafka 只是传输 Adapter，不是事件权威源。处理顺序固定为：

```text
解析并校验消息
-> 验证 Ingress Trust Profile
-> 在 PostgreSQL Event Inbox 中登记事件与 occurrence
-> 事务提交成功
-> 提交 Kafka offset
```

数据库失败时不得提交 offset。同一事件重复投递时，Inbox 返回已有结果后允许提交 offset。现有一次性 `LiveAgentKafkaConsumer` 不直接承担业务处理，新增 Adapter 使用手动 offset 提交。

### 3.3 冲突投递

- 同一 `event_id`、同一摘要：记录 `DUPLICATE` occurrence，返回首次 Inbox 事实。
- 同一 `event_id`、不同摘要：保留首个事实，记录 `CONFLICT` occurrence，把 Inbox 标记为 `CONFLICT`，冻结受影响计划并转人工。
- 冲突已经可靠落库后允许提交 Kafka offset，避免单条毒消息永久阻塞分区。
- 禁止 last-write-wins，禁止覆盖首次 payload。

## 4. 可信事件授权

事件 payload 不允许包含 `trusted=true`、`approved=true` 或等价权限字段。

`IngressTrustProfile` 在进程启动时冻结，至少钉住 profile ID、transport、topic、业务 source 和启用状态。Adapter 完成来源校验后持久化 `VerifiedIngressProvenance`，包含：

```text
provenance_id
profile_id
transport
topic
source
received_at
payload_digest
```

Skill Runtime 新增不可变 `EventAuthorizationContext`。它只能由读取已验证 Inbox 事实的内部工厂构造，包含 `event_id`、`provenance_id`、`payload_digest` 和 `observed_version`。普通 Pydantic 构造不能伪造来源。

`SkillManifest` 增加受控 `authorization_requirement`：

- `NONE`
- `HUMAN_APPROVAL`
- `TRUSTED_EVENT_OR_HUMAN`

`ApprovalContext` 与 `EventAuthorizationContext` 保持独立；两者同时出现时拒绝执行，避免权限来源歧义。

## 5. Event Store 物理模型

Phase 12B 新增三张关系表。

### 5.1 plan_event_inbox

保存唯一事件事实、摘要、验证 provenance、处理状态、lease 和失败事实。状态集固定为：

```text
RECEIVED
VERIFIED
CONFLICT
PROCESSING
WAITING_HUMAN
APPLIED
FAILED
```

### 5.2 plan_event_occurrences

每次传输投递一行，保存 occurrence ID、event ID、摘要、transport、topic、partition、offset、分类和接收时间。分类为 `ACCEPTED | DUPLICATE | CONFLICT | REJECTED`。不保存未经脱敏的完整 Kafka 原始消息。

### 5.3 plan_event_applications

按 `event_id + root_plan_run_id` 唯一，关联 source PlanVersion、紧急 child PlanRun、applied PlanVersion、ImpactAnalysis 和失败事实。状态集固定为：

```text
PENDING
FREEZING
EMERGENCY_RUNNING
WAITING_RECONCILIATION
REPLAN_READY
APPLIED
FAILED
```

### 5.4 PlanStore 扩展

`plan_runs` 增加：

- `plan_kind`: `CARD_BATCH | EMERGENCY_SOLD_OUT`
- `priority`: 普通计划 0，紧急计划 100
- `root_plan_run_id`
- `parent_plan_run_id`
- `trigger_event_id`

`plan_versions` 增加 `change_reason` 和 `source_event_ids`。历史版本保持不可变；扩展列只在创建新版本时写入。

## 6. ImpactAnalyzer 与冻结

`ImpactAnalyzer` 只读取可信事件、PlanStore 当前版本、Capability Profile 和资源键，不读取 LLM 输出。返回：

```text
scope: PRODUCT | ROOM | PLATFORM
affected_logical_keys
affected_node_ids
resource_keys
reason_codes
analysis_digest
```

首期 SOLD_OUT 正常事件解析为 PRODUCT。来源冲突或无法证明商品边界时提升为 ROOM；平台级故障事实才允许 PLATFORM。

- PRODUCT：冻结受影响依赖闭包内尚未开始的节点，PlanRun 可保持 ACTIVE。
- ROOM/PLATFORM：冻结整个 PlanRun，停止派发新节点。
- 已 RUNNING 节点不做强制取消，在原 deadline 内协作式闭合。
- 受影响在途结果完整写入旧 NodeRun，但标记为 superseded，不进入新版本汇总。
- 未受影响在途结果保持正常成功证据，可被后续版本复用。

## 7. 紧急 child DAG

规范 DAG 固定为：

```text
VALIDATE_SOLD_OUT_EVENT
-> handle_sold_out_event@2.0.0
-> recommend_backup_product@1.0.0
-> generate_on_live_prompt@1.0.0
-> COLLECT_SOLD_OUT_RESPONSE
```

准备与汇总是确定性控制节点，不新增 Skill Attempt。紧急计划继承 root、room、trace 和 trigger event，优先级为 100，并与普通计划共享资源锁和 fencing。

`handle_sold_out_event@2.0.0` 的业务参数固定为：

```json
{
  "product_id": "p001",
  "expected_version": 3
}
```

room、trace、幂等键和事件授权只属于 `SkillExecutionContext`。幂等键固定由 `event_id + root_plan_run_id + skill_id` 派生。该 Skill 只执行一次 `LiveOperationsPort.mark_sold_out()`，输出已确认售罄商品快照、旧版本和新版本，不再内部推荐备选或生成提示。

Catalog 的单活版本切换必须与新 Handler、授权校验和 Executor 回归位于同一个可独立提交的 Task。事件公共模型可以提前建立，但在 `2.0.0` Handler 尚未可执行时不得先把默认 Catalog 从 `1.x` 切到 `2.0.0`，避免中间提交留下 Schema 与执行行为不一致的运行时。

## 8. 失败与对账

- `TRANSIENT_INFRA`、`RATE_LIMITED` 且副作用为 `NOT_SENT`：按 D-024 写操作预算进入持久化重试。
- `VERSION_CONFLICT`：不重试。若事件不可信或平台事实与 observed version 冲突，转人工。
- `SIDE_EFFECT_UNKNOWN`：进入 `WAITING_RECONCILIATION`，禁止再次发送写请求。
- `POLICY_DENIED`、`INVALID_INPUT`、`INTERNAL_INVARIANT`：fail-closed。

严格读后对账通过 `LiveOperationsPort.resolve_product_context()` 获取当前商品快照。只有商品已售罄、版本至少为 `expected_version + 1`，且事实能和原 event/Attempt 闭合时，才把原 NodeRun 确认为成功。商品仍是原版本、版本发生其他变化或证据不完整时保持人工对账；不得创建新的写 Operation。

## 9. 增量 Replan

紧急 child PlanRun 成功后，`ReplanCoordinator` 在 root 级数据库锁内：

1. 读取最新 PlanVersion。
2. claim 当前 root 下所有已验证、未应用事件。
3. 合并 ImpactAnalysis 的依赖闭包。
4. 基于最新可信输入重建候选节点输入。
5. 使用 Phase 12A canonical JSON 算法计算输入指纹。
6. 为每个逻辑节点创建新 node ID。
7. 指纹相同且来源成功的节点写 `SUCCEEDED + reused_from_node_id`，读取旧成功 NodeRun 输出，不复制 NodeRun。
8. 受影响或指纹变化节点重新进入 PENDING/READY。
9. 新 PlanVersion 提交后把 EventApplication 标记 APPLIED。

每个 root 最多创建版本 2 和版本 3。超过预算、重复 `failure_signature + input_fingerprint` 或 Replan 锁内发现版本已变化时，冻结并转人工，不覆盖其他 Worker 创建的版本。

## 10. Harness 与路由

新增启动冻结的 `sold_out_execution_route: LEGACY | PLAN_ENGINE`，Phase 12B 默认 `LEGACY`。显式 PlanEngine 路由启用后：

- EventIngress/PreemptionCoordinator 是可信事件唯一写入口。
- OnLive Harness 不调用 `handle_sold_out_event` 写操作，只读取 EventApplication、紧急计划结果和 EvidenceRef 生成建议。
- Runtime 或 PlanEngine 失败不得在同次事件中 fallback Legacy。
- Phase 14 发布门禁通过后再把默认值切为 `PLAN_ENGINE`。

## 11. SkillPolicyView 迁移

新增只读 `SkillPolicyView`，从 Catalog 快照提供生命周期、风险、Schema、门禁和版本查询。Phase 12B 迁移 Security Hook、ToolMaskPolicy、Planner、Pre/OnLive Flow、AgentToolExecutor 和 SkillExecutor 的生产调用。ToolRegistry 只保留 deprecated 查询兼容，不接受新增消费者。

Phase 12B Acceptance 前，除 `src/config/tool_registry.py` 兼容 Facade 自身外，`src/` 不得再导入 ToolRegistry；历史兼容 API 的删除和只针对该 API 的测试清理仍留在 Phase 14。

## 12. 验收

必须覆盖：

- 事件规范摘要、重复和冲突投递。
- Kafka 先落库后提交 offset 与崩溃恢复。
- 可信/不可信来源和授权伪造拒绝。
- PRODUCT/ROOM/PLATFORM impact scope。
- 在途节点晚到结果和 superseded 证据。
- 紧急计划优先级、资源锁和 fencing。
- CAS 成功、版本冲突、限流和副作用未知严格对账。
- 多事件合并、依赖闭包、指纹复用和两版本预算。
- Harness 不重复执行售罄写。
- Fake、真实 PostgreSQL 和真实 Kafka 集成。
- 无外部 API 的专项 Demo 与默认全量回归。

Acceptance 通过前不得开始 Phase 13。
