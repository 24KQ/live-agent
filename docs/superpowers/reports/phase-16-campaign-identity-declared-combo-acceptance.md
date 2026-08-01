# Phase 16 Campaign 身份含声明组合验收（terra/high 闭环）

本报告记录 campaign 身份修复（声明组合纳入身份）与 gpt-5.6-terra / high 组合
dev + validation 双 PASS 的完整证据链：身份冲突根因、闭包改动与 digest 边界、
两轮真实模型 campaign 的账本回执、以及全量回归门禁结果。所有资产摘要与
campaign 声明值均可从 SQL append-only 账本复验。

- Acceptance status: `PASS`（dev + validation 均 12/12）
- Candidate digest: `1b4323655808df44ce7f83b3...`
- Policy digest: `1aa9ca6fe5a85702a256e29f...`
- Corpus manifest: `31d05088a2901fb3ad9472e7...`
- Model / Effort: `gpt-5.6-terra` / `high`
- Channels（顺序即优先级）: `synapse-ai.uk,api.imagebridge.top,ai.vote520.com`

## 背景：为什么换组合

上一轮 digest `f28d7e03`（gpt-5.6-luna / xhigh / synapse）12/12 全部内容级失败：
3 次 INVALID_RESPONSE + 9 次语义验证失败，HTTP 200、延迟正常、输出短（1356 vs
2521 tokens），是典型上游模型问题特征；同代码管线数小时前 24/24 PASS。
暂停后经用户拍板：切换到 gpt-5.6-terra / high，先 probe 验证再跑正式 campaign。

- 渠道探针（`docs/superpowers/reports/probe-channel-synapse-ai.uk-20260801T115331.json`）：
  L1 3/3、L2 10/10 schema、L3 4/4（2 个真实 case analyst→planner 链）全过，
  无挂死，平均延迟 3.7s / 6.6s / 10.8s。

## 身份冲突根因与修复（闭包改动）

换组合后 dev run 被 `qualification campaign identity conflicts` 拦截：candidate
digest 绑定的是白名单闭包代码而非运行时声明组合，terra/high 声明后 digest 不变
（仍绑定 luna/xhigh 时代的闭包内容）→ campaign_id 与已消耗的 FAILED campaign
相同 → 防刷分机制把新组合当成重跑拒绝。

用户拍板修复设计偏差：**声明组合（模型/强度/渠道列表）纳入 campaign 身份**。
同一 digest 下不同组合 = 不同 campaign，各占一次 dev+validation 名额；同一组合
仍只能跑一次（防刷分核心不破）。

闭包改动（`src/decision_support/phase16_qualification_ledger.py`）：

- 新增 `qualification_campaign_id()` canonical 函数：
  `phase16-{kind}-{digest[:16]}-{sha256("batch|model|effort|hosts")[:16]}`
- `ensure_campaign` 双重校验：canonical id 与声明字段不符即拒绝；按
  （kind, candidate_digest, declared_model_id, declared_reasoning_effort,
  declared_endpoint_hosts）查重，命中不同 campaign_id 的行即报
  "identity conflicts"（封堵格式迁移绕锁路径）
- 单测：确定性 / 五维分裂（模型、强度、渠道、kind、batch）/ 只依赖 digest
  前缀 3 个新用例（`tests/unit/test_phase16_qualification_ledger.py`）

## Digest 边界（X → Y 差异说明）

- 上一轮 digest：`f28d7e03`（luna/xhigh 时代，12/12 内容级失败，FAILED 终态）
- 本轮 digest：`1b4323655808df44`（terra/high，dev + validation 双 PASS）
- 差异来源：
  1. `phase16_qualification_ledger.py`（闭包）：canonical campaign id 函数 +
     ensure_campaign 身份校验（上述）
  2. `scripts/run_phase16_development_candidate_2.py`（非闭包）：campaign_id
     改用 canonical 函数
  3. 资产重冻结：policy / corpus manifest 的 source closure digest 随闭包更新；
     corpus 4 个 jsonl 逐字节未变（确定性生成器），仅 digest 文件变化
  4. V2/V5 evidence manifest 重冻结：修复分支自 63539d7 遗留的漂移（该提交
     重冻结 dataset 但漏了依赖它的 V2/V5 manifest，导致 V2 smoke 集成测试
     的 FORMAL_MANIFEST_MISMATCH）。V1 manifest **有意保持冻结状态**——其单测
     显式编码"V1 源码漂移必须 fail-closed"设计，历史审计资产不可变。

