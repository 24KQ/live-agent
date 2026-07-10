# Phase 5G-B：LangGraph Harness Agent Loop 实施计划

## Summary

本阶段把播中 Agent 从“一次性 LLM 决策”升级为 LangGraph 显式编排的 Harness Agent Loop。重点不是普通 ReAct，而是把上下文、推理、工具策略、工具执行、观察回灌、再规划都拆成可测试节点和条件边。

## Key Changes

- 新增 `OnLiveHarnessPlanner`
  - 解析受控 JSON 决策。
  - 校验 action 与工具白名单。
  - LLM 失败时降级到 Phase 5F planner。

- 新增 `build_on_live_harness_agent_graph()`
  - 节点：`load_context`、`pre_reasoning_hook`、`agent_reasoning`、`route_agent_decision`、`pre_tool_call_hook`、`route_tool_policy`、`execute_tool`、`post_tool_call_hook`、`observe_result`、`route_replan`、`write_audit`。
  - 条件边控制 agent action、工具策略、replan。
  - `max_iterations` 阻断死循环。

- 工具协议兼容
  - Agent 输出 `recommend_backup_product`。
  - 旧执行器兼容 `recommend_backup_product` 与 `recommend_backup`。

- CLI 演示
  - `scripts/run_phase5g_harness_agent_demo.py`
  - 展示三条路径：no_action、final_answer、call_tool + replan。

## Test Plan

```powershell
pytest tests/unit/test_agent_harness_context.py -v
pytest tests/unit/test_agent_lifecycle_hooks.py -v
pytest tests/unit/test_on_live_harness_planner.py -v
pytest tests/unit/test_on_live_harness_agent_graph.py -v
pytest tests/integration/test_on_live_harness_agent_flow.py -v
pytest tests/unit/test_on_live_agent_graph.py -v
pytest tests/unit/test_on_live_llm_planner.py -v
pytest tests/unit/ -v
python scripts/run_phase5g_harness_agent_demo.py
git status --short --ignored
git add -n .
```

## Assumptions

- 并行新增，不替换旧播中 Graph。
- 不新增 LangChain / OpenAI SDK。
- 不接真实平台 API。
- 高风险工具不自动执行，后续再接 LangGraph interrupt。
- 留迹必须记录测试结果、CLI 反馈、问题修复、限制和下一步方向。
