# Phase 16 V9 契约批准记录（qualification contract approval record）

> 本记录正式化 **V9 = `DEVELOPMENT_VALIDATION_QUALIFIED`**（高 reasoning 模式资格认证）。
> 它是 Phase 16 实际执行行为的回溯性分类与评价契约，**不改变**历史 campaign 的执行
> 身份，也**不抹除**其相对于 v2 政策（`phase16-qualification-policy-v2`）的偏差。
> 原始 V5（`PHASE16_V5 CONTROLLED_E2E_QUALIFIED`）保持「未通过、未恢复」的历史状态。
> 本记录随用户对本文「批准项清单」逐项确认后生效。

## 0. 背景与依据

- 触发：codex（OpenAI Codex，原始开发者）对
  `docs/superpowers/handoffs/2026-08-01-phase16-v5-closeout-work-report.md` 的审查
  （R1–R12），指出 v2 政策声明与实际执行之间存在系统性偏差。
- 流程：Claude 与 codex 经两轮直接讨论（codex session
  `019f5166-5aef-7653-8b9b-3805ab96e409`，gpt-5.6-luna / max）达成共识；
  用户拍板「以现在的为准」（= 以实际执行且跑通的现状为新契约，旧契约舍弃）。
- 依据账本：`phase16_qualification_*`（PostgreSQL append-only），核验查询仅 SELECT、
  `.env` 凭据 load-into-process 不落盘不打印（2026-08-02 执行）。

## 1. V9 定义与结论

- 定义：`V9 DEVELOPMENT_VALIDATION_QUALIFIED` —— **开发/验证阶段**的资格认证，
  覆盖 12 个高冲突案例 × Analyst/Planner 双阶段的真实模型执行，
  **不是生产就绪证明**。
- 结论措辞（本记录及所有同步文档统一使用）：
  > V9 是开发/验证阶段的 qualification；holdout 30 例生产泛化验证**未完成**
  > （延后 phase17）；V9 ≠ 原始 Phase 16 V5 PASS。
- 终态：8 个 PASS run（含 terra/high 注入 failover run）+ 4 个 FAILED run 保留
  在账本（防刷分、终态不可重跑）。

## 2. 新政策 manifest

- `evaluation/manifests/phase16-qualification-policy-v3.json`
- policy_digest：`f286114f23d638a7c6aba3300ab6d9b0fc62fc1638596e151d9a55f86da6df16`
- 三角色 digest 字段（codex 第 3 轮要求：一个字段不得同时承担两个角色）：
  - `historical_execution_policy_digest` = v2 最终重冻结 digest
    `1aa9ca6fe5a85702a256e29fb5d6f3d22334bfeb55c6b0ab02300ba62e4926d7`
    （历史 campaign 实际引用的执行契约，不可修改）；
  - `v9_evaluation_policy_digest` = v3 自身 digest（本文件 `policy_digest`），
    用于**回溯评价**这批历史证据的评价契约；
  - `policy_digest`（自身）= v3 的**执行**契约 digest，未来新 campaign 直接引用。
- 未来新 campaign 必须直接引用 v3 digest；**回溯阶段禁止新增 candidate**。

## 3. v2 → v3 参数差异表（每项：声明 / 事实 / v3 语义）

| 参数 | v2 声明 | 执行事实（账本） | v3 语义 |
|:--|:--|:--|:--|
| `project_budget_cny` | 5.000000 | 实际 6.604131 | 未来约束 **10.000000**（2026-08-02 用户明确批准的新上界，**含历史成本**）；另记 `retrospective_budget_actual_cny` = 6.604131、`forward_budget_remaining_cny` = **3.395869**（= 10 − 6.604131，未来可用余额） |
| `campaign_budget_cny` | 4.000000 | 单 run 最高 0.622227，未触顶 | 4.000000（不变） |
| `retry_allowed` | false（未强制） | TRANSPORT 重试 + 换端已执行（注入 run 24 receipts / 72 attempts） | **true** + `retry_semantics`：TRANSPORT_ERROR/HTTP_5XX/DEADLINE_EXCEEDED 可重试、每端点 ≤2 次、90s/次、窗口 ≥1.0s、429 换端不重试 |
| `fallback_allowed` | false | 渠道链 failover 已执行 | **true** + `fallback_semantics`：仅限声明渠道白名单有序尝试，`attempt_count`/`responded_endpoint_host` 入账 |
| `maximum_development_candidates` | 2（未强制） | 9 个 dev campaign | `maximum_future_development_candidates` = **2**（未来强制）+ `historical_development_candidates_in_scope` = **9**（仅这 9 个可进回溯评价） |
| 身份字段 | kind + candidate_digest + 模型/强度/渠道 | 同左 | 未来须含 `provider_id + endpoint_host + model_id + reasoning + profile/prompt/schema/source/dataset digest + split`（9 项）；历史无法独立证明 provider → 标 `PROVIDER_IDENTITY_UNVERIFIED`，不作身份链闭合 PASS |
| 调用计数 | receipts | 271 receipts / 332 transport attempts / 24 logical stages per run | 三计数分离：`logical_stage_count` / `transport_attempt_count` / `receipt_count` |
| 使用量口径 | — | 全部 receipt 有 usage（`receipt_complete=true` 0 缺失） | usage 未知记 `UNKNOWN_USAGE`，保留预算占用，不得结算为 0 |
| holdout | 30 PASS 要求 | 未执行 | 保留要求；V9 结论标注「未完成生产泛化验证」 |

