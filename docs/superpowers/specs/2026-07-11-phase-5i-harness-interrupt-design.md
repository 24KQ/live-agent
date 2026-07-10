# Phase 5I Harness Interrupt 人审恢复设计

## 背景

Phase 5G-B 已经实现播中 LangGraph Harness Agent Loop，Phase 5H 已经把最终状态写入审计和 DecisionTrace。当前缺口是高风险工具只会进入 `pending_human`，还没有真正暂停等待人工审批，也不能从同一个 LangGraph thread 恢复。

Phase 5I 将 `pending_human` 接入 LangGraph `interrupt()`，让播中 Agent 在高风险动作前可暂停、可审批、可恢复。

## 核心流程

```text
pre_tool_call_hook
-> route_tool_policy
   -> auto_execute -> execute_tool
   -> pending_human -> human_approval_interrupt
      -> approved -> execute_tool -> observe_result -> replan
      -> rejected -> write_audit -> END
   -> blocked -> write_audit
```

## 设计要点

- 复用 `HumanApprovalRequest` / `HumanApprovalResponse`，并为播中审批增加 `tool_arguments` 和 `context_summary`。
- `human_approval_interrupt` 节点构造审批 payload 并调用 `interrupt()`。
- `Command(resume=...)` 恢复时必须校验 trace_id、room_id、tool_name。
- approved 后执行原 pending tool；rejected 后不执行工具，状态为 `rejected_by_human`。
- 审批结果进入 Graph state，最终由 Phase 5H 的 `OnLiveHarnessAuditWriter` 写入审计 payload。

## 边界

- 本阶段不接真实平台 API。
- 不新增数据库表。
- CLI 和测试默认使用 dry-run 审计。
- 真实审批 UI 后续由 Web 副屏承接。

## 后续方向

1. Phase 6C：Web 副屏展示 interrupt pending 状态，并提供 approve/reject 操作入口。
2. 播后复盘：将人工审批结果纳入 DecisionTrace 的采纳/拒绝分析。
3. 生产化：审批 pending/resume 审计增加真实幂等键，避免恢复重跑导致重复写入。
