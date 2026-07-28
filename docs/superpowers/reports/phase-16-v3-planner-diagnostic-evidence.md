# Phase 16 V3 Planner Diagnostic Evidence

本报告只依据 V3 PostgreSQL append-only 账本与命令入口的脱敏输出编写。不保存或展示
API Key、Prompt、模型正文、思维链、原始 Provider ID 或经营建议。

- Diagnostic run: `phase16-v3-planner-diagnostic-001`
- Parent V2 manifest digest: `228e012f559e2ecb6118c2eeb2b733bb3f3ee571c66e5183de4aca7ba994b1c9`
- Frozen Planner Profile digest: `e3b5d0cea141b69b1d7f4574f58d99202687277a9b3eb21edd9afc151f7eca43`
- Fixed case digest: `979d90b04ca16b1450b887d83740c506dbfa0172a3b861ca923077f1adbf3a1a`
- Model target: `deepseek-v4-pro`
- Formal conclusion: `FAILED / MODEL_FAILURE_INVALID_OUTPUT_JSON`
- Production default route: `DETERMINISTIC_ONLY`
- Phase state: `AWAITING_PHASE_17_GATE`

## One-Shot Result

- Fixed case / attempt: `phase16-high-conflict-paired-development-001` /
  `c9a2d791-ad8c-4925-a54c-19797a338818`
- Dispatch state: `request_sent=true`
- Model-port category: `INVALID_OUTPUT_JSON`
- HTTP status / retry-after: `NOT_AVAILABLE / NOT_AVAILABLE`
- Response digest: `e2461c6b872fbdf2d69ad495c0a704d4cb9f217ce50bdad537b7e81854595e72`
- Persisted latency: `40749.051 ms`
- Validation / outcome: `FAILED / MODEL_FAILURE_INVALID_OUTPUT_JSON`
- Outcome digest: `cbdf2f97e03ecf29235d88e3a9fd57f7e2330111b1066daeb739d1072c737c2e`
- Provider receipt / usage: `NOT_AVAILABLE`
- Retry policy: `ZERO_RETRY_AFTER_SEND`

`INVALID_OUTPUT_JSON` 表示共享 DeepSeek 模型端口收到已发送调用的响应后，无法将其解析为
可消费 JSON。它不是网络、鉴权、预算、生产路由或本地确定性双 Agent 演练的失败；但也不能被
解释为 Planner 或完整双 Agent 真实集成通过。

## Integrity Limitation

这条历史 failure 的 `fact_digest` 与 HMAC 在数据库读取时为
`UNVERIFIABLE_LEGACY_LATENCY_PRECISION`。根因是初始实现以 adapter 高精度毫秒值生成
digest/HMAC，而 `NUMERIC(16,3)` 在持久化时只保留三位小数。原始高精度值没有保存，故不能
重建认证输入。

该限制不会通过 UPDATE、重签或重发来修补：V3 行保持 append-only。代码现已在未来 failure
写入前以 half-up 规则量化到三位毫秒，并有单元和 PostgreSQL 回归测试；但本报告不能将该
后续修复倒灌为历史行的认证成功。

## Budget And Scope

- Phase 16 cap: `1.000000 CNY`
- V3 reservation: `0.052000 CNY`
- Conservative maximum exposure after V1/V2 facts and V3 reservation: `0.202165 CNY`
- Additional V3 authenticated actual spend: `NOT_AVAILABLE`

V1 的 `FAILED / ANALYST_VALIDATION_FAILED` 与 V2 的
`FAILED / MODEL_OUTCOME_UNAVAILABLE` 均保持不可变。V3 不重试、不改变默认路由，也不使
Phase 16 的原始严格真实模型 smoke 成为 `PASS`。
