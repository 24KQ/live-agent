# Phase 17 Holdout 执行契约批准记录（phase-17 approval record）

> 本记录独立于 Phase 16 契约批准记录（`phase-16-v9-contract-approval-record.md`），
> 正式化 **Phase 17 holdout 执行契约**（`phase17-holdout-execution-v1`，
> `PHASE17_HOLDOUT_EXECUTION_V1`）的批准事实：15 CNY 预算封装、身份固定、
> 不可重签 digest 注册表、数据集 manifest 机制、预算池事件记账。
> 本记录随用户对「批准项清单」（§7）逐项确认后生效。

## 0. 背景与依据

- 触发：codex（OpenAI Codex，原始开发者）第十六轮总裁决（2026-08-03）——
  **不通过阶段② Gate**，状态应为 `PHASE17_CONTRACT_ADMISSION_READY`，必须补齐：
  1. Phase 17 独立 ledger/policy 正向路径（v2 表族 CHECK 硬编码 15+15 batch、
     reservation ≤ 10，phase17 的 10+20 / 15 CNY 无法复用）；
  2. 实际数据集 manifest 与 case membership 校验（30 例在第一次真实调用前全部冻结）；
  3. 固定模型/渠道身份（provider/model/endpoint/reasoning/tokens/deadline 冻结字段）；
  4. 不可任意重签的 contract digest（approved registry，registry 更新须用户批准）；
  5. 三角色反向隔离测试（v2/v3 policy 对象不得进入 phase17 入口）；
  6. 15 CNY 实际预算池测试（reserved/settled ≤ forward 不变式，行锁内强制）；
  7. approval record 独立文件（本文件）；
  8. 清理 `_disc16` 临时文件（已清理）。
- 流程：Claude 全盘接受 codex 第十六轮裁决并落地为代码；本检查点
  `fdac72b` 提交后进入 codex 第十七轮双向讨论（讨论到一致后进入阶段③）。
- **codex 第十七轮裁决（2026-08-03）**：仍**不通过"允许真实模型调用"Gate**，
  但放行阶段③数据起草/离线准备；6 项 P0/P1 阻塞已全盘接受并落地（本检查点）：
  P0-1 阈值语义（9/10、18/20 批内阈值 + 27/30 聚合 + 精确 batch 集合校验）、
  P0-2 全链身份绑定（candidate/campaign/manifest/adapter）、
  P0-3 逐 attempt receipt/HMAC 证据、P1-4 真实执行入口（重冻结 + registry）、
  P1-5 migration chain、P1-6 清理 + 受控全量 integration gate。
  放行条件原文（已全部完成，见 §6）："先补齐：精确 10/20 case-set 校验及
  9/10、18/20、27/30 聚合；campaign/candidate/manifest/adapter 全链身份绑定；
  逐 attempt receipt、端点、响应摘要、HMAC 和一致的成本口径；最终真实执行入口、
  source closure、migration chain；清理未跟踪文件并完成一次受控的全量
  integration gate。完成后，我可以放行'冻结数据集后的真实 Probe 评审'；
  每次真实模型调用仍需用户单独批准。"
- 关联：Phase 16 V9 批准记录 §7 第 7 项（2026-08-02 用户批准 15 CNY 总盘封装）
  的批准事实现由本记录承接；Phase 16 文件只保留引用。

## 1. Phase 17 执行契约定义

- manifest：`evaluation/manifests/phase17-holdout-execution-v1.json`
- contract_digest：`c2dc8b022607c80f069318eb0f6a69732647f1a27ed9c24ec5b734b381e19af4`
  （2026-08-03 定稿重冻结；contract_digest 自校验 =
  `canonical_json_sha256(model_dump(exclude={"contract_digest"}))`）
- 执行身份：`PHASE17_HOLDOUT_EXECUTION_V1`（v2 历史入口只接受
  `V2_HISTORICAL_EXECUTION`；v3 回溯契约无执行身份）
- `implementation_status = WIRED_INTO_RUNTIME`（兑现 v3「未来执行契约由 phase17
  定义并接入运行时」的声明）
- 预算封装：`project_budget_cny = 15.000000`（总盘，**含历史支出**
  `retrospective_budget_actual_cny = 6.604131`）、未来可用余额
  `forward_budget_remaining_cny = 8.395869`；**不改写** v2/v3 历史预算事实
  （v3 的 `forward_contract_draft` 10.0 草案原样不动）
- holdout 结构：30 例 = batch 1（10 例，阈值 ≥9/10）+ batch 2（20 例，阈值 ≥18/20）；
  总阈值 ≥27/30；关键安全指标 0 严重失败
- 阶段预留：`stage_reservation_cny = 0.100000`（per attempt 最坏情况占用上限）

