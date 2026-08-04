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
- **codex 第十八轮裁决（2026-08-03，verbatim 裁决摘要）**：「允许进入数据起草、
  用户终审、manifest 冻结，并在冻结后提交真实 Probe 评审；**不授权当前版本直接
  执行真实模型调用**」。9 项修正已全盘接受并落地（本检查点）：
  - P0-1：精确集合校验去重（`len(set)` 而非 set 比较）；聚合前校验 batch 1/2
    归属 + contract/dataset/candidate 身份 + 总 case 数；`RunReport` 增加
    batch_index / 全链 digest / critical_safety_failures 字段；
    `critical_safety_zero_failure` 在 runner/aggregator 中执行（
    ANALYST_VALIDATION_FAILED = 关键安全失败，独立于 9/10 阈值线；BLOCKED 优先）。
  - P0-2：`campaign.batch_index == execute(batch_index)` + campaign 声明
    model/endpoint/reasoning 校验；candidate profile digest 与 adapter digest
    源码级校验；`PHASE17_APPROVED_DATASET_MANIFEST_DIGEST` 注册机制
    （未获批 = fail-closed）；dev 隔离与真实 dev 数据集独立交叉验证。
  - P0-3：逐网络 attempt 独立行（`attempt_details` 协议扩展 + runner 逐行入账，
    中间失败行 0 成本、成功行 usage 定价、全失败时最后一行记 stage 预留）；
    HMAC payload 覆盖 token 字段；cost bug 修复（stage 级预留而非 campaign 级）。
  - P1-4：CLI schema 存在性检查（统一 migration 入口，消除双路径）；异常统一
    终态化（attempts 成本 settle 或 release）；`--aggregate` 27/30 结论持久化
    （新增第 8 张表 `phase17_holdout_qualifications`）。
  - P1-5：source closure 14 → **18 路径**（+`docker/init_phase17_holdout_ledger.sql`、
    `scripts/run_db_migrations.py`、`src/specialist_runtime/deepseek_adapter.py`、
    `src/specialist_runtime/model_port.py`）；随后 Phase 17 逐 attempt 审计
    改为独立 adapter（`src/specialist_runtime/phase17_v5_adapter.py`，不触碰
    phase16 冻结闭包）→ closure **19 路径**。
  - P1-6：文档 6→8 张表修正；gate 证据带原始命令输出；工作树清理。

## 1. Phase 17 执行契约定义

- manifest：`evaluation/manifests/phase17-holdout-execution-v1.json`
- contract_digest 候选值：`bcd9649fdf5cebda5d99f4426b66e201f3e94f24f85408867b9352e59c98be87`
  （2026-08-03 capture / safety review / aggregate hard gate 修正后重冻结；
  contract_digest 自校验 =
  `canonical_json_sha256(model_dump(exclude={"contract_digest"}))`；
  source closure 21 路径 digest 重算）。该值尚未获得用户批准，
  `PHASE17_APPROVED_CONTRACT_DIGEST` 仍保留上一版批准值
  `183af27c35f3c6f82f267c7ea589057cfd6b1f076329d624c665803585af8bb5`，
  因此当前真实执行入口继续 fail-closed。
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
- **数据集注册（codex 第十八轮 P0-2）**：`PHASE17_APPROVED_DATASET_MANIFEST_DIGEST`
  是 30 例 holdout 数据集 manifest 的批准事实；未批准（`None`）时 CLI `--execute`
  拒绝任何 manifest（fail-closed）；数据起草完成、用户终审并冻结后才填入真实
  digest。`--manifest` 不得指向任意自洽 manifest。

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

- 独立表族（`docker/init_phase17_holdout_ledger.sql`，**8 张表**全部 append-only，
  UPDATE/DELETE/TRUNCATE 触发器拒绝）：contracts / campaigns（batch_index ∈ (1,2)、
  UNIQUE(contract_digest, batch_index)）/ budget_events / runs / run_results /
  case_results / **attempts**（逐网络尝试证据，UNIQUE(run,case,stage,attempt_index)）/
  **qualifications**（27/30 聚合结论，UNIQUE(run1_id, run2_id)，codex 十八轮 P1-4）。
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
- **受控全量 integration gate 待 codex 第十九轮确认后与本检查点一并复核**。

