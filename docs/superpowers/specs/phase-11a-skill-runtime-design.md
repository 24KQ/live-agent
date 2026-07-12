# Phase 11A 受控 Skill Runtime Design

更新日期：2026-07-12

文档状态：已审核并冻结；Phase 11A 技术验收已完成，Acceptance 待用户审核

关联决策：D-004 至 D-009、D-035 至 D-049

## 1. 设计目标

Phase 11A 将现有 ToolRegistry 白名单升级为受控 Skill Runtime 第一版，使能力元数据只有一个事实源，并让四个播前核心能力进入统一、可版本化、可回滚的执行入口。

本阶段完成后必须证明：

- 13 个现有工具的名称、版本、Schema、生命周期、风险、门禁和幂等要求只在 SkillManifest 中维护。
- ToolRegistry 仍服务现有调用方，但只由 Manifest 生成只读兼容投影。
- `query_products`、`generate_live_plan`、`generate_product_card`、`setup_live_session` 通过同一 SkillExecutor 执行。
- 四个核心 Skill 使用显式不可变输入，不依赖 Handler 内部重新查询或重建上游结果。
- 每次调用钉住精确 Skill 版本、执行路由、幂等键和可信审批证据。
- 两个迁移批次可以独立切换和回滚，单次调用不会跨执行器自动 fallback。
- 新旧行为等价证据在隔离测试环境生成，高风险写操作永远不参与双执行。

## 2. 非目标

以下内容不属于 Phase 11A：

- 外部 Skill 安装、目录扫描、热加载、插件市场或 PostgreSQL 动态配置。
- 多活动版本、流量百分比 A/B 或跨版本长期并行。
- 真实淘宝生产 API、跨进程 Worker、消息队列执行或分布式沙箱。
- PlanEngine、DAG 调度、增量 Replan、lease、fencing token 和 PlanStore。
- Phase 11B 的完整平台 Adapter、统一超时策略和剩余 9 个 Handler 迁移。
- Specialist Agent、Agent handoff、ReviewAgent 或其他多 Agent 拓扑。
- 将现有播前 Graph 全面改成异步节点或把所有调用迁移到 `ainvoke()`。

## 3. 评审发现与修正

原 Design 假设旧 ToolRegistry Schema、AgentToolExecutor dispatch 和播前 Graph 数据流一致。代码评审确认该假设不成立：

- `generate_product_card` 声明单商品输入，旧执行器却忽略 `product_id` 并生成三张手卡。
- `setup_live_session` 声明 `plan_item_ids` 和 `idempotency_key`，旧执行器与业务服务却忽略调用方幂等键。
- 查询、排品和手卡服务都会写正式审计，不适合直接挂到运行时影子路由双算。
- 人工批准来自 LangGraph `interrupt()` 的受控恢复，不应混入 LLM 可控 arguments。
- `jsonschema` 当前是可选导入，环境缺库时会跳过校验。

因此本设计不再盲目复制旧契约，而是把四个核心 Skill 修正为显式输入；旧行为仅在兼容边界适配。D-035 相应标记为 `CONDITIONAL`，D-038 被测试专用比较器取代。

## 4. 总体架构

```text
Python SkillCatalog
  -> 启动时校验 13 个 SkillManifest 与 JSON Schema
  -> 生成 ToolRegistry 只读兼容投影
  -> 关联 4 个受控 SkillHandler

调用方
  -> RoutedPreLiveBusinessService / AgentToolExecutorCompatibilityAdapter
  -> 固定 SkillCall + SkillExecutionContext + RoutePolicy
  -> LEGACY 或 SKILL_RUNTIME
  -> SkillExecutor
       -> 版本 / 生命周期 / Schema / 门禁 / 幂等校验
       -> SkillHandler
       -> PreLiveBusinessFlowService
  -> SkillExecutionResult
  -> 领域对象或 AgentObservation

测试专用行为比较器
  -> 相同不可变输入
  -> 两套隔离服务栈与内存 AuditStore
  -> 比较规范化业务结果和审计语义
```

依赖方向固定为：Manifest 不依赖 ToolRegistry；ToolRegistry 只消费 Catalog 投影；Router 不修改 Catalog；Handler 不决定路由；业务服务不读取路由配置。

