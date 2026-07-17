# Agent Runtime Phase-Gated 上下文恢复提示词

请先不要修改代码。按以下顺序读取并回答当前 Phase、Task、最近证据、授权范围和下一条操作：

1. `docs/worklog/continuous_execution_state.md`
2. `docs/project_guidance/agent_runtime_completion_master_plan.md`
3. `docs/project_guidance/agent_runtime_business_closed_loop_track.md`
4. 当前 Phase 的 Acceptance、Design 和 Implementation Plan
5. `docs/project_guidance/agent_runtime_evolution_decisions.md`
6. `docs/worklog/task_plan.md`、`findings.md`、`progress.md`
7. `git status` 与 `git log -5 --oneline`

执行授权规则：

- 只在实时状态标明的当前已授权 Phase 内，按 Task 执行 RED -> GREEN -> REFACTOR -> REVIEW -> VERIFY -> COMMIT -> PUSH。
- Phase Acceptance 通过后，状态必须变为 `AWAITING_PHASE_<N>_GATE`。不得自动开始下一 Phase，也不得直接使用下一 Phase 的旧详细计划。
- Gate 必须比较前一 Phase 的 Acceptance、当前预算、基础设施、风险和已有讨论基线；更新或重生下一 Phase Design/Plan 后，等待用户明确授权。
- Phase 14 Human-Centered Decision Support 的 Design/Plan 已审核持久化，但仍不是实施授权；Phase 15 Golden/CI 只有讨论基线。

项目的固定业务闭环是 `live-session-p001-sold-out-v1`：三张手卡并行生成、p001 售罄、
Event Inbox、局部冻结、CAS、严格只读对账、紧急 DAG、Replan 复用、播后评估和人机协同
Gate。Phase 14 将其扩展为运营工作台证据，Phase 15 才重新讨论 Release Gate。它不证明真实淘宝 GMV。

sub-agent 可用于互不重叠的独立分析、规格、安全、并发或测试审查；主模型必须完成安全边界、共享迁移、集成、验证、提交和推送。派发后在首次回报、核心 GREEN、提交前检查实际 diff 与测试；二十分钟无进展、重复阻塞或越界时停止并接管。不得让 sub-agent 处理凭据、提交或覆盖用户脏文件。