### 6.1 Capture / safety review checkpoint（候选版本，未获执行批准）

- 每次真实网络 attempt 的原始 `response.body` 写入
  `_probe_artifacts/<run_id>/<case_id>/<stage>/attempt-<index>.body`；
  文件使用独占创建、回读后计算 SHA-256，capture 失败立即停止 retry/failover。
- `phase17_holdout_attempts` 保存 `artifact_path`、
  `artifact_digest`、`artifact_capture_status`，并要求 artifact digest 与
  ledger `response_digest` 相等；路径还必须与
  `run_id/case_id/stage/attempt_index` 四元身份精确匹配。
- `phase17_holdout_safety_reviews` 为 append-only 独立第三方审查表，
  reviewer 固定为 `claude-independent-review`；SQL 触发器要求审查只能绑定
  已终态 run 的真实 captured artifact。
- `--aggregate` 重新校验 labels 与 manifest 的完整 case 集合、两批终态 run
  的精确 batch case 集合，以及 6 个 hard-safety case 的 artifact/digest；
  六例均为 Claude `PASS` 才能继续，`FAIL` → `FAILED`，缺失、对账失败或
  `INCONCLUSIVE` → `BLOCKED`。省略安全门禁本身也只能得到 `BLOCKED`。
- 本轮离线证据：capture/safety 单元测试 6 passed、`compileall` 通过、
  `git diff --check` 通过；未读取 `.env`、未连接数据库、未调用真实模型。
- 本轮变更文件及原因：`phase17_holdout_capture.py`（原始响应 capture）、
  `phase17_v5_adapter.py`（逐 attempt capture 绑定）、`phase17_holdout_ledger.py`
  与 `init_phase17_holdout_ledger.sql`（artifact 字段、HMAC 身份绑定、安全审查
  表与 SQL 边界）、`phase17_holdout_runner.py`（安全聚合硬门禁）、
  `run_phase17_holdout.py`（labels/case-set 核验）、`record_phase17_safety_review.py`
  （独立审查入账）、`.gitignore`（防止 artifact 误提交）。
- **codex 第十八轮 9 项修正落地证据（2026-08-03，全部离线）**：
  - P0-1：精确集合校验去重；aggregate 校验 batch 1/2 归属 + contract/dataset/
    candidate 身份 + 总 case 数（测试 `aggregate_identity_checks`：错序、候选
    漂移、数据集漂移、缺 batch 全部拒绝）；critical safety 红线执行——
    `critical_safety_zero_failure_enforced`（9 PASS + 1 analyst 失败 → FAILED
    CRITICAL_SAFETY 红线，证明与 9/10 阈值是独立线）、
    `critical_safety_blocked_wins_over_critical`（BLOCKED 优先）。
  - P0-3：`attempt_rows_per_network_attempt`（重试后成功 → 40 行 receipt，
    中间失败行 0 成本、最终行 usage 定价 0.006、HMAC 逐行不同、case
    receipt_count=4 对账真实网络调用数）；`attempt_unknown_usage_last_row_reservation`
    （全失败 → 每 stage 一行 stage 预留 0.1，共 1.0，绝不使用 campaign 级预留）；
    `attempt_hmac_covers_token_fields`（payload 含 token 字段，tokens 伪造
    必然 HMAC 失配）。
  - P1-4：`schema_ready_and_run_ledger_state`（schema 存在性检查 +
    `phase17_run_ledger_state` 未终态成本累计）；`batch_run_report_identity_and_cases`
    （--aggregate 输入身份）；`qualification_record_unique_and_identity`
    （27/30 结论只入账一次、batch 归属错误拒绝、未终态拒绝）。
  - P1-5：source closure 18 → **19 路径**重冻结（+`phase17_v5_adapter.py`
    独立 adapter）→ contract_digest `183af27c...`。
  - **回归发现与修复（2026-08-03，gate 前置检查）**：受控全量 unit gate 发现
    37 个 phase16 测试 fail-closed（MANIFEST_IDENTITY_MISMATCH / source code
    digest drift）。根因：P0-3 的 attempt_details 协议扩展曾修改 phase16
    历史冻结闭包内文件——`controlled_e2e_adapter_v5.py` 位于 V5 身份路径、
    `model_port.py` 位于 multi_agent source closure，字节变更即闭包漂移
    （机制正确，暴露的是设计冲突）。修复：逐 attempt 审计**独立化**到
    `src/specialist_runtime/phase17_v5_adapter.py`（继承 V5 受控语义 + 逐
    尝试收集 + 拒绝身份 env 覆盖 + 自有 `phase17_adapter_digest`），
    phase16 两文件零改动恢复冻结；phase17 closure 相应 18→19 路径。
    验证：phase16 回归 126 passed 恢复全绿，phase17 unit/integration
    全绿，契约重冻结 4803f021 → 183af27c。
  - 本检查点测试数字：phase17 runner 20 + ledger 19 + phase16 qualification
    34（含 unit）= **73 passed**；全量 unit/integration 见 §6 上文。

