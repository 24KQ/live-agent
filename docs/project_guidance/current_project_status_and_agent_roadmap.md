# LiveAgent 当前项目状态与 Agent 化路线图

更新日期：2026-07-11

## 当前状态结论

LiveAgent 已完成从播前到播后再到记忆回写的基础业务闭环：

```text
播前排品
-> 建播 hard-gate
-> 播中弹幕 / 售罄 / 告警
-> 播后复盘
-> 记忆回写
-> 下一次播前建议受记忆影响
```

当前系统已经具备产品雏形，但 Agent 能力还没有充分发挥。更准确地说，当前项目是：

```text
规则业务系统 + LangGraph 编排骨架 + 少量 LLM 能力 + 记忆/审计/安全体系
```

下一阶段应从“继续堆业务能力”切换到“LangGraph Agent 化改造”。

## 2026-07-11 更新：Agent 化路线修正

重新对照 `docs/study/taobao_anchor_agent_harness.md` 后，项目的 Agent 化重点不应是裸 ReAct，而应是 Harness 工程：

```text
Execution Loop
Tool Registry
Context Manager
State Store
Lifecycle Hooks
Evaluation Interface
```

Phase 5G-B 已按这个方向新增 LangGraph Harness Agent Loop。它不是普通 `while LLM -> tool -> LLM`，而是把关键控制点拆成 LangGraph 节点和条件边：

```text
load_context
-> pre_reasoning_hook
-> agent_reasoning
-> route_agent_decision
-> pre_tool_call_hook
-> route_tool_policy
-> execute_tool
-> post_tool_call_hook
-> observe_result
-> route_replan
-> write_audit
```

当前更准确的项目定位是：

```text
业务 Workflow 闭环 + LangGraph Harness Agent 播中核心 + 记忆/信任/审计/副屏工程底座
```

后续 Agent 主线应优先补：

1. Harness Agent 审计与 DecisionTrace 接入。
2. 高风险工具的 LangGraph interrupt 人审恢复。
3. WebSocket 副屏展示 Harness Agent 节点状态。

## 已完成能力

| 模块 | 当前状态 |
| --- | --- |
| 播前业务 | 已完成货盘查询、排品、手卡、合规摘要、建播 hard-gate |
| 播中业务 | 已完成售罄事件、备选推荐、弹幕聚合、Kafka 弹幕守护进程 |
| 播后业务 | 已完成归因、复盘、trust 更新、记忆回写 |
| 记忆层 | 已完成 L1/L2/L3、trust_score、衰减、冲突修正、语义检索 |
| LLM 能力 | 已接入 DeepSeek 手卡生成，失败时降级到模板 |
| LangGraph | 已完成播前 graph、PostgreSQL checkpoint、interrupt 人审恢复 |
| Web 副屏 | 已完成 FastAPI + HTML 副屏，主要端点已接真实 PostgreSQL 数据 |
| 审计与安全 | 已完成 ToolRegistry、SecurityHook、ToolCallAuditStore |

## 当前主要缺口

### 1. Agent 决策能力不足

当前多数流程仍是确定性代码串联，LLM 没有真正决定下一步做什么。现有 LangGraph 主要是线性 workflow：

```text
START
-> query_products
-> generate_live_plan
-> generate_product_cards
-> compliance_check
-> setup_live_session
-> END
```

它还没有充分体现：

- `add_conditional_edges`
- LLM planner 节点
- Tool Calling
- Reason -> Act -> Observe -> Replan
- 根据状态动态选择路径

### 2. LLM 集成稳定性不足

当前全量测试曾出现：

```text
test_deepseek_card_differs_from_template
```

失败原因是 LLM 手卡结果与模板一致，可能是 DeepSeek 调用失败后 fallback，也可能是 LLM 输出没有通过 schema 校验。这说明 LLM 目前更像增强能力，不是稳定 Agent 大脑。

### 3. 弹幕语义聚合仍待增强

Phase 4D 已经实现“只持久化聚合结果，不存原始弹幕”的正确成本模型。但聚合分类仍主要依赖关键词，面对 10w+ 直播间的“千人千语”表达时命中率不足。

建议后续采用：

```text
规则/关键词快速聚合
-> 未分类问题进入语义聚合
-> 必要时低频批量 LLM 兜底
```

而不是每条弹幕都调用 LLM。

