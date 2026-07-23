# Phase 16 Official Real-Model Smoke Evidence

本 Addendum 仅从 PostgreSQL append-only formal ledger 的最小脱敏字段生成。它不保存或展示 API Key、Prompt、模型正文、思维链、原始 provider ID 或经营建议。

- Formal run: `phase16-official-smoke-v1`
- Formal manifest digest: `d75b8dce67ac49e8cbb9c71388fc9e666703c7296f585eb9e3b792bd0abaeb7b`
- Formal evidence conclusion: `FAILED`
- Production default route: `DETERMINISTIC_ONLY`
- Phase state: `AWAITING_PHASE_17_GATE`

## Strict Result

- Required cases / calls: `10 / 20`
- Completed cases / calls: `1 / 1`
- Validation facts: `1`
- Claimed / unclaimed fixed slots: `1 / 9`
- Dispatch attempts Analyst / Planner: `1 / 0`
- Authenticated PASS outcomes: `0 / 0` (`NOT_APPLICABLE`)
- ScriptedModel baseline comparison: `NOT_COMPARABLE_AFTER_ANALYST_FAILURE`
- Retry policy: `ZERO_RETRY_AFTER_SEND`
- Text repair or scripted substitution: `FORBIDDEN`

## Budget

- Formal cap: `1.000000 CNY`
- Historical direct-mode spend: `0.073220 CNY`
- Current known actual spend: `0.079526 CNY`
- Frozen fixed slots: `10`
- Frozen maximum exposure: `0.993220 CNY`

## Receipt And Validation Facts

### `phase16-high-conflict-paired-development-001` / `ANALYST`

- Profile digest: `415b331477a55c58bd61e0d632ec3b74aa3137a5c30f8fd1344ab19fb2875bee`
- Provider receipt digest: `944d5b5959acd28393ba0132aca92f9846588d9359635e60565189adbb2b27bc`
- Response digest: `df336bbd6bbd2ba4ea65ac4eb6f617d6159004220bd95a67603b5525de0b4b90`
- Model / finish reason: `deepseek-v4-flash` / `stop`
- Usage input / output / total: `2610 / 1848 / 4458`
- Latency: `14138.545 ms`
- Cost input / output / total: `0.002610 / 0.003696 / 0.006306 CNY`

- Validation `phase16-high-conflict-paired-development-001` / `ANALYST`: `FAILED` / `ANALYST_VALIDATION_FAILED` / `41790eda4476eadf43a49877f5a673659b111ef724dbed7ef926b5b222e0e643`
- Outcome `phase16-high-conflict-paired-development-001`: `FAILED` / `ANALYST_VALIDATION_FAILED` / `4f8f8e2ddd230a82d11a59eac4b36c246b46abaa848c935ac8bb8cacf6db349b`

## Interpretation

已记录的 Analyst validation 未通过正式校验，稳定原因码为 `ANALYST_VALIDATION_FAILED`。账本记录 `0` 个 Planner dispatch 与 `9` 个未 claim 的固定 slot；D-170 要求在已发送后立即停止，不能重试、修补模型文本或以 ScriptedModel 代替。

正式账本故意只保留稳定验证原因码与摘要，不保存模型正文或内部异常文本；因此本报告不把 token 数、Schema、AgentAction 或 EvidenceRef 中的任一可能原因推断为确定根因。该限制保护敏感载荷，也避免在没有原始证据时制造错误归因。

本 Addendum 取代此前对 Phase 16 真实模型证据“未执行/INCONCLUSIVE”的当前表述：本次正式 run 的外部结论为 `FAILED`。这不改变已通过的确定性工程验收，也不会开启 `DECISION_SUPPORT` 或自动经营动作。
