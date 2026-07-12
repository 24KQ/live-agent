# LiveAgent Agent Runtime 上下文恢复提示词

用途：当后续多轮对话或上下文压缩导致注意力丢失时，把本文内容直接发给执行者，用于恢复项目定位、当前阶段、关键决策和执行约束。

## 恢复顺序

请先不要直接实施代码。请按以下顺序恢复项目上下文，并以这些文档为事实源：

1. `D:\java\agent\docs\project_guidance\agent_runtime_evolution_roadmap.md`
2. `D:\java\agent\docs\project_guidance\agent_runtime_evolution_decisions.md`
3. `D:\java\agent\docs\superpowers\specs\phase-11a-skill-runtime-design.md`
4. `D:\java\agent\docs\superpowers\plans\2026-07-12-phase-11a-skill-runtime-plan.md`
5. `D:\java\agent\docs\superpowers\reports\phase-11a-skill-runtime-acceptance.md`
6. `D:\java\agent\docs\worklog\task_plan.md`
7. `D:\java\agent\docs\worklog\findings.md`
8. `D:\java\agent\docs\worklog\progress.md`
9. `git status` 与最近提交

## 项目定位

项目业务范围不是只有播中，而是播前、播中、播后三个场景：

- 播前：商品查询、排品、手卡生成、建播准备，目前偏 Workflow / Graph。
- 播中：实时控场、弹幕、库存、售罄抢占、人审与工具执行，目前已有单体 Agent Harness。
- 播后：Replay、Evaluation、复盘、风险归因、记忆沉淀，目前偏评估与复盘流程。

当前项目不是成熟多 Agent 系统，也不能为了加 Agent 而加 Agent。正确目标是建设面向淘宝直播全链路的可控 Agent Runtime。

## 技术分层

- `Tool` 是底层动作和外部副作用。
- `Skill` 是可治理、可版本化、可审计的业务能力单元。
- `Agent` 是有目标、上下文、工具选择权和局部推理循环的决策者。
- `PlanEngine` 是确定性 DAG 调度、恢复和 Replan 组件。
- `Orchestrator` 是确定性协调器，不默认包装成 Agent。
- `Evaluation Interface` 决定 Agent 是否真的比固定基线更好。

## 当前阶段状态

- 已完成 `D-001` 至最新决策编号的架构决策持久化。
- Phase 11A Design 已审核冻结。
- Phase 11A 技术验收已完成，Acceptance 待用户审核；Phase 11B 未开始。
- Phase 11A 目标是 Skill Runtime：
  - `SkillManifest` 是唯一事实源。
  - `ToolRegistry` 是只读兼容投影。
  - 13 个现有工具迁移 Manifest 元数据。
  - 4 个播前核心 Handler 进入新执行链：
    - `query_products`
    - `generate_live_plan`
    - `generate_product_card`
    - `setup_live_session`
  - 正式路由只有 `LEGACY` 和 `SKILL_RUNTIME`。
  - 不做插件安装、热加载、数据库动态配置、PlanEngine 或多 Agent 实施。

## 后续阶段边界

Phase 11B-14 只保留高层大纲，采用 Just-in-Time 设计：

- Phase 11B：统一执行与平台契约。
- Phase 12A：DAG PlanEngine。
- Phase 12B：抢占与增量 Replan。
- Phase 13：三场景 Agent 化评估与试点。
- Phase 14：Golden Dataset 与发布门禁。

## Phase 13 的正确理解

- 不是只做 LiveOpsAgent。
- 也不是默认做三个 Agent。
- 候选 Specialist Agent 包括：
  - `PlannerAgent`：播前复杂计划与重规划。
  - `LiveOpsAgent`：播中实时事件与控场。
  - `ReviewMemoryAgent`：播后复盘、归因、记忆沉淀。
- 每个 Agent 都必须先有确定性基线，再用相同 Skill、Hook、权限和评估样本对照。
- 严重安全违规必须为 0。
- 成功率至少提升 5 个百分点，或相关恢复率至少提升 10 个百分点。
- 延迟和 Token 成本增幅不得超过 20%。
- 达不到门槛就删除 Agent 试点，保留确定性子图。

## 后续执行约束

- 不要把“播前、播中、播后”机械等同于“三个 Agent”。
- 不要恢复旧的“只围绕播中单体 Agent Harness”的窄定位。
- 不要把 Orchestrator 或 PlanEngine 包装成 Agent，除非有新的评估决策。
- 修改代码时遵守 `AGENTS.md`：新增或修改代码需要详细中文注释，文件使用 UTF-8。
- 修改中文文档优先使用 `apply_patch`，不要用 PowerShell heredoc 或管道写大段中文。
- 可以根据任务需要使用 sub-agent 做代码审查、并行分析和复杂任务拆分。
- 当前下一步是由用户审核 Phase 11A Acceptance；审核完成前不提前设计或实施 Phase 11B。
