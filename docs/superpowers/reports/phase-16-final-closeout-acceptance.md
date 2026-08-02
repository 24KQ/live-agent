# Phase 16 最终收口验收（证据补强版）

本报告是 Phase 16 受控多 Agent E2E（V5）收口的最终验收文档，按已批准计划
**带补充项批准**执行：补强"真实 failover / 重试证据"与"完整账本 + 总成本 +
merge 就绪"两类缺口。报告同时记录补强 run 暴露并修复的真实 schema 缺陷。
所有数据均可从 SQL append-only 账本复验。

- Acceptance status: `PASS`
- Candidate digest: `1b4323655808df44ce7f83b3...`（补强 run 未触发新重冻结）
- Policy digest: `1aa9ca6fe5a85702a256e29f...`（执行契约，v2）
- V9 评价契约（2026-08-02）：`evaluation/manifests/phase16-qualification-policy-v3.json`
  digest `75319a4a...`，批准记录见
  `docs/superpowers/reports/phase-16-v9-contract-approval-record.md`；codex 第六轮
  终审正式裁决 **`V9_RETROSPECTIVE_EVALUATION_CLOSED`**
  —— 本报告为 **V9 = `DEVELOPMENT_VALIDATION_QUALIFIED`**（高 reasoning 模式资格
  认证）的证据基座，≠ 原始 V5 PASS（V5 保持「未通过、未恢复」）
- Corpus manifest: `31d05088a2901fb3ad9472e7...`
- Model / Effort: `gpt-5.6-terra` / `high`
- 渠道链（顺序即优先级）: `synapse-ai.uk,api.imagebridge.top,ai.vote520.com`
  - 补强 run 声明组合: `ai.vote520.com,synapse-ai.uk,api.imagebridge.top`（vote520 优先）
- 账本总成本（12 runs）: `6.6041 CNY`（全精度 **6.604131**）/ `1,894,873 tokens`
  / `271 receipts` / `332 transport attempts`（Σ attempt_count）

## 1. 补充项 A —— 重试路径证明边界（诚实声明）

V5 adapter 有三类可重试 / 换端路径，**真实模型证据只覆盖其中一类**：

| 路径 | 触发条件 | 证明方式 |
|:--|:--|:--|
| TRANSPORT_ERROR 重试 + 换端 | 连接层失败（本报告注入验证） | **真实 run 证据**（24/24 次 attempt_count=3） |
| HTTP 5xx 退避重试（1s） | 服务端错误 | 仅单测（`tests/unit/test_phase16_v5_controlled_e2e.py` 脚本化 503 队列 + fake sleep 断言 backoff） |
| 429 换端不重试 | 网关级限流 | 仅单测（脚本化 429 队列断言换端、attempt_count 语义） |
| DEADLINE_EXCEEDED 不重试 | 绝对 deadline 耗尽 | 仅单测（不足窗口断言不发起第二次调用） |

- 单测覆盖：TRANSPORT 重试成功 `attempts==2`、503 重试 + backoff≈1s、429 不重试
  `attempts==1`、deadline 不足不重试、连续两次 TRANSPORT → 返回失败、一次成功
  `attempts==1`、渠道序列 `[500,500,200]` → `attempts==3` 且 URL 序列正确、
  `[429,200]` 换渠道、未知 host / 白名单外 effort 拒绝。
- 诚实边界：5xx / 429 / DEADLINE 路径尚未被真实 provider 事件触发过；未来若发生，
  由 ledger 的 `attempt_count` / `responded_endpoint_host` 列直接验证。

## 2. 真实 failover 证据 run（补充证据）

### 2.1 注入方法（纯本机、可逆、零系统改动）

- 利用 `HttpxAsyncHttpTransport()` 无参构造默认 `trust_env=True`，以进程级
  `HTTPS_PROXY=http://127.0.0.1:9` + `NO_PROXY=synapse-ai.uk,api.imagebridge.top`
  定向让 `ai.vote520.com` 连接拒绝（TRANSPORT_ERROR），synapse / imagebridge 不受影响。
