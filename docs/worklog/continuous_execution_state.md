# LiveAgent 连续执行实时状态

文档状态：`PHASE_15_COMPLETE_INCONCLUSIVE`

最后更新：2026-07-18

## 1. 当前游标

| 字段 | 当前值 |
|---|---|
| 当前阶段 | Phase 15 Golden Release Gates Stage B |
| 最近完成任务 | Task 12：Demo、Phase 15 Acceptance 与 Final Acceptance |
| 当前任务 | Phase 15 Acceptance 已完成，停止在阶段边界 |
| 当前任务状态 | `COMPLETE` / `PHASE_15_COMPLETE_INCONCLUSIVE` |
| 当前子步骤 | 三场景闭环、两次本地 Release、双轨结论和两份 Acceptance 已生成；外部证据保持 BLOCKED/INCONCLUSIVE，不自动进入下一阶段 |
| 当前分支 | `main` |
| 当前业务基线 | Phase 15 Task 12 Acceptance（代码 `c01a5da`，最终状态 `38413bc`） |
| 远端状态 | `origin/main=38413bc`；用户脏文件保持 unstaged，恢复时必须核对本地/远端 HEAD |
| 真实模型累计费用 | 0.042344 元；Phase 14 Task 4 新增 0 元 |

## 2. 当前授权边界

- 已完成：Phase 12B Task 1-11 与 Acceptance。
- 已审核：Phase 14 Human-Centered Decision Support Design/Plan、D-113 至 D-122；Phase 15 Design/Plan、D-123 至 D-132 和恢复协议。
- 当前授权：Phase 15 Stage B Task 1-12 连续实施；Task 12 已完成，阶段停止。
- 仍禁止：Task 6 预检前运行真实模型；伪造真人或 GitHub Actions 证据；修改用户脏文件；跳过 RED/REVIEW/VERIFY。
- 调整边界：采用受控自主调整；设计范围内可自主修正，架构级变化先写决策日志，触及硬边界时暂停。
- 当前禁止：提交红灯/半成品/已知失败代码、修改或提交用户脏文件、自动进入下一 Phase。

## 3. 当前执行记录

```text
Phase / Task: Phase 15 / Task 12
状态: COMPLETE
目标: Demo、Phase 15 Acceptance 与 Final Acceptance
禁止事项: 不调用真实模型；不伪造真人/托管 CI 证据；不修改用户脏文件；不把临时兼容脚本纳入提交
当前 HEAD: `38413bc` 已提交并推送；恢复时以 `git log -1 --oneline --decorate` 和 `git status --short` 读取精确值
本 Task 文件: scripts/run_all.py、README.md、Phase 15 Acceptance/Final Acceptance、Task 12 测试和阶段留痕
用户脏文件: 4 个既有修改文档、development_pitfalls.md、patch_run_all.py、tmp_gen_story.py
最近命令与结果: Task 12 专项 `3 passed`、聚合 `33 passed`；完整 unit `1382 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`；迁移 dry-run、正式源码 compileall、PR/Nightly 本地门禁和严格差异/编码检查通过；Release 正确 BLOCKED；真实模型未调用
错误与尝试次数: Task 11 初始缺少 `src.release_gates.routing`，符合预期 RED；D-133 已记录 Settings/profile Schema 扩展；用户既有脏文件保持原状
设计偏差与决策编号: 沿用 D-121、D-123 至 D-133；Technical PASS 与 Promotion 独立，技术失败优先 NOT_RELEASED
下一条精确操作: 停止在 Phase 15 边界；不自动开始新 Phase，后续如需继续必须重新进行 Just-in-Time Gate
模型费用累计: 0.042344 元
```

## 2026-07-18 Phase 15 Task 12 COMMIT/PUSH 与最终状态

- Task 12 已以 `c01a5da docs: accept agent runtime release` 独立提交并推送，`origin/main` 与本地 HEAD 一致。
- 两份 Acceptance 已生成：本地技术 dry-run 通过，真实模型、真人对照和托管 GitHub Actions evidence 缺失，阶段和总验收均为 `INCONCLUSIVE`；Promotion 保持 `BLOCKED`，默认路由保持 `DETERMINISTIC_ONLY`。
- Phase 15 状态固定为 `PHASE_15_COMPLETE_INCONCLUSIVE`，不自动进入下一阶段；用户已有脏文件仍未暂存。

## 2026-07-18 Phase 15 Task 8 RED

- Task 7 `984b3ff` 已推送，连续游标进入 Task 8。
- Task 8 的统一 CLI、覆盖率入口和 GitHub Actions 证据读取入口尚未存在；先建立非法 mode、Manifest/Subject 不匹配、数据库缺失、覆盖率不足和外部证据缺失的红灯测试。
- PR/Nightly/Release 本地演练默认使用确定性 Subject 观察，不调用真实模型；缺少强制外部证据时只能返回明确 `BLOCKED`，不得伪造托管运行证据。

## 2026-07-18 Phase 15 Task 8 GREEN / REVIEW

- 统一 CLI、覆盖率入口、Actions 证据读取入口和 `phase15-demo` 已实现；Task 8 专项与 entrypoint 聚合为 `12 passed`。
- 主模型已直接运行 `python scripts/run_release_gate.py --mode pr`，48 个 case 全部技术 PASS，Promotion 因模型/真人证据缺失保持 BLOCKED，`external_calls=false`。
- Sub-agent 监控留痕：

