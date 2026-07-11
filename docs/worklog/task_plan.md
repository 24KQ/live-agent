# LiveAgent 工作日志计划

## 目标

把 `docs/worklog/` 从本机临时记录升级为可追踪的项目工作日志，用于记录阶段计划、发现、进度和后续迭代方向。

## 记录原则

- 只记录项目事实、阶段结论、测试结果和后续计划。
- 不记录真实 `.env`、API key、平台 token、本机私密路径和个人账号密码。
- 中文内容统一 UTF-8，无 BOM 优先。
- 修改后运行 `python scripts/check_doc_encoding.py`。

## 2026-07-11 文档编码治理任务

- [x] 新增 `scripts/check_doc_encoding.py`，用于扫描文档编码风险。
- [x] 新增 `docs/project_guidance/document_encoding_policy.md`，固定中文文档写入规范。
- [x] 将 `docs/worklog/` 纳入版本控制，作为后续迭代留迹入口。
- [x] 更新 `current_project_status_and_agent_roadmap.md`，记录编码治理状态。
- [x] 更新 `phase_execution_log.md`，追加本次治理记录。

## 后续维护要求

- 每个阶段结束后更新 `phase_execution_log.md`。
- 重要架构判断同步更新 `current_project_status_and_agent_roadmap.md`。
- 长期任务过程记录可追加到 `docs/worklog/progress.md`。
- 排障结论和设计取舍追加到 `docs/worklog/findings.md`。
# 2026-07-11 Phase 7A 任务

- [x] 提交 Phase 6C 功能代码。
- [x] 提交编码治理和阶段记录。
- [x] 新增 AgentReplayService 和回放模型。
- [x] 新增规则评估器和维度分模型。
- [x] 新增内存 Store、PostgreSQL Store 和 Worker。
- [x] 新增 LLM Judge 结构化接口。
- [x] 扩展 FastAPI 评估接口和 WebSocket 消息。
- [x] 新增 `/evaluation` 运维页面。
- [x] 跑全量测试、demo、编码扫描和 diff 检查。

---
