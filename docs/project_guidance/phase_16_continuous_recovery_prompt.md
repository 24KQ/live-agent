# Phase 16 Continuous Recovery Prompt

恢复时依次读取：

1. `docs/worklog/continuous_execution_state.md`
2. `docs/superpowers/specs/phase-16-controlled-multi-agent-escalation-design.md`
3. `docs/superpowers/plans/2026-07-18-phase-16-controlled-multi-agent-escalation-plan.md`
4. 总控计划、决策日志、三个 worklog、`git status` 与最近 Git log。

固定事实：Phase 15 保持历史 `INCONCLUSIVE`，默认 `DETERMINISTIC_ONLY`。Phase 16
只扩展 LIVE 高冲突售罄；自动升级需要 proposal-eligible Bundle 和冻结三选二规则，人工
升级需要当前 Workspace lease。双 Agent 零 Skill、零 Store、零写权限；任一失败为
`DEGRADED`，不回退单 Copilot。Analyst/Planner/Coordinator 预算分别固定为
`2s/1200/0.03`、`2s/2800/0.07`、`5s/4000/0.10`；Task 10 预检前禁止真实模型，整个
smoke 上限为 1.00 CNY。

每个 Task 执行 RED、GREEN、REVIEW、VERIFY、DOCS、COMMIT、PUSH 并更新实时状态。不得
修改或提交主工作区用户脏文件。Phase 16 Acceptance 后停止在 `AWAITING_PHASE_17_GATE`；
广泛文档审计必须重新授权，不能自动开始。
