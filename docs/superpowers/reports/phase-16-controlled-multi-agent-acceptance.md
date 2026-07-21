# Phase 16 Controlled Multi-Agent Escalation Acceptance

本报告只记录本地确定性保护、受控双 Agent 演练、人工命令边界和真实 smoke 外部证据状态。它不把 ScriptedModel 或本地预检冒充为真实模型调用。

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

- Dataset / Manifest: `phase16-controlled-multi-agent-v1` / `45950a3c77d1b1ebdcb6a471302d9db099efe935db4a289159737f4e6148a6d8`
- Source closure digest: `283e30d1de39f8589450da6b369b1c4063be42a9b8bbc661cd80cf984d8791b4`
- Profile digests: `{"decision_planner": "70d9a6c3cedd2d571b6794b31983b0e20d9bb1d6f7c5c97cbd4e95b3c64c9183", "evidence_analyst": "aeafd9bfcc519d17e05ab8361be3c65aa16e8b4eb6a506bb0fa3d258ff5026ef"}`
- Cases / route-correct / paired identity: `48 / 48 / 24`
- Analyst / Planner / READY / DEGRADED / no-send: `30 / 26 / 24 / 6 / 18`
- Scripted reserved cost: `2.72 CNY`

## Real Smoke Evidence

- Scope: `PHASE16_MULTI_AGENT_SMOKE` (10 cases / 1.00 CNY hard cap)
- Smoke status: `BLOCKED`
- Real model calls / cost: `0 / 0.000000 CNY`
- Blockers:
  - `ENDPOINT_UNAVAILABLE`
  - `PHASE16_SMOKE_PREFLIGHT_REQUIRED`
  - `REAL_MODEL_SMOKE_NOT_RUN`
  - `USAGE_CONTRACT_UNAVAILABLE`

真实 endpoint、usage 合同和真实模型回执未提供，因此 Phase 16 结论保持 INCONCLUSIVE；默认路由继续为 DETERMINISTIC_ONLY。Phase 16 完成后不自动实施 Phase 17，当前状态固定为 AWAITING_PHASE_17_GATE。

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
- 真实模型调用/费用：`0 / 0.000000 CNY`

本节只证明 PR coverage 技术门禁已通过；真实 endpoint、usage 合同和模型回执仍缺失，所以 Acceptance 总结继续为
`INCONCLUSIVE`，默认路由不改变。整改提交为 `599c98e`（测试）和 `6216f9f`（CI/source closure）。
