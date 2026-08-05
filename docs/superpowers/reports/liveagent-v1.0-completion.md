# LiveAgent v1.0 完结报告（interview-ready）

> 状态：`LIVEAGENT_V1_INTERVIEW_READY`（2026-08-05 定稿）
> 本文档与 `liveagent-architecture-positioning.md`、`liveagent-v1.0-lessons-learned.md`、
> `liveagent-interview-qa-prep.md`、`phase-17-holdout-approval-record.md` 配套使用。
> 所有数字均来自 append-only 账本与冻结契约/数据集，可独立复核（核验命令见 §9）。
> 编码：UTF-8 / LF / 无 BOM。

---

## 1. 一句话定位与非目标

> **LiveAgent：受治理、有限循环、工具调用、人工审批协同的直播运营 Agent 系统。**

- **是**：面向直播运营的 Analyst→Planner 受控双 Agent 链路，工程实现与受控验证完成；
  播前 Workflow、播中 bounded Agent、高风险场景受控双 Agent + 人工审批。
- **不是**：不是通用自主 Agent（无上限自主探索）、不是聊天机器人、不是已生产部署的系统；
  **不宣称生产就绪，不做生产泛化证明**。

## 2. 架构区分与一次完整 Agent loop

### 三种形态（详见架构定位文档 §2-§4）

| 阶段 | 形态 | 边界理由 |
|:--|:--|:--|
| 播前（排品/手卡） | **Workflow**（确定性流程） | 输入确定、步骤固定、无模型决策点 |
| 播中（弹幕/告警→建议→人审） | **bounded Agent**（`BoundedSpecialistRunner` + Harness 图） | 有决策点，但循环/预算/截止时间硬上限 |
| 高冲突证据场景 | **受控双 Agent**（EvidenceAnalyst → DecisionPlanner） | 分析/决策职责分离；受控顺序 + 人审，不做自主辩论 |

### 一次完整 Agent loop（真实案例：售罄处理 `live-session-p001-sold-out-v1-agent-decision-appendix.md`）

1. 确定性售罄保护先执行（DETERMINISTIC_ONLY 保底）；
2. 证据冻结投影注入 Analyst：模型只看到经 Resolver 身份/摘要/作用域校验的证据，不能自行查 Store；
3. Analyst 产出结构化冲突分析（schema 冻结，`additionalProperties: false`）；
4. Planner 基于分析产出决策方案（改价/替补/弹幕回复），阶段间确定性校验；
5. 高风险动作**不自动执行**：人审批准（AFTER_OPERATOR_CONFIRMATION / AFTER_RECONCILIATION）才落地；
6. 每个 attempt 以 receipt + HMAC 证据入 append-only 账本，预算先预留后结算。

## 3. Holdout 30 例：数据来源、冻结方式、评分规则

- **数据来源**：30 例合成数据（弹幕冲突 / 库存告警 / 售罄 / 改价 / 替补 / 播后复盘 6 类），
  标签由项目作者起草、用户终审；**输入与标签分离**（模型/候选代码/运行器不可读取 labels）；
  与 dev 12 例不重叠（ID 去重 + 语义近重复 + Prompt/源码泄漏检查）。
- **冻结方式**：30 例在第一次真实调用前整体冻结；dataset digest `28b1499f...`、
  contract digest `86a76ff8...`、candidate digest 与 prompt/schema/profile 全部落盘；
  loader fail-closed：digest 或 membership 不匹配即拒绝加载。
- **评分规则**：每例两阶段（ANALYST + PLANNER）输出结构校验 + 预期路由匹配
  （`expected_route`，如 `MULTI_AGENT_READY`）；**关键安全指标 0 严重失败**；
  阈值预声明：batch1 ≥9/10、batch2 ≥18/20、总计 ≥27/30。

### 结果（append-only 账本，可复核）

| 批次 | 案例数 | 阈值 | 结果 | 成本 (CNY) | run_id |
|:--|:--|:--|:--|:--|:--|
| batch1（探路） | 10 | 9/10 | **10/10 PASS** | 0.327462 | `phase17-holdout-4d6ce907...` |
| batch2（剩余） | 20 | 18/20 | **20/20 PASS** | 0.686223 | `phase17-holdout-4c2d9d2a...` |
| **总计** | **30** | **27/30** | **30/30 QUALIFIED** | **1.013685** | `phase17-holdout-qualification-292f62ae...` |

- 安全门禁：6/6 hard-safety PASS（danmu-001/002、soldout-001/002、price-001/002），
  `critical_safety_failures = 0`；每例由独立第三方逐例语义审查（reviewer=`claude-independent-review`）。
- 证据绑定：60/60 attempt 的 `evidence_ids` 均为输入可见 ID 子集；60/60 attempt 的
  artifact SHA-256 == ledger `response_digest` 全 MATCH（独立复算，非执行者自报）。
- 宣布状态：`PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD`。

## 4. 失败 / 重试 / 预算 / 防刷分策略

- **失败如实入账**：append-only 账本保留全部历史终态（含首轮 0/10 run、BLOCKED run 1.0 CNY），
  不重跑、不洗绿、不改写；
- **重试受控**：契约冻结 `max_attempts_per_endpoint=2`、最小重试窗口 1s；
  可重试类：TRANSPORT_ERROR / HTTP_5XX / DEADLINE_EXCEEDED（同端点重试）；429 语义为
  `SWITCH_ENDPOINT_NO_RETRY`，但白名单为单端点、无端可换——如实 INCONCLUSIVE，绝不切换端点
  （`MODEL_AND_ENDPOINT_IDENTITY_MATCH` 硬性拦截）；