```text
Sub-agent ID / 角色: 019f7327-ad05-7980-a8c6-941c32872aac / Hegel / Task 8 CLI 规格与质量只读审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 scripts/run_release_gate.py、scripts/check_coverage_gate.py、scripts/fetch_github_actions_evidence.py、Task 8 测试和 Phase 15 Design/Plan；禁止修改文件
预期交付物与测试: 检查退出码、Manifest 身份、48 case 聚合、无外部调用和报告稳定性
首次回报: 完成，报告 1 Critical、6 Important
最近可验证进展: 主模型复核并修复 Release 强制证据、36/48 split、Manifest/Dataset 身份、EvidenceRef、非有限预算和敏感回显问题；专项 `20 passed`
状态: COMPLETED_REPORT_CONSUMED / STOPPED
接管原因（如适用）: 主模型逐项检查并修复所有 Critical/Important；未采用未经复核的代码变更
```

审查整改摘要：

- Release 自动要求数据库、覆盖率和 Actions evidence；缺失时聚合 `Technical BLOCKED`，最终 `NOT_RELEASED`。
- PR/Nightly 只执行 36 个非 holdout case，Release 执行完整 48 个 case。
- 自定义 Subject Manifest 必须匹配冻结 ID、版本和摘要；Dataset 必须匹配仓库冻结 Manifest 摘要。
- Actions evidence 使用严格身份字段和 artifact/commit 摘要校验，输出只保留白名单字段；Release case 保留 EvidenceRef。

## 2026-07-18 Phase 15 Task 5 最终复审派发

```text
Sub-agent ID / 角色: `019f72f7-eda0-72e3-8e70-80ab9b3737f5` / 规格审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 human_study.py、Task 5 测试、DDL、Phase 15 Plan；禁止修改文件
预期交付物与测试: 核对 3-5 人/8 trial、封闭响应、Promotion BLOCKED、study/Manifest 隔离、重启恢复与阶段边界
首次回报: 等待窗口内未返回可验证报告
最近可验证进展: Task 5 unit/API `7 passed`；PostgreSQL `2 passed`；完整 unit `1346 passed`；integration `154 passed`
状态: STOPPED / TAKEN_OVER
接管原因（如适用）: 提交前无稳定回报，主模型按同一清单完成实际差异和测试复核
```

```text
Sub-agent ID / 角色: 未派发（线程容量限制） / 代码质量与安全审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 Task 5 生产代码、Manifest 闭包修复和目标测试；禁止修改文件
预期交付物与测试: 检查 SQL 约束、并发/幂等、身份泄露、敏感字段、API fail-closed、中文注释和跨阶段污染
首次回报: 未派发
最近可验证进展: 真实模型与真人证据费用均为 `0`；迁移 dry-run、敏感扫描、git diff --check 已通过
状态: TAKEN_OVER
接管原因（如适用）: 已完成线程占满容量；主模型执行代码质量、安全、SQL、编码和完整回归复核
```

## 2026-07-18 Phase 15 Task 6 审查派发

```text
Sub-agent ID / 角色: `019f7307-c1dd-78c2-b26c-78cd679da196` / Task 6 规格与代码质量只读审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 copilot_smoke.py、Task 6 测试、Phase 15 budget.py/Plan；禁止修改文件
预期交付物与测试: 核对预检身份、可信发送门、单次请求/预算幂等、unknown usage、fallback/Schema/严重违规和 no-network 边界
首次回报: 等待窗口内未返回可验证报告
最近可验证进展: Task 6 unit `7 passed`；PostgreSQL `1 passed`；相关 Phase 15 聚合 `18 passed`/`5 passed`
状态: STOPPED / TAKEN_OVER
接管原因（如适用）: 提交前无稳定回报，主模型完成实际差异、专项/全量测试、预算和 no-network 复核
```

当前 sub-agent：Task 1 的迁移只读 explorer 已完成并关闭；入口/扫描 explorer 的首次派发因线程配额拒绝。主模型已复核实际差异并接管 RED/GREEN、整合、验证、提交和推送。以下为历史 sub-agent 留痕：