## 5. 公共模型与契约

### 5.1 SkillManifest

SkillManifest 使用冻结 Pydantic 模型，字段固定为：

- `skill_id`：稳定能力标识，与标准工具名一致。
- `version`：精确版本字符串；13 个 Skill 首版均为 `1.0.0`。
- `description`：能力说明。
- `lifecycle`：允许生命周期集合。
- `risk_level`：风险等级。
- `parameter_schema`：Draft 2020-12 JSON Schema。
- `gate_decision`：自动、软门禁或硬门禁。
- `requires_idempotency_key`：是否强制幂等键。
- `compatibility_note`：仅四个受控 Schema 修正需要填写，说明与冻结旧投影的差异。

Catalog 显式注册 13 个 Manifest，不扫描任意目录，不执行外部代码。`skill_id` 唯一，同一 ID 只能有一个活动版本。非法 Schema、重复 ID 或空版本导致 Catalog 构建失败。

### 5.2 SkillExecutionContext

可信执行上下文使用冻结 Pydantic 模型，至少包含：

- `room_id`、`trace_id`。
- `lifecycle`。
- `execution_route`：`LEGACY` 或 `SKILL_RUNTIME`。
- `idempotency_key`：需要幂等的 Skill 必填。
- `approval`：可选 ApprovalContext。

这些字段由受信任的 Graph、Facade 或兼容适配器构造，不进入 LLM 生成的业务 arguments。调用开始后上下文不可修改。

### 5.3 ApprovalContext

ApprovalContext 包含：

- `source`：`HUMAN_INTERRUPT` 或 `TRUSTED_COMPAT`。
- `decision`：`APPROVED` 或 `REJECTED`。
- `operator_id`。
- `approval_audit_id`。

`HUMAN_INTERRUPT` 必须同时提供 operator_id 与 approval_audit_id。`TRUSTED_COMPAT` 只能由内部 Facade 根据旧 `confirmed_setup` 构造，并进入审计或执行证据；外部 arguments 无法指定来源。

### 5.4 四个显式业务输入

业务 arguments 只包含可持久化快照：

| Skill | arguments | 输出 |
| --- | --- | --- |
| `query_products` | 空对象 | `products` 商品快照列表 |
| `generate_live_plan` | `products` 商品快照列表 | `plan` 计划快照 |
| `generate_product_card` | `product` 单商品快照 | `card` 单手卡快照 |
| `setup_live_session` | `plan` 计划快照 | `allowed`、`setup_status` |

`room_id`、`trace_id`、幂等键和审批证据只来自 SkillExecutionContext。所有 Schema 默认拒绝未声明的额外业务字段。

### 5.5 SkillCall 与 SkillExecutionResult

SkillCall 包含 `skill_id`、`skill_version`、`arguments` 和 `context`，并使用冻结 Pydantic 模型。

SkillExecutionResult 包含：

- `skill_id`、`skill_version`。
- `status`：`success`、`pending` 或 `error`。
- `output`：JSON 安全 dict。
- `summary`：不含敏感数据的摘要。
- `audit_id`：可空。
- `error_code`：成功时为空，失败时使用稳定枚举。

Phase 11A 稳定错误码为：`SKILL_NOT_FOUND`、`VERSION_MISMATCH`、`LIFECYCLE_MISMATCH`、`INVALID_ARGUMENTS`、`IDEMPOTENCY_REQUIRED`、`APPROVAL_REQUIRED`、`APPROVAL_REJECTED`、`HANDLER_NOT_FOUND`、`HANDLER_FAILED`。完整 FailurePolicy 仍属于后续阶段。

## 6. Catalog 与 ToolRegistry 投影

SkillCatalog 负责：

- 启动时使用 `jsonschema` Draft 2020-12 Validator 检查全部 Schema。
- 按 skill_id 查询活动 Manifest。
- 精确校验调用版本。
- 关联四个核心 Handler。
- 生成 ToolMetadata 只读投影。

9 个未迁移工具的七个 ToolMetadata 字段必须与冻结快照逐字段一致。4 个核心 Skill 使用本设计定义的新 parameter_schema，其他六个字段仍必须一致，并通过 `compatibility_note` 与白名单测试证明差异是有意修正。

