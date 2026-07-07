# Phase 1 播前地基层设计

## 1. 背景

LiveAgent 的产品主线是“播前 -> 播中 -> 播后”。从用户体验看，下一步应优先做播前能力；从工程实现看，播前能力不能脱离生命周期、状态模型、Reducer、安全 Hook 和审计。因此 Phase 1 不做抽象空转的底层，也不直接做完整播前智能体，而是用播前场景打通最小可控闭环。

Phase 1 的正式定位是：**播前最小可控闭环，也就是播前地基层**。

## 2. 目标

Phase 1 要让系统具备一个可运行、可测试、可审计的播前基础流程：

```text
查询货盘 -> 生成播前建议 -> 尝试改价 -> hard-gate 等待确认 -> 确认后更新状态 -> 写审计日志
```

这个流程要证明三件事：

- 产品上，LiveAgent 已经开始服务“播前”阶段，而不是只停留在基础设施。
- 工程上，所有高风险动作都经过确定性代码和安全 Hook，不由模型或脚本直接改状态。
- 合规上，建议、确认、拒绝、工具调用和状态变更都能留下审计记录。

## 3. 非目标

Phase 1 暂不实现以下内容：

- 完整 LLM 排品。
- 完整商品手卡生成。
- Kafka 弹幕消费和播中实时响应。
- 售罄应急、切品建议、抢占恢复。
- Web 副屏。
- 播后复盘、信任分和长期记忆回写。
- 真实淘宝生产 API 接入。

这些内容保留到 Phase 2 及以后实现。

## 4. 模块设计

### 4.1 状态模型

文件：`src/state/models.py`

负责定义 Phase 1 的核心领域对象：

- `LifecycleStage`：支持 `PRE_LIVE`、`ON_LIVE`、`POST_LIVE`。
- `Product`：商品 ID、名称、价格、库存、是否上架、转化率、标签。
- `LiveRoomState`：直播间 ID、当前生命周期、商品货盘、当前商品。
- `Action`：工具或 Reducer 可以执行的动作，例如 `SET_PRICE`、`MARK_SOLD_OUT`、`SWITCH_PRODUCT`。
- `DecisionTrace`：建议、确认、拒绝、执行结果和审计关联 ID。

模型使用 Pydantic 校验输入，拒绝价格为负、库存为负、商品 ID 为空、未知动作类型等非法数据。

### 4.2 生命周期

文件：`src/core/lifecycle.py`

负责控制状态阶段切换。Phase 1 重点支持：

- 初始阶段为 `PRE_LIVE`。
- 允许 `PRE_LIVE -> ON_LIVE`。
- 允许 `ON_LIVE -> POST_LIVE`。
- 允许 `POST_LIVE -> PRE_LIVE`，用于下一场直播准备。
- 拒绝跳过阶段或未知阶段。

Phase 1 的播前工具必须只在 `PRE_LIVE` 可用。

### 4.3 工具注册表

文件：`src/config/tool_registry.py`

负责声明每个工具的元数据：

- 工具名。
- 可用生命周期。
- 风险等级。
- 参数 Schema。
- 是否需要幂等键。
- 安全策略：`auto`、`soft-gate`、`hard-gate`、`block`。

Phase 1 至少注册这些播前工具：

- `query_products`：查询货盘，`auto`。
- `suggest_price_change`：生成改价建议，`soft-gate`。
- `set_product_price`：执行改价，`hard-gate`。
- `create_live_plan_draft`：生成播前排品草案，`soft-gate`。

### 4.4 Reducer

文件：`src/state/reducer.py`

负责确定性状态更新。Phase 1 支持：

- `SET_PRICE`：更新商品价格。
- `MARK_SOLD_OUT`：将商品库存置为 0，并标记不可上架。
- `SWITCH_PRODUCT`：切换当前讲解商品。

Reducer 不做外部 IO，不直接读写数据库。它只接收当前 State 和 Action，返回新的 State 或明确错误，便于单元测试。

### 4.5 安全 Hook

文件：`src/core/security_hooks.py`

负责在工具执行前做安全决策：

- `auto`：低风险只读工具可自动执行。
- `soft-gate`：需要提示主播，但不需要强制确认。
- `hard-gate`：必须等待主播确认后才允许执行。
- `block`：任何情况下都拒绝执行。

Phase 1 的关键验收是：改价工具 `set_product_price` 必须进入 `hard-gate`，未确认时不得调用 Reducer。

### 4.6 审计

文件：`src/audit/tool_call_audit.py`

负责记录工具调用和状态变更。Phase 1 审计记录至少包含：

- `trace_id`
- `room_id`
- `tool_name`
- `action_type`
- `risk_level`
- `gate_decision`
- `operator_decision`
- `request_payload`
- `result_payload`
- `created_at`

审计写入 PostgreSQL。写入失败时，业务动作应返回明确错误，不能假装执行成功。

## 5. 数据流

Phase 1 的推荐数据流如下：

```text
CLI 输入或测试用例
  -> Tool Registry 查询工具元数据
  -> Security Hook 判断是否 auto/soft-gate/hard-gate/block
  -> hard-gate 等待主播确认
  -> Reducer 执行确定性状态更新
  -> Audit 写入工具调用和状态变更
  -> 返回最新状态和 trace_id
```

这个数据流刻意不接 LLM。Phase 1 的重点是先证明控制边界成立，后续再把 LLM 放进“建议生成”位置。

## 6. 错误处理

Phase 1 采用 fail-closed 原则：

- 未注册工具：拒绝执行。
- 生命周期不匹配：拒绝执行。
- 参数 Schema 不合法：拒绝执行。
- 缺少 hard-gate 确认：拒绝执行。
- Reducer 遇到不存在的商品：拒绝执行。
- 审计写入失败：返回失败，不报告业务动作成功。

所有失败都要返回可读错误，并尽可能带上 `trace_id`。

## 7. 测试策略

Phase 1 使用 TDD。建议测试分层：

- `tests/unit/test_state_models.py`：模型校验。
- `tests/unit/test_lifecycle.py`：生命周期合法和非法切换。
- `tests/unit/test_tool_registry.py`：工具元数据、生命周期、风险等级。
- `tests/unit/test_reducer.py`：`SET_PRICE`、`MARK_SOLD_OUT`、`SWITCH_PRODUCT`。
- `tests/unit/test_security_hooks.py`：四级安全策略。
- `tests/integration/test_tool_call_audit.py`：审计写入 PostgreSQL。
- `tests/integration/test_pre_live_flow.py`：播前最小闭环。

验收命令：

```powershell
pytest -v
python scripts/check_infra.py
```

## 8. 验收标准

Phase 1 通过的标准：

- 能创建并校验播前商品货盘状态。
- 能拒绝非法生命周期切换。
- 能注册并查询播前工具。
- 能通过安全 Hook 拦截改价操作。
- 未确认 hard-gate 时，状态不变化。
- 确认 hard-gate 后，Reducer 能更新商品价格。
- 审计表能记录建议、确认、工具调用和状态变更。
- 全量测试通过。
- `python scripts/check_infra.py` 在本机环境通过。

## 9. 与后续阶段关系

Phase 1 完成后，Phase 2 可以在这个地基上继续做完整播前能力：

- 样例商品数据初始化。
- 货盘查询工具真实读库。
- 排品方案生成。
- 商品手卡生成。
- CLI 播前演示。

Phase 2 再开始引入更明显的业务体验；Phase 1 则负责把“可控、可审计、可恢复”的基础规则立住。