```text
Sub-agent ID / 角色: Task 5 Compiler 规格与代码质量只读审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 src/decision_support/commands.py、Task 5 单元/集成测试、Phase 14 Design/Plan；禁止修改文件
预期交付物与测试: 核对 OperatorDecision/ExecutionCommand/PlanCommand 的权限、版本、lease/fencing、幂等和禁止直接执行边界，报告 Critical/Important/Normal 发现
首次回报: 未返回可验证报告
最近可验证进展: 主模型已完成全量 unit/integration、compileall、迁移 dry-run 和 git diff --check
状态: STOPPED / TAKEN_OVER
接管原因（如适用）: 提交前未返回可验证进展；主模型复核实际差异、计划契约和全部验证证据后接管

Sub-agent ID / 角色: Task 4 Specialist Runtime API 只读分析（已登记任务）
派发时间: 2026-07-18
只读或写入文件边界: 只读 specialist_runtime/model_port.py、runner.py、profiles.py、registry.py 与相关单元测试
预期交付物与测试: 报告 AgentTask/AgentResult/预算/deadline/Skill 调用边界，禁止修改文件
首次回报: 已完成
最近可验证进展: 确认 AgentTask/AgentResult、Profile digest、预算、deadline、Skill Port 和取消边界；发现 Profile 完整摘要与共享 Runner 集成测试缺口
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: Task 4 LiveOps/Profile 模式只读分析（已登记任务）
派发时间: 2026-07-18
只读或写入文件边界: 只读 specialist_runtime/live_ops.py、scripted_model.py、phase13 LiveOps 测试与 Phase 14 Design/Plan
预期交付物与测试: 报告可复用的 Profile/Adapter/Schema 约束和 Task 4 测试缺口，禁止修改文件
首次回报: 已完成
最近可验证进展: 对照 Phase 13 LiveOps/Profile 模式确认固定输出、EvidenceRef、只读 Skill 和 ScriptedModel 组合；发现备品快照、风险白名单、过期与 DEGRADED 门禁缺口
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f7032-c12e-7072-845d-f4bc8247e0a9 / Task 3 最终规格审查
派发时间: 2026-07-17
只读或写入文件边界: 只读 Phase 14 Design/Plan、D-114/D-117、evidence.py 与测试
预期交付物与测试: 核对六类证据、scope、版本、时间、冲突、对账降级和 Task 3 范围
首次回报: 已完成
最近可验证进展: 初审发现可信时钟、冻结和 Manifest 三项缺口；整改后复审无 Critical/Important
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f7032-d565-7392-8090-293f18589b7e / Task 3 代码质量与安全审查
派发时间: 2026-07-17
只读或写入文件边界: 只读 evidence.py、Task 3 测试与相关冻结模型
预期交付物与测试: 检查摘要、时间重绑定、model_construct、Resolver 权限、确定性与测试缺口
首次回报: 已完成
最近可验证进展: 复审发现 envelope、父事实绑定、外层重载和自由摘要四项 Important；均已新增 RED/GREEN 证据
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f706c-61e1-71f3-8b2d-80e8b72838de / Task 3 最终规格复审
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 3 Design/Plan、D-114/D-117、当前代码和测试
预期交付物与测试: 核对六角色证据、窄只读父事实 Resolver、Store 父绑定和 Task 3 范围
首次回报: 已成功派发，等待可验证结论
最近可验证进展: 最终结论无 Critical/Important；复核验证 `79 passed`
状态: COMPLETED（无 Critical/Important）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f706c-765a-7111-afc7-31edbdf347da / Task 3 最终质量与安全复审
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 3 Python、测试、Store 事务和冻结 Manifest
预期交付物与测试: 核对循环导入、不可变性、摘要/时间、SQL 作用域、夹具旁路和测试隔离
首次回报: 已成功派发，等待可验证结论
最近可验证进展: D-121 威胁模型复核无 Critical/Important；复核验证 `79 passed`
状态: COMPLETED（无 Critical/Important）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f7020-1ff4-7a61-8a4c-38c740da92d9 / Task 3 EvidenceRef 与 Resolver 模式分析
派发时间: 2026-07-17
只读或写入文件边界: 只读 specialist_runtime 证据协议、Registry、Runner 与测试
预期交付物与测试: 提炼严格 EvidenceRef、白名单解析、摘要/作用域/时间校验的可复用模式
首次回报: 已成功派发，等待可验证结论
最近可验证进展: 正在核对现有证据协议和安全边界
状态: COMPLETED（建议场景专用六角色 Registry、完整 scope 与本地摘要重算）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f7020-33ea-78d3-9d60-efee73b2ef66 / Task 3 EventStore 与 PlanStore 公开读取分析
派发时间: 2026-07-17
只读或写入文件边界: 只读 Phase 12B 事件、计划、商品、弹幕和节奏模型及公开 API
预期交付物与测试: 给出复合售罄 EvidenceBundle 的最小输入类型与只读 Port 边界
首次回报: 已成功派发，等待可验证结论
最近可验证进展: 正在检查事实来源、版本、provenance 和公开查询能力
状态: COMPLETED（确认五个窄只读 Port，禁止注入完整 EventStore/PlanStore）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6ffb-0d67-7830-aab9-2a118eacf37d / Task 2 最终规格复审
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 2 Design/Plan、D-114/D-117、models/store/SQL/tests
预期交付物与测试: 核对三视图、五事实、CAS、幂等、lease/fencing、作用域、版本与事务原子性
首次回报: 已成功派发，等待可验证结论
最近可验证进展: 正在核对审查整改后的完整 Task 2 差异
状态: COMPLETED（最终规格复审无 Critical/Important）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6ffb-2246-7440-acda-9ccdcbbbe143 / Task 2 最终代码质量与安全复审
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 2 Python/SQL/tests
预期交付物与测试: 核对 SQL 注入、事务、锁序、迁移幂等、模型严格性、测试隔离与中文注释
首次回报: 已成功派发，等待可验证结论
最近可验证进展: 正在检查最终生产实现和 29 条专项证据
状态: COMPLETED（最终质量/安全复审无 Critical/Important，批准 Task 2）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6fe6-02a3-7830-9d42-6d027a5a9892 / Task 2 最终规格复审
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 2 Design/Plan、D-114/D-117、models/store/SQL/tests
预期交付物与测试: 检查五事实、三视图、append-only、作用域、版本、幂等、lease/fencing 与范围边界
首次回报: 已成功派发，等待首次可验证结论
最近可验证进展: 正在只读核对规格与 Task 2 diff
状态: COMPLETED（发现数据库时钟、幂等重放、Proposal lineage 与 scope 缺口，已由主模型修复）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6fe6-1712-7682-a078-c676e0334894 / Task 2 代码质量与安全复审
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 2 Python/SQL/tests 与当前 diff
预期交付物与测试: 检查并发锁序、事务原子性、SQL 约束、错误归一化、内存/PostgreSQL 等价与缺失测试
首次回报: 已成功派发，等待首次可验证结论
最近可验证进展: 正在只读检查事务、锁序、SQL 与测试
状态: COMPLETED（发现约束、NUL、事务与可读性缺口，已由主模型修复）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6fc9-a615-7341-8fc7-01dadec85a91 / Task 2 Store 与迁移模式分析
派发时间: 2026-07-17
只读或写入文件边界: 只读 PlanStore、Evaluation Store、Candidate Store、既有 SQL 与测试
预期交付物与测试: 推荐可复用的 append-only、幂等、lease/fencing、事务与 PostgreSQL 测试模式；不修改文件
首次回报: 建议根投影加五类事实表、根行锁、幂等优先、版本 CAS 和 lease/fencing
最近可验证进展: 结论已由主模型核对并用于首轮 GREEN；未修改文件
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6fc9-ba60-73c0-8542-5e095c906ce6 / Task 2 规格与状态机分析
派发时间: 2026-07-17
只读或写入文件边界: 只读 Phase 14 Design/Plan、D-113 至 D-120 和相关领域模型
预期交付物与测试: 给出五类事实、Workspace 三视图、版本/幂等/操作员锁/fencing 的最小冻结 API 与测试矩阵；不修改文件
首次回报: 固定六个模型、单向三视图、append/get/list 与 PostgreSQL 等价测试矩阵
最近可验证进展: 结论已由主模型核对；Task 3-6 行为保持在范围外；未修改文件
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6fac-8994-7c12-b723-51b9309c1f9b / Task 1 规格审查
派发时间: 2026-07-17
只读或写入文件边界: 只读 Phase 14 Design/Plan、D-113/D-116/D-117/D-120 与当前 diff
预期交付物与测试: 按严重度报告默认路由、权限、evidence-only、no-fallback 和启动冻结缺口
首次回报: 发现伪造 OperatorDecision、非原子终态与旧 API 注释问题
最近可验证进展: 修复后复审无 Critical/Important；两个 Minor 注释已同步修正
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f6fac-bbdc-7230-873c-2c294a15bdb9 / Task 1 代码质量与安全审查
派发时间: 2026-07-17
只读或写入文件边界: 只读 Task 1 生产代码、测试与当前 diff
预期交付物与测试: 按严重度报告路由绕过、旧 checkpoint、持久化、fallback、兼容与测试风险
首次回报: 发现旧 checkpoint、Planner fallback、TypeError 重试与原子写入风险
最近可验证进展: 真实 InMemorySaver checkpoint 绕过已补测试修复；最终复审无 Critical/Important/Minor
状态: COMPLETED
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f714c-6b3c-77a2-a664-b5f7fa9b4096 / Task 8 前端规格与代码质量审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 front/index.html、Task 8 Dashboard contract 测试和 Phase 14 Design/Plan；禁止修改文件
预期交付物与测试: 核对三视图、运营权限、对账/降级/重连、会话隔离、API 端点、记忆/结果回放和移动布局
首次回报: 发现 1 Critical、6 Important、2 Normal；主模型已按报告新增 RED/GREEN 并修复
最近可验证进展: 第二轮复审仅提出旧 HTTP 响应竞态、Proposal 重连门控、DEGRADED 恢复和结果渲染；均已修复并由主模型重跑 `6 passed`、相关聚合 `60 passed`
状态: COMPLETED（初审发现已整改；最终新线程未返回可验证结论，已关闭并由主模型复核）
接管原因（如适用）: 无

Sub-agent ID / 角色: 019f715e-1a2d-73d2-9f51-2ebf0059bdeb / Task 8 最终只读复审
派发时间: 2026-07-18
只读或写入文件边界: 只读 front/index.html、Task 8 Dashboard contract 测试和 Phase 14 Design/Plan；禁止修改文件
预期交付物与测试: 复核无方案决定门控、session 隔离、状态恢复、三视图权限和移动布局
首次回报: 两次等待均未返回可验证报告
最近可验证进展: 主模型已独立重跑 Task 8 专项、完整 unit/integration、JavaScript 语法和静态门禁；未采用未返回的子任务结论
状态: STOPPED / NO_VERIFIED_REPORT
接管原因（如适用）: 子任务未在可用时间内收敛；主模型接管最终审查并关闭线程

Sub-agent ID / 角色: 019f7182-41f5-7bc2-9823-58632112de85 / Task 9 最终规格与安全复审
派发时间: 2026-07-18
只读或写入文件边界: 只读 review_feedback.py、PromotionPolicy、Candidate Store、Task 9 DDL 和测试；禁止修改文件
预期交付物与测试: 复核人工确认意图、可信 Trace Resolver、active conflict、CAS/幂等/重启恢复和 Manifest
首次回报: 两次等待未返回可验证报告
最近可验证进展: 主模型已独立取得 Task 9/Phase 13 相关 unit `34 passed`、integration `4 passed`，完整 unit `1300 passed`、integration `150 passed`
状态: STOPPED / NO_VERIFIED_REPORT
接管原因（如适用）: 两次等待均未返回可验证报告；主模型已独立复核实际差异并重跑专项、完整 unit/integration，未采用未验证结论
```