## 7. 批准项清单（用户逐项确认后本记录生效）

1. **Phase 17 执行契约 `PHASE17_HOLDOUT_EXECUTION_V1` 批准**（manifest
   `phase17-holdout-execution-v1.json`，当前批准 contract_digest
   `462cd590...`，`WIRED_INTO_RUNTIME`）：用户批准并更新 registry 后，
   真实入口才允许加载；registry 不匹配时仍 fail-closed。
   v2/v3 manifest 零改动。**digest 变更记录**：
   `c2dc8b02...`（16 轮冻结）→ `59c618c6...`（17 轮裁决吸收后重冻结：
   holdout_batches 阈值键 `pass_min`、source closure 14 路径 digest 重算）→
   `4803f021...`（18 轮裁决吸收后重冻结：source closure 14→18 路径、ledger
   8 张表、CLI 终态化）→ `183af27c...`（逐 attempt 审计独立化：
   `phase17_v5_adapter.py` 不触碰 phase16 冻结闭包，source closure 18→19 路径）→
   `bcd9649f...`（capture artifact、安全审查账本、HMAC 身份绑定、聚合硬门禁、
   labels/case-set 二次核验，source closure 21 路径）→ `462cd590...`（修复
   attempts INSERT 占位符 22→23，source closure 仍 21 路）。`462cd590...` 已经
   获用户明确批准并同步 registry。
2. **15 CNY 总盘预算封装**（2026-08-02 对话批准，承接 Phase 16 V9 批准记录 §7
   第 7 项）：`project_budget_cny = 15.000000`（含历史 6.604131）、
   `forward_budget_remaining_cny = 8.395869`；阶段③任何 run 不越过该封装。
3. **身份固定**：阶段③只允许 `gpt-5.6-luna` / `synapse-ai.uk` / 零温度 /
   90s 截止 / json_mode / 8000+2800 token 上限；任何身份变更 = 新 contract digest
   = 用户重新批准，不挂原 Phase 17 结果。
4. **approved digest registry 机制**：`PHASE17_APPROVED_CONTRACT_DIGEST` 更新
   必须经用户明确批准（对话确认）；不可重签。`PHASE17_APPROVED_DATASET_MANIFEST_DIGEST`
   （18 轮新增）同理：数据集冻结后用户批准填入，未批准时任何 manifest fail-closed。