- 无需 hosts 文件、无需管理员、不触碰提供商后台；run 结束后恢复 `.env`
  （synapse-first 原顺序，行尾归一化为 CRLF），注入变量仅存在于该 run 的进程环境。

### 2.2 预检 probe（证据文件）

- `docs/superpowers/reports/probe-channel-ai.vote520.com-20260801T135617.json`：
  TRANSPORT_ERROR × 3（注入生效）
- `docs/superpowers/reports/probe-channel-synapse-ai.uk-20260801T135727.json`：
  L1 3/3 + L2 10/10 健康（对照组）
- 两个文件均已核对：无任何 API key / 敏感载荷泄漏。

### 2.3 注入 dev campaign 结果

- Campaign: `phase16-development-1b4323655808df44-d90f9f3d7fec454b`
  - 声明组合（vote520 优先）→ 合法新 campaign_id（同 digest 异组合）
- Run: `phase16-development-1b4323655808df44-d90f9f3d7fec454b-20260801T140424`
- Status: `PASS`（EXECUTION_COMPLETE），E2E_MULTI_AGENT_READY `12/12`，其余指标
  12/12（**该 run 内部口径**；12 个 run 整体为 8 PASS / 4 FAILED），
  ledger authenticated = `True`
- Assessment digest: `b3f56d1f09a57c78df08552c...`；evaluation digest
  `da5e76328280a901...`
- Receipt 分布（账本查询，24/24 行）：
  - `attempt_count = 3`（vote520 失败 → 重试 → failover 至 synapse 成功），
    `responded_endpoint_host = synapse-ai.uk`，`reasoning_effort = high`，
    `model = gpt-5.6-terra`，`finish_reason = stop`，`receipt_complete = True`
  - tokens：168,393 total；成本：0.5370 CNY
  - 延迟：ANALYST avg 10.5s（min 7.7s / max 14.5s）；PLANNER avg 13.2s
    （min 7.8s / max 21.4s）
- 意义：**24 次调用全部真实走完「失败 → 重试 → 换渠道 → 成功」全链**，是
  TRANSPORT_ERROR 重试 + failover 的首份真实 provider 证据；同一 run 同时验证
  了 attempt_count / responded_endpoint_host 两列在真实路径下的记录正确性。
- 次要旁证：更早的 xhigh run `phase16-development-9eda8e8ae370dc1b-20260731T213032`
  （imagebridge 优先）曾有 9 次 receipt 记录 attempt_count>1、2 个不同响应端点，
  同样走通 failover（该 run 因部分调用失败整体 FAILED，见第 5 节清单）。

## 3. 补强 run 暴露的真实 schema 缺陷与修复

- 缺陷：campaigns 表遗留约束 `UNIQUE (policy_digest, campaign_kind, candidate_digest,
  batch_index)` 不含声明组合。补强 run（vote520 优先）第一次启动即被该约束以
  UniqueViolation 拦截——同 digest 的 synapse-first campaign（3b173072）已占行，
  与"组合是身份的一部分"（1b54346 身份修复）直接冲突。上轮身份修复漏改 campaigns
  表约束，且集成测试无"同 digest 不同组合并存"覆盖，缺陷漏过。
- 修复（本次收口）：
  - 新幂等迁移 `docker/alter_phase16_qualification_campaign_identity.sql`：
    DROP 该遗留约束（DO-block，可重复执行）；
  - `docker/init_phase16_qualification_ledger.sql` CREATE TABLE 同步删除该约束行
    （新库一致）；
  - `scripts/run_db_migrations.py` 注册 `phase16_qualification_campaign_identity`；
  - 新增 2 个集成回归测试：`test_qualification_same_digest_distinct_declared_combos_are_separate_campaigns`
    （同 digest 异组合并存）与 `test_qualification_same_digest_same_declared_combo_is_idempotent_single_row`
    （同组合幂等单行）。
