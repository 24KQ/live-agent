# Phase 2D LangGraph 播前 Harness 骨架设计

## 背景与目标

Phase 2A 已经完成基于 PostgreSQL 样例货盘的播前业务闭环，Phase 2B/2C 已经验证基础播中售罄和弹幕事件。Phase 2D 开始接入最终架构中的 LangGraph，但只做轻量编排骨架：把现有播前服务包装为 graph 节点，验证 LangGraph 与生命周期、工具门禁、审计链路的配合方式。

本阶段不接 LLM、不接真实平台 API、不做持久 checkpoint、不做 interrupt 暂停恢复。所有业务规则继续由现有 Python service、SecurityHook、ToolRegistry 和 PostgreSQL 审计承担。

## 设计边界

- LangGraph 只作为编排层，不直接访问数据库或修改业务状态。
- 播前节点顺序固定为：查询货盘、生成排品、生成商品手卡、合规/风险摘要、模拟建播 hard-gate。
- Graph state 保存 `room_id`、`trace_id`、节点历史、商品数量、排品数量、手卡数量、门禁结果和审计 ID。
- 未确认建播时返回 `pending_confirmation`，不得伪装成建播成功。
- 确认建播时继续复用现有 `setup_live_session` 审计写入。

## 验收标准

- `langgraph` 依赖可导入。
- Graph 能从 START 跑到 END。
- 未确认建播时，graph 返回 hard-gate pending 且没有建播审计 ID。
- 确认建播时，graph 返回 `setup_audit_id`，并能按 `trace_id` 查到完整审计链路。
- CLI 能演示完整播前 graph，并输出节点历史、计数、门禁结果和审计工具列表。
- 阶段执行记录包含测试结果、CLI 结果、问题修复和后续迭代建议。
