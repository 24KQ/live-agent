# LiveAgent 架构定位说明（architecture positioning）

> 本文档回答三类常见追问：**这是不是一个 Agent 项目？架构上的边界为什么这样切？
> 哪些是刻意设计、哪些是已知局限？** 面向项目展示与面试语境，2026-08-02 定稿。
> 与 codex（原始开发者）第十一轮至十三轮讨论后按一致结论整理。

## 1. 一句话定位

> **LiveAgent：受治理、有限循环、工具调用、人工审批协同的直播运营 Agent 系统。**

它不是通用自主 Agent（不追求无上限的自主探索），不是聊天机器人（有明确的业务闭环），
也不是玩具项目（治理深度见 §5）。面向应届 Agent 岗求职的生产导向、人机协同直播运营
Agent 项目：Phase 16/V9 工程实现和受控验证完成，但不宣称已经生产部署或具备通用自主 Agent 能力。

## 2. 三种架构形态的区分（播前 / 播中 / 双 Agent）

| 阶段 | 形态 | 架构边界与设计理由 |
|:--|:--|:--|
| 播前（排品/手卡） | **Workflow**（LangGraph Workflow + RulesPlanner，确定性流程） | 输入确定、步骤固定、无模型决策点；用 Agent 反而引入不可控性 |
| 播中（弹幕/告警 → 建议 → 人审） | **bounded Agent**（有限模型/Skill 执行循环 + Harness 图，见 §3） | 有模型决策点，但循环有硬上限（见 §3） |
| 高冲突证据场景 | **受控双 Agent**（EvidenceAnalyst → DecisionPlanner 顺序编排） | 分析与决策职责分离；受控顺序 + 人审，不做自主辩论（见 §4） |

一句话：**流程确定用 Workflow，有决策点用 bounded Agent，高风险决策加人审，不用
Agent 的地方坚决不用**——这是刻意的架构克制，不是能力缺失。

## 3. Agent loop 与它的上限（为什么必须有上限）

播中的 bounded Agent 由两个运行入口共同体现：`BoundedSpecialistRunner`
（`src/specialist_runtime/runner.py` 的有限模型/Skill 执行循环）与
`on_live_harness_agent_graph.py`（带工具观察和 replan 的 Harness 图）——它们是
同一设计原则的两个实例，不是同一个运行入口。

核心循环在 `BoundedSpecialistRunner.run()`：
`for model_index in range(profile.max_model_calls)`——模型调用 → 动作解析 → 经
Skill port 执行 → 证据绑定 → 结构校验 → 继续或终止。每个 agent 的边界由
`src/specialist_runtime/profiles.py` 的 `SpecialistProfile` 冻结：

| 上限字段 | 作用 |
|:--|:--|
| `max_model_calls` | 模型调用轮数硬上限（防失控循环） |
| `max_skill_calls` | Skill 调用次数上限 |
| `max_total_tokens` / `max_output_tokens` | 上下文与输出预算 |
| `deadline_seconds` | 单任务截止时间 |
| `max_case_cost_cny` | 单 case 预算（先预留后结算，防超支） |
| `allowed_skill_ids` / `skill_versions` | 权限白名单 + 版本冻结 |

**为什么必须有上限**：Agent 的收益来自"模型决策点"，风险也来自同一个地方——无上限的
循环意味着无上限的成本、延迟与不可审计行为。上限 + 预算 + append-only 账本，把
"自主性"变成"受治理的有限自主"，这是运营类产品与 demo 的分界线。

## 4. Analyst → Planner：为什么是受控顺序编排，不是自主辩论

`src/decision_support/multi_agent.py`：`evidence_analyst` 分析证据（弹幕/库存/售罄/
价格冲突），产出结构化分析；`decision_planner` 基于分析产出决策方案（改价/替补推荐/
弹幕回复）；Coordinator 协调两阶段，超阈值自动升级（Escalation）。

选择受控顺序编排的理由（有意设计，非能力缺失）：

1. **可审计性**：每阶段产出有 schema 契约（`result_schema_hash`，`additionalProperties:
   false`）；模型收到的是经过 Resolver 身份、摘要和作用域校验的**冻结证据投影**
   （`resolved_evidence`），不能自行查询 Store、不能修改权威事实、也不能伪造通过
   校验的 EvidenceRef；
2. **可拦截性**：两阶段之间、人审节点前都有确定性校验与门禁，高风险动作不自动执行；
3. **业务形态**：运营决策要的是"可解释、可拦截、可回滚"的建议，不是"两个模型争论
   出一个结论"；自主辩论提高自主性的同时提高不可审计性与不可预测性，与场景目标相反。

