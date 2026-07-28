# Phase 16 V4 Disabled-Thinking JSON Protocol Probe Evidence

本报告只根据 `phase16-v4-json-probe-001` PostgreSQL append-only 账本中的脱敏事实编写。
它不保存或展示 API Key、Prompt、模型正文、思维链、原始 Provider ID 或经营建议。

- Probe run: `phase16-v4-json-probe-001`
- Fixed case: `phase16-v4-json-probe-minimal-json-001`
- Protocol digest: `b14fcec3d2a3bdc448c7b4f35b56208de273dc83ee0518ebcf98d390171aad54`
- Model / thinking mode: `deepseek-v4-pro` / `disabled`
- Formal probe conclusion: `PASS / JSON_PROTOCOL_PASS`
- Production default route: `DETERMINISTIC_ONLY`
- Phase state: `AWAITING_PHASE_17_GATE`

## Strict Probe Result

- Dispatch attempts: `1 / 1`
- Retry policy: `ZERO_RETRY_AFTER_SEND`
- Provider receipt complete: `true`
- Provider finish reason: `STOP`
- Usage input / output / total: `45 / 5 / 50`
- Persisted latency: `1150.662 ms`
- Receipt HMAC verification: `PASS`
- Outcome digest verification: `PASS`

该请求显式发送 DeepSeek OpenAI-compatible 顶层字段
`{"thinking":{"type":"disabled"}}`，并要求固定无业务 JSON `{"status":"ok"}`。共享
Adapter 成功消费最终 JSON；账本保存的是 Provider ID、响应和输出的 SHA-256 摘要，而非原始内容。

## Budget

- Phase 16 total cap: `1.000000 CNY`
- V1/V2/V3 conservative prior exposure: `0.202165 CNY`
- V4 reservation: `0.010000 CNY`
- Maximum conservative exposure after V4 reservation: `0.212165 CNY`
- Usage-price-bound cost: `0.000165 CNY`

价格估算使用 DeepSeek V4 Pro 的冻结公开价格：输入 `3 CNY / million tokens`、输出
`6 CNY / million tokens`。该值由已认证 usage 推导；账本保留预约与 usage，不以未保存的
Provider 计费正文伪造结算事实。

## Scope And Non-Claims

本次 PASS 只证明：在明确禁用思考模式时，`deepseek-v4-pro` 能通过现有单次 Adapter 返回有完整
receipt/usage 的最小 JSON 对象。它不创建 AgentAction、ConflictAnalysis、Proposal、OperatorDecision
或经营命令。

因此，该结论不覆盖也不推翻以下不可变历史：V1 为
`FAILED / ANALYST_VALIDATION_FAILED`，V2 为 `FAILED / MODEL_OUTCOME_UNAVAILABLE`，V3 为
`FAILED / MODEL_FAILURE_INVALID_OUTPUT_JSON`。它更不能被表述为真实双 Agent `10/10`、`20/20`
端到端通过。默认生产路由继续 `DETERMINISTIC_ONLY`，Phase 16 继续停在
`AWAITING_PHASE_17_GATE`；任何新的双 Agent 实验必须另行设计、预算、授权和建立独立 run。
