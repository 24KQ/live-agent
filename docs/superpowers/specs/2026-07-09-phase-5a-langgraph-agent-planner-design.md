# Phase 5A: LangGraph Agent Planner + Tool Calling + Conditional Edges

## 设计日期

2026-07-09

## 背景

Phase 0-4E 已完成业务闭环：播前 -> 播中 -> 播后 -> 记忆回写。但 Agent 能力尚未充分发挥：

- LLM 仅用于手卡生成（增强能力），不是决策大脑
- LangGraph graph 是线性 workflow，缺少 add_conditional_edges
- 没有 LLM planner 节点进行动态路由
- 没有 Reason -> Act -> Observe -> Replan 循环

Phase 5A 把播前链路从"线性业务 workflow"升级为"Agent 决策编排"。

## 设计目标

1. LLM Planner 节点生成结构化决策（JSON，Pydantic schema 校验）
2. LangGraph add_conditional_edges 根据 planner 输出动态路由
3. ToolExecutor 在 ToolRegistry 白名单内执行工具，LLM 不能直接写库
4. 至少实现一轮 Reason -> Act -> Observe -> Finish/Replan
5. hard-gate 继续走 interrupt 人审，不允许 LLM 绕过
6. LLM 失败/schema 失败/超时时 fallback 到现有确定性链路

## 架构

```
START
 -> collect_context
 -> llm_planner
 -> route_by_decision (conditional edge)
    -> retrieve_memory       (route=memory_first)
    -> generate_live_plan    (route=direct_plan)
    -> generate_product_cards (route=cards_first)
    -> risk_check            (route=risk_check)
    -> deterministic_fallback (route=fallback)
    -> finish                (route=finish)
 -> observe_result
 -> replan_or_finish (conditional edge)
    -> llm_planner           (replan_count < max_replan=1)
    -> setup_live_session    (finish)
 -> END
```

## 组件

### Agent 决策模型 (src/core/agent_decision.py)

- AgentToolCall: tool_name, arguments, risk_level
- AgentPlannerDecision: trace_id, room_id, goal, route, reason, tool_calls, requires_human_approval, fallback_reason
- AgentObservation: tool_name, status, summary, audit_id
- AgentReplanRoute: StrEnum (memory_first/direct_plan/cards_first/risk_check/fallback/finish)

### LLM Planner (src/skills/agent_planner.py)

- AgentPlanner: 封装 DeepSeek chat completions API
- build_planner_prompt: 输入货盘摘要、记忆摘要、trust_score、工具白名单
- plan: 调用 LLM -> 解析 JSON -> Pydantic 校验 -> 返回 AgentPlannerDecision
- 失败/超时/schema 校验失败时返回 fallback decision

### Tool Executor (src/core/agent_tool_executor.py)

- AgentToolExecutor: 在 ToolRegistry 白名单内执行工具
- 执行前检查 lifecycle、risk_level、gate_decision
- 每次调用写审计，返回 AgentObservation
- 复用 PreLiveBusinessFlowService 现有能力

### Agent Graph (src/core/pre_live_agent_graph.py)

- PreLiveAgentGraphState: TypedDict, JSON 可序列化
- build_pre_live_agent_graph: StateGraph + conditional edges + checkpoint + interrupt
- 保留原 pre_live_graph.py 不破坏

## 约束

- LLM 不直接写数据库，不绕过 SecurityHook / Reducer / ToolRegistry
- hard-gate (setup_live_session) 必须走 interrupt 人审
- Replan 最多 1 次，避免无限循环
- Graph state 保持 JSON 可序列化，不保存 Pydantic 对象
- 不接 Kafka consumer / 不做播中 ReAct

## 错误处理

- LLM API 失败 -> route=fallback
- JSON 解析失败 -> route=fallback
- Pydantic schema 校验失败 -> route=fallback
- 未知工具 -> fail-closed, AgentObservation.status=error
- lifecycle 不匹配 -> fail-closed
- fallback 始终走原有 PreLiveBusinessFlowService 确定性链路

## 测试策略

- 单元测试使用 mock LLM (FakeAgentPlanner)，不依赖真实 DeepSeek API
- 集成测试可选包含真实 LLM 冒烟测试，但不加入默认全量测试
- TDD 红绿灯：先写失败测试，再实现
