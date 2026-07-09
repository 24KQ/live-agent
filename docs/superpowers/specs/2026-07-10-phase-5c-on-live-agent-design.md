# Phase 5C: 播中 Agent 动态决策小循环设计

## 日期

2026-07-10

## 背景

播中（ON_LIVE）是目前唯一没有 Agent 能力的阶段。弹幕进来走固定关键词规则，库存事件走固定链路。当大量弹幕集中问价格、库存告警时，系统无法主动给主播建议。

## 设计目标

在播中加入 Agent 观察-决策-建议小循环：

1. 收集播中上下文（弹幕聚合、库存状态）
2. 根据上下文决策本轮目标
3. 执行播中工具
4. 生成主播建议
5. 写入审计

## 关键约束

- 不加 LLM planner，复用 AgentRulesPlanner
- 不加 interrupt，高风险动作沿用 hard-gate
- Agent 只建议，不自动执行高风险动作
- 复用现有 ToolRegistry 和安全 Hook

## Graph 流程

START → collect_on_live_context → on_live_planner → route_by_decision (conditional)
  → execute_tools → observe_result → write_audit → END
  → END（finish 路由时直接结束）

## 路由规则

- 弹幕高频（>= 10条同类）：建议主播回应
- 库存告警：建议切换备选商品
- 无事件：finish，不干预
- planner 失败：fallback 降级
