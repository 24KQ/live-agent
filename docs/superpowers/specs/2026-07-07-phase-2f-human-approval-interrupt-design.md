# Phase 2F LangGraph Interrupt 人审恢复设计

## 背景与目标

Phase 2E 已经证明播前 LangGraph 可以使用官方 PostgresSaver 持久化 checkpoint，并在模拟进程重启后恢复执行。Phase 2F 将建播 hard-gate 从 `confirmed_setup=True` 参数模拟升级为真正的 human-in-the-loop：Graph 在高风险建播节点调用 `interrupt()` 暂停，人工审批后通过 `Command(resume=...)` 恢复。

本阶段不接 LLM、不接 Kafka consumer、不做 Web 审批界面、不接真实平台 API。目标是验证“暂停、审批、恢复、审计、拒绝不执行”的工程骨架。

## 设计边界

- LangGraph 继续只做编排层，不直接写业务表、不绕过 ToolRegistry、SecurityHook 或审计。
- `trace_id` 继续作为 LangGraph `thread_id`，保持 checkpoint、审计和 CLI 回放统一。
- 建播人审请求使用 JSON 可序列化模型，不把 Pydantic 领域对象放入 checkpoint。
- 人审恢复输入只允许 `approved` 或 `rejected`，并校验 `trace_id`、`room_id`、`tool_name` 与 pending 请求一致。
- `interrupt()` 恢复时会重跑当前节点开头，因此 pending 审计由服务层按 idempotency_key 幂等写入。
- `approved` 后调用既有 `setup_live_session(..., confirmed_setup=True)`；`rejected` 后不调用建播服务，不写建播成功审计。

## 数据与审计

- 新增 `HumanApprovalRequest` 和 `HumanApprovalResponse` 模型。
- 审批审计工具名为 `setup_live_session_approval`。
- pending 审计记录 `operator_decision=pending`。
- resume 审计记录 `operator_decision=approved/rejected`，并在 `result_payload` 中保存恢复状态、操作员标识和审批理由。
- 建播成功仍使用既有 `setup_live_session` 审计记录。

## 验收标准

- Graph 首次运行到建播节点时返回 `__interrupt__`，payload 包含 `trace_id`、`room_id`、`tool_name`、风险等级和待确认动作。
- 使用同一 `thread_id` 和 `Command(resume=...)` 可恢复执行。
- approve 场景最终 `setup_status=prepared`，审计包含 pending、approved/resumed 和建播成功。
- reject 场景最终 `setup_status=rejected`，审计包含 pending、rejected，且没有建播成功审计。
- 前半段查询货盘、排品、手卡审计不重复写入。
- CLI 可一次演示 approve/reject 两条路径，并输出 `trace_id/thread_id`、interrupt payload 摘要和最终审计链路。