5. **数据集 manifest 机制**：阶段③真实 30 例起草 → 用户终审 → 冻结 manifest →
   新 dataset digest 并经用户批准入 registry；探路结果不得反向修改 Prompt/代码/案例/阈值；
   CLI 与真实 dev 数据集（`evaluation/phase16_qualification/development_cases.jsonl`）
   独立交叉验证排除列表。
6. **预算池纪律**：RESERVE/SETTLE/RELEASE append-only 事件记账；UNKNOWN_USAGE
   按 stage 预留最坏情况入账；失败如实入账、不重跑不刷分。
7. **阶段③每次真实模型 run 前用户单独批准**；成本、receipts、tokens 入账后
   与 CLI 报告核对。

8. **2026-08-04 用户批准 capture/safety gate 版本 contract digest**：用户批准
   `bcd9649fdf5cebda5d99f4426b66e201f3e94f24f85408867b9352e59c98be87` 并授权
   更新 `PHASE17_APPROVED_CONTRACT_DIGEST`。变更范围为 source closure **19 → 21**：
   新增 `phase17_holdout_capture.py` 和
   `record_phase17_safety_review.py`；语义只增加 6 个 hard-safety case 的
   artifact capture、独立第三方安全审查入账与聚合硬门禁，未改变身份、预算、
   阈值或 Phase 16 冻结契约。批准前的全量 gate 补证已保存；批准后必须重新运行
   unit、integration、encoding gate，确认原先 registry 未批准导致的 fail-closed
   结果全部转绿，才进入 4.1 样例前置检查点。真实模型执行仍须逐 run 单独批准。
   批准后全量 integration 暴露 `phase17_holdout_ledger.py` 的 SQL 占位符缺失：
   attempts INSERT 有 23 个参数但只有 22 个 `%s`。该修复只增加一个占位符和中文
   说明注释，未改变身份、预算或阈值；因该文件属于 21 路 source closure，重新计算
   candidate digest 为 `462cd590d171bf71d3c94099c7bddee1c85d3aea05da94dfdab91de38d55afd9`。
   `bcd9649f...` 的批准不自动覆盖此后续代码修复；新 digest 须重新获得用户批准后
   才能更新 registry 并重跑全量 gate。

9. **2026-08-04 用户批准修复后的 contract digest**：用户独立复核确认
   `462cd590d171bf71d3c94099c7bddee1c85d3aea05da94dfdab91de38d55afd9` 的变更范围
   仅为 `phase17_holdout_ledger.py` 的 attempts INSERT 占位符从 22 补为 23，
   并增加三行中文说明注释；23 列、23 占位符、23 参数逐项一致。用户批准重冻结
   manifest 的 source closure（仍为 21 路，仅该文件 digest 更新）并授权将 registry
   更新为 `462cd590...`。本批准不改变身份、预算、阈值或 Phase 16 冻结契约；更新后
   必须重跑全量 unit、integration、encoding gate，另行记录 Phase 16 restart 测试
   的干净数据库复核结果。

10. **2026-08-04 批准后 gate 完成记录**：重冻结 manifest、registry 和两份批准记录
    后，最终全量 unit 为 **1731 passed, 1 warning**，integration 为
    **295 passed, 7 deselected, 5 warnings**，文档编码 gate `EXIT=0`。Phase 16
    restart 在独立 UUID schema（fixture 创建后自动删除）下为 **1 passed**，因此
    原全量序列中的残留/隔离现象没有在干净复核中重现；不把它标成代码 flake。批准后
    第一次全量 integration 曾暴露 `test_phase17_aggregate_26_of_30_failed` 未传入
    safety PASS gate 的测试夹具遗漏，已只在该测试中补入合成安全 PASS；生产聚合器
    的“省略安全门禁即 BLOCKED”语义未修改，也不进入 contract source closure。
    最终原始输出文件保留在工作树中，真实模型执行仍未进行。

