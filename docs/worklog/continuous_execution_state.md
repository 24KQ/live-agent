# LiveAgent 连续执行实时状态

文档状态：`AWAITING_IMPLEMENTATION_AUTHORIZATION`

最后更新：2026-07-14

## 1. 当前游标

| 字段 | 当前值 |
|---|---|
| 当前阶段 | Phase 12A |
| 最近完成任务 | Task 5：PostgreSQL PlanStore、DDL 与并发证据 |
| 下一任务 | Task 6：Checkpoint 一致性与人工命令恢复 |
| 下一任务状态 | `NOT_STARTED` |
| 当前子步骤 | 等待用户单独授权正式实施 |
| 当前分支 | `main` |
| 当前业务基线 | `37d6f8a feat: persist phase 12a plans` |
| 远端状态 | `origin/main` 必须包含该业务基线；最新文档 HEAD 以恢复时的 `git log` 为准 |
| 真实模型累计费用 | 0 元 |

## 2. 当前授权边界

- 已授权：持久化 Phase 12A-14 总控计划、Design、Implementation Plan、决策和恢复协议。
- 未授权：修改业务代码、执行 Phase 12A Task 6、运行真实模型或开始 Phase 12B-14 实施。
- 文档持久化完成后必须保持本状态，等待用户明确发出正式实施授权。

## 3. 当前关键不变量

- PlanStore 是执行事实权威源，checkpoint 只保存引用。
- 不得因 checkpoint 领先而补造 NodeRun 或外部业务证据。
- 不得在同次 Runtime/PlanEngine 调用失败后 fallback Legacy。
- `TRUSTED_COMPAT` 必须在 Phase 12A Acceptance 前退役。
- PlanEngine 和 Orchestrator 默认是确定性组件。
- Agent 候选必须和确定性基线对照，严重安全违规必须为 0。
- 真实模型总费用不得超过 3 元人民币。
- Phase 13 与本轮 Phase 14 首次 Release 共用 `agent-runtime-completion-v1` 预算作用域。

## 4. 最近验证证据

| 范围 | 证据 |
|---|---|
| Phase 12A Task 5 专项 | `13 passed` |
| 当前默认单元测试基线 | `807 passed, 4 warnings` |
| Phase 11B/12A PostgreSQL 集成基线 | `11 passed` |
| 最新业务提交 | `37d6f8a` |
| 本轮目标文档严格编码检查 | `16 files, 0 issues` |
| 决策与计划结构 | `D-001..D-093` 连续；Task `9/11/10/10` 连续 |
| 文档差异检查 | `git diff --check` 退出码 0 |
| 全仓编码扫描 | `4 errors/58 warnings`，均为目标外历史问题 |

以上是进入本轮文档持久化前的既有证据。本轮不修改代码，因此不重新运行业务测试。

## 5. 用户已有未提交文件

以下文件不属于本轮交付，不得覆盖、还原或提交：

- `docs/project_guidance/agent_runtime_context_recovery_prompt.md`
- `docs/project_guidance/current_project_status_and_agent_roadmap.md`
- `docs/superpowers/reports/phase-11a-skill-runtime-acceptance.md`
- `docs/superpowers/specs/phase-11a-skill-runtime-design.md`
- `docs/development_pitfalls.md`
- `scripts/patch_run_all.py`
- `scripts/tmp_gen_story.py`

## 6. 正式实施后的更新格式

每个 Task 开始时，将本节复制为当前记录并替换内容：

```text
Phase / Task:
状态: RED | GREEN | REFACTOR | REVIEW | VERIFY | COMMIT | PUSHED | BLOCKED
目标:
禁止事项:
当前 HEAD:
本 Task 文件:
用户脏文件:
最近命令与结果:
错误与尝试次数:
设计偏差与决策编号:
下一条精确操作:
模型费用累计:
```

更新时机固定为：

1. Task 开始前。
2. RED 失败符合预期后。
3. 核心 GREEN 后。
4. 规格或质量审查发现需要整改时。
5. 全部验证完成、准备提交时。
6. 推送成功并切换到下一 Task 时。

## 7. 三次失败协议

- 第一次：记录原始错误，定位根因并做最小修复。
- 第二次：不得重复同一操作，改用不同诊断或实现路径。
- 第三次：重新检查设计假设、决策日志和相关事实源。
- 三次后仍无法推进：写明阻塞证据；只有外部状态或用户决策确实不可替代时才暂停。

## 8. 压缩后恢复顺序

```text
本文件
-> docs/project_guidance/agent_runtime_completion_master_plan.md
-> 当前阶段 Design
-> 当前阶段 Implementation Plan
-> docs/worklog/task_plan.md
-> docs/worklog/findings.md 与 progress.md 最新章节
-> docs/project_guidance/agent_runtime_evolution_decisions.md
-> git status
-> git log -5 --oneline
-> 最近验证命令
```

恢复后必须先回答：当前 Task 是什么、已完成到哪个子步骤、最近证据是什么、下一条命令是什么、哪些用户文件不能提交。不能回答时不得直接修改代码。
