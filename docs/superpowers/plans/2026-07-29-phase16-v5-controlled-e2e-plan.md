# Phase 16 V5 受控 E2E 真实模型证据实施计划

## 目标

以独立 V5 campaign 验证禁思考、JSON mode 下的真实双 Agent 严格 10/10 E2E；不改写 V1 至 V4 历史，
不打开生产路由。

## Task 0：文档和身份冻结

- 新增 V5 Design、Plan、决策记录、恢复状态和 worklog；将 V5 定义为 Phase 16 的有限证据收口。
- 新建 `codex/phase16-v5-controlled-e2e`，基线为 `origin/main@281cea6`；不触碰根工作区未跟踪文件。
- 提交并推送文档身份后，才开始 V5 代码与账本实现。

## Task 1：隔离协议与 Profile

- 新建 V5 专用 Transport/Adapter，显式注入 `thinking.disabled`，继续使用共享 Adapter 的 JSON mode、
  deadline、usage、receipt 和安全解析；不得修改 V1 至 V4 所绑定的共享 Adapter。
- 新建冻结 V5 Analyst/Planner Profile、无案例 FINAL 信封结构示例和 system-managed Evidence ID 验证。
- 新建 V5 Manifest，绑定合成校准 case、正式十例、价格、Profile/Prompt/Schema/source digest 和执行协议。

## Task 2：campaign 账本与 Runner

- 新建 PostgreSQL append-only V5 campaign、校准/正式 run、固定 slot、attempt、receipt、validation 和
  terminal outcome；校准与正式调用共享 `1.000000 CNY`。
- 复用 V2 的只读 projection、共享 Runner 与语义验证，不复制 AgentAction/Schema/EvidenceRef 逻辑。
- CLI 默认 dry-run；`--execute-calibration` 与 `--execute-formal` 分离，后者要求已认证的校准 PASS。

## Task 3：离线验证

- RED/GREEN 覆盖协议注入、Prompt/Manifest 身份、预算、校准门、重复 claim、崩溃恢复、脱敏、receipt/usage、
  非 stop、JSON/Schema/Evidence/语义失败和默认路由隔离。
- 使用真实 PostgreSQL 覆盖并发、append-only、HMAC/延迟量化、DDL 和恢复。
- 运行完整 unit/integration、冻结 coverage 90/85、release cases、compileall、迁移 dry-run、敏感载荷、
  文档编码和 `git diff --check`。

## Task 4：真实证据与收口

- 仅在代码、Manifest、迁移和本地门禁都已提交推送后执行校准。校准失败则写脱敏 FAILED 报告并停止。
- 校准 PASS 后运行正式十例；任一已发送失败立即终止，不重试。
- 渲染脱敏 V5 Evidence 和 Phase 16 Acceptance。V5 10/10 PASS 才可将 Phase 16 标为
  `PASS: CONTROLLED_E2E_QUALIFIED`；历史 V1 至 V4 始终保留。
- 最终 PR Gate 全绿后以 merge commit 合并；默认路由保持 `DETERMINISTIC_ONLY`，状态保持
  `AWAITING_PHASE_17_GATE`，不自动开始思考模式支持或 Phase 17。

