# Phase 16 V3 Planner 诊断实施记录

## 已执行步骤

1. 从 V2 执行提交建立独立 V3 分支、run、case slot、账本与 CLI；不修改 V1/V2。
2. 复用 V2 Planner Profile、冻结 case projection 与共享 `BoundedSpecialistRunner`，为
   `ModelFailure` 保留精确类别、发送状态、响应摘要和延迟。
3. 通过单元测试、真实 PostgreSQL 临时 schema、完整 unit/integration、编译、敏感载荷、
   文档编码和差异门禁后，执行一次显式 `--execute`。
4. 唯一请求已发送并以 `FAILED / MODEL_FAILURE_INVALID_OUTPUT_JSON` 终止；没有重试。
5. 发现历史 failure 的延迟精度使 digest/HMAC 无法从数据库重建后，修复未来写入规范，
   但保留历史行和认证失败事实，不回填、不改写、不再联网。

## 验收结论

- V3 没有产生 Provider 成功回执、usage 或可消费 Planner 结果。
- V3 精确诊断分类优于 V2 的 `MODEL_OUTCOME_UNAVAILABLE`，但不满足真实双 Agent
  `10/10 case`、`20/20 call`、完整认证回执与验证链的正式 `PASS` 条件。
- 历史 V3 failure HMAC 为 `UNVERIFIABLE_LEGACY_LATENCY_PRECISION`，报告必须保留该
  限制，不能声称 V3 为认证外部证据。
- 默认路由保持 `DETERMINISTIC_ONLY`；Phase 16 保持 `AWAITING_PHASE_17_GATE`，不自动
  开始 Phase 17 或新的真实模型实验。
