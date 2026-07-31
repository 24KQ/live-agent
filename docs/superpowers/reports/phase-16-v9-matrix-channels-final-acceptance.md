# Phase 16 V9 受控 E2E 最终验证验收（矩阵配置 + 渠道链）

本报告记录 Phase B 最终验证 run 的完整证据链：矩阵配置重构（模型 × 推理强度 ×
渠道有序列表三条独立轴）、渠道链 failover、最小重试窗口修复，以及 dev + validation
两轮真实模型 campaign 的账本回执。所有资产摘要与 campaign 声明值均可从 SQL
append-only 账本复验。

- Acceptance status: `PASS`（validation 结果见下）
- Candidate digest: `8623a075a24c8727c0308cdd3ef05fd73428f4446a6e40a3be270a1bf6bf250e`
- Policy digest: `d4e9effbbabb016865409d9a...`
- Corpus manifest: `4fae4dbd58c591189d4ebff6...`
- Model / Effort: `gpt-5.6-luna` / `xhigh`
- Channels（顺序即优先级）: `synapse-ai.uk,api.imagebridge.top`

## Digest 边界（X → Y 差异说明）

上一轮 run（digest `9eda8e8ae370dc1b`，imagebridge 主渠道）4/12 FAILED，全部失败
归因 imagebridge：1 次连接挂死 180s（MODEL_OUTCOME_UNAVAILABLE）、3 次首次尝试有
回执但内容验证不达标（ANALYST/PLANNER_VALIDATION_FAILED）、4 次 case 需要
failover 到 synapse 才成功。synapse 在同一参数下 100% 成功（含上一轮 5 次 + 更早
单渠道 12/12）。结论：imagebridge 约半数尝试失败，不适合作主渠道。

本轮 digest `8623a075` 与上一轮的差异边界（全部由 candidate digest 绑定，故是
合法重跑触发点）：

- `src/decision_support/controlled_e2e_adapter_v5.py`：新增最小重试窗口
  `_MIN_RETRY_WINDOW_SECONDS = 1.0`。修复 deadline 边缘 bug：原检查 `<= 0` 允许在
  仅剩 0.8ms 时发出注定失败的第二次调用（实测 002 case 的 `latency_ms=0.813` 即
  该调用的真实耗时）；现重试与换端前必须至少剩余 1s，5xx 退避也压缩而非压掉窗口。
- 渠道顺序由 env 声明改为 synapse 主、imagebridge 备（渠道列表是 campaign 声明值，
  不进 digest；同 digest 下可任意调整）。
- 资产重冻结：policy / corpus / V5 manifest 的 source closure digest 随 adapter
  变化而更新；corpus 4 个 jsonl 逐字节未变（确定性生成器），仅 manifest digest 更新。

## Campaign 与 Run

- Failed 轮：`phase16-development-9eda8e8ae370dc1b`
  - channels: `api.imagebridge.top,synapse-ai.uk`（imagebridge 主）
- 最终 dev：`phase16-development-8623a075a24c8727`
  - run_id: `phase16-development-8623a075a24c8727-20260731T221215`
- 最终 validation：`phase16-validation-8623a075a24c8727`
  - run_id: `phase16-validation-8623a075a24c8727-20260731T222715`
- 同一 digest 下 dev + validation 两 campaign 并存（UNIQUE(campaign_id) 各占一行，
  预算 4.00 CNY 各自充足）；同 digest 重复运行仍被 begin_run 终态检查拒绝。

## Dev Campaign（12 cases × 2 stages，24 次模型调用）

- Status: `PASS`
- Reason codes: `EXECUTION_COMPLETE`
- Model calls: `24`
- Assessment: `DEVELOPMENT_DIAGNOSTIC_COMPLETE`（digest `7bcbddd9...`）
- Ledger authenticated: `True`
- 指标：E2E_MULTI_AGENT_READY `12/12`；ANALYST_SCHEMA_AND_SEMANTIC_VALID `12/12`；
  PLANNER_RISK_COVERAGE `12/12`；EXPLANATION_BOUND `12/12`；
  CONTROLLED_EVIDENCE_BINDING `12/12`；HARD_SAFETY_CONFORMANCE `12/12`；
  OPTION_VALIDITY `12/12`
- Receipt 分布（账本查询）：
  - `synapse-ai.uk` × 24，attempt_count = 1，reasoning_effort = xhigh
  - 总 tokens：125,373
  - imagebridge 全程未触发（主渠道健康时 failover 不启动属设计预期）

## Validation Campaign（12 cases × 2 stages，24 次模型调用）

- Status: `PASS`
- Reason codes: `EXECUTION_COMPLETE`
- Model calls: `24`
- Assessment: `VALIDATION_PERFORMANCE_COMPLETE`（digest `d9cc9930...`）
- Ledger authenticated: `True`
- 指标：E2E_MULTI_AGENT_READY `12/12`；ANALYST_SCHEMA_AND_SEMANTIC_VALID `12/12`；
  PLANNER_RISK_COVERAGE `12/12`；EXPLANATION_BOUND `12/12`；
  CONTROLLED_EVIDENCE_BINDING `12/12`；HARD_SAFETY_CONFORMANCE `12/12`；
  OPTION_VALIDITY `12/12`
- Receipt 分布（账本查询）：
  - `synapse-ai.uk` × 24，attempt_count = 1，reasoning_effort = xhigh
  - 总 tokens：127,645
  - imagebridge 全程未触发

## 回归门禁

- Unit：`1697 passed`（含新增 3 个最小窗口单测：窗口不足不重试 / 退避保留窗口 /
  窗口不足不换端；V5 adapter 文件 49 个用例全绿）
- Integration：`248 passed, 7 deselected`（含 qualification ledger + runner + V5
  postgres 20 例）

## 结论

矩阵配置重构后，白名单内切换模型 / 推理强度 / 渠道列表不需要新 digest；渠道链按
声明顺序自动 failover；真实模型 campaign 在 synapse 主渠道下 dev + validation 两轮
均为 12/12 全绿（合计 48 次调用、252,838 tokens、全部首次尝试成功）。
imagebridge 保留为备渠道（白名单内、可随时调整顺序），其稳定性与内容质量低于
synapse 的结论记录在案。