## 4. 当前关键不变量

- PlanStore 是执行事实权威源，checkpoint 只保存引用。
- 不得因 checkpoint 领先而补造 NodeRun 或外部业务证据。
- 不得在同次 Runtime/PlanEngine 调用失败后 fallback Legacy。
- `TRUSTED_COMPAT` 必须在 Phase 12A Acceptance 前退役。
- PlanEngine 和 Orchestrator 默认是确定性组件。
- Agent 候选必须和确定性基线对照，严重安全违规必须为 0；人机协同 Copilot 不得代替高风险运营决定。
- Phase 14 真实模型预算上限为 1.00 元，Phase 15 Release 预留 0.60 元，项目规划上限为 4.00 元。
- 可信售罄的冻结/CAS/陈旧执行阻断可自动完成；备品、提示、优先级和恢复时机必须由 OperatorDecision 确认。

## 5. 最近验证证据

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
| Phase 12A Task 6 相关回归 | `59 passed` |
| Task 6 后默认单元测试 | `816 passed, 4 warnings` |
| Task 6 后完整集成测试 | `77 passed, 3 deselected, 5 warnings` |
| Task 6 提交与推送 | `6029ad3`，`origin/main=6029ad3` |
| Phase 12A Task 7 专项 | `9 passed` |
| Task 7 后默认单元测试 | `824 passed, 4 warnings` |
| Task 7 后完整集成测试 | `78 passed, 3 deselected, 5 warnings` |
| Task 7 提交与推送 | `7cbf026`，`origin/main=7cbf026` |
| Phase 12A Task 8 专项 | `31 passed`，生产 `TRUSTED_COMPAT` 0 命中 |
| Task 8 后默认单元测试 | `824 passed, 4 warnings` |
| Task 8 后完整集成测试 | `78 passed, 3 deselected, 5 warnings` |
| Task 8 提交与推送 | `9a8e5a6`，`origin/main=9a8e5a6` |
| Phase 12A Task 9 Demo 专项 | `4 passed`，直接脚本五行 JSON |
| Phase 12A 单元聚合 | `259 passed` |
| Phase 12A PostgreSQL/PostgresSaver 聚合 | `14 passed` |
| Phase 12A 最终全量回归 | `906 passed, 3 deselected, 9 warnings` |
| Task 9 静态门禁 | migration dry-run 与 diff 退出码 `0`；编码扫描仅既有 `4 errors/58 warnings` |
| Phase 12A Task 9 提交与推送 | `c88efdf`，`origin/main=c88efdf` |
| Phase 12B Task 1 专项/共享回归 | `43 passed` / `106 passed` |
| Phase 12B Task 1 完整验证 | unit `859 passed`；integration `78 passed, 3 deselected` |
| Phase 12B Task 1 静态门禁 | 11 文件严格 UTF-8、compileall、边界扫描、diff 通过；编码扫描仅既有 `4 errors/58 warnings` |
| Phase 12B Task 1 提交与推送 | `d794ff3`，`origin/main=d794ff3` |
| Phase 12B Task 2 专项/公共聚合 | `16 passed` / `94 passed` |
| Phase 12B Task 2 完整验证 | unit `875 passed`；integration `78 passed, 3 deselected` |
| Phase 12B Task 2 静态门禁 | 8 文件严格 UTF-8、compileall、diff 通过；编码扫描仅既有 `4 errors/58 warnings` |
| Phase 12B Task 2 提交与推送 | `8b1600b`，`origin/main=8b1600b` |
| Phase 12B Task 3 RED/GREEN | `11 failed`；迁移 `6 passed`；PostgreSQL 专项 `6 passed` |
| Phase 12B Task 3 完整验证 | unit `881 passed`；integration `84 passed, 3 deselected` |
| Phase 12B Task 3 静态门禁 | 12 文件严格 UTF-8、11 方法签名等价、compileall 与 diff 通过 |
| Phase 12B Task 3 提交与推送 | `25793f2`，`origin/main=25793f2` |
| Phase 12B Task 4 RED/GREEN | `9 failed`；unit `9 passed`；真实 Kafka/PostgreSQL `2 passed` |
| Phase 12B Task 4 当前完整验证 | unit `890 passed`；integration `86 passed, 3 deselected` |
| Phase 12B Task 4 编码与静态门禁 | compileall/diff 通过；全仓历史 `4 errors/56 warnings`，目标命中 0 |
| Phase 12B Task 4 提交与推送 | `0762c2c`，`origin/main=0762c2c` |
| Phase 12B Task 5 RED/GREEN | `10 failed`；Task 聚合 `10 passed` |
| Phase 12B Task 5 相关回归 | Phase 12A Store/状态机/迁移/PostgreSQL `155 passed` |
| Phase 12B Task 5 最终专项 | `16 passed`，包含 superseded 禁止重试/回收与局部失败隔离 |
| Phase 12B Task 5 完整验证 | unit `900 passed`；integration `92 passed, 3 deselected` |
| Phase 12B Task 5 静态门禁 | 12 文件严格 UTF-8、compileall、migration dry-run、diff 通过；历史编码 `4 errors/56 warnings`，目标命中 0 |
| Phase 12B Task 5 提交与推送 | `375b671`，`origin/main=375b671` |
| Phase 12B Task 6 RED/GREEN | RED：unit `16 failed, 51 passed`，集成因缺模块收集失败；GREEN：专项 `64 passed` |
| Phase 12B Task 6 完整验证 | unit `911 passed, 4 warnings`；integration 全套退出码 0、无失败输出 |
| Phase 12B Task 6 提交与推送 | `9d4bf97`，`origin/main=9d4bf97` |
| Phase 12B Task 7 RED/GREEN | 输入/Proposal RED `2 failed`；Capability/Store RED `3 failed`；Worker RED `2 failed`；最终专项 `19 passed` |
| Phase 12B Task 7 完整验证 | unit `922 passed, 4 warnings`；integration `95 passed, 3 deselected, 5 warnings` |
| Phase 12B Task 7 并发与安全审查 | 双连接 global claim、固定 DAG 门禁、迟到冲突二次验证、迁移前 CARD_BATCH 兼容均通过 |
| Phase 12B Task 8 RED/GREEN | 首个 RED `2 failed`；最终 Replan unit `8 passed`；PostgreSQL 双 Worker CAS `1 passed` |
| Phase 12B Task 8 完整验证 | unit `930 passed, 4 warnings`；integration `96 passed, 3 deselected, 5 warnings` |
| Phase 12B Task 8 恢复与审查 | Application 部分补偿、复用链、Store 锁内 superseded 复核、版本输入冻结和 source version 门禁均通过 |
| Phase 12B Task 9 RED/GREEN | 初始 RED `7 failed, 21 passed`；安全审查整改 RED `7 failed`；最终专项 `124 passed`；生产 ToolRegistry import `0` 命中 |
| Phase 12B Task 9 完整验证 | unit `943 passed, 4 warnings`；integration `96 passed, 3 deselected, 5 warnings`；独立复核无阻断或重要项 |
| Phase 12B Task 10 RED/GREEN | RED `4 failed`；Coordinator/Harness/API 专项最终 `141 passed`；生产路由、证据摘要和 no-fallback 门禁通过独立复核 |
| Phase 12B Task 10 完整验证 | unit `957 passed, 4 warnings`；integration `97 passed, 3 deselected, 5 warnings`；PostgreSQL/EventStore/Harness 聚合 `141 passed` |
| Phase 12B Task 10 提交与推送 | `e6f3414`，`origin/main=e6f3414` |
| Phase 12B Task 11 Demo | `3 passed`；八场景 CLI 与固定 Trace/报告均退出码 0 |
| Phase 12B Acceptance 聚合 | unit `104 passed`；integration `19 passed`；全仓 `1057 passed, 3 deselected, 9 warnings` |
| Phase 13 JIT Gate | Design/Plan 已审核；D-100..D-108 已持久化；业务实施未授权 |
| Phase 13 文档验证 | 9 个目标文件严格 UTF-8 通过；决策 108 项连续完整；全仓仅既有 `4 errors/53 warnings` |
| Phase 13 Task 1 RED/GREEN | 初始缺模块 RED；审查回归最高 `9 failed`；最终专项 `30 passed` |
| Phase 13 Task 1 审查 | 规格与代码质量复审均无 Critical/Important/Normal 阻断项 |
| Phase 13 Task 2 RED/GREEN | 初始缺模块 RED；审查回归最高 `5 failed`；Task 1+2 最终 `50 passed` |
| Phase 13 Task 2 审查 | 规格无阻断；质量无 Critical/Important，2 项 Minor 已记录 |
| Phase 13 Task 3 RED/GREEN | 初始缺模块 RED；审查回归覆盖公共池/NaN/FK/精度；最终专项 `19 passed` |
| Phase 13 Task 3 审查 | 规格与质量复审无 Critical/Important 阻断项 |
| Phase 13 Task 4 RED/GREEN | 初始缺模块 RED；多轮审查整改后 Runner `47 passed`，SkillExecutor/预算聚合 `61 passed` |
| Phase 13 Task 4 完整验证 | Phase 13 Task 1-4 `109 passed`；unit `1071 passed, 4 warnings`；integration `104 passed, 3 deselected, 5 warnings` |
| Phase 13 Task 4 安全边界 | 完整请求计价、发送前 Token 限制、稳定 Task 执行身份、费用超额如实入账、Evidence/fallback 审计与取消恢复均已覆盖 |
| Phase 13 Task 5 RED/GREEN | 首轮缺模块 RED；最终独立指标、claim、终态、候选唯一性和完成数门禁专项 unit `30 passed` |
| Phase 13 Task 5 PostgreSQL | `8 passed`；覆盖并发 claim/选择、lease/fencing、Manifest 不可更新、候选级终结和迁移重启 |
| Phase 13 Task 5 完整验证 | unit `1101 passed, 4 warnings`；integration `112 passed, 3 deselected, 5 warnings`；真实模型费用 0 元 |
| Phase 13 Task 6 完整验证 | unit `1121 passed, 4 warnings`；integration `112 passed, 3 deselected, 5 warnings`；真实模型费用 0 元 |
| Phase 13 Task 7 专项/相关回归 | unit `17 passed`；PostgreSQL 恢复 `1 passed`；Harness/Preemption/Store/权限聚合 `182 passed` |
| Phase 13 Task 7 完整验证 | unit `1138 passed, 4 warnings`；integration `113 passed, 3 deselected, 5 warnings`；退出码均为 0 |
| Phase 13 Task 7 审查 | infrastructure 失败半 pair Important 已补 RED 修复；规格与质量复审无剩余 Critical/Important |
| Phase 13 Task 7 提交与推送 | `4b26a31`，`origin/main=4b26a31` |

