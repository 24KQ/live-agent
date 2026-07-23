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
- Analysis: `phase16-analysis:phase16-escalation:automatic:bundle-phase16-demo` / `69c0307b18a6682c674690cee1bb14ac10dfe5f8016b4acca49b0257c1682750`
- Proposal: `phase16-proposal:phase16-escalation:automatic:bundle-phase16-demo` / `1c276f0e16ec73953d4985e1795c4863c1803cee9c8eec1aac9f4154bea8e6ca`
- Outcome: `phase16-outcome:phase16-escalation:automatic:bundle-phase16-demo` / `56167aa373b74e1809030ddb9b4bb3881b726c1f3da828b24eb858d6f9150117`
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
- Initial audit digest: `d4150a5ddabce4781022c090650b29623efeca31f0089651c1b044e288db7b7b`
- Replay audit digest: `d4150a5ddabce4781022c090650b29623efeca31f0089651c1b044e288db7b7b`

## Frozen Scripted Evaluation

- Dataset / Manifest: `phase16-controlled-multi-agent-v1` / `d60d306b7a0977168c66d3629c01914dc1c931665772d50f6a0310614787a182`
- Source closure digest: `a0c7e4bdc6e8b9d1e79e70737c0b82bfa638162599fae71b9ea23efd10e4ad6b`
- Profile digests: `{"decision_planner": "70d9a6c3cedd2d571b6794b31983b0e20d9bb1d6f7c5c97cbd4e95b3c64c9183", "evidence_analyst": "aeafd9bfcc519d17e05ab8361be3c65aa16e8b4eb6a506bb0fa3d258ff5026ef"}`
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

该 Addendum 收口后的新鲜工程验证为：unit `1596 passed, 1 warning`、integration `214 passed, 7 deselected,
5 warnings`、Phase 16 escalation PostgreSQL `31 passed`、formal ledger/runner PostgreSQL `29 passed`，19 个迁移
实际应用与 dry-run 均无失败。两次补充只读终审在读取前因本地代理 `502`/`503` 终止，未产生可采纳审查结论；主模型已
完成同范围复核，不把该外部故障描述为审查通过。
