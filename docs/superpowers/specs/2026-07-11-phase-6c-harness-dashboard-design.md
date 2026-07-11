# Phase 6C：Web 副屏 Agent 可观测与人审入口设计

## 目标

Phase 6C 把 Phase 5G-B/5H/5I 已具备的 LangGraph Harness Agent 能力暴露到 Web 副屏，让主播或运营能看到 Agent 的节点路径、pending 高风险工具、人审结果、工具 observation 和最终建议。

本阶段采用 PostgreSQL 持久化：LangGraph checkpoint 继续由官方 PostgresSaver 管理；新增 `live_agent_harness_sessions` 表只保存 Web 查询所需的会话快照。

## 设计决策

- Web 业务状态和 LangGraph checkpoint 分离，避免业务表变成自研 checkpoint。
- 高风险工具仍必须进入 `interrupt()`，Web 只通过 `Command(resume=...)` 恢复同一 `trace_id/thread_id`。
- 后端新增 `HarnessDashboardService` 作为 FastAPI 与 LangGraph 的边界，API 不直接操作 graph。
- 单元测试使用内存 store 和内存 checkpointer；集成测试使用 PostgreSQL store 和 PostgresSaver。
- 前端继续使用 Vanilla JS，不引入 React/Vue。

## 数据流

```text
Web 点击启动
-> POST /api/agent/harness/start
-> HarnessDashboardService.start_session()
-> LangGraph 运行到 human_approval_interrupt
-> live_agent_harness_sessions 写入 pending_human
-> Web 展示审批卡片

Web 点击批准/拒绝
-> POST /api/agent/harness/approval
-> Command(resume=...)
-> approved 执行工具 / rejected 跳过工具
-> 更新 live_agent_harness_sessions
-> WebSocket 推送 agent_harness_update
```

## 遗留限制

- 真实平台工具仍由本地 demo executor 模拟，不调用淘宝/抖音 API。
- 当前 Web 人审只覆盖 Harness 演示链路，后续可接入真实库存事件触发。
- pending 审批没有过期时间和多操作员锁，后续生产化阶段补齐。
