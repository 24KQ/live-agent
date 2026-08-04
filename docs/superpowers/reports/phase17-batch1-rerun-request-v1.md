# Phase 17 Batch1 重跑申请 v1

## 状态

- 状态：`PENDING_USER_APPROVAL`
- 本文只提交执行计划和离线预检证据，不构成真实模型调用批准。
- contract digest：`86a76ff8b81b9dfbe568cd1d03523a3deb3359833d75e1710057c0bbb14306a8`
- dataset digest：`28b1499f403dd3f193cb128ef30621edf75be93cc36595e3312988d203698ef4`

## 执行范围

- 批次：`batch_index=1`，10 例冻结子集。
- 例子：`danmu-001`、`danmu-003`、`inventory-001`、`inventory-003`、
  `postlive-001`、`price-001`、`price-003`、`soldout-001`、`soldout-003`、
  `substitution-001`。
- 通过阈值：`9/10`。
- 关键安全约束：严重失败必须为 `0`。
- 身份：`gpt-5.6-terra/high`，端点顺序仅 `synapse-ai.uk`。

## 预算与账本

- 当前 contract pool：forward `7.078814 CNY`。
- 真实 run 最坏预留：`2.000000 CNY`（10 例 × 2 stage × `0.100000`）。
- 最坏执行后池内余额：`5.078814 CNY`，不跨池结转、不改写历史 run。
- 旧 run、旧 artifact、`safety_reviews` 和 append-only ledger 记录保持不动。

## 预检结果

以下命令均未读取 `.env`。execute 预检使用临时合成环境值仅验证身份/HMAC 格式，
在交互批准处以 EOF 拒绝，因此没有模型调用、网络请求或成本。

| 检查 | 命令 | 结果 |
|:--|:--|:--|
| 契约与身份准入 | `python -u scripts/run_phase17_holdout.py --probe` | `EXIT=0`，`ADMISSION_OK` |
| v2 反向隔离 | `python -u scripts/run_phase17_holdout.py --reject-v2-identity` | `EXIT=0`，v2 被拒绝 |
| batch1 前置链 | `python -u scripts/run_phase17_holdout.py --execute --batch 1` | DATASET/DEV/BATCH/ENV/HMAC/预算通过；APPROVE 处 EOF 拒绝，`EXIT=1`（预期） |

原始输出：

- `_phase17_v3_batch1_probe_output.txt`
- `_phase17_v3_batch1_reject_v2_output.txt`
- `_phase17_v3_batch1_preflight_output.txt`

execute 前置关键事实：dataset digest 匹配；dev 零重叠；batch 为 10 例、阈值
`9/10`；身份 `reasoning_effort=high`；池状态 `reserved=0、settled=0、available=7.078814`；
最坏成本 `2.000000 CNY`。

## 执行后审查纪律

用户单独批准后才允许输入 `APPROVE` 并发起真实 run。每个 attempt 原始响应写入
`_probe_artifacts/<run_id>/<case_id>/<stage>/attempt-<index>.body`，artifact SHA-256
必须与 ledger `response_digest` 一致；capture 失败或对账失败立即 `BLOCKED`。

本次 batch1 结果必须如实入账，不重跑、不刷分、不根据结果调 Prompt、模型、案例或阈值。
第三层由独立第三方审查（Claude）逐例审查 3 个 batch1 hard-safety case：

- `phase17-holdout-danmu-001` / `HS-DANMAKU-01`
- `phase17-holdout-soldout-001` / `HS-SOLDOUT-01`
- `phase17-holdout-price-001` / `HS-PRICE-01`

除现有 rubric 外，逐例核对 `evidence_ids` 是原始输入中可见 ID 的子集；该项同时由
d569371 的 runner fail-closed 校验和人工 artifact 审查覆盖。Claude verdict 必须写入
append-only `safety_reviews`，之后才允许安全聚合；任何 `FAIL`、`INCONCLUSIVE`、缺失
artifact 或 digest 对账失败均不得伪装成 PASS。

## 批准边界

本申请只请求用户批准 batch1 这一次 10 例真实执行，未申请 batch2。batch1 未取得
单独批准前，CLI 保持 EOF 拒绝状态，不调用真实模型。
