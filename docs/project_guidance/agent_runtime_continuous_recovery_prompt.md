# LiveAgent Agent Runtime 连续执行恢复提示词

用途：上下文压缩、中断或切换会话后，将本文作为恢复入口。本文只负责恢复顺序和强约束，具体事实以引用文件为准。

## 恢复指令

请先不要修改代码，也不要凭记忆推断当前进度。依次读取：

1. `D:\java\agent\docs\worklog\continuous_execution_state.md`
2. `D:\java\agent\docs\project_guidance\agent_runtime_completion_master_plan.md`
3. 当前阶段 Design
4. 当前阶段 Implementation Plan
5. `D:\java\agent\docs\worklog\task_plan.md`
6. `D:\java\agent\docs\worklog\findings.md`
7. `D:\java\agent\docs\worklog\progress.md`
8. `D:\java\agent\docs\project_guidance\agent_runtime_evolution_decisions.md`
9. `git status --short --branch`
10. `git log -5 --oneline --decorate`

如果实时状态与 Git 冲突，以已提交 Git 事实和实际工作树为准，并先修正实时状态；不得通过还原用户文件来制造“干净”状态。

## 项目定位

项目覆盖播前、播中、播后三场景，技术目标是人机协同决策支持与受控执行 Runtime：

- Skill Runtime 统一版本、Schema、权限、幂等、审计和执行。
- PlanEngine 负责确定性 DAG、恢复、抢占和增量 Replan。
- Agent 只能压缩可信证据并提出受限方案；运营拥有高风险经营决定的最终权威。
- Orchestrator 和 PlanEngine 不默认包装成 Agent。
- Phase 13 的自主 Specialist 结论保持历史事实；新 Phase 14 只实现一个默认关闭的播中 Copilot，并预留受控多 Agent 接口。

## 当前阶段基线

- Phase 11A、11B 已完成并通过用户验收。
- Phase 12A、12B、13 已完成并有 Acceptance；最新已推送提交为 `e82ef78`。
- Phase 14 Design/Plan 已审核持久化，状态为 `PHASE_14_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`。
- Phase 15 只保留 Golden/CI/发布门禁讨论基线；当前不得提前实施。

## 阶段文件

Phase 12A：

- `D:\java\agent\docs\superpowers\specs\phase-12a-dag-plan-engine-design.md`
- `D:\java\agent\docs\superpowers\plans\2026-07-14-phase-12a-dag-plan-engine-plan.md`

Phase 12B：

- `D:\java\agent\docs\superpowers\specs\phase-12b-preemption-replan-design.md`
- `D:\java\agent\docs\superpowers\plans\2026-07-14-phase-12b-preemption-replan-plan.md`

Phase 13：

- `D:\java\agent\docs\superpowers\specs\phase-13-specialist-agent-evaluation-design.md`
- `D:\java\agent\docs\superpowers\plans\2026-07-14-phase-13-specialist-agent-evaluation-plan.md`

Phase 14：

- `D:\java\agent\docs\superpowers\specs\phase-14-human-centered-decision-support-design.md`
- `D:\java\agent\docs\superpowers\plans\2026-07-17-phase-14-human-centered-decision-support-plan.md`

Phase 15：

- `D:\java\agent\docs\superpowers\specs\phase-15-golden-release-gates-discussion-baseline.md`

## 不可遗忘的约束

- 修改代码必须有详细 UTF-8 中文注释。
- 可按当前 Phase Plan 派发受控 sub-agent；主模型负责安全边界、集成、验证、提交和推送，并监控二十分钟无进展、重复阻塞和越界接管条件。
- 严格 RED、GREEN、REFACTOR；不向 `main` 推送红灯或半成品。
- 每个 Task 至少一个独立 ASCII commit，并在验证后推送 `origin/main`；Phase 15 默认路由晋升按未来重新冻结的计划使用代码提交和 Acceptance 提交两步闭合。
- 不覆盖或提交实时状态文件列出的用户脏文件。
- ToolRegistry 分阶段退役，Phase 15 决定删除；新代码使用 Catalog、SkillPolicyView 或 SkillExecutor。
- `TRUSTED_COMPAT` 在 Phase 12A Acceptance 前删除。
- 可信售罄事件由 PlanEngine 唯一执行写操作，Harness 只消费证据。
- Agent 严重安全违规必须为 0；未达到收益和成本门槛就删除候选。
- Phase 14 真实模型上限为 1.00 元，Phase 15 Release 预留 0.60 元；真实调用必须先通过全部身份、价格、usage 与哈希预检。
- 不接真实淘宝 API、自由 A2A、动态 handoff、共享 scratchpad、外部插件或热加载；Phase 14 明确交付受控运营工作台和 API。

## 恢复后的第一步

读取实时状态中的“下一条精确操作”。如果状态仍为 `PHASE_14_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`，只允许检查或更新 Phase 14 文档，不得开始 Runtime/UI/数据库实现。只有用户明确授权 Phase 14 正式实施后，才能把状态改为 `IN_PROGRESS` 并执行当前阶段 Plan。