- 迁移执行：`30/30 [PASS]`；防刷分语义不受影响——同 (digest, 组合) 仍被
  campaign_id PRIMARY KEY + ensure_campaign 声明字段查重双重拒绝。

## 4. 补充项 B —— 完整 campaign / run 账本清单

### 4.1 campaigns 表（13 行）

| campaign_id | kind | digest | model/effort | 声明渠道（顺序即优先级） |
|:--|:--|:--|:--|:--|
| phase16-development-candidate-1 | DEV | eb0f109b | luna/– | synapse |
| phase16-validation-candidate-1 | VAL | eb0f109b | luna/– | synapse |
| phase16-development-cdd634447ad6e459 | DEV | cdd63444 | luna/– | synapse |
| phase16-development-3c10985be7a89a36 | DEV | 3c10985b | luna/– | synapse |
| phase16-development-5f4afbda491d345b | DEV | 5f4afbda | luna/– | synapse |
| phase16-validation-5f4afbda491d345b | VAL | 5f4afbda | luna/– | synapse |
| phase16-development-9eda8e8ae370dc1b | DEV | 9eda8e8a | luna/xhigh | imagebridge, synapse |
| phase16-development-8623a075a24c8727 | DEV | 8623a075 | luna/xhigh | synapse, imagebridge |
| phase16-validation-8623a075a24c8727 | VAL | 8623a075 | luna/xhigh | synapse, imagebridge |
| phase16-development-f28d7e03ef8be1ff | DEV | f28d7e03 | luna/xhigh | synapse, imagebridge, vote520 |
| phase16-development-1b4323655808df44-3b1730721cba735f | DEV | 1b432365 | terra/high | synapse, imagebridge, vote520 |
| phase16-validation-1b4323655808df44-3b1730721cba735f | VAL | 1b432365 | terra/high | synapse, imagebridge, vote520 |
| phase16-development-1b4323655808df44-d90f9f3d7fec454b | DEV | 1b432365 | terra/high | **vote520, synapse, imagebridge** |

注：`f28d7e03` 为失败预案 campaign（内容级失败，FAILED 终态，防刷分占位）；
`d90f9f3d` 为本报告注入证据 run 的新增行（同 digest 异组合 → 合法新名额）。
所有 campaign `reservation_cny = 1.500000`。

### 4.2 runs 表 + receipts（12 行）

| run_id（截断时间戳） | status | receipts | retried | 端点 | 成本 CNY | tokens |
|:--|:--|:--|:--|:--|:--|:--|
| candidate-1 dev 093840 | PASS | 24/24 | 0 | –* | 0.6222 | 182,557 |
| cdd63444 dev 115908 | FAILED | 22/22 | 0 | –* | 0.5912 | 170,928 |
| 3c10985b dev 154732 | FAILED | 23/23 | 0 | 1 | 0.5841 | 172,616 |
| 5f4afbda dev 160222 | PASS | 24/24 | 0 | 1 | 0.6086 | 180,326 |
| 5f4afbda val 161046 | PASS | 24/24 | 0 | 1 | 0.6170 | 181,900 |
| 9eda8e8a dev 213032 | FAILED | 20/20 | 9 | 2 | 0.4616 | 110,686 |
| 8623a075 dev 221215 | PASS | 24/24 | 0 | 1 | 0.5138 | 125,373 |
| 8623a075 val 222715 | PASS | 24/24 | 0 | 1 | 0.5265 | 127,645 |
| f28d7e03 dev 071416 | FAILED | 14/14 | 0 | 1 | 0.4550 | 135,288 |
| 1b432365-3b173072 dev 125602 | PASS | 24/24 | 0 | 1 | 0.5419 | 169,143 |
| 1b432365-3b173072 val 130201 | PASS | 24/24 | 0 | 1 | 0.5453 | 170,018 |
| 1b432365-d90f9f3d dev 140424 | PASS | 24/24 | **24** | 1（synapse） | 0.5370 | 168,393 |

