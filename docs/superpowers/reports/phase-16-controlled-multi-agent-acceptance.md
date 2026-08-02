# Phase 16 Controlled Multi-Agent Escalation Acceptance

本报告只记录本地确定性保护、受控双 Agent 演练和人工命令边界。它不把 ScriptedModel 或本地预检冒充为真实模型调用；正式网络回执由独立 Addendum 维护。

- Acceptance status: `INCONCLUSIVE`
- Phase state: `AWAITING_PHASE_17_GATE`
- Production default route: `DETERMINISTIC_ONLY`
- Live session: `live-session-p001-sold-out-v2`
- Incident: `incident:event-phase16-demo-sold-out:plan-root-phase16-demo`

## Protection And Controlled Route

- Automatic protection: `APPLIED`
- Authoritative Phase 12B Coordinator evidence: `true`
- Protected EventApplication state: `APPLIED`
- Protected sold-out write count: `1`
- Protected root PlanRun: `3c564d10-ac62-54cf-a4db-ec0023b783ee`
- Protection facts bound into EvidenceBundle: `true`
- Execution order: `AUTOMATIC_PROTECTION, CONFLICT_ANALYSIS, LIVE_DECISION_PLANNING, OPERATOR_DECISION_COMPILED`
- Evidence bundle: `bundle-phase16-demo` / `601760845217e4490e9f70e5278acc697bf24f6e8792da5d7de305bdaa8f0847`
- Dual-Agent calls: `CONFLICT_ANALYSIS, LIVE_DECISION_PLANNING`
- Analyst / Planner calls: `1 / 1`
- Escalation: `phase16-escalation:automatic:bundle-phase16-demo` / `6c2b63c2f449898a66d175626e22fb8d5eb3358692cad27343da8d4e2ae2918d`
- Analysis: `phase16-analysis:phase16-escalation:automatic:bundle-phase16-demo` / `a569f73ce2ff87e2588f1a6789a6dfe774f597859ea3ad16117649d1724bddcc`
- Proposal: `phase16-proposal:phase16-escalation:automatic:bundle-phase16-demo` / `bb784d0d5faea1505993a8586977753d098b8a0dbef16f2948d99c3020605503`
- Outcome: `phase16-outcome:phase16-escalation:automatic:bundle-phase16-demo` / `08da14524f4ce42b63bced7bd8e7d0843ddfc58a6e25a30e3d10ead5131d1cd7`
- READY proposal origin: `MULTI_AGENT`
- READY outcome: `READY`
- Exact lineage complete: `true`

## Human Recovery Boundary

- Valid operator decision kinds: `APPROVE, MODIFY, REJECT`
- Selected operator decision: `MODIFY`
- Compiled command: `execution-command:decision-phase16-demo-modify`
- Compiled command bound to PlanStore context: `true`
- Execution command persisted: `true`
- Execution command submitted: `false`
- Execution submissions: `0`

## Restart Audit

- Replay stable: `true`
- Store reconstructed from append-only facts: `true`
- Replay Agent calls: `none`
- Initial audit digest: `a7791ae680824b74ae411355cd9a14b8dbaed3d71781bc2601313249bb689851`
- Replay audit digest: `a7791ae680824b74ae411355cd9a14b8dbaed3d71781bc2601313249bb689851`

## Frozen Scripted Evaluation

- Dataset / Manifest: `phase16-controlled-multi-agent-v1` / `69dce8ee66f611e169fe18168c5f6fa75e351526a217f868b85835aba488d55d`
- Source closure digest: `015e203020dc76019cc70ba160dd6e0faa2d1882c28c8f063f252afeb10025de`
- Profile digests: `{"decision_planner": "5ab24657effc5f2c6dc71c3ea34395be1c4b7a717f9a7452efb4ba5b395d4206", "evidence_analyst": "0de062677e38856b8d6834932f2785b63d44aabc9545bc622092e4382eddea46"}`
- Cases / route-correct / paired identity: `48 / 48 / 24`
- Analyst / Planner / READY / DEGRADED / no-send: `30 / 26 / 24 / 6 / 18`
- Scripted reserved cost: `2.72 CNY`

## Deterministic Demo Real-Smoke Preflight

- Scope: `PHASE16_MULTI_AGENT_SMOKE` (10 cases / 1.00 CNY hard cap)
- Smoke status: `BLOCKED`
- Real model calls / cost: `0 / 0.000000 CNY`
- Blockers:
  - `ENDPOINT_UNAVAILABLE`
  - `PHASE16_SMOKE_PREFLIGHT_REQUIRED`
  - `REAL_MODEL_SMOKE_NOT_RUN`
  - `USAGE_CONTRACT_UNAVAILABLE`

