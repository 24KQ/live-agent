# Phase 16 Continuous Recovery Prompt

恢复时依次读取：

1. `docs/worklog/continuous_execution_state.md`
2. `docs/superpowers/specs/2026-07-22-phase16-official-smoke-evidence-design.md`
3. `docs/superpowers/plans/2026-07-22-phase16-official-smoke-evidence-plan.md`
4. `docs/superpowers/specs/phase-16-controlled-multi-agent-escalation-design.md`
5. `docs/superpowers/plans/2026-07-18-phase-16-controlled-multi-agent-escalation-plan.md`
6. 总控计划、决策日志、三个 worklog、`git status` 与最近 Git log。

固定事实：Phase 15 保持历史 `INCONCLUSIVE`，默认 `DETERMINISTIC_ONLY`。Phase 16 的本地确定性
Acceptance 仍为 `INCONCLUSIVE`，但正式外部证据必须以
`docs/superpowers/reports/phase-16-official-smoke-evidence.md` 为准：唯一正式 run
`phase16-official-smoke-v1` 已发送第一条 Analyst 请求，得到完整 receipt/usage 后因
`ANALYST_VALIDATION_FAILED` 终止，外部结论为 `FAILED`。Planner 与其余 slot 未发送；严禁再次执行
`scripts/run_phase16_real_smoke.py --execute`、清空账本、重试、修补模型文本或用 ScriptedModel 替代真实结果。
历史直接模式支出为 `0.073220 CNY`，正式已结算为 `0.006306 CNY`，当前已知总额为 `0.079526 CNY`；十个
固定 case 每例预约 `0.092000 CNY`，最大暴露 `0.993220 CNY`。正式 `PASS` 仍要求 10/10 case、20/20
调用、完整 receipt/usage/validation 与 HMAC 认证；Smoke Profile 不进入生产 LIVE 路由。

正式收口分支为 `codex/phase16-official-smoke-evidence`。Task 0-5 的历史闭包和空 slot 报告整改、最终复验和文档已完成，
当前只可提交、推送、等待 PR Gate 并合并，不得产生新的模型请求。最终本地证据为 unit `1596 passed, 1 warning`、
integration `214 passed, 7 deselected, 5 warnings`、Phase 16 escalation PostgreSQL `31 passed`、formal ledger/runner
PostgreSQL `29 passed`。两次补充只读终审在读取前因本地代理 `502`/`503` 终止，未产生可采纳结论，主模型已接管复核。v1 Manifest 的八项源码摘要
仅为 execution identity subset；完整闭包以独立 Git-blob audit 复核。Phase 16 只扩展 LIVE 高冲突售罄；自动升级需要
proposal-eligible Bundle 和冻结三选二规则，人工升级需要当前 Workspace lease。双 Agent 零 Skill、零 Store、
零写权限；任一失败为 `DEGRADED`，不回退单 Copilot。Analyst/Planner/Coordinator 生产预算分别固定为
`2s/1200/0.03`、`2s/2800/0.07`、`5s/4000/0.10`；默认继续 `DETERMINISTIC_ONLY`。

每个 Task 执行 RED、GREEN、REVIEW、VERIFY、DOCS、COMMIT、PUSH 并更新实时状态。不得修改或提交
主工作区用户脏文件。所有 Task 完成后仍保持 `AWAITING_PHASE_17_GATE`；广泛文档审计和 Phase 17
必须重新授权，不能自动开始。