表中前八项保留进入正式实施前的基线，后续各项按 Task 6-9 的提交与验收顺序追加。

## 6. 用户已有未提交文件

以下文件不属于本轮交付，不得覆盖、还原或提交：

- `docs/project_guidance/agent_runtime_context_recovery_prompt.md`
- `docs/project_guidance/current_project_status_and_agent_roadmap.md`
- `docs/superpowers/reports/phase-11a-skill-runtime-acceptance.md`
- `docs/superpowers/specs/phase-11a-skill-runtime-design.md`
- `docs/development_pitfalls.md`
- `scripts/patch_run_all.py`
- `scripts/tmp_gen_story.py`

## 7. 正式实施后的更新格式

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

每次派发 sub-agent 还必须追加：

```text
Sub-agent ID / 角色:
派发时间:
只读或写入文件边界:
预期交付物与测试:
首次回报:
最近可验证进展:
状态: RUNNING | REVIEWING | COMPLETED | STOPPED | TAKEN_OVER
接管原因（如适用）:
```

监控规则：首次回报、核心 GREEN 和提交前必须由主模型检查实际 diff 与测试；二十分钟无可验证进展、连续两次同一阻塞、越界或建议放宽安全/预算/指标时立即停止并接管。每个 Task 提交前不得保留运行中的 sub-agent。