## 4. 回溯评价范围（账本原始导出 2026-08-02）

### 4.1 campaigns（13 行，全部列入 `retrospective_campaign_ids`）

- 9 个 DEVELOPMENT + 4 个 VALIDATION；`validation-candidate-1` 无 run（runs=0）。
- 8 个 v2 policy digest 变体（每次候选重冻结追加一行，policy_id/version 均为 v2 / 2.0.0）；
  最终 terra/high campaigns 引用 `1aa9ca6fe5a8...`。
- 详情见 `docs/superpowers/reports/phase-16-final-closeout-acceptance.md` §4.1。

### 4.2 runs（12 行）

| run（截断时间戳） | status | receipts | cost CNY（全精度） | tokens |
|:--|:--|:--|:--|:--|
| candidate-1 dev 093840 | PASS | 24 | 0.622227 | 182,557 |
| cdd63444 dev 115908 | FAILED | 22 | 0.591219 | 170,928 |
| 3c10985b dev 154732 | FAILED | 23 | 0.584121 | 172,616 |
| 5f4afbda dev 160222 | PASS | 24 | 0.608604 | 180,326 |
| 5f4afbda val 161046 | PASS | 24 | 0.616977 | 181,900 |
| 9eda8e8a dev 213032 | FAILED | 20 | 0.461568 | 110,686 |
| 8623a075 dev 221215 | PASS | 24 | 0.513807 | 125,373 |
| 8623a075 val 222715 | PASS | 24 | 0.526488 | 127,645 |
| f28d7e03 dev 071416 | FAILED | 14 | 0.455001 | 135,288 |
| 1b432365-3b173072 dev 125602 | PASS | 24 | 0.541872 | 169,143 |
| 1b432365-3b173072 val 130201 | PASS | 24 | 0.545277 | 170,018 |
| 1b432365-d90f9f3d dev 140424 | PASS | 24 | 0.536970 | 168,393 |

## 5. 账本统计核验结果（闭合 R4 异议）

| 指标 | 账本权威值 | 说明 |
|:--|:--|:--|
| receipts | **271** | 原 report 声称 267（少 4，已修正） |
| 成本全精度 | **6.604131** CNY | 原 6.6042 系逐行 4 位舍入相加伪影；acceptance 的 6.6041 正确 |
| tokens | 1,894,873 | 与文档一致 |
| transport attempts | **332** | Σ receipt.attempt_count；注入 run = 24 receipts / 72 attempts |
| legacy rows | 46 | candidate-1 + cdd63444（attempt 列迁移前，`responded_endpoint_host` NULL），已如实标注 |
| incomplete receipts | 0 | 全部 `receipt_complete=true` |

## 6. 诚实声明

- **PROVIDER_IDENTITY_UNVERIFIED**：历史 campaign 的 provider 身份由渠道配置层
  （endpoint host ↔ provider 映射）表达，账本 receipt 无独立 provider 字段；
  现有 host/receipt/映射证据仅在可验证范围内作为补充证明，不包装为身份链闭合 PASS。
- **retry 证据边界**：TRANSPORT 重试 + failover 有真实证据（注入 run 24/24 ×
  attempt_count=3 → synapse）；HTTP 5xx / 429 / DEADLINE 路径仅单测证明，无真实
  provider 事件样本。
- **holdout**：30 例生产泛化验证未执行，延后 phase17，不得在 V9 结论中声称
  「生产就绪」。
- **预算**：执行期超出 v2 声明 5.0 至实际 6.604131，每次预算变更均经用户逐次
  批准（对话转录）；v3 以 **10.0** 为未来约束（2026-08-02 用户批准，**含历史
  成本**），未来可用余额 = 10.0 − 6.604131 = **3.395869**（`forward_budget_remaining_cny`）。

## 7. 批准项清单（用户逐项确认后本记录生效）

1. V9 = `DEVELOPMENT_VALIDATION_QUALIFIED`（高 reasoning 模式资格认证），
   ≠ `PHASE16_V5 CONTROLLED_E2E_QUALIFIED`；V5 保持「未通过、未恢复」。
2. v3 policy（`f286114f...`）作为新契约；未来 `project_budget_cny` = 10.0
   （含历史，余额 `forward_budget_remaining_cny` = 3.395869）、
   `maximum_future_development_candidates` = 2（运行时强制，ledger 层）、
   retry/fallback 正式化为受控语义。
3. 回溯评价范围 = 13 campaigns / 12 runs（第 4 节清单）；回溯阶段禁止新增 candidate。
4. holdout 延后 phase17，V9 结论标注「未完成生产泛化验证」。
5. merge 前提：v3 + 本记录 + 账本闭合 + 远端 PR Gate 全绿 + 用户最终 merge 审批。
6. V9 报告与 Acceptance 文档同步更新（本仓库 `codex/phase16-v5-controlled-e2e`）。

## 8. 相关文件

- 政策 manifest：`evaluation/manifests/phase16-qualification-policy-v3.json`
- 验收文档：`docs/superpowers/reports/phase-16-final-closeout-acceptance.md`
- 工作报告：`docs/superpowers/handoffs/2026-08-01-phase16-v5-closeout-work-report.md`
- codex 讨论：session `019f5166-5aef-7653-8b9b-3805ab96e409`（只读模式，未修改任何文件）