以上 BLOCKED 仅表示本地确定性 Demo 不发送真实模型请求，不是当前正式真实模型证据结论。正式 PostgreSQL 回执见 [Phase 16 Official Real-Model Smoke Evidence](phase-16-official-smoke-evidence.md)。默认路由继续为 DETERMINISTIC_ONLY，阶段状态固定为 AWAITING_PHASE_17_GATE。

## PR Coverage Remediation

首次 PR coverage 报告曾为 `BLOCKED`，line `82.85%`、branch `67.96%`；该历史事实保留，不把它改写成业务失败或删除。
整改新增版本化 Manifest：`evaluation/manifests/phase16-coverage-source-closure-v1.json`，固定 11 个源码文件作为
coverage 分母，并由 `scripts/coverage_source.py` 校验存在、Git 跟踪、非 symlink、UTF-8/LF 和源码摘要。整改后的 PR
采样使用同一 coverage 数据库联合运行 unit/integration，并在报告生成后校验文件集合与 Manifest 完全一致；line/branch
门槛仍为 `90/85`，未使用排除代码或降低阈值。干净证据为：

- unit：`1555 passed, 1 warning`
- integration：`185 passed, 7 deselected, 5 warnings`
- coverage：line `92.035%`、branch `85.081%`
- Gate：`PASS`
- 覆盖率整改时的真实模型调用/费用：`0 / 0.000000 CNY`

本节只证明 PR coverage 技术门禁已通过；它不覆盖后续正式真实模型证据。确定性 Demo 的 Acceptance 状态保持
`INCONCLUSIVE`，正式外部结论以 Addendum 为准，默认路由不改变。整改提交为 `599c98e`（测试）和 `6216f9f`（CI/source closure）。

## Official Evidence Closeout

本报告顶部的 `INCONCLUSIVE` 只描述本地确定性 Demo；唯一正式真实模型 run 的外部结论以
[Official Smoke Evidence](phase-16-official-smoke-evidence.md) 为准，仍为 `FAILED / ANALYST_VALIDATION_FAILED`。
正式 smoke 不会重试，默认路由继续 `DETERMINISTIC_ONLY`，阶段仍为 `AWAITING_PHASE_17_GATE`。

V1 后的独立 V2 实验保留原账本与失败事实，改用 system-managed EvidenceRef、DeepSeek V4 Pro 和独立
append-only ledger。V2 首个 case 的 Analyst 已通过完整 receipt 与结构校验；Planner 请求已发送但没有
可消费 outcome，故 V2 以 `FAILED / MODEL_OUTCOME_UNAVAILABLE` 收口，不能将单段成功写成双 Agent
`10/10` 通过。完整脱敏事实见 [Phase 16 V2 Official Smoke Evidence](phase-16-v2-official-smoke-evidence.md)。

独立 V3 单 Planner 诊断对同一冻结 V2 Planner Profile 进行一次新调用，端口返回
`FAILED / MODEL_FAILURE_INVALID_OUTPUT_JSON`。该历史 failure 在摘要/HMAC 前使用了高精度延迟、
落库后为三位毫秒，故读取认证为 `UNVERIFIABLE_LEGACY_LATENCY_PRECISION`；它只能解释诊断方向，
不能成为真实双 Agent `PASS` 证据。V3 不会重试，完整脱敏事实见
[Phase 16 V3 Planner Diagnostic Evidence](phase-16-v3-planner-diagnostic-evidence.md)。

V3 收口后的新鲜工程验证为：unit `1616 passed, 1 warning`、integration `222 passed, 7 deselected,
5 warnings`、V3 unit `9 passed`、V3 PostgreSQL `3 passed`，并已实际应用 V3 专属 DDL。全量迁移命令
仍会被既有 V1 schema-contract 防护拒绝，故本报告不将其描述为全绿；该历史 schema 问题与 V3 DDL 无关。
两次补充只读终审在读取前因本地代理 `502`/`503` 终止，未产生可采纳审查结论；主模型已完成同范围复核，不把该
外部故障描述为审查通过。

独立 V4 禁思考 JSON 协议探针以新 run 发送一次无业务数据的 `deepseek-v4-pro` 请求，并得到完整
receipt/usage、可复验 HMAC 与 `PASS / JSON_PROTOCOL_PASS`。它仅证明最小 JSON 协议可消费，
不是 V1/V2/V3 的重试，也不构成真实双 Agent `10/10` 通过。完整脱敏事实见
[Phase 16 V4 Disabled-Thinking JSON Protocol Probe Evidence](phase-16-v4-json-probe-evidence.md)。
默认路由继续 `DETERMINISTIC_ONLY`，阶段继续 `AWAITING_PHASE_17_GATE`。