更新时机固定为：

1. Task 开始前。
2. 每次 sub-agent 派发、首次回报、停止或接管时。
3. RED 失败符合预期后。
4. 核心 GREEN 后。
5. 规格或质量审查发现需要整改时。
6. 全部验证完成、准备提交时。
7. 推送成功并切换到下一 Task 时。

## 8. 三次失败协议

- 第一次：记录原始错误，定位根因并做最小修复。
- 第二次：不得重复同一操作，改用不同诊断或实现路径。
- 第三次：重新检查设计假设、决策日志和相关事实源。
- 三次后仍无法推进：写明阻塞证据；只有外部状态或用户决策确实不可替代时才暂停。

## 9. 压缩后恢复顺序

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

## 10. Phase 14 Task 10 Sub-agent 留痕

```text
Sub-agent ID / 角色: 019f71a6-421e-73f0-9a40-ae6e93bafa34 / Task 10 规格与质量只读审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 evaluation.py、Task 10 测试/冻结数据、Phase 13 Manifest、Phase 14 Design/Plan；禁止修改文件
预期交付物与测试: 检查事故维度覆盖、Manifest 身份绑定、配对指标数学、脱敏和生产边界
首次回报: 两次等待未返回；后续返回一份包含 5 个 Important 和 2 个 Minor 的审查报告
最近可验证进展: 主模型逐项整改后重跑 Task 10 专项 `9 passed`、数据/Phase 13 回归 `20 passed`、完整 unit `1310 passed`、integration `150 passed`
状态: STOPPED / COMPLETED_REPORT_CONSUMED
接管原因（如适用）: 审查线程未在提交前稳定收敛，主模型停止线程并独立复查所有发现；报告中 Critical 为 0，5 个 Important 已全部修复并重新验证
```

