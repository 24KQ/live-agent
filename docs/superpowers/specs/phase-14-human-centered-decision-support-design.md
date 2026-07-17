# Phase 14：三场景人机协同决策支持设计

文档状态：`REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`

实施前置：本文件及对应 Implementation Plan 已完成用户审核。Phase 14 业务代码、数据库迁移、Web UI、真实模型调用和人工对照，必须获得单独实施授权后才能开始。

## 1. 定位与边界

LiveAgent 的业务范围固定为播前、播中、播后三场景。Phase 14 将项目定位为：

> 面向直播电商播前、播中、播后三场景的人机协同决策支持与受控执行 Runtime。

确定性系统负责事实验证、保护动作、权限、幂等、恢复、审计和执行；运营主控保留经营决策权；受限 Agent 仅归纳证据并生成可审计方案。三个业务场景不机械等同于三个 Agent。首期正式实现一个播中 Copilot，播前和播后以确定性流程加人工协同完成闭环，并通过统一 Profile Registry 为未来 Specialist 扩展预留接口。

本期不接入真实淘宝 API、自由 A2A、动态 handoff、共享 scratchpad、UI 插件或热加载。Phase 13 的 `LiveOpsAgent=REJECTED`、`PlannerAgent=INCONCLUSIVE`、`ReviewMemoryAgent=INCONCLUSIVE` 仍是不可改写的自主候选评估结论；本期的 `live_ops_decision_support@1.0.0` 是目标、输出和指标均不同的人机协同 Profile，不复活任何被拒绝候选。

## 2. 三场景统一工作台

`LiveSessionWorkspace` 以稳定 `live_session_id` 关联 `room_id`、`trace_id`、PlanRun、Event Inbox、DecisionTrace、Replay 和 Evaluation 身份，并只呈现三个确定性视图：

- `PREPARE`：冻结商品/计划、可读取记忆锚点、风险清单和运营准备确认。
- `LIVE`：可信事件、库存/备品事实、弹幕聚合、主播节奏、方案比较、人工决定和执行状态。
- `REVIEW`：事实回放、方案与人工决定的差异、结果反馈、记忆候选资格与确认。

同一工作台只能使用有来源、版本、时间戳和摘要的 `EvidenceRef`。Agent 不可读取任意 Store；业务写操作不得由自然语言或 Agent 动作直接触发。

## 3. 播中复合售罄事故

首个深切片固定为 `live-session-p001-sold-out-v1` 的复合变体：可信售罄事件与备品冲突、弹幕噪声、主播节奏同时进入工作台。

可信售罄事件到达后，Phase 12B 既有确定性控制面可自动冻结受影响计划、CAS 标记售罄、阻断陈旧节点和进入严格只读对账。备品选择、主播提示、优先级和恢复时机属于经营决策，必须等待运营主控。`SIDE_EFFECT_UNKNOWN` 不得被建议文本掩盖，必须保持 `WAITING_RECONCILIATION` 或人工处理。

`EvidenceBundle` 是不可变快照，至少包含事件/来源验证、商品与库存版本、根计划/紧急计划引用、弹幕主题聚合、节奏信号、证据时间和输入指纹。证据来源冲突、超时、作用域不一致或摘要不匹配时，方案生成 fail-closed，但确定性保护不回滚。

## 4. Copilot 与人工决定协议

`live_ops_decision_support@1.0.0` 使用现有 `BoundedSpecialistRunner`、`AgentTask`、`AgentResult`、`EvidenceRef` 和 Profile Registry。它最多执行两次模型调用、三次白名单只读 Skill、4000 tokens、五秒绝对 deadline；没有写 Skill、没有 Agent 间调用、没有自由工具发现，也不会自动执行经营恢复。

`LiveDecisionProposal` 必须输出一至三个 `DecisionOption`。每个 option 使用封闭字段表达商品策略、可选备品、主播提示、执行时机、风险标记和 EvidenceRef；不得携带工具调用、SQL、自由执行指令或未校验商品 ID。模型失败、超时、预算/Schema/证据拒绝时，工作台显示 `DEGRADED`，提供确定性事实摘要；正式评估中该 fallback 记为 Copilot 失败。

运营端提交不可变 `OperatorDecision`：`APPROVE`、`MODIFY` 或 `REJECT`。`MODIFY` 只允许修改备品、提示语、优先级和时机，必须携带原因码、预期 proposal 版本、操作员身份和幂等键。确定性 Validator/Compiler 再生成关联的 PlanCommand 或 SkillCall；原始 proposal、人工修改、校验结果和最终命令分别留痕，任何一方不能覆盖另一方。

路由固定为启动冻结的 `DETERMINISTIC_ONLY | DECISION_SUPPORT`，生产默认 `DETERMINISTIC_ONLY`，Demo 显式选择 `DECISION_SUPPORT`。Phase 14 不切换默认值；该决定留给 Phase 15 Release Gate。

## 5. 记忆闭环

播后 Agent 或规则只能 `stage` MemoryCandidate。确定性规则先检查双独立 DecisionTrace、作用域、白名单、冲突和敏感字段；只有资格通过的候选才显示给运营。运营确认后才允许 PromotionPolicy 幂等写入 active memory，下一次 `PREPARE` 视图通过受治理 `retrieve_anchor_memory` 读取。运营不能强制晋升规则拒绝的候选，Agent 自由文本不得进入 active memory。

## 6. 评估与预算

完整自动回归使用 ScriptedModel。真实 `deepseek-v4-flash` 仅在 endpoint、价格、usage、Prompt、Schema、数据集和代码哈希预检通过后运行最多十个 smoke case；不保存 chain-of-thought。Phase 14 真实模型新增预算上限为 1.00 元，原有 0.60 元 Release 预算完整转移并保留给 Phase 15。

离线数据集覆盖复合售罄、备品冲突、噪声、节奏、证据过期、冲突、版本冲突和未知副作用。人工对照采用三至五名代理运营、四组等价场景、随机交叉顺序，总共二十四至四十次决策；它是可用性证据，不得表述为生产 A/B。

Phase 14 Acceptance 采用严格 AND：严重安全违规为零；安全正确决策率至少 90%；关键冲突漏报相对无 Copilot 条件下降至少 30%；决策中位耗时下降至少 20%。样本不足、外部模型证据不足或预算/基础设施阻断时结论为 `INCONCLUSIVE`，不得切换默认路由。

## 7. 验收边界

- 运营工作台可从播前进入播中，再进入播后，并产生可回放的同一业务闭环。
- 自动保护与人工经营决定被严格区分，所有执行均经过 Runtime/PlanEngine 权威路径。
- Agent 无法写平台状态、晋升记忆、绕过审批或调用其他 Agent。
- 结构化修改、并发操作员锁、幂等、版本冲突、对账和重启恢复均有内存与 PostgreSQL 证据。
- Phase 14 Acceptance 完成后状态必须为 `AWAITING_PHASE_15_GATE`，不得自动实施 Golden/CI/发布门禁。
