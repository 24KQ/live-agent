# LiveAgent 当前项目状态与 Agent 化路线图

更新日期：2026-07-09

## 当前状态结论

LiveAgent 已完成从播前到播后再到记忆回写的基础业务闭环：

```text
播前排品
-> 建播 hard-gate
-> 播中弹幕 / 售罄 / 告警
-> 播后复盘
-> 记忆回写
-> 下一次播前建议受记忆影响
```

当前系统已经具备产品雏形，但 Agent 能力还没有充分发挥。更准确地说，当前项目是：

```text
规则业务系统 + LangGraph 编排骨架 + 少量 LLM 能力 + 记忆/审计/安全体系
```

下一阶段应从“继续堆业务能力”切换到“LangGraph Agent 化改造”。

## 2026-07-11 更新：Agent 化路线修正

重新对照 `docs/study/taobao_anchor_agent_harness.md` 后，项目的 Agent 化重点不应是裸 ReAct，而应是 Harness 工程：

```text
Execution Loop
Tool Registry
Context Manager
State Store
Lifecycle Hooks
Evaluation Interface
```

Phase 5G-B 已按这个方向新增 LangGraph Harness Agent Loop。它不是普通 `while LLM -> tool -> LLM`，而是把关键控制点拆成 LangGraph 节点和条件边：

```text
load_context
-> pre_reasoning_hook
-> agent_reasoning
-> route_agent_decision
-> pre_tool_call_hook
-> route_tool_policy
-> execute_tool
-> post_tool_call_hook
-> observe_result
-> route_replan
-> write_audit
```

当前更准确的项目定位是：

```text
业务 Workflow 闭环 + LangGraph Harness Agent 播中核心 + 记忆/信任/审计/副屏工程底座
```

后续 Agent 主线应优先补：

1. Harness Agent 审计与 DecisionTrace 接入。
2. 高风险工具的 LangGraph interrupt 人审恢复。
3. WebSocket 副屏展示 Harness Agent 节点状态。

## 已完成能力

| 模块 | 当前状态 |
| --- | --- |
| 播前业务 | 已完成货盘查询、排品、手卡、合规摘要、建播 hard-gate |
| 播中业务 | 已完成售罄事件、备选推荐、弹幕聚合、Kafka 弹幕守护进程 |
| 播后业务 | 已完成归因、复盘、trust 更新、记忆回写 |
| 记忆层 | 已完成 L1/L2/L3、trust_score、衰减、冲突修正、语义检索 |
| LLM 能力 | 已接入 DeepSeek 手卡生成，失败时降级到模板 |
| LangGraph | 已完成播前 graph、PostgreSQL checkpoint、interrupt 人审恢复 |
| Web 副屏 | 已完成 FastAPI + HTML 副屏，主要端点已接真实 PostgreSQL 数据 |
| 审计与安全 | 已完成 ToolRegistry、SecurityHook、ToolCallAuditStore |

## 当前主要缺口

### 1. Agent 决策能力不足

当前多数流程仍是确定性代码串联，LLM 没有真正决定下一步做什么。现有 LangGraph 主要是线性 workflow：

```text
START
-> query_products
-> generate_live_plan
-> generate_product_cards
-> compliance_check
-> setup_live_session
-> END
```

它还没有充分体现：

- `add_conditional_edges`
- LLM planner 节点
- Tool Calling
- Reason -> Act -> Observe -> Replan
- 根据状态动态选择路径

### 2. LLM 集成稳定性不足

当前全量测试曾出现：

```text
test_deepseek_card_differs_from_template
```

失败原因是 LLM 手卡结果与模板一致，可能是 DeepSeek 调用失败后 fallback，也可能是 LLM 输出没有通过 schema 校验。这说明 LLM 目前更像增强能力，不是稳定 Agent 大脑。

### 3. 弹幕语义聚合仍待增强

Phase 4D 已经实现“只持久化聚合结果，不存原始弹幕”的正确成本模型。但聚合分类仍主要依赖关键词，面对 10w+ 直播间的“千人千语”表达时命中率不足。

建议后续采用：

```text
规则/关键词快速聚合
-> 未分类问题进入语义聚合
-> 必要时低频批量 LLM 兜底
```

而不是每条弹幕都调用 LLM。

## 推荐下一阶段：Phase 5A

建议 Phase 5A 定义为：

```text
LangGraph Agent Planner + Tool Calling + Conditional Edges
```

目标是把播前流程从“线性业务流水线”升级为“Agent 决策编排”。

### Phase 5A 目标链路

```text
START
-> collect_context
-> llm_planner
-> conditional_route
   -> retrieve_memory
   -> generate_plan
   -> generate_cards
   -> risk_check
   -> request_human_approval
   -> deterministic_fallback
-> observe_result
-> replan_or_finish
-> END
```

### Phase 5A 关键要求

- LLM planner 只能输出结构化 JSON，必须通过 Pydantic schema 校验。
- LLM 不直接写数据库，只能选择 ToolRegistry 白名单工具。
- 所有 tool call 必须写入审计并关联 trace_id。
- 使用 LangGraph `add_conditional_edges` 体现动态路由。
- 至少实现一轮 `Reason -> Act -> Observe`，后续再扩展多轮 ReAct。
- LLM 失败、schema 失败或超时时，必须 fallback 到现有 Phase 2A/2D 稳定链路。
- hard-gate 仍通过现有 SecurityHook + interrupt 人审，不允许 LLM 绕过。

## 后续优先级建议

1. Phase 5A：LangGraph Agent Planner + Tool Calling + Conditional Edges。
2. Phase 5B：弹幕语义聚合增强，采用 embedding/LLM 低频兜底，不全量 LLM。
3. Phase 5C：播中 ReAct 小循环，让 Agent 基于弹幕、库存、流量观察动态生成主播建议。
4. Phase 5D：LLM 播后复盘总结，输出自然语言报告，但仍保留结构化归因。
5. 部署阶段：守护进程管理、数据清理策略、真实平台 API 适配层。