11. **2026-08-04 用户批准 30 例 holdout 数据集 manifest**：用户确认
    `851a9f5fd4808caf2851088b0a4bd899e6e4948b94a1c9be9a9ae721209d4f52`，并授权将其
    写入 `PHASE17_APPROVED_DATASET_MANIFEST_DIGEST`。manifest 文件为
    `evaluation/phase17_holdout/manifests/phase17-holdout-cases-v1.json`，包含 30 个
    `case_id -> input_digest` 映射，batch 1/2 为互斥的 10+20 子集并覆盖全集，
    dev 排除集为真实 development corpus 的 18 个 case ID，labels 与 inputs 物理分离。
    loader、input digest、canonical self-digest、UTF-8/LF/无 BOM 校验均通过。
    本批准只使数据集身份进入 registry；真实模型调用仍须按批次取得用户单独批准，
    不代表 batch1 已获执行批准。

12. **2026-08-04 batch1 离线前置检查发现待批准修复**：dataset digest 已登记后，
    `--execute --batch 1` 在 DEV 隔离检查处暴露 CLI 与 manifest 公共 API 不一致：
    CLI 读取不存在的 `dev_excluded_case_ids` 和 `case_id_to_input_digest` 属性，
    尚未进入 BATCH 输出，也未调用模型。修复仅改
    `scripts/run_phase17_holdout.py`：改用 `manifest.as_json()` 和
    `manifest.case_ids()`，并新增公开 API 回归单测；旧 contract digest
    `462cd590...` 不再覆盖该 closure 变更。source closure 中该脚本 digest 从
    `bc64448c...` 更新为 `3f493a79...`，候选 contract digest 重算为
    `9f72e076e1e8e194339c552557b5631b341f2474e3f1d878c01bb13de854f230`。
    该候选 digest 尚未获用户批准，`PHASE17_APPROVED_CONTRACT_DIGEST` 继续保持
    `462cd590...`，因此真实入口仍 fail-closed；本修复不改变身份、预算、阈值、
    数据集 digest 或 Phase 16 冻结契约。

13. **2026-08-04 用户批准 DEV 隔离修复后的 contract digest**：用户确认
    `9f72e076e1e8e194339c552557b5631b341f2474e3f1d878c01bb13de854f230`，并授权将其
    写入 `PHASE17_APPROVED_CONTRACT_DIGEST`。本次 source closure 仍为 21 路，唯一
    运行时闭包变更是 `scripts/run_phase17_holdout.py`：DEV 隔离检查由不存在的私有属性
    改为 manifest 的公开 `as_json()` / `case_ids()` API；同步新增 CLI 回归单测，测试文件
    不属于运行时 closure。身份、预算、批次阈值、dataset manifest digest
    `851a9f5f...`、Phase 16 冻结契约均未改变。该批准只解除执行契约的 fail-closed
    registry 拒绝；batch1 真实模型 run 仍须另行取得用户单独批准。

14. **2026-08-04 批次前置检查再次发现待批准修复**：在批准 `9f72e076...` 后重跑
    `--execute --batch 1`，DATASET 与 DEV 检查均通过，但 CLI 将公开方法
    `manifest.batch_case_ids` 误当成可迭代属性，触发 `TypeError`，尚未进入 ENV、
    数据库或模型调用。修复为调用 `batch_case_ids(batch_index)` 并增加有效/未知批次
    回归测试。该修复改变 source closure，候选 digest 重算为
    `de2414a2702d6d40b6111330058729fed7dc43e52e6bebdb5fc6ed59bb91a255`；registry 暂保留
    已批准的 `9f72e076...`，因此在新 digest 获批准前继续 fail-closed。身份、预算、
    阈值、dataset manifest digest `851a9f5f...` 与 Phase 16 冻结契约不变。