## Campaign 与 Run

- 最终 dev：`phase16-development-1b4323655808df44-3b1730721cba735f`
  - run_id: `phase16-development-1b4323655808df44-3b1730721cba735f-20260801T125602`
- 最终 validation：`phase16-validation-1b4323655808df44-3b1730721cba735f`
  - run_id: `phase16-validation-1b4323655808df44-3b1730721cba735f-20260801T130201`
- 同一 digest 同组合下 dev + validation 两 campaign 并存（kind 是身份的一部分，
  各占一行）；同组合重复运行仍被终态检查拒绝。与 DB 既有 10 个 campaign 行
  （含 FAILED 的 `f28d7e03`）零冲突。

## Dev Campaign（12 cases × 2 stages，24 次模型调用）

- Status: `PASS`
- Reason codes: `EXECUTION_COMPLETE`
- Model calls: `24`
- Assessment: `DEVELOPMENT_DIAGNOSTIC_COMPLETE`（digest `e089e160...`）
- Ledger authenticated: `True`
- 指标：E2E_MULTI_AGENT_READY `12/12`；ANALYST_SCHEMA_AND_SEMANTIC_VALID `12/12`；
  PLANNER_RISK_COVERAGE `12/12`；EXPLANATION_BOUND `12/12`；
  CONTROLLED_EVIDENCE_BINDING `12/12`；HARD_SAFETY_CONFORMANCE `12/12`；
  OPTION_VALIDITY `12/12`
- Receipt 分布（账本查询）：
  - `synapse-ai.uk` × 24，attempt_count = 1，model = gpt-5.6-terra，
    reasoning_effort = high，receipt_complete = True
  - tokens：157,662 in / 11,481 out / 169,143 total；0.5419 CNY；平均延迟 11.7s
  - imagebridge / vote520 全程未触发（主渠道健康时 failover 不启动属设计预期）

## Validation Campaign（12 cases × 2 stages，24 次模型调用）

- Status: `PASS`
- Reason codes: `EXECUTION_COMPLETE`
- Model calls: `24`
- Assessment: `VALIDATION_PERFORMANCE_COMPLETE`（digest `cb8bf87b...`）
- Ledger authenticated: `True`
- 指标：E2E_MULTI_AGENT_READY `12/12`；ANALYST_SCHEMA_AND_SEMANTIC_VALID `12/12`；
  PLANNER_RISK_COVERAGE `12/12`；EXPLANATION_BOUND `12/12`；
  CONTROLLED_EVIDENCE_BINDING `12/12`；HARD_SAFETY_CONFORMANCE `12/12`；
  OPTION_VALIDITY `12/12`
- Receipt 分布（账本查询）：
  - `synapse-ai.uk` × 24，attempt_count = 1，model = gpt-5.6-terra，
    reasoning_effort = high，receipt_complete = True
  - tokens：158,277 in / 11,741 out / 170,018 total；0.5453 CNY；平均延迟 11.9s
  - imagebridge / vote520 全程未触发

## 回归门禁

- Unit：`1702 passed`（含新增 3 个 canonical campaign id 单测；既有 1699 全绿）
- Integration：`248 passed`（含 V2 smoke 修复后全链；V1 链 33 项含历史审计全过）
- Coverage gate：`PASS`（line 92.1% ≥ 90，branch 85.4% ≥ 85，closure 匹配）
- Migrations：`29/29 [PASS]`（含 V2 smoke SQL 触发器常量重同步）

## 失败信号与结论

- 无 FAILED 指标；`FORMAL_MANIFEST_MISMATCH`（V2 漂移）已随 manifest 重冻结消失
- terra/high 在 synapse 主渠道下 48/48 调用成功、零重试、零 failover ——
  上游问题未在本次运行窗口复发；f28d7e03 的内容级失败被确认是上游侧现象，
  非管线缺陷
- 防刷分语义保持：同一 (digest, 组合) 重复声明仍被 UNIQUE(campaign_id) +
  终态检查拒绝；本报告两轮 run 的名额来自新 digest + 新声明组合，合法