现有 `ToolRegistry.get()`、`tool_names()` 和 `is_available()` 保持行为兼容；未知工具继续 fail-closed。ToolRegistry 不提供运行时注册或修改能力，并标记为 deprecated，保留至 Phase 12 验收。

## 7. Executor 与 Handler

### 7.1 SkillExecutor

标准接口是异步单次尝试：

```text
async execute(SkillCall) -> SkillExecutionResult
```

校验顺序固定为：

```text
查询 Manifest
-> 精确版本校验
-> 生命周期校验
-> JSON Schema 校验
-> 幂等要求校验
-> 风险与可信审批校验
-> Handler 存在性校验
-> 执行一次 Handler
-> 生成结构化结果
```

任何前置失败都不得调用 Handler。Executor、Handler 和业务客户端都不做隐藏重试。同步 Handler 通过同一个内部单次执行核心运行；异步接口使用线程桥接阻塞业务函数，避免阻塞事件循环。

`SyncSkillExecutorAdapter` 只服务现有同步 Graph 和兼容入口，复用同一单次执行核心，不复制校验或 dispatch 逻辑，不作为新调用方公共 API。

### 7.2 四个 Handler

- QueryProductsHandler 调用现有货盘查询与审计逻辑。
- GenerateLivePlanHandler 使用调用中的商品快照，不重新查询货盘。
- GenerateProductCardHandler 一次只处理一个商品快照。
- SetupLiveSessionHandler 使用调用中的计划快照、上下文幂等键和可信审批证据。

PreLiveBusinessFlowService 增加单商品手卡方法和显式幂等键参数；原批量手卡与旧建播签名保留兼容外观，并委托新原子方法。业务服务继续拥有实际审计写入与业务规则，Handler 不复制审计逻辑。

## 8. 路由、Facade 与旧入口

### 8.1 RoutePolicy

Settings 增加两个启动配置：

- `SKILL_ROUTE_PRELIVE_GENERATION`。
- `SKILL_ROUTE_PRELIVE_SETUP`。

只接受 `LEGACY` 或 `SKILL_RUNTIME`，默认均为 `LEGACY`。装配时生成不可变 RoutePolicy；配置变更通过重启或重建服务实例生效，不提供进程内更新 API。

### 8.2 RoutedPreLiveBusinessService

Facade 实现现有 PreLiveBusinessServiceProtocol，保持播前 Graph 拓扑、同步节点、checkpoint 和 interrupt 不变：

- `query_products`、`generate_plan` 和 `generate_cards` 使用 generation 批次路由。
- Runtime 路径的 `generate_cards` 对计划前三个商品逐个调用 `generate_product_card`，再组装原列表返回值。
- `setup_live_session` 使用 setup 批次路由。
- 人审 Graph 把已校验 ApprovalContext 传给 Facade；旧 confirmed_setup 由 Facade 映射为 `TRUSTED_COMPAT`。
- 每个方法开始时读取一次不可变 RoutePolicy，执行中不切换路径。

新 Runtime 失败时 Facade 不调用 legacy fallback。配置回滚只影响重启或重新装配后的新调用，第二批回滚不影响已验收的第一批。

### 8.3 AgentToolExecutor 兼容适配

AgentToolExecutor 保留同步 API。四个核心工具的旧参数先由兼容适配层规范化：

- 缺少商品或计划快照时，可以使用现有服务补全，但必须标记为 compatibility enrichment。
- 规范化后的调用进入统一 SkillExecutor，不保留四个独立 dispatch 分支。
- Runtime 结果映射为现有 AgentObservation。
- 审批不足返回 pending；版本、Schema 和生命周期错误返回 error。

该补全逻辑只为旧调用兼容，未来 PlanEngine 必须直接提供显式快照，禁止依赖 compatibility enrichment。

## 9. 行为比较与审计隔离

正式 RoutePolicy 不包含 `SHADOW_COMPARE`。测试专用比较器：

1. 构造同一组冻结商品和计划快照。
2. 创建 legacy 与 Runtime 两套独立服务对象。
3. 为两套服务注入独立内存 AuditStore。
4. 分别执行查询、排品和前三张手卡。
5. 规范化领域输出，忽略随机 audit_id、时间和非业务顺序噪声。
6. 比较商品、计划、手卡、状态和审计事件语义。