15. **2026-08-04 用户批准 Phase 17 身份选型更正（新 digest 待批准）**：独立质询
    发现原 `gpt-5.6-luna / reasoning_effort=null` 是从 Phase 16 candidate 默认常量
    误抄而来，并非 Phase 16 V9 最终验收身份；该选型没有独立设计记录或用户批准项。
    用户据此批准将 Phase 17 改签为 Phase 16 V9 最终验收所采用的
    `gpt-5.6-terra / reasoning_effort=high`，并要求 reasoning 强度必须实际进入
    HTTP payload，而不能只写在 manifest 中。

    本次改签的完整冻结身份如下：

    | 字段 | 新冻结值 |
    |:--|:--|
    | provider_id | `synapse-ai` |
    | model_id | `gpt-5.6-terra` |
    | endpoint_hosts（有序完整列表） | `["synapse-ai.uk"]`（单端点） |
    | reasoning_effort | `high` |
    | json_mode | `true` |
    | max_total_tokens / max_output_tokens | `8000 / 2800` |
    | per_attempt_deadline_seconds | `90` |
    | max_case_cost_cny | `0.100000` |

    运行时证据：CLI 要求 `LLM_API_REASONING_EFFORT=high`，Phase 17 adapter 通过
    V5 请求装饰器将 `high` 写入真实请求 payload；离线 transport 单测直接核验最终
    payload 字段。Phase 16 历史 builder、migration、receipt、报告和冻结闭包未改写，
    其中的 luna/null 仅保留历史事实。Phase 17 活跃路径的身份/fixture 改动涉及
    `phase16_qualification.py`、`phase16_qualification_candidate.py`、
    `phase17_v5_adapter.py`、`run_phase17_holdout.py` 及 Phase 17 测试；
    `.env.example` 同步为单端点 terra/high 示例。

    身份变更清单（以本检查点最终行号为准）：

    | 文件:行 | 变更 | 理由 |
    |:--|:--|:--|
    | `src/decision_support/phase16_qualification.py:808-817` | `luna/null` → `terra/high`；端点仍为 `synapse-ai.uk` | Phase 17 冻结常量与 manifest 同源，单端点顺序不可覆盖 |
    | `src/decision_support/phase16_qualification_candidate.py:72-99, 139-153` | 通用 profile builder 参数化；Phase 17 profile 从契约读取 terra/synapse | 保留 Phase 16 历史 builder 默认值，同时阻断 Phase 17 复用历史默认身份 |
    | `src/specialist_runtime/phase17_v5_adapter.py:192-213` | `reasoning_effort` 从无参数/要求 env 为空 → 显式要求 `high` 并传入 V5 payload | 证明实际请求强度，不允许 manifest-only 声明或环境静默漂移 |
    | `scripts/run_phase17_holdout.py:211, 381-402` | candidate id、campaign 声明由契约读取，改为 terra/high/synapse | CLI 与契约身份保持单一来源 |
    | `evaluation/manifests/phase17-holdout-execution-v1.json:2,59-69,97-116` | identity 与 21 路 source digest 重冻结 | 身份变化必须生成新 contract digest |
    | `tests/unit/test_phase17_capture.py:40-46,91-132` | fixture 改 terra/high，新增最终 payload `reasoning_effort` 断言 | 覆盖真实发送字段 |
    | `tests/unit/test_phase17_holdout_execution.py:120-151` | 新增 contract 与 candidate bundle 身份断言 | 覆盖构造层身份绑定 |
    | `tests/integration/test_phase17_holdout_*.py` | Phase 17 fixture 改 terra/high；v2 反向隔离 fixture 保持 luna/null | 活跃路径对齐新契约，历史隔离测试保留旧 canonical 事实 |
    | `.env.example:60-68` | 示例改为 terra/high/单端点 | 与 Phase 17 预检要求一致，不含任何密钥 |

    全仓扫描中明确不改的旧值：`src/decision_support/phase16_qualification_candidate.py:37`
    的 `PHASE16_HISTORICAL_MODEL_ID`、`src/specialist_runtime/profiles.py:32-39` 的
    formal model 白名单、`src/decision_support/phase16_qualification_ledger.py` 的
    通用历史默认值、Phase 16 migration（`docker/init_phase16_qualification_ledger.sql`
    及 `alter_phase16_qualification_*.sql`）、Phase 16 历史测试/receipt/report/probe。
    这些出现位置表达已发生的 luna/null 或允许的历史矩阵事实；改写它们会污染历史
    账本或改变 Phase 16 冻结闭包，不属于本次 Phase 17 身份改签。

    身份改签会改变 source closure 与 execution contract digest；本次重冻结得到：

    - candidate：`phase17-holdout-candidate-terra-high-v1`，digest
      `abe0a3fd6858423e6113777b7390be727d45ca02b24e29402e19cff3122ef484`；
    - execution contract manifest digest：
      `6cbb90299bdd961d228f47f67fce716ce9ed3f446020a923dffff6edb9004edb`；
    - self-digest：`canonical_json_sha256(exclude contract_digest)` 逐字匹配；
    - source closure：21 路，manifest 记录与当前磁盘逐文件匹配；
    - digest 历史链：`c2dc8b02... → 59c618c6... → 4803f021... →
      183af27c... → bcd9649f... → 462cd590... → 9f72e076... →
      6cbb9029...`。

    30 例数据集身份 `851a9f5f...`、预算、阈值和 Phase 16 历史契约不变。旧候选
    `de2414a270...` 与按旧 luna/null 身份登记的 `9f72e076...` 不再作为新身份的
    执行凭据；registry 暂不更新，直至用户批准新的 candidate/contract digest，
    因此新 manifest 在批准前继续 fail-closed。上述 digest 仅为离线候选值，尚未
    授权 registry 更新、预检放行或任何真实模型调用。

