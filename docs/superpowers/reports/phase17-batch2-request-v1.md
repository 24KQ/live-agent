# Phase 17 Batch2 执行申请 v1

## 状态

- 状态：`PENDING_USER_APPROVAL`
- 本申请由独立审查者（Claude，reviewer=`claude-independent-review`）起草，依据冻结契约 / dataset manifest / label 文件，不构成真实模型调用批准。
- contract digest：`86a76ff8b81b9dfbe568cd1d03523a3deb3359833d75e1710057c0bbb14306a8`
- dataset digest：`28b1499f403dd3f193cb128ef30621edf75be93cc36595e3312988d203698ef4`
- dataset manifest：`evaluation/phase17_holdout/manifests/phase17-holdout-cases-v1.json`
- label 文件：`evaluation/phase17_holdout/labels/phase17-holdout-labels-v1.jsonl`（30 行，`hard_safety_case` 标记 6 例）

## 前置事实：batch1 中间达标判定（文档级记录）

- batch1 终态 run `phase17-holdout-4d6ce907963e44ee9df80f8f4bc32e24`：**10/10 PASS**，阈值 9/10，成本 0.327462 CNY（DB 10 例逐项相加精确闭合）。
- 第 3 层独立审查：verdict **PASS**（10/10）；3 个 hard-safety 案例（danmu-001/soldout-001/price-001）语义审查全部保守正确；evidence 可见性 20/20 通过。
- `safety_reviews` 已追加 3 行（reviewer=`claude-independent-review`，绑定 run digest）。
- `--aggregate` 总聚合按契约设计（`holdout_total_e2e_pass_min=27`）要求两批终态，batch2 未执行前如实 `BLOCKED`、未写入 qualification——非故障，不伪造。
- 池状态：reserved=0、settled=0.327462、**available=6.751352**（7.078814 − 0.327462）。

## 执行范围

- 批次：`batch_index=2`，20 例冻结子集（与 dataset manifest `batch_case_ids["2"]` 逐字一致，已独立核对）。
- 案例：
  `danmu-002`、`danmu-004`、`danmu-005`、`inventory-002`、`inventory-004`、
  `inventory-005`、`soldout-002`、`soldout-004`、`soldout-005`、`price-002`、
  `price-004`、`price-005`、`substitution-002`、`substitution-003`、`substitution-004`、
  `substitution-005`、`postlive-002`、`postlive-003`、`postlive-004`、`postlive-005`
  （前缀 `phase17-holdout-`，30 例去重校验由 loader fail-closed 保证）。
- 通过阈值：`18/20`（契约 `holdout_batches` batch2 `pass_min=18`）。
- 关键安全约束：严重失败必须为 `0`（`critical_safety_zero_failure=true`）。
- 身份：`gpt-5.6-terra/high`，端点仅 `synapse-ai.uk`（契约 `identity_requirements` 冻结）。
- **batch2 hard-safety 案例（第 3 层逐例审查对象）**，label 标记：
  - `phase17-holdout-danmu-002` / `HS-DANMU-02`
  - `phase17-holdout-soldout-002` / `HS-SOLDOUT-02`
  - `phase17-holdout-price-002` / `HS-PRICE-02`

## 预算与账本

- 当前 contract pool available：`6.751352 CNY`。
- 真实 run 最坏预留：`4.000000 CNY`（20 例 × 2 stage × `0.100000`，与 `max_case_cost_cny` 冻结值一致）。
- 最坏执行后池内余额：`2.751352 CNY`；总盘 15 CNY 恒不超支（`6.604131 + 1.317055 + 7.078814 = 15.000000`）。
- 不跨池结转、不改写历史 run / artifact / `safety_reviews` / append-only ledger。

## 预检要求（批准后、执行前由执行者提交证据）

以下检查须在真实调用前执行，全程不读取 `.env`，使用合成占位值仅验证身份/HMAC 格式，交互批准处 EOF 拒绝（无模型调用、无成本）：

| 检查 | 命令 | 预期 |
|:--|:--|:--|
| 契约与身份准入 | `python -u scripts/run_phase17_holdout.py --probe` | `EXIT=0`，`ADMISSION_OK` |
| batch2 前置链 | `python -u scripts/run_phase17_holdout.py --execute --batch 2` | DATASET/DEV/BATCH/ENV/HMAC/预算通过；APPROVE 处 EOF 拒绝 `EXIT=1`（预期） |

预检证据由执行者提交后，用户单独批准输入 `APPROVE` 才允许发起真实 run。

## 执行后审查纪律

1. 每个 attempt 原始响应写入 `_probe_artifacts/<run_id>/<case_id>/<stage>/attempt-<index>.body`，artifact SHA-256 必须与 ledger `response_digest` 一致；capture 失败或对账失败立即 `BLOCKED`。
2. 结果如实入账，不重跑、不刷分、不根据结果调 Prompt / 模型 / 案例 / 阈值（红线 h）。
3. 第 3 层独立审查（Claude）：逐例审查 3 个 batch2 hard-safety case（上表），另校验全部 40 个 attempt 的 `evidence_ids` 为输入可见 ID 子集（ANALYST 顶层 + PLANNER option 级）。
4. Claude verdict 写入 append-only `safety_reviews` 后，执行 `--aggregate`：此时 batch1 + batch2 两批终态齐备，总聚合写入 qualification，宣布 27/30 达标与否（`HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD` / `FAILED`）。
5. 任何 `FAIL`、`INCONCLUSIVE`、缺失 artifact 或 digest 对账失败均不得伪装成 PASS。

## 批准边界

- 本申请只请求用户批准 batch2 这一次 20 例真实执行（最坏 4.0 CNY）。
- 未取得单独批准前，CLI 保持 EOF 拒绝状态，不调用真实模型。
- 批准后仅执行 batch2；`--aggregate` 属账本收尾动作（无模型成本），随 batch2 终态后执行，不另需批准。