已知局限（如实声明）：这不是多 Agent 自主辩论或长期协商；分歧检测/对抗验证属于
功能扩展，不在当前目标内。

## 5. Skill、Tool、普通函数：边界与治理

- **Skill**（`src/skill_runtime/catalog.py`，17 个 SkillManifest：`query_products` /
  `suggest_price_change` / `set_product_price` / `handle_sold_out_event` /
  `generate_danmaku_reply` / `retrieve_anchor_memory` 等）：**可被 Agent 调用的
  能力单元**，带生命周期（PRE/LIVE/POST）× 风险等级（LOW/MEDIUM/HIGH）× 门禁
  （AUTO/SOFT_GATE/HARD_GATE）三重治理，executor 执行、attempt_store 审计；
- **Tool**（`src/core/agent_tool_executor.py`、`src/memory/tool_mask_policy.py`）：
  基于 trust_score 的工具可见性 mask，控制 agent 能"看到"哪些工具——权限与
  上下文窗口的治理层；
- **普通函数**：无模型参与、无审计要求的确定性实现，不允许绕过 Skill 治理被
  Agent 直接调用。

边界判据：**模型能调用的必须是受治理的 Skill；Skill 必须注册在 Catalog 并带
lifecycle/risk/gate；高风险 Skill 必须过 HARD_GATE 人审。** 模型输出防伪造：
输出 schema 冻结 + EvidenceRef 身份绑定 + 动作证据归属校验
（`runner.py` 对 `action.evidence_refs` 的归属校验）。

## 6. Agent 图分层（为什么有两个图）

- `src/core/on_live_agent_graph.py`：**基础图**——骨架与测试路径（collect →
  planner → route → execute → observe → audit，含 FALLBACK 降级路由），
  `_DefaultPlanner` 为测试/快速验证用；
- `src/core/on_live_harness_agent_graph.py`：**Harness 图**——生产执行路径，
  完整 replan（`src/plan_engine/`：preemption / failure_policy / emergency /
  replan / proposal）。

**受治理的完整 Harness 执行路径，作为未来生产候选路径；当前未在生产部署**；
基础图仅骨架与测试，不作为生产入口。这是分层设计，不是功能缺失。

## 7. 记忆系统：受治理的检索与候选存储，不是自主学习

`stage_memory_candidates` / `retrieve_anchor_memory` / `review_memory.py` 构成
**受治理的记忆管线**：证据约束（只接受经校验的播后证据）、脱敏、幂等键、
SOFT_GATE 门禁、信任评分（trust_score 影响工具可见性）。

**如实定位**：这是"受治理的检索与候选存储"，不是模型在线学习/自我内化。
安全走在能力前面是刻意的工程顺序——先证明记忆过程可审计、可回滚，再谈学习能力。

## 8. 默认路由 DETERMINISTIC_ONLY（安全降级设计）

默认路由为 `DETERMINISTIC_ONLY`：确定性规则引擎先执行（售罄保护等），仅在高冲突
证据满足门槛时顺序运行 Analyst → Planner，产出建议交由人审；模型不可用时系统
按规则引擎安全降级，业务仍可运行。LLM 生产自动路由**未放开**（见诚实边界）。

## 9. 诚实边界汇总（与 README「诚实边界」节一致）

- V9 = 开发/验证阶段资格认证，不等于原始 V5 官方 DeepSeek 契约 PASS，不等于生产就绪
- Phase 17 holdout 30 例（**计划中的、尚未执行**）= 预声明工程验收线（90%），
  不是统计显著性声明；只验证冻结的 Phase 16 Analyst→Planner 受控链路
- 真实平台 API / 真实经营副作用 / 业务 KPI / 生产运维：不在本版本范围内，未进行
  生产就绪声明
- 基础图模拟工具路径、记忆非自主学习、Analyst→Planner 非自主辩论：见上文各节

## 10. 相关文件索引

- Agent 定义：`src/specialist_runtime/profiles.py`
- Agent loop：`src/specialist_runtime/runner.py`（`BoundedSpecialistRunner.run()`）
- 双 Agent 编排：`src/decision_support/multi_agent.py`
- Skill 系统：`src/skill_runtime/catalog.py`、`src/skill_runtime/executor.py`
- 工具治理：`src/core/agent_tool_executor.py`、`src/memory/tool_mask_policy.py`
- Agent 图：`src/core/on_live_agent_graph.py`、`src/core/on_live_harness_agent_graph.py`
- 记忆管线：`src/specialist_runtime/review_memory.py`、`src/skill_runtime/catalog.py`（记忆相关 skill）
- 验证证据链：README「验证证据链」节、`docs/superpowers/reports/phase-16-v9-contract-approval-record.md`