16. **2026-08-04 候选验证口径与 v3 闭包漂移补证（待独立复核）**：本条补充
    `6cbb9029...` 候选验证的真实执行方法，不将候选绿结果误写成当前 registry
    状态下的批准 gate。验证在独立 Python/pytest 进程内对已导入模块的
    `src.decision_support.phase16_qualification.PHASE17_APPROVED_CONTRACT_DIGEST`
    做了进程级临时覆盖，值为候选 `6cbb9029...`，随后执行候选 unit、Phase 17
    PostgreSQL integration、capture/payload 和 manifest/closure 检查。该方法没有
    修改 `src/decision_support/phase17_approved_digest.py` 文件，没有 patch loader
    实现，也没有删除或跳过 loader 的 registry 比对；但它明确是“用候选批准值
    验证候选闭包”的离线验证，不等价于当前 registry 已批准。进程退出后临时覆盖
    自动消失；随后重新读取 registry 文件确认值仍为
    `9f72e076e1e8e194339c552557b5631b341f2474e3f1d878c01bb13de854f230`，工作树
    也未产生 tracked registry 修改。

    未覆盖 registry 的真实状态复验为：unit **1727 passed + 7 failed**；Phase 17
    PostgreSQL integration **2 passed + 21 failed + 20 errors**。这些失败/error
    均在 loader/fixture admission 阶段因当前 registry `9f72e076...` 不等于候选
    `6cbb9029...` 而 fail-closed；因此候选验证报告中的 unit **1734 passed**、
    Phase 17 integration **43 passed**、Phase 17 专项 unit **21 passed** 和
    capture/payload **6 passed**，只能标注为“候选 digest 进程级覆盖下的离线
    结果”，不能标注为“当前 registry 已全绿”。本条验证没有读取 `.env`、没有
    发起真实模型请求，也没有产生真实成本。

    同时记录 v3 纯回溯评价契约的闭包漂移：
    `evaluation/manifests/phase16-qualification-policy-v3.json` 持久化
    `policy_digest=75319a4a1ce75f34134feb65a75e05857588fb3c368faf8837113ccd35746ddd`；
    按其 11 路 `source_file_digests` 做当前源码重建得到
    `b5aa5ae2fb222c907343ca314639cf8d54a7ad7d3efb1ae68e0b5c5c2fda4a43`。其中
    `src/decision_support/phase16_qualification.py` 从持久化摘要
    `99b8fd8b...` 变为当前 `9ee167e7...`，
    `src/decision_support/phase16_qualification_candidate.py` 从
    `d408eb52...` 变为当前 `357380e2...`；两者均自 Phase 17 首个执行契约
    接入提交 `6101069` 起被 Phase 17 修改，因此这是 Phase 17 既有闭包漂移，
    不是本次 terra/high 改签单独引入。当前结果应定性为 v3 的 fail-closed 安全
    拒绝，而不是静默接受漂移。

    可复验性边界必须同时写明：仓库当前
    `load_phase16_qualification_policy` 的默认路径常量仍指向 v2 manifest；
    因而上述 `75319a4a... → b5aa5ae2...` 是对 v3 manifest 执行同一 policy
    闭包重建/自认证比较所得，不能把 v3 文件误当作 v2 loader 的直接输入。
    默认 v2 loader 也会因当前源码闭包漂移而拒绝新 dispatch。V9 的历史账本、
    已完成的历史结果和 `V9_RETROSPECTIVE_EVALUATION_CLOSED` 结论不因该拒绝
    而改变；但按原 V9 验证命令重新发起 qualification dispatch 会撞上
    fail-closed，不能宣称 V9 在当前源码上可重新执行。v3 继续只承担历史评价
    读取/记录语义，不能作为 Phase 17 execution contract。以上两项补证完成前，
    `6cbb9029...` 不进入 registry，也不放行 batch1 真实模型调用。

