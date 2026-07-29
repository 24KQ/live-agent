# Phase 16 V5 Claude Code 移交说明

## 1. 交接目标与起点

本文件将 Phase 16 未完成的真实模型证据收口移交给 Claude Code。只允许推进
V5 受控 E2E campaign；不得重新开启已完成的 Phase 16 Task 1-11，也不得改写或
重跑 V1、V2、V3、V4 的历史事实。

- 工作分支：`codex/phase16-v5-controlled-e2e`
- 固定交接提交：`c85b8494a41616dcf261ccbe7e9f177e6955e935`
- 工作目录：`D:\\java\\agent\\.worktrees\\phase16-v5-controlled-e2e`
- 根目录 `D:\\java\\agent` 不是本任务工作区；其中的未跟踪文件和本地状态不得读取、修改、暂存或清理。
- V5 尚未读取 LLM 凭据、未发送 DeepSeek 请求，V5 费用为 `0.000000 CNY`。

本次交接的目标不是把默认路由打开，也不是把真实模型结果包装成生产上线证明。唯一目标是：在
冻结、可审计、人工授权的条件下，取得 V5 真实双 Agent 的严格结论，或如实记录其失败。

## 2. 必须保留的历史事实

| 版本 | 已知结论 | Claude Code 的限制 |
| --- | --- | --- |
| V1 | 已发送；`ANALYST_VALIDATION_FAILED` | 只读，不重跑、不修改 Manifest、账本或报告。 |
| V2 | 已发送；`EXECUTED_FAILED` | 只读，不把其结果计入 V5。 |
| V3 | Planner 诊断失败并含不可核验旧失败授权 | 只读，不用于证明 V5 成功。 |
| V4 | `PASS / JSON_PROTOCOL_PASS` | 仅证明最小 JSON 协议，不等同双 Agent E2E。 |
| V5 | 离线实现和 Gate 已通过；尚未联网 | 唯一允许继续修改和执行的版本。 |

历史证据入口：

- `docs/superpowers/reports/phase-16-official-smoke-evidence.md`
- `docs/superpowers/reports/phase-16-v2-official-smoke-evidence.md`
- `docs/superpowers/reports/phase-16-v3-planner-diagnostic-evidence.md`
- `docs/superpowers/reports/phase-16-v4-json-probe-evidence.md`
- `docs/superpowers/reports/phase-16-controlled-multi-agent-acceptance.md`

## 3. V5 冻结契约

- Provider 固定为 `https://api.deepseek.com`，模型固定为 `deepseek-v4-pro`；温度为 `0`，无 fallback。
- V5 Adapter 必须使用 JSON mode 和 `thinking={"type":"disabled"}`，不得保存、显示或回传
  `reasoning_content`。
- Analyst 和 Planner Profile 均为零 Skill、单次调用、60 秒 deadline、6000 总 token、2800 最大输出 token。
- 系统持有 `finding_codes`、完整 `EvidenceRef` 与谱系事实；模型只可返回受控的 evidence ID、约束码、
  风险码、解释与 Planner 候选。不得为了提高通过率放松 AgentAction、JSON Schema、EvidenceRef、领域语义、
  usage 或 provider receipt 校验。
- V5 使用独立的 `phase16-v5-synthetic-calibration-001` 合成校准输入。它不得复用正式十例的 case、
  digest、投影、Evidence Bundle 或账本 slot。
- V5 campaign 的总预算固定为 `1.000000 CNY`，每阶段预约最多 `0.030000 CNY`。不得关闭、绕过或增大
  预算门禁。
- 默认路由永久保持 `DETERMINISTIC_ONLY`；V5 不得进入 LIVE Registry、Coordinator、Store、HTTP、
  WebSocket、OperatorDecision 或经营命令路径。
- V5 账本范围仅限 append-only、CAS、恢复与 no-resend。禁止扩展数据库账号、GRANT/REVOKE、lease、
  fencing 或其他无关数据库安全工作。

当前实现的主要入口：

- `scripts/run_phase16_v5_controlled_e2e.py`
- `src/decision_support/controlled_e2e_v5.py`
- `src/decision_support/controlled_e2e_ledger_v5.py`
- `src/decision_support/controlled_e2e_adapter_v5.py`
- `evaluation/manifests/phase16-v5-controlled-e2e-v1.json`
- `evaluation/manifests/phase16-v5-controlled-e2e-calibration-v1.json`
- `tests/unit/test_phase16_v5_controlled_e2e.py`
- `tests/integration/test_phase16_v5_controlled_e2e_postgres.py`

## 4. 权限、凭据与不可变审计