\* candidate-1 / cdd63444 两 run 的 receipt 写于 attempt_count / responded_endpoint_host
列迁移之前（默认 1 / NULL），属遗留行；此后 run 全部带完整传输层事实。
FAILED 均为 development 预案 run（基础设施瞬态 / 上游内容级失败），终态不可重跑，
与验收结论无关。

## 5. 补充项 C —— phase16 总成本与 merge 就绪声明

### 5.1 总成本

- 资格账本口径（本报告 12 runs 全量）：**6.6041 CNY**（全精度 6.604131），
  1,894,873 tokens；271 张 receipt、332 次 transport attempts（Σ attempt_count，
  含注入 run 24 receipts × attempt_count=3 = 72 attempts）。
- 更早 V1/V2/V3/V4 smoke 与探针成本记录于各自验收文档（smoke 账本独立）。
- 预算约束：policy `project_budget_cny` 内；每 run 4.00 CNY 预算上限、0.10 CNY
  stage 预约，未触顶。

### 5.2 merge 就绪声明（带前提）

- 代码与证据层面：Phase 16 V5 受控多 Agent E2E 已按规划收口——传输层重试 +
  渠道链 failover + 矩阵配置运行时化 + 身份含声明组合 + 补强证据全部落地；
  dev / validation 双 PASS（terra/high）、真实 failover 证据 PASS、账本
  authenticated、回归测试全绿。
- **最终确认由 GitHub Actions PR 运行负责**：合并触发 agent-runtime-pr.yml
  （unit → integration → coverage gate → release gate（零 phase16 引用）→
  敏感载荷检查 → 文档编码检查）；本地等价检查此前逐项 PASS，最终以 PR 运行
  结果为准。
- merge 动作按约定等待用户审批，不随本 commit 发生。

## 6. Digest 边界说明

- 候选 digest `1b4323655808df44` **未变**：补强 run 不修改任何闭包代码，只改
  `.env`（非闭包）声明的渠道顺序 → 新 campaign_id `d90f9f3d` 而非新 digest。
  这正体现"声明组合是身份的一部分"修复的生效路径：切模型/强度/渠道零重认证
  周期，白名单外参数才触发重冻结。
- 本轮收口含闭包内改动（ledger 身份校验 + campaigns 约束），但已在上一 commit
  （1b54346）完成重冻结；本次仅新增迁移/测试/证据/文档，不改变 policy / candidate
  digest 前缀。

## 7. 回归门禁（本地复核，最终以 PR 运行为准）

- 针对性回归（本次）：`63 passed`（V5 adapter 重试/failover 单测 + ledger 身份
  单测 + 5 项 ledger 集成测试，含 2 个新增 schema 回归用例）
- 全量单测：unit `1703 passed`；integration `250 passed`（7 deselected）
- Coverage gate：`PASS`（line 92.03% ≥ 90，branch 85.24% ≥ 85，closure 匹配）
- Release gate（--mode pr）：`PASS`（technical 36/36，零 phase16 引用，
  external_calls = false）
- 敏感载荷检查（--tracked）：`PASS`；文档编码检查（--docs-only）：`PASS`
- Migrations：`30/30 [PASS]`

## 8. 结论

- 三个补充项全部落实：重试路径证明边界诚实声明（第 1 节）、完整账本清单
  （第 4 节）、总成本 + merge 就绪声明（第 5 节）。
- 真实 failover 证据补强完成：24 张 stage receipt × `attempt_count=3` = 72 次
  transport attempts 全链「失败 → 重试 → 换渠道 → 成功」（attempt 级聚合证据，
  逐次网络 receipt 不存在于账本，v3 已定义三计数口径），含 schema 缺陷发现与修复闭环。
- Phase 16 收口证据链完整；merge 到 main 是用户审批的最终步骤。