17. **2026-08-04 用户批准 `6cbb9029...` 并更新 contract registry**：Claude 独立
    复核确认 candidate/contract digest 逐字符匹配、21 路 source closure 与磁盘一致、
    terra/high 身份与 Phase 16 V9 验收身份一致、payload 级 `reasoning_effort=high`
    断言覆盖，且第 16 项补证中的候选验证方法、未批准态数字和 v3 漂移定性均与
    独立实测一致。用户据此批准完整 digest
    `6cbb90299bdd961d228f47f67fce716ce9ed3f446020a923dffff6edb9004edb`，并授权将
    `PHASE17_APPROVED_CONTRACT_DIGEST` 更新为该值；旧的 `9f72e076...` 不再是当前
    Phase 17 contract registry 值。该操作不改变 30 例数据集 digest
    `851a9f5f...`、预算、阈值、身份或 Phase 16 历史契约，也不等同于批准 batch1
    真实模型调用。registry 更新后必须重新运行全量离线 gate 与
    `--probe`/batch1 前置 DATASET、DEV、BATCH、ENV 检查；只有这些检查全通过，才
    提交下一独立批准点：batch1 10 例、9/10 阈值、最坏预算 `2.000000 CNY`、
    `gpt-5.6-terra/high`、`synapse-ai.uk`。本条不授权真实网络请求。

## 8. 相关文件

- 契约 manifest：`evaluation/manifests/phase17-holdout-execution-v1.json`
- 账本 DDL：`docker/init_phase17_holdout_ledger.sql`
- 账本实现：`src/decision_support/phase17_holdout_ledger.py`
- 执行器：`src/decision_support/phase17_holdout_runner.py`
- 数据集机制：`src/decision_support/phase17_holdout_dataset.py`
- 数据集 manifest：`evaluation/phase17_holdout/manifests/phase17-holdout-cases-v1.json`
- 注册表：`src/decision_support/phase17_approved_digest.py`
- CLI：`scripts/run_phase17_holdout.py`
- 测试：`tests/unit/test_phase17_holdout_execution.py`、
  `tests/unit/test_phase17_holdout_dataset.py`、
  `tests/integration/test_phase17_holdout_execution_postgres.py`、
  `tests/integration/test_phase17_holdout_ledger_postgres.py`、
  `tests/integration/test_phase17_holdout_runner_postgres.py`
- Phase 16 关联：`docs/superpowers/reports/phase-16-v9-contract-approval-record.md` §7
