# Phase 5G-B：LangGraph Harness Agent Loop 设计文档

## 背景

Phase 5F 已经把播中决策从确定性规则升级为 LLM planner，但整体仍偏“一次性建议”。结合 `docs/study/taobao_anchor_agent_harness.md` 的 Harness 思路，本阶段把播中 Agent 的关键控制点显式落到 LangGraph 节点和条件边上。

本阶段不做普通 ReAct `while loop`。ReAct 只作为思想，实际工程形态是 LangGraph Harness：

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

## 设计目标

- 体现 LangGraph：节点、条件边、状态回灌、可 checkpoint。
- 体现 Harness：上下文、工具协议、生命周期 Hook、风险阻断、观察回灌。
- 保留现有播中 Graph，不破坏 Phase 5C/5F 旧链路。
- 高风险工具不自动执行，后续可接 `interrupt()` 人审恢复。

## 核心模块

- `src/skills/on_live_harness_planner.py`
  - 输出 `OnLiveHarnessDecision`
  - action 固定为 `call_tool | final_answer | no_action | fallback`
  - 工具名必须来自 ToolRegistry 的 ON_LIVE 白名单
  - LLM 失败时降级到 Phase 5F `OnLiveLLMPlanner`

- `src/core/on_live_harness_agent_graph.py`
  - 新增 `build_on_live_harness_agent_graph()`
  - 使用 `StateGraph` 显式编排 Harness 节点
  - 工具 observation 回灌后可重新进入 `pre_reasoning_hook`
  - `max_iterations` 防止死循环

- `src/core/agent_lifecycle_hooks.py`
  - 已在 Phase 5G 第一阶段加入
  - 本阶段复用其 pre/post tool call 约束

## 工具协议

Agent 侧统一输出 ToolRegistry 标准工具名：

- `handle_sold_out_event`
- `recommend_backup_product`
- `generate_on_live_prompt`
- `aggregate_danmaku_questions`
- `generate_danmaku_reply`

旧 `_LocalServiceExecutor` 内部曾使用 `recommend_backup`，本阶段增加 `recommend_backup_product` 别名兼容。

## 验收标准

- 无事件路径不进入工具节点。
- final_answer 路径直接写入建议并结束。
- call_tool 路径经过工具策略、执行、post hook、观察、replan。
- 高风险工具只 pending，不自动执行。
- 超过 `max_iterations` 强制结束。
- 旧播中 Graph 测试继续通过。
