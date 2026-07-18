# Phase 15 Continuous Recovery Prompt

你正在继续 LiveAgent Phase 15。先读取以下事实源，不要从旧对话猜测状态：

1. `docs/worklog/continuous_execution_state.md`
2. `docs/superpowers/specs/phase-15-golden-release-gates-design.md`
3. `docs/superpowers/plans/2026-07-18-phase-15-golden-release-gates-plan.md`
4. `docs/project_guidance/agent_runtime_completion_master_plan.md`
5. `docs/project_guidance/agent_runtime_evolution_decisions.md` 的 D-123 至 D-132
6. `docs/worklog/task_plan.md`
7. `docs/worklog/findings.md` 与 `docs/worklog/progress.md`
8. `git status --short` 与 `git log -5 --oneline --decorate`

## 恢复规则

- Stage A 已完成；实时状态明确记录用户已授权 Stage B 时，按 Phase 15 Plan 的 Task 1-12 继续。
- 当前游标为 Task 5 READY_TO_PUSH；恢复时先读取 Task 5 文件范围和最终测试证据，不跳过既有 RED/审查证据。
- Phase 15 使用双轨结论：技术 Release 可以 PASS，Copilot 仍可能为 `KEEP_DISABLED` 或 `BLOCKED`。
- 真实模型预算上限为 0.60 元；没有真实 usage 时按预留额结算并禁止 Promotion。
- 没有 3-5 名真实参与者的 24-40 条记录时，不生成真人 Promotion 证据。
- 不提交用户已有脏文件：旧恢复提示词、旧项目状态路线图、Phase 11A 文件、`docs/development_pitfalls.md`、`scripts/patch_run_all.py`、`scripts/tmp_gen_story.py`。
- 每个 Task 必须 RED/GREEN/REVIEW/VERIFY/DOCS/COMMIT/PUSH；每次提交前确认没有运行中的 sub-agent。
- Technical PASS 后才能晋升确定性 `SKILL_RUNTIME`/`PLAN_ENGINE` 默认；`DECISION_SUPPORT` 必须独立满足 Promotion 门槛。
- Phase 15 Acceptance 后停止，不自动进入新阶段。

恢复后先输出：当前 Stage/Task、最近提交、最近验证证据、用户脏文件、下一条精确命令。信息不完整时只做只读检查，不直接改代码。
