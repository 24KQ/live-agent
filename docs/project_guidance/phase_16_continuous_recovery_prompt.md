# Phase 16 Continuous Recovery Prompt

恢复时依次读取：

1. `docs/worklog/continuous_execution_state.md`
2. `docs/superpowers/reports/phase-16-v4-json-probe-evidence.md`
3. `docs/superpowers/reports/phase-16-v3-planner-diagnostic-evidence.md`
4. `docs/superpowers/specs/2026-07-28-phase16-v3-planner-diagnostic-design.md`
5. `docs/superpowers/plans/2026-07-28-phase16-v3-planner-diagnostic-plan.md`
6. `docs/superpowers/reports/phase-16-v2-official-smoke-evidence.md`
7. `docs/superpowers/reports/phase-16-official-smoke-evidence.md`
8. `docs/superpowers/specs/phase-16-controlled-multi-agent-escalation-design.md`
9. `docs/superpowers/plans/2026-07-18-phase-16-controlled-multi-agent-escalation-plan.md`
10. 总控计划、决策日志、三个 worklog、`git status` 与最近 Git log。

固定事实：Phase 15 保持历史 `INCONCLUSIVE`，默认 `DETERMINISTIC_ONLY`。Phase 16 的本地确定性
Acceptance 仍为 `INCONCLUSIVE`，但正式外部证据必须以
`docs/superpowers/reports/phase-16-official-smoke-evidence.md` 为准：唯一正式 run
`phase16-official-smoke-v1` 已发送第一条 Analyst 请求，得到完整 receipt/usage 后因
`ANALYST_VALIDATION_FAILED` 终止，外部结论为 `FAILED`。Planner 与其余 slot 未发送；严禁再次执行
`scripts/run_phase16_real_smoke.py --execute`、清空账本、重试、修补模型文本或用 ScriptedModel 替代真实结果。
历史直接模式支出为 `0.073220 CNY`，正式已结算为 `0.006306 CNY`，当前已知总额为 `0.079526 CNY`；十个
固定 case 每例预约 `0.092000 CNY`，最大暴露 `0.993220 CNY`。正式 `PASS` 仍要求 10/10 case、20/20
调用、完整 receipt/usage/validation 与 HMAC 认证；Smoke Profile 不进入生产 LIVE 路由。

正式收口分支为 `codex/phase16-official-smoke-evidence`。Task 0-5 的历史闭包和空 slot 报告整改、最终复验和文档已完成。
PR #2 首轮 Gate 暴露默认 shallow checkout 缺失历史执行 Git blob，以及报告器单测未隔离 CI PostgreSQL 环境；整改已对
PR/Nightly/Release 固定 `fetch-depth: 0` 并补齐对应测试。恢复时必须先读取 PR #2 与 Git 状态：未合并时只可推送整改并等待
required checks，合并后只可停在 `AWAITING_PHASE_17_GATE`，不得产生新的模型请求。最终本地证据为 unit `1596 passed, 1 warning`、
integration `214 passed, 7 deselected, 5 warnings`、Phase 16 escalation PostgreSQL `31 passed`、formal ledger/runner
PostgreSQL `29 passed`。三次补充只读终审在读取前因本地代理 `502`/`503` 终止，未产生可采纳结论，主模型已接管复核。v1 Manifest 的八项源码摘要
仅为 execution identity subset；完整闭包以独立 Git-blob audit 复核。Phase 16 只扩展 LIVE 高冲突售罄；自动升级需要
proposal-eligible Bundle 和冻结三选二规则，人工升级需要当前 Workspace lease。双 Agent 零 Skill、零 Store、
零写权限；任一失败为 `DEGRADED`，不回退单 Copilot。Analyst/Planner/Coordinator 生产预算分别固定为
`2s/1200/0.03`、`2s/2800/0.07`、`5s/4000/0.10`；默认继续 `DETERMINISTIC_ONLY`。

2026-07-28 的 V3 是独立单 Planner 诊断，而非 V1/V2 重试。其唯一请求已发送并以
`FAILED / MODEL_FAILURE_INVALID_OUTPUT_JSON` 终止，不能再次执行
`scripts/run_phase16_v3_planner_diagnostic.py --execute`。初始 V3 failure 以高精度延迟计算
digest/HMAC、以三位毫秒落库，历史认证为 `UNVERIFIABLE_LEGACY_LATENCY_PRECISION`；禁止回填、
重签或把它描述为认证真实模型证据。未来写入的精度规范化修复不改变这条历史事实。

V4 是独立的禁思考 JSON 协议探针，而非 V1/V2/V3 重试。唯一
`phase16-v4-json-probe-001` 已以 `PASS / JSON_PROTOCOL_PASS` 收口，完整 receipt/usage 与 HMAC
可复验；不得再次执行 `scripts/run_phase16_v4_json_probe.py --execute`。它只证明最小 JSON 协议可消费，
不得写成 AgentAction、Planner 或真实双 Agent `10/10` 通过；默认路由和 Phase Gate 不改变。

每个 Task 执行 RED、GREEN、REVIEW、VERIFY、DOCS、COMMIT、PUSH 并更新实时状态。不得修改或提交
主工作区用户脏文件。所有 Task 完成后仍保持 `AWAITING_PHASE_17_GATE`；广泛文档审计和 Phase 17
必须重新授权，不能自动开始。
