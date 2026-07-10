# Phase 5I Harness Interrupt 人审恢复实施计划

## Summary

把播中 Harness Agent 的高风险工具从 `pending_human` 状态升级为 LangGraph 原生 `interrupt()` 人审恢复。

```text
pending_human -> interrupt(payload) -> Command(resume=approved/rejected)
```

## Key Changes

- `src/core/human_approval.py`
  - `HumanApprovalRequest` 新增 `tool_arguments` 和 `context_summary`。
  - 保持播前旧审批字段兼容。

- `src/core/on_live_harness_agent_graph.py`
  - 新增 `human_approval_interrupt` 节点。
  - `route_tool_policy` 将 `pending_human` 路由到 interrupt 节点。
  - approved 后执行原 pending tool；rejected 后写审计结束。
  - state 增加审批请求、审批结果、操作员和原因字段。

- `src/core/on_live_harness_audit.py`
  - 审计结果 payload 包含审批请求和恢复结果。

- `scripts/run_phase5i_harness_interrupt_demo.py`
  - 演示 approve / reject 两条路径。

## Test Plan

```powershell
pytest tests/unit/test_human_approval.py -v
pytest tests/unit/test_on_live_harness_agent_interrupt.py -v
pytest tests/unit/test_on_live_harness_agent_graph.py -v
pytest tests/unit/test_on_live_harness_audit.py -v
pytest tests/unit/test_pre_live_graph_interrupt.py -v
pytest tests/integration/test_on_live_harness_interrupt_flow.py -v
pytest tests/unit/ -v
python scripts/run_phase5i_harness_interrupt_demo.py
git status --short --ignored
git add -n .
```

## Assumptions

- 高风险工具不允许自动执行。
- `trace_id` 继续作为 LangGraph `thread_id`。
- `Command(resume=...)` 的 trace、room、tool 必须和 pending 请求一致。
- 本阶段只做 CLI 演示，不做 Web 审批按钮。
- 建议提交信息：`feat: add phase 5i harness interrupt human approval`。
