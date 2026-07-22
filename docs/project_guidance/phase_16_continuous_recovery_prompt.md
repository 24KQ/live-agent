# Phase 16 Continuous Recovery Prompt

恢复时依次读取：

1. `docs/worklog/continuous_execution_state.md`
2. `docs/superpowers/specs/2026-07-22-phase16-official-smoke-evidence-design.md`
3. `docs/superpowers/plans/2026-07-22-phase16-official-smoke-evidence-plan.md`
4. `docs/superpowers/specs/phase-16-controlled-multi-agent-escalation-design.md`
5. `docs/superpowers/plans/2026-07-18-phase-16-controlled-multi-agent-escalation-plan.md`
6. 总控计划、决策日志、三个 worklog、`git status` 与最近 Git log。

固定事实：Phase 15 保持历史 `INCONCLUSIVE`，默认 `DETERMINISTIC_ONLY`。Phase 16 Task 1-11
已完成，Acceptance 为 `INCONCLUSIVE`，真实模型调用/费用为 `0.000000 CNY`。Phase 16 只扩展 LIVE
高冲突售罄；自动升级需要 proposal-eligible Bundle 和冻结三选二规则，人工升级需要当前 Workspace lease。
双 Agent 零 Skill、零 Store、零写权限；任一失败为 `DEGRADED`，不回退单 Copilot。Analyst/Planner/
Coordinator 预算分别固定为 `2s/1200/0.03`、`2s/2800/0.07`、`5s/4000/0.10`；10-case smoke 上限为
1.00 CNY，当前没有 endpoint/usage 合同和真实回执。

正式收口分支为 `codex/phase16-official-smoke-evidence`。先完成 docs-only Task 0 并推送；之后才可
修改代码或迁移。唯一正式 run 为 `phase16-official-smoke-v1`：历史直接模式支出 `0.073220 CNY`
计入一元总预算，十个固定 case 每例预约 `0.092000 CNY`，最大暴露 `0.993220 CNY`。预检未发送为
`BLOCKED + INCONCLUSIVE`；一旦发送，任何失败均为 `FAILED`、立即停止、绝不重试。正式 `PASS` 必须
是 10/10 case、20/20 调用和完整 receipt/usage/validation。Smoke Profile 不进入生产 LIVE 路由，默认
继续 `DETERMINISTIC_ONLY`。

每个 Task 执行 RED、GREEN、REVIEW、VERIFY、DOCS、COMMIT、PUSH 并更新实时状态。不得修改或提交
主工作区用户脏文件。所有 Task 完成后仍保持 `AWAITING_PHASE_17_GATE`；广泛文档审计和 Phase 17
必须重新授权，不能自动开始。