## 2026-07-18 Phase 15 Task 8 COMMIT/PUSH 与 Task 9 RED

- Task 8 已以 `d2d4c89 build: add local phase 15 release gates` 提交并推送，`origin/main=d2d4c89`。
- 用户已有脏文件和无关临时脚本保持 unstaged；连续游标进入 Task 9。
- Task 9 先验证三层 workflow 的触发条件、运行环境、case split、secret 暴露和 artifact retention，真实 GitHub Actions run evidence 仍不能伪造。

## 2026-07-18 Phase 15 Task 11 RED / GREEN

- Task 10 已以 `1f4af05 refactor: retire tool registry facade` 提交并推送，`origin/main=1f4af05`。
- Task 11 RED 为缺少 `src.release_gates.routing` 的收集失败；新增 `ReleaseRouteProfile`、Settings profile/promotion 字段、三路 `from_settings` 解析和 D-133 后，专项已 `5 passed`。
- Sub-agent 本 Task 未派发；现有并发额度已满，主模型负责实现和审查，真实外部 Release 与模型仍未调用。

## 2026-07-18 Phase 15 Task 10 VERIFY / REVIEW

- 删除 `src/config/tool_registry.py`；`AgentToolExecutor` 删除 `registry` 位置/关键字兼容参数，只接受启动冻结 `SkillPolicyView`。
- 生产源码 `rg -n "ToolRegistry|get_default_tool_registry|src\\.config\\.tool_registry" src` 无命中；旧测试和 Phase 3A Demo 已迁移到 Catalog/SkillPolicyView。
- Task 10 专项 `21 passed`；完整 unit `1372 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`；目标 compileall、Manifest 重建、生产 Facade 扫描和 `git diff --check` 通过；真实模型、外部 GitHub 和生产副作用未调用。
- Sub-agent 监控留痕：