- **预算封装**：Phase 17 总盘 15 CNY（含 V9 历史 6.604131）；预算池与 contract_digest 绑定，
  跨池不结转；每次真实 run 前用户单独批准（最坏预留，先预留后结算）；
- **防刷分**：contract digest 不可重签（approved registry 二次拒绝）、数据集冻结、
  探路结果不得触发 Prompt/代码/模型/案例/阈值调整、预算数字必须带池身份。

### 账本总账（独立复核闭合）

| 项 | 金额 (CNY) |
|:--|:--|
| V9 历史评价（12 runs / 271 receipts / 8 PASS 4 FAILED） | 6.604131 |
| 首轮 BLOCKED run 池 | 1.000000 |
| 首轮 0/10 batch1 池 | 0.317055 |
| 重跑 batch1 + batch2（第三版契约池） | 1.013685 |
| 池内未用余额 | 6.065129 |
| **合计** | **15.000000** ✓ |

## 5. V9、Phase 17、原始 V5 的关系

- **原始 V5**：官方 DeepSeek 单渠道契约（预算 ≤1.0、零重试），因执行偏差保持
  **"未通过、未恢复"** 历史状态，不被追认；
- **V9**（`75319a4a...`）：纯回溯评价身份，闭合于 2026-08-02；
- **Phase 17**（`86a76ff8...`）：新建独立执行契约，兑现 V9"未来执行契约由 phase17 定义"的声明；
  三个契约三个身份，互不覆盖；v2/v3 manifest 历史冻结 digest 承诺未破坏（git diff 为空）。

## 6. 真实模型证据与合成/模拟证据边界

- **真实模型**：holdout 30/30 为真实模型调用（gpt-5.6-terra / high，单端点 synapse-ai.uk），
  每次 run 前用户单独批准，成本如实入账（§3 表）；
- **合成/模拟**：holdout 数据集为**合成数据**（如实声明，不包装成真实业务泛化证据）；
  demo 与 CI 全程 `PHASE15_REAL_MODEL=0` 可跑（dry-run）；
- **内部评价属性**：标签由项目作者 + 用户终审，属内部评价金标准，非独立第三方标注。

## 7. 生产化剩余工作（范围外声明）

- 不接入真实平台 API、无真实业务 KPI（转化率/损失率）、无生产部署；
- 生产化缺口（如实列出）：单节点 compose、默认凭据、部分端点未鉴权、无 HA/密钥管理/
  鉴权/限流/监控/灾备——**未完成即不声明生产就绪**；
- 路线（计划内，非本版本范围）：只读建议 → 人审 Skill → 有限自动化。

## 8. 可复制的 Demo 与验证命令

```bash
# 三场景 demo（播前手卡 / 播中决策+人审 / 播后复盘，dry-run 无真实模型调用）
python scripts/run_all.py phase13-demo
python scripts/run_all.py phase14-demo
python scripts/run_all.py phase15-demo
python scripts/run_all.py phase16-demo

# holdout 契约与身份准入（dry-run）
python -u scripts/run_phase17_holdout.py --probe

# 账本导出核验
python scripts/verify_phase16_qualification_ledger_export.py

# 全量测试 gate（unit + integration，本地基线全绿；postgres restart 并发 flaky 例已单独验证 PASS）
pytest tests/unit tests/integration
```

## 9. 两层表达与样本量边界

**事实层**：内部评价（合成数据 + 项目作者/用户标注），非独立金标准；
**方法论层**：数据冻结、预声明阈值、禁调参、append-only、失败终态保留、fail-closed
registry、分层独立核验——评估纪律已被验证，**方法论价值不得替代真实业务证据**。

> **样本量边界原文：「30 例 holdout 是预声明的工程验收线，不是统计显著性或总体成功率
> 90% 的声明。」** 统一表述为"达到预声明工程门槛"，不写"模型准确率达到 90%"。

## 10. 四线验收对照表

| 线 | 验收 | 状态 |
|:--|:--|:--|
| 功能 | demo 三场景闭环（§8 命令可复现） | ✅ |
| 质量 | holdout 30/30 QUALIFIED（预声明阈值 27/30，§3） | ✅ |
| 发布冻结 | 文档 + tag `liveagent-v1.0-interview-ready` | ✅（本文档 + tag） |
| 生产部署 | **不在本版本范围内，未进行生产就绪声明** | ⚪ 范围外 |

## 11. 最终状态与保留声明

**最终状态：`LIVEAGENT_V1_INTERVIEW_READY`**（可面试交付：功能线 ✓、质量线 ✓、
发布冻结 ✓、生产部署范围外 ✓）。

> **保留声明：「Phase 17 holdout 只验证冻结的 Phase 16 Analyst→Planner 受控链路，
> 不代表整个 LiveAgent 已生产就绪，也不代表原始 V5 官方 DeepSeek 契约通过。」**

## 12. 证据链文件索引

- 执行契约：`evaluation/manifests/phase17-holdout-execution-v1.json`（digest `86a76ff8...`）
- 数据集：`evaluation/phase17_holdout/manifests/phase17-holdout-cases-v1.json`（digest `28b1499f...`）
- 审批记录：`docs/superpowers/reports/phase-17-holdout-approval-record.md`
- 踩坑复盘：`docs/superpowers/reports/liveagent-v1.0-lessons-learned.md`（10 条全闭环）
- 架构定位：`docs/superpowers/reports/liveagent-architecture-positioning.md`
- 面试预案：`docs/superpowers/reports/liveagent-interview-qa-prep.md`（13 个追问）
- 账本：`phase17_holdout_*` 系列表（postgres，append-only）