## 2. 身份固定（codex 十六轮 P0）

契约以 `identity_requirements` 字段冻结模型/渠道身份，**运行时不可覆盖**：

| 字段 | 冻结值 |
|:--|:--|
| provider_id | `synapse-ai` |
| model_id | `gpt-5.6-luna` |
| endpoint_hosts | `["synapse-ai.uk"]` |
| reasoning_effort | `null` |
| json_mode | `true` |
| max_total_tokens | `8000` |
| max_output_tokens | `2800` |
| per_attempt_deadline_seconds | `90` |
| max_case_cost_cny | `0.100000` |

强制点（双层 fail-closed）：
- model 层 validator：`identity_requirements` 与冻结常量
  `PHASE17_IDENTITY_REQUIREMENTS`（tuple 归一化）逐项相等，否则拒绝加载；
- runner 构造断言：candidate bundle 的 `model_id` / `endpoint_host` /
  `temperature == 0` / `deadline_seconds` 与契约身份精确一致，否则
  `Phase17HoldoutExecutionError`（联网前）。

## 3. 不可重签 digest 注册表（codex 十六轮 P0）

- `src/decision_support/phase17_approved_digest.py` 定义
  `PHASE17_APPROVED_CONTRACT_DIGEST`，**不在** phase17 source closure 中
  （避免自指循环：digest 依赖文件内容、文件内容依赖 digest）。
- loader 末尾强制：`contract.contract_digest == PHASE17_APPROVED_CONTRACT_DIGEST`，
  否则拒绝加载——篡改参数后即使重算 digest 使 payload 自洽，仍被注册表拒绝；
  registry 更新 = 显式修改注册文件 = 用户批准事件。
- 语义：参数变化 → closure 文件变 → 重新冻结 manifest（新 contract digest）→
  更新注册值并经用户批准；只改注册值不重冻结 → 旧 manifest 与注册值失配 → fail-closed。

## 4. 数据集 manifest 机制（阶段②交付机制，真实数据阶段③）

- `src/decision_support/phase17_holdout_dataset.py`：
  `Phase17HoldoutDatasetManifest`（冻结，非 pydantic）+ loader + 运行时
  `validate_phase17_holdout_case` membership 校验。
- 约束（codex 十三轮 7+2 数据身份加强版）：30 例精确；`case_id -> input_digest`
  完整映射；batch 固定 10 + 20 不相交子集并恰好覆盖全集；holdout case 不得在
  dev 数据集；labels 物理隔离于 inputs 根目录之外；manifest_digest 自校验；
  loader 拒绝 BOM/CRLF。
- **阶段②交付机制本身**：真实 30 例 manifest 文件在阶段③数据起草并经用户终审
  后冻结生成；任何真实模型调用前 membership 校验已可用。manifest 变更 = 数据
  身份变更 = 新 digest = 新契约。

## 5. 独立账本与预算池（codex 十六轮 P0）

- 独立表族（`docker/init_phase17_holdout_ledger.sql`，6 张表全部 append-only，
  UPDATE/DELETE/TRUNCATE 触发器拒绝）：contracts / campaigns（batch_index ∈ (1,2)、
  UNIQUE(contract_digest, batch_index)）/ budget_events / runs / run_results /
  case_results。
- 预算池 = 纯事件记账（无 UPDATE）：RESERVE（campaign 建立）、SETTLE（run 结算）、
  RELEASE（未产生成本时释放预留）；`reserved = Σ RESERVE − Σ RELEASE`、
  `settled = Σ SETTLE`、`available = forward − reserved − settled`。
- 不变式（contract 行锁内强制 + SQL 后置）：`reserved + settled ≤ forward`；
  结算 = 预留转实际（同一事务 RELEASE + SETTLE），每个 campaign 在池中至多占用一次。
- 隔离：v2/v3 policy digest 在 phase17 表族永远没有 contract 行 → campaign 建立
  与池查询 fail-closed；phase17 digest 也进不了 v2 池（既有集成测试证明）。
- UNKNOWN_USAGE 纪律：usage 未知按最坏情况以 **stage 级预留**（0.1/attempt）全额
  入账，绝不结算为 0（与 v2 attempt reservation 口径一致）；不使用 campaign 级
  预留，避免多 case 失败重复全额占用。

## 6. 测试证据（2026-08-03，全部离线，无真实模型调用）

- phase17 测试 **全绿（89 passed：unit 59 + integration 30）**：unit（契约不可变性 /
  身份冻结 / 不可重签 / dataset manifest 12 项）+ integration（contract 注册与准入 /
  预算池预留与耗尽 / 结算与释放 / run 生命周期 / append-only / 反向隔离 / runner
  端到端三路径 PASS·FAILED·BLOCKED / 身份不匹配联网前拒绝 / membership 校验先于
  账本写入）。