## 推荐下一阶段：Phase 5A

建议 Phase 5A 定义为：

```text
LangGraph Agent Planner + Tool Calling + Conditional Edges
```

目标是把播前流程从“线性业务流水线”升级为“Agent 决策编排”。

### Phase 5A 目标链路

```text
START
-> collect_context
-> llm_planner
-> conditional_route
   -> retrieve_memory
   -> generate_plan
   -> generate_cards
   -> risk_check
   -> request_human_approval
   -> deterministic_fallback
-> observe_result
-> replan_or_finish
-> END
```

### Phase 5A 关键要求

- LLM planner 只能输出结构化 JSON，必须通过 Pydantic schema 校验。
- LLM 不直接写数据库，只能选择 ToolRegistry 白名单工具。
- 所有 tool call 必须写入审计并关联 trace_id。
- 使用 LangGraph `add_conditional_edges` 体现动态路由。
- 至少实现一轮 `Reason -> Act -> Observe`，后续再扩展多轮 ReAct。
- LLM 失败、schema 失败或超时时，必须 fallback 到现有 Phase 2A/2D 稳定链路。
- hard-gate 仍通过现有 SecurityHook + interrupt 人审，不允许 LLM 绕过。

## 后续优先级建议

1. Phase 5A：LangGraph Agent Planner + Tool Calling + Conditional Edges。
2. Phase 5B：弹幕语义聚合增强，采用 embedding/LLM 低频兜底，不全量 LLM。
3. Phase 5C：播中 ReAct 小循环，让 Agent 基于弹幕、库存、流量观察动态生成主播建议。
4. Phase 5D：LLM 播后复盘总结，输出自然语言报告，但仍保留结构化归因。
5. 部署阶段：守护进程管理、数据清理策略、真实平台 API 适配层。
---

## 2026-07-11 当前状态补充：Phase 5H 后的 Agent 能力

Phase 5H 已补齐 Harness Agent 的审计与 DecisionTrace 证据链。当前项目已经不只是“workflow + 局部 LLM”，播中链路具备了更明显的 Agent 特征：

- LangGraph 显式节点图：推理、工具策略、工具执行、观察、replan、审计都在图中可见。
- Agent state 可回放：`completed_nodes`、`observations`、`executed_tools`、`final_suggestion`、`audit_status` 都进入最终状态。
- 工具调用可复盘：低风险工具自动执行，高风险工具 pending/blocked 会进入审计 payload。
- DecisionTrace 闭环已打底：本阶段先记录 Agent 建议证据，播后再回填主播反馈和 trust_delta。

### 当前仍缺的 Agent 核心能力

1. **人审 interrupt 恢复**：高风险工具现在只 pending，还没有用 LangGraph `interrupt()` 暂停和恢复。
2. **前端可观测性**：Web 副屏还没有展示 Harness 节点路径、审计状态和 pending 工具。
3. **真实反馈更新**：DecisionTrace 里的采纳结果、业务结果、trust_delta 仍需播后复盘回填。

### 推荐下一步

优先做 **Phase 5I：LangGraph interrupt 人审恢复**。理由是它最能继续体现 Agent + LangGraph 的项目特征：Agent 不是自动乱执行，而是在高风险动作处可暂停、可人工批准、可从同一个 thread 恢复继续跑。
---

## 2026-07-11 当前状态补充：Phase 5I 后的 Agent 能力

Phase 5I 已把播中 Harness Agent 的高风险工具接入 LangGraph `interrupt()`。当前 Agent 不再只是给建议或返回 pending 状态，而是具备了人机协同恢复能力：

- 高风险工具会暂停在 `human_approval_interrupt` 节点。
- 人工通过 `Command(resume=approved/rejected)` 恢复同一条 `thread_id`。
- approved 后执行原 pending tool，并继续 observe/replan。
- rejected 后跳过工具，写审计结束。
- 审批请求、审批结果、操作员和原因进入审计 payload。

### 下一步推荐

优先做 **Phase 6C：Web 副屏人审入口与 Agent 可观测面板**。原因是 5G-B/5H/5I 已经把 Agent 内核补齐，下一步要让主播在前端看见节点路径、pending 审批和最终建议，形成真正可用的人机协同产品体验。

---

## 2026-07-11 文档编码治理补充