```text
Sub-agent ID / 角色: 019f7351-ba35-7701-91d0-d53ba72baa6d / Task 10 Facade 退役规格与安全只读审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 src/config/tool_registry.py 删除差异、src/core/agent_tool_executor.py、src/skill_runtime/policy_view.py、迁移测试和 Phase 15 Design/Plan；禁止修改文件
预期交付物与测试: 检查生产 import 为零、旧参数消失、Catalog/PolicyView 单一事实源和 no-fallback 路径
首次回报: 返回 0 Critical、4 Important；报告生成于主模型整改前
最近可验证进展: 主模型修复售罄幂等键 Context 化、Legacy 异常摘要脱敏和 README 退役说明，并独立重跑 `21 passed`、unit/integration 全量
状态: COMPLETED_REPORT_CONSUMED / STOPPED
接管原因（如适用）: 4 项 Important 中 3 项已整改；PolicyView 注入项属于 D-121 同进程可信装配边界，旧 Flow 测试替身需要显式门禁差异，未新增不安全生产旁路
```

## 2026-07-18 Phase 15 Task 9 GREEN / REVIEW

- 新增三层 GitHub Actions workflow；契约专项 `3 passed`，只做 YAML 静态解析，不连接 GitHub 或外部服务。
- PR 固定 Python 3.12/PostgreSQL 15/36 非 holdout/14 天 artifact；Nightly 固定 PostgreSQL/Kafka/36 非 holdout/30 天 artifact；Release 固定 tag 或手动触发、保护环境、48 full case/180 天 artifact。
- Release workflow 仍会通过本地 CLI 的外部 evidence 门禁 fail-closed；当前没有伪造托管 run evidence，也未启动真实模型。
- Sub-agent 监控留痕：

```text
Sub-agent ID / 角色: 019f7340-bf4d-75e1-b9c7-4d6058c7c005 / Confucius / Task 9 workflow 规格与安全只读审查
派发时间: 2026-07-18
只读或写入文件边界: 只读 .github/workflows/*.yml、Task 9 contract tests、Phase 15 Design/Plan；禁止修改文件
预期交付物与测试: 检查触发器、权限、Python/PostgreSQL/Kafka、36/48 split、secret 和 retention
首次回报: 返回 0 Critical、5 Important；报告生成于主模型整改前
最近可验证进展: 主模型补齐 Release coverage/DSN/evidence、Kafka 探活、PostgresSaver 专项和顶层权限/trigger 测试；workflow contract `3 passed`
状态: COMPLETED_REPORT_CONSUMED / STOPPED
接管原因（如适用）: 报告中的可修复 Important 已全部整改并重跑；tag ruleset 属 GitHub 仓库外部配置，明确记录为未由 YAML 强制的外部门禁
```

Task 9 审查整改摘要：

- Release workflow 生成 coverage，使用本地 PostgreSQL DSN，读取受保护的 Actions evidence JSON，并把身份参数传入同一校验器；缺 secret/evidence 仍 fail-closed。
- Nightly/Release 增加 Kafka/Zookeeper 端口探活和官方 PostgresSaver 集成测试入口。
- 契约测试锁定三层顶层与 job `contents: read` 权限、PR/Nightly 不接收 pull request 触发、Release 只接受 tag/手动触发。
- `phase15-release-*` tag 创建权限/保护 ruleset 需要在 GitHub 仓库设置中配置，属于本地代码无法替代的外部验收证据。

## 2026-07-18 Phase 15 Task 9 VERIFY

- Workflow contract `3 passed`；完整 unit `1375 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`，退出码均为 0。
- 目标 YAML 严格 UTF-8/LF/BOM/replacement/trailing whitespace、YAML parse、敏感扫描、迁移 dry-run 和 `git diff --check` 均通过。
- Task 9 已补齐 Release coverage/DSN/protected evidence、Nightly/Release Kafka readiness 和 PostgresSaver 专项；真实 GitHub Actions run、environment secret 和 tag ruleset 尚未取得，保持外部 `BLOCKED`。
- 当前只暂存 Task 9 文件，准备提交 `ci: add hosted agent runtime gates`。

## 2026-07-18 Phase 15 Task 8 VERIFY

- Task 8 专项 `10 passed`；完整 unit `1371 passed, 4 warnings`；完整 integration `155 passed, 3 deselected, 5 warnings`，均为退出码 0。
- PR/Nightly 实际报告均为技术 `PASS`、36 个非 holdout case；Release 实际报告为 48 个 case、技术 `BLOCKED`、最终 `NOT_RELEASED`，原因包含数据库、覆盖率和 Actions evidence 缺失。
- `compileall`、迁移 dry-run、敏感扫描、目标 13 文件严格 UTF-8/LF/BOM/replacement/trailing whitespace、`git diff --check` 均通过；仓库历史文档扫描为既有 4 errors/52 warnings，未归因于本 Task。
- 当前只暂存本 Task 文件，保留用户已有脏文件和无关临时脚本；准备提交 `build: add local phase 15 release gates`。