- codex 第十七轮 6 项裁决全部落地并有测试证据：
  - P0-1 阈值语义：9/10 达标即 PASS、8/10 FAILED、BLOCKED 优先；27/30 聚合器
    （PASS / FAILED / BLOCKED 三路径）；精确 batch 集合校验（2 例子集、9+1 混合、
    batch2 混入均先于账本写入拒绝）。
  - P0-2 全链身份绑定：candidate policy_digest / model / endpoint / deadline 构造期
    拒绝；campaign contract / dataset / candidate 三 digest 漂移在联网前拒绝。
  - P0-3 逐 attempt 证据：`phase17_holdout_attempts` 20 行对账 case 聚合；
    receipt_hmac 64 hex 由 ledger 内部计算、响应事实不同则 HMAC 不同；slot 重复 /
    终态后追加 / UPDATE / DELETE 均被拒绝。
  - P1-4 真实执行入口：`--execute` env 身份检查 + 预算预检 + 交互 APPROVE +
    v5 adapter 装载 + 输入文件约定 + 池核对。
  - P1-5 migration chain：`phase17_holdout_ledger` 已注册（required，dry-run 验证）。
- 全量 unit 套件 **1725 passed**（基线 1713 + 新增 12）。
- runner 端到端离线链（fake model port）证明：contract 准入 → campaign 预留 →
  run slot 冻结 → 逐 case Analyst→Planner 双阶段 → 终态判定 → 按实际成本结算；
  BLOCKED run 按 stage 预留最坏情况入账。
- **受控全量 integration gate 待 codex 第十八轮确认后与本检查点一并复核**。

## 7. 批准项清单（用户逐项确认后本记录生效）

1. **Phase 17 执行契约 `PHASE17_HOLDOUT_EXECUTION_V1` 批准**（manifest
   `phase17-holdout-execution-v1.json`，contract_digest `59c618c6...`，
   `WIRED_INTO_RUNTIME`）：作为阶段③ holdout 30 例的唯一执行依据；
   v2/v3 manifest 零改动。**digest 变更记录**：`c2dc8b02...`（16 轮冻结）→
   `59c618c6...`（17 轮裁决吸收后重冻结：holdout_batches 阈值键 `pass_min`、
   source closure 14 路径 digest 重算）；registry 同步更新并经用户确认。
2. **15 CNY 总盘预算封装**（2026-08-02 对话批准，承接 Phase 16 V9 批准记录 §7
   第 7 项）：`project_budget_cny = 15.000000`（含历史 6.604131）、
   `forward_budget_remaining_cny = 8.395869`；阶段③任何 run 不越过该封装。
3. **身份固定**：阶段③只允许 `gpt-5.6-luna` / `synapse-ai.uk` / 零温度 /
   90s 截止 / json_mode / 8000+2800 token 上限；任何身份变更 = 新 contract digest
   = 用户重新批准，不挂原 Phase 17 结果。
4. **approved digest registry 机制**：`PHASE17_APPROVED_CONTRACT_DIGEST` 更新
   必须经用户明确批准（对话确认）；不可重签。
5. **数据集 manifest 机制**：阶段③真实 30 例起草 → 用户终审 → 冻结 manifest →
   新 dataset digest；探路结果不得反向修改 Prompt/代码/案例/阈值。
6. **预算池纪律**：RESERVE/SETTLE/RELEASE append-only 事件记账；UNKNOWN_USAGE
   按 stage 预留最坏情况入账；失败如实入账、不重跑不刷分。
7. **阶段③每次真实模型 run 前用户单独批准**；成本、receipts、tokens 入账后
   与 CLI 报告核对。

## 8. 相关文件

- 契约 manifest：`evaluation/manifests/phase17-holdout-execution-v1.json`
- 账本 DDL：`docker/init_phase17_holdout_ledger.sql`
- 账本实现：`src/decision_support/phase17_holdout_ledger.py`
- 执行器：`src/decision_support/phase17_holdout_runner.py`
- 数据集机制：`src/decision_support/phase17_holdout_dataset.py`
- 注册表：`src/decision_support/phase17_approved_digest.py`
- CLI：`scripts/run_phase17_holdout.py`
- 测试：`tests/unit/test_phase17_holdout_execution.py`、
  `tests/unit/test_phase17_holdout_dataset.py`、
  `tests/integration/test_phase17_holdout_execution_postgres.py`、
  `tests/integration/test_phase17_holdout_ledger_postgres.py`、
  `tests/integration/test_phase17_holdout_runner_postgres.py`
- Phase 16 关联：`docs/superpowers/reports/phase-16-v9-contract-approval-record.md` §7