`setup_live_session` 不进入双算比较，只做审批、幂等和单路执行专项测试。测试比较器不得被生产模块导入。

## 10. 两批迁移流程

### 10.1 元数据前置门禁

1. 冻结 13 个旧 ToolMetadata 测试快照。
2. 建立 13 个首版 Manifest。
3. 验证 9 个严格投影和 4 个受控 Schema 差异。
4. 统一让运行时 ToolRegistry 从 Catalog 投影。
5. 删除硬编码 ToolMetadata 的运行时读取，不提供旧表 fallback。

### 10.2 第一批：读取与生成

```text
默认 LEGACY
-> 隔离测试比较器通过
-> Runtime 专项契约测试通过
-> generation 配置切换为 SKILL_RUNTIME
-> 播前 Graph 查询、排品、三张手卡回归通过
```

第一批未通过时不得开启第二批。

### 10.3 第二批：模拟建播

```text
setup 保持 LEGACY
-> 无审批 / 拒绝 / 批准测试
-> 显式幂等键重放测试
-> checkpoint + interrupt 恢复测试
-> setup 配置切换为 SKILL_RUNTIME
-> 完整播前闭环回归通过
```

写操作始终单路执行，禁止影子执行和隐式 fallback。

## 11. 测试与验收

### 11.1 模型、Catalog 与投影

- 13 个 ID 和版本唯一，非法 Manifest 与 Schema fail-fast。
- 9 个工具严格投影一致，4 个工具只允许 parameter_schema 白名单差异。
- ToolRegistry 查询 API、排序、生命周期和未知工具行为兼容。
- ApprovalContext 的来源与必填证据校验正确。

### 11.2 Executor

- 未知 Skill、错误版本、错误生命周期、非法参数、缺幂等键、缺审批和未知 Handler 均在执行前失败。
- 拒绝审批不执行 Handler，可信批准只执行一次。
- Handler 异常转换为 `HANDLER_FAILED`，不触发隐藏重试或 legacy fallback。
- 异步入口与同步桥接得到相同结果。

### 11.3 第一批

- Query、Plan 和单 Card Handler 使用显式输入。
- Facade 生成前三张手卡且不隐式重建货盘或计划。
- 隔离比较器的新旧规范化结果和审计语义等价。
- generation 路由切换和回滚不影响 setup 路由。

### 11.4 第二批

- 缺审批返回 pending，拒绝不产生建播成功审计。
- HUMAN_INTERRUPT 批准携带 operator 与 approval audit ID。
- TRUSTED_COMPAT 被明确标记，不可由 arguments 构造。
- 相同幂等键重放复用原 audit_id，不产生重复成功副作用。
- setup 路由无法配置为双执行，失败后不自动调用 legacy。

### 11.5 回归与交付

- ToolRegistry、Security Hook、ToolMaskPolicy、AgentToolExecutor、播前 Graph、checkpoint、interrupt 和 Harness 相关测试通过。
- 无外部依赖的 Phase 11A demo 展示全 legacy、第一批 Runtime、完整 Runtime 和批次回滚。
- `git diff --check` 通过；本次文件严格 UTF-8、无 BOM、无 replacement character、无混合换行和尾随空白。
- 全仓编码扫描的历史问题与本次命中分开报告。

全部门禁通过后才能生成 Phase 11A Acceptance 并进入 Phase 11B。

## 12. 兼容与后续阶段

ToolRegistry 查询 API 保留至 Phase 12 验收。`TRUSTED_COMPAT`、AgentToolExecutor 参数补全和同步桥接器同样在 Phase 12 重审，不视为长期公共接口。

Phase 11B 在本设计之上补齐平台 Adapter、统一超时和完整结构化失败映射，并迁移剩余 Handler。Phase 12 才引入 PlanStore 与 DAG。Phase 13-14 继续只保留路线图大纲。

本 Design 已完成用户审核并冻结。后续实施必须遵循独立 Implementation Plan；任何改变输入契约、审批信任边界、双执行限制或回滚语义的修改，都必须先更新决策日志与本 Design。