项目留迹已经成为后续迭代的重要输入，因此中文文档编码需要纳入工程规范。本次治理完成后，当前文档体系调整为：

- `docs/project_guidance/`：正式项目指导、状态路线图、阶段执行日志和编码规范。
- `docs/worklog/`：可追踪的过程工作日志，只记录脱敏项目事实和后续任务，不记录真实密钥或本机私密信息。
- `scripts/check_doc_encoding.py`：只读扫描工具，用于区分终端显示乱码和文件内容损坏。

后续所有中文留迹默认遵循 `docs/project_guidance/document_encoding_policy.md`：优先使用 `apply_patch` 修改，避免 PowerShell heredoc / 管道写入大段中文，阶段收尾时运行编码扫描和 `git diff --check`。
---

## 2026-07-11 当前状态补充：Phase 6C 后的产品化 Agent 能力

Phase 6C 已把播中 Harness Agent 从 CLI 演示推进到 Web 副屏产品体验：

- Web 可以启动一条 Harness Agent 会话，并看到 `trace_id`、状态、节点路径和 pending 高风险工具。
- 高风险工具不会自动执行，必须在副屏点击批准后才通过 `Command(resume=...)` 恢复同一 LangGraph thread。
- 拒绝路径不会执行工具，会把 `rejected_by_human`、操作员和原因写入会话状态。
- PostgreSQL `live_agent_harness_sessions` 保存 Web 查询所需的会话快照；LangGraph checkpoint 仍由官方 PostgresSaver 负责。
- WebSocket 已能推送 `agent_harness_update`，副屏可实时刷新审批状态和最终建议。

### 现在项目为什么更像 Agent

当前核心亮点不再只是“业务 workflow + LLM 文案”，而是具备了 Agent 工程闭环：

```text
Context -> Reason -> Tool Policy -> Interrupt -> Human Resume
-> Tool Execution -> Observation -> Replan -> Audit -> Web Observability
```

这条链路体现了 LangGraph 的状态图、条件边、checkpoint、interrupt/resume 和可观测状态，而不是普通脚本式 ReAct。

### 下一步优先级

1. Phase 7A：Agent Replay / Evaluation。把每次 Harness 会话做成可回放、可评分、可复盘的评估接口。
2. Phase 7B：生产化硬化。补审批 TTL、操作员锁、幂等键、错误告警、恢复脚本和敏感字段脱敏检查。
3. Phase 7C：一键演示与部署包装。把 seed、Kafka、API、Web、demo 组合成可交付的项目演示入口。
# 2026-07-11 当前状态补充：Phase 7A 后的生产级 Agent 评估能力

Phase 7A 已把 Harness Agent 的可观测能力推进到生产评估闭环：

- `AgentReplayService` 可以把 checkpoint / Harness session / audit / DecisionTrace 整理为标准化时间线。
- `AgentRuleEvaluator` 提供不依赖 LLM 的规则评分，覆盖状态完整性、工具选择、安全策略、人审合规、执行效率和业务效果。
- PostgreSQL `live_agent_evaluation_runs` 成为评估任务事实源和轻量队列，Worker 通过 `FOR UPDATE SKIP LOCKED` 抢占任务。
- `AgentLLMJudge` 已实现结构化 JSON Judge，外部模型失败时只标记 partial，不影响规则评分。
- FastAPI 提供评估创建、状态查询、回放读取和人工复核接口。
- `/evaluation` 运维页面独立展示评估分数、覆盖率、违规项和回放时间线。

当前项目已经具备更接近生产的 Agent 工程能力：

```text
LangGraph Harness Loop
-> Interrupt / Resume
-> Audit / DecisionTrace
-> Web Human Approval
-> Replay / Rule Evaluation / Human Review
```

这意味着项目不只是“会调用 LLM 的 workflow”，而是可以说明 Agent 为什么这么做、做了什么工具调用、有没有绕过人审、结果是否可回放，并且能在版本升级后做回归评估。

下一步优先级：

1. Phase 7B：生产硬化。补审批 TTL、操作员锁、幂等键、租约恢复脚本、告警和更严格脱敏。
2. Phase 7C：Golden Dataset 批量回归。补 case 管理、批量 API、版本对比和发布门槛。
3. Phase 8：真实平台 Adapter。把本地 demo executor 替换为可插拔平台适配器，但仍保持高风险动作人审。

---