- Claude Code 可以修复 V5 的离线缺陷、补测试、重新冻结 V5 Manifest/Profile/source digest、提交、推送和创建或更新 PR。
- 任何 V5 代码修改必须有详细 UTF-8 中文注释；文档使用 UTF-8、LF、无 BOM。
- 真实调用只可读取现有 `.env` 中的既有变量。不得打印、复制、修改或提交 API Key，也不得将完整
  Prompt、模型正文、思维链、原始 provider ID 或经营建议写入日志、报告、Issue、PR 或账本。
- 校准和正式调用都必须由用户在当前对话中单独明确批准。Claude Code 不能自行调用，即使所有 Gate 都通过。
- 每个已发送 stage 仅允许一次调用。任何已发送失败、非 `stop`、缺 usage、缺 provider receipt、JSON/
  Schema/Evidence/语义/预算失败，都必须写入 append-only 脱敏事实并终止当前 V5 run；不得重试、改文本后重发、
  删除账本事实或修改历史结果。

## 5. Claude Code 的执行顺序

### A. 离线准备

1. 确认当前分支从 `c85b849` 或其后续 V5 提交继续，且不在根目录工作树操作。
2. 先执行默认 dry-run；该命令不读 `.env`、不连 PostgreSQL、不会联网：

   ```powershell
   python scripts/run_phase16_v5_controlled_e2e.py
   ```

3. 如有离线缺陷，使用测试驱动方式只修 V5。修改冻结输入、Profile、Prompt、Schema 或 Adapter 时，必须
   重建并验证 Manifest digest，补充 unit 和 PostgreSQL 测试，并在 PR 前重新完成全部离线 Gate。
4. 提交前至少运行：V5 专项测试、完整 unit/integration、覆盖率 90/85、36 个 PR release case、
   `python -m compileall -q src`、`python scripts/run_db_migrations.py --dry-run`、
   `python scripts/check_sensitive_payloads.py --tracked`、文档编码检查和 `git diff --check`。
5. 覆盖率采样必须与 `.github/workflows/agent-runtime-pr.yml` 保持一致：unit 与 integration 使用同一
   coverage 数据库，使用 `evaluation/manifests/phase16-coverage-source-closure-v1.json` 生成 include，
   并保持 line `90`、branch `85` 门槛。

### B. PR Gate 与校准授权

1. 推送 V5 分支，创建或更新 PR。必须等远端 PR Gate 对当前 HEAD 全绿，不能用旧 SHA 的 Gate 代替。
2. Gate 全绿后，Claude Code 只能向用户报告固定提交、Gate 结果、预算剩余和 dry-run 结果，并请求单独批准：

   ```powershell
   python scripts/run_phase16_v5_controlled_e2e.py --execute-calibration
   ```

3. 未收到明确批准前不得执行该命令。校准必须完成 `Analyst -> Planner` 且严格 `2/2 PASS`；单次校准
   PASS 只能解锁正式申请，不能把 Phase 16 标为 PASS。

### C. 正式调用、收口与合并

1. 校准 `2/2 PASS` 后，Claude Code 必须再次请求用户单独批准，才能执行：

   ```powershell
   python scripts/run_phase16_v5_controlled_e2e.py --execute-formal
   ```

2. 正式 PASS 的唯一条件是：十个冻结 case 全部通过、`20/20` stage 调用、完整 usage 与 provider receipt、
   所有 AgentAction/Schema/Evidence/语义校验通过、每例为 `MULTI_AGENT_READY`，并且总成本不超过
   `1.000000 CNY`。否则不得使用任何“部分通过”措辞替代正式 PASS。
3. 无论正式结果为 PASS、FAILED 或发送前 BLOCKED，都从 V5 PostgreSQL append-only 事实渲染脱敏报告，
   同步 Phase 16 Acceptance、决策记录、路线图、恢复提示与 worklog，保持
   `AWAITING_PHASE_17_GATE` 和 `DETERMINISTIC_ONLY`。
4. 若校准或正式 run 已发送后失败，完成失败报告和新方案后停止，等待用户批准独立 V6；不得继续同一 V5 run。
5. 所有文档更新推送后重新通过当前 PR HEAD 的 Gate。Claude Code 只可请求用户批准 merge commit，
   不得自行合并 `main`。

## 6. 交接完成判定

本移交在以下条件满足时完成：本文件单独提交为
`docs: hand off phase16 v5 closeout to claude code` 并推送至 V5 分支；`git diff --check`、敏感载荷扫描和
本文件的 UTF-8/BOM/LF/replacement character/尾随空白检查均通过。此提交本身不运行迁移、不读取 `.env`、
不连接数据库，也不调用真实模型。
