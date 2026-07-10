# Phase 5H Harness Agent 审计与 DecisionTrace 闭环实施计划

## Summary

把 Phase 5G-B 的 `write_audit` 占位节点升级为真实审计闭环：

```text
Harness Agent state
-> OnLiveHarnessAuditWriter
-> ToolCallAuditStore AuditEvent
-> DecisionTraceRecord / dry-run payload
-> Graph 最终 state 回填 audit_status
```

## Key Changes

1. 新增 `src/core/on_live_harness_audit.py`
   - `OnLiveHarnessAuditWriter`
   - dry-run 模式
   - ToolCallAuditStore / DecisionTraceStore 依赖注入
   - 审计 payload 递归脱敏

2. 修改 `src/core/on_live_harness_agent_graph.py`
   - state 增加 `anchor_id`、`audit_ids`、`decision_trace_ids`、`audit_status`、`audit_payload`
   - `build_on_live_harness_agent_graph()` 增加 `audit_writer`
   - `write_audit` 节点调用 writer
   - 审计失败时返回 `audit_status=error`，不覆盖 Agent 推理结果

3. 修改 `scripts/run_phase5g_harness_agent_demo.py`
   - 展示 `audit_status`
   - 展示 `audit_ids`
   - 展示 `decision_trace_ids`
   - 展示 dry-run audit payload 摘要

4. 测试
   - `tests/unit/test_on_live_harness_audit.py`
   - `tests/unit/test_on_live_harness_agent_graph.py`
   - `tests/integration/test_on_live_harness_audit_flow.py`

## Test Plan

```powershell
pytest tests/unit/test_on_live_harness_audit.py -v
pytest tests/unit/test_on_live_harness_agent_graph.py -v
pytest tests/integration/test_on_live_harness_audit_flow.py -v
pytest tests/unit/test_on_live_harness_planner.py -v
pytest tests/unit/ -v
python scripts/run_phase5g_harness_agent_demo.py
git status --short --ignored
git add -n .
```

## 验收标准

- 无 store 时返回 `audit_status=dry_run`。
- 注入 fake/real store 时返回审计 ID。
- pending / blocked / max_iterations 均进入审计 payload。
- DecisionTrace dry-run payload 包含最终建议和 Agent 状态。
- Graph 审计失败不崩溃。
- CLI 能展示审计状态。

## 后续迭代方向

1. Phase 5I：LangGraph interrupt 人审恢复。
2. Phase 6C：Web 副屏展示 Harness 节点路径和审计状态。
3. 播后复盘阶段回填真实主播采纳结果和 trust_delta。
