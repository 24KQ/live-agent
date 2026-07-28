# Phase 16 V2 Official Real-Model Smoke Evidence

本报告只根据 `phase16-official-smoke-v2` PostgreSQL append-only 账本中的脱敏事实编写。它不保存或展示 API Key、Prompt、模型正文、思维链、原始 provider ID 或经营建议。

- Formal run: `phase16-official-smoke-v2`
- Formal manifest digest: `228e012f559e2ecb6118c2eeb2b733bb3f3ee571c66e5183de4aca7ba994b1c9`
- Formal evidence conclusion: `FAILED`
- Stable failure code: `MODEL_OUTCOME_UNAVAILABLE`
- Production default route: `DETERMINISTIC_ONLY`
- Phase state: `AWAITING_PHASE_17_GATE`

## Strict Result

- Required cases / calls: `10 / 20`
- Completed cases / sent calls: `1 / 2`
- Completed Analyst / Planner validation: `PASS / FAILED`
- Claimed / unclaimed fixed slots: `1 / 9`
- Retry policy: `ZERO_RETRY_AFTER_SEND`
- Text repair or ScriptedModel substitution: `FORBIDDEN`

首个固定 case `phase16-high-conflict-paired-development-001` 的 Analyst 已通过
`ANALYST_VALIDATION_PASS`。同一 case 的 Planner 请求已发送，但模型端口没有产生可消费的
`ModelSuccess` outcome，因此写入 `FAILED / MODEL_OUTCOME_UNAVAILABLE` 并立即关闭 case。其余
九个 slot 未发送；同一 V2 run 不得重新领取或重发。

## Receipt And Validation Facts

### `phase16-high-conflict-paired-development-001` / `ANALYST`

- Profile digest: `7bdf995a2c8892c8a05ae6da094825a5abfe0b9a1f06ddf5e7d61968f921dff0`
- Model / finish reason: `deepseek-v4-pro` / `stop`
- Usage input / output / total: `2471 / 1871 / 4342`
- Latency: `24968.233 ms`
- Authenticated receipt cost: `0.018639 CNY`
- Validation: `PASS / ANALYST_VALIDATION_PASS`

### `phase16-high-conflict-paired-development-001` / `PLANNER`

- Profile digest: `e3b5d0cea141b69b1d7f4574f58d99202687277a9b3eb21edd9afc151f7eca43`
- Provider receipt / usage: `NOT_AVAILABLE`
- Validation: `FAILED / MODEL_OUTCOME_UNAVAILABLE`

账本不会保存模型正文或内部异常详情，所以本报告不把未保存的网络、供应商或端口内部原因
推断为确定根因。可复核结论仅是：该 Planner 请求被保守视为已发送，但未形成可验证的成功
outcome，故 V2 不能满足严格 `10/10` 正式通过标准。

## Budget

- Formal cap: `1.000000 CNY`
- Imported historical spend: `0.079526 CNY` (`HISTORICAL_DIRECT_MODE` + `V1_FORMAL_FAILED`)
- Newly authenticated V2 actual spend: `0.018639 CNY`
- Known authenticated actual total: `0.098165 CNY`
- Planner usage is unavailable; it is not reported as known actual spend.
- Frozen maximum exposure: `0.999526 CNY`

V1 的 `FAILED / ANALYST_VALIDATION_FAILED` 保持不可变。V2 的失败也不改变已经通过的本地
确定性工程验收；两者均不允许开启 `DECISION_SUPPORT` 或自动经营动作。
