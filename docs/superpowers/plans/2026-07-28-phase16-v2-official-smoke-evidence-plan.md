# Phase 16 V2 正式真实模型证据实施计划

## 执行顺序

1. 持久化 D-172 至 D-176 与 V2 设计，冻结 V1 不可变和 V2 独立边界。
2. 先为 V2 Profile、system-managed evidence mode、Pro 价格和 fail-closed
   preflight 写 RED 测试，再实现最小共享 Runner 扩展。
3. 新建 V2 Manifest、PostgreSQL append-only ledger、单一 Runner 与默认 dry-run
   CLI；V1 文件只能读取，不能迁移或重签。
4. 以单元、集成、真实 PostgreSQL、迁移、编码、敏感信息和 source closure 验证后，
   执行一次 `--execute`。不得做探索性联网调用或重试。
5. 从 V2 账本生成脱敏报告并更新 Acceptance/状态；通过 PR Gate 后 merge commit
   合并，停止在 `AWAITING_PHASE_17_GATE`。

## 执行结果

- Task 1-4 已完成。唯一 V2 `--execute` 在本地 Gate 后运行；第一个 case 的 Analyst
  通过完整 receipt/usage/受控 ID 验证，Planner 已发送但没有可消费 outcome。
- 根据零重试规则，V2 已以 `FAILED / MODEL_OUTCOME_UNAVAILABLE` 收口，未发送其余九个
  slot。脱敏账本事实见 `phase-16-v2-official-smoke-evidence.md`。
- 后续仅允许 PR 收口或新的独立设计授权；不得在该 V2 run 上修补、重试或篡改账本。

## 验收

V2 PASS 需要十个固定 case、二十次 Pro 调用、每次 `finish_reason=stop`、完整
provider receipt/usage/HMAC、受控 ID 与完整 EvidenceRef 谱系验证、`MULTI_AGENT_READY`
路由以及总费用不超过 1.00 元。任一已发送失败均按事实收口为 V2 FAILED，不修改 V1。
