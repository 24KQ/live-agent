# Phase 16 V3 Planner 诊断设计

## 状态

`EXECUTED_FAILED_WITH_UNVERIFIABLE_LEGACY_FAILURE_AUTH`。

V3 是为解释 V2 `MODEL_OUTCOME_UNAVAILABLE` 而创建的独立、单 Planner 诊断，
不是 V1 或 V2 的重试，也不是新的生产 Agent 能力。它不改变 `DETERMINISTIC_ONLY`
默认路由或 `AWAITING_PHASE_17_GATE` 阶段状态。

## 目标与边界

- 只重建冻结 case `phase16-high-conflict-paired-development-001` 的 V2 Planner 输入。
- 只发起一次 DeepSeek V4 Pro Planner 调用，复用 V2 冻结 Profile 和
  `BoundedSpecialistRunner`，不复制 AgentAction、JSON Schema 或 EvidenceRef 验证。
- 使用独立 V3 PostgreSQL append-only 账本、run ID、case slot、DDL 和 HMAC 域；V1/V2
  的 Manifest、账本、回执、终态和报告均为只读历史。
- 不保存 API Key、Prompt、模型正文、思维链、原始 Provider ID 或经营建议；只保存脱敏
  request/response 摘要、端口失败类别、发送状态、HTTP 元数据、延迟和终态原因码。

## 预算与终态

V3 仅预约 `0.052000 CNY`。在 `0.079526 CNY` 的 V1/历史已知支出、
`0.018639 CNY` 的 V2 Analyst 实际支出以及 `0.052000 CNY` 的 V2 Planner 未知最大
暴露之后，V3 最大总暴露为 `0.202165 CNY`，低于 Phase 16 的 `1.000000 CNY` 硬上限。

V3 仅有一个终态 slot：预发送阻断为 `BLOCKED`；已发送的任何 `ModelFailure`、缺回执、
Schema/EvidenceRef 失败或 Runner 契约失约为 `FAILED`。终态后禁止重发、补发、文本修补
或以 ScriptedModel 替代。即使 V3 成功，也只能证明一个 Planner 诊断回执路径，不能把
原始严格 10/10 双 Agent smoke 提升为 `PASS`。

## 执行事实与完整性限制

唯一 V3 调用已发送，端口记录 `MODEL_FAILURE_INVALID_OUTPUT_JSON`，因此 V3 为 `FAILED`。
首次实现将 adapter 的高精度浮点延迟用于 failure digest/HMAC，而 PostgreSQL
`NUMERIC(16,3)` 只保存三位毫秒。读取历史行时无法重建原 digest 或 HMAC。

后续实现已在写摘要/HMAC 前统一使用三位毫秒、half-up 量化，并通过离线和 PostgreSQL
测试验证未来行可复验；但不得回填、重签、更新或以新代码重新解释已发送的 V3 行。因此
该历史事实的认证结论为 `UNVERIFIABLE_LEGACY_LATENCY_PRECISION`。它可用于定位
`INVALID_OUTPUT_JSON` 方向，不能成为认证的真实双 Agent 成功证据。
