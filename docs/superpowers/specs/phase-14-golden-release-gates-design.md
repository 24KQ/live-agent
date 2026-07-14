# Phase 14 Golden Dataset and Release Gates Design

文档状态：`FROZEN_NOT_AUTHORIZED_FOR_IMPLEMENTATION`

依赖：Phase 13 已为三个候选生成正式去留结论后才允许实施。

## 1. 设计目标

Phase 14 把现有测试、Replay、Agent 评估和版本证据收敛为自动发布门禁：

- 数据集内容、标签、生成器和哈希可追踪。
- 确定性安全规则优先，LLM Judge 只补充语义质量。
- PR、Nightly、Release 三层成本与基础设施边界清楚。
- 任一 Skill、Plan、Prompt、模型或数据版本变化都能得到可比较报告。
- 通过门禁后，新 Runtime 成为项目默认路径，旧兼容面按计划退役。

## 2. 非目标

- 不建立训练平台、在线标注系统或模型微调管线。
- 不把公开仓库中的 holdout 宣称为密码学秘密。
- 不让 LLM Judge 覆盖安全、权限、状态或动作规则。
- 不新增前端运营控制台或 HTTP 管理接口。
- 不接真实淘宝 API。

## 3. Golden Dataset 布局

固定目录：

```text
evaluation/
  schemas/
  generators/
  cases/development/
  cases/validation/
  cases/holdout/
  manifests/
  pricing/
  reports/
```

每个 JSONL case 必须包含：

```text
case_id
schema_version
scenario
lifecycle
input_snapshot
expected_rules
allowed_outcomes
severe_violation_conditions
metric_labels
evidence_fixture_refs
```

所有内容必须脱敏，不包含真实密钥、本机用户目录、真实观众标识、订单信息或未经许可的主播原话。

manifest 固定保存：

- dataset ID 和 semantic version。
- generator path、version、seed 和 Git commit。
- 每个 split 的文件、case 数和 SHA-256。
- Schema、规则、Prompt、模型和价格表哈希。
- 创建时间、审核状态和 supersedes 关系。

生成器重复运行必须字节一致。数据变更必须升级 dataset version，不允许原地改写已用于 Release 的 manifest。

首版数据组成固定为：

- `runtime-core-v1`：24 个确定性 case，Skill Runtime、DAG/Checkpoint 和 Event/Replan 各 8 个；每类都按 development 2、validation 4、holdout 2 拆分，因此三个 split 总数分别为 6、12、6。
- `phase13-v1`：复用 Phase 13 已冻结的 240 个 Specialist case 及原 manifest，不重新生成、不移动 split、不修改标签。

因此首版总清单包含 264 个 case。Runtime core case 由独立固定 seed 生成器从已验收 Fixture 构建；不能把 pytest 输出临时转换成标签，也不能把 Phase 13 holdout 复制到其他 split。

## 4. Evaluation Interface

统一输入为冻结 `EvaluationCase + SubjectManifest`，统一输出为 `EvaluationCaseResult`：

```text
case_id
subject_id / subject_version
rule_result
semantic_result
severe_violations
metrics
latency_ms
token_usage
cost
evidence_refs
artifact_digest
```

Subject 可以是确定性基线、保留 Agent、Skill/Prompt 新版本或 PlanEngine 版本。所有 subject 必须经过同一 case runner，不能为某个候选单独放宽解析或错误处理。

## 5. 规则优先裁决

确定性规则负责：

- Skill/Tool 白名单和精确版本。
- 生命周期、风险、审批和事件授权。
- 参数 Schema、输出 Schema 和额外字段拒绝。
- Plan 状态、依赖、资源锁、Replan 和命令幂等。
- EvidenceRef 可解析性和摘要一致性。
- 敏感信息和路径泄露。
- 严重安全违规。

任一严重违规使 case 和 Release 直接失败。平均分、业务提升和 Judge 分不能覆盖。

## 6. LLM Judge

语义 Judge 固定使用 `deepseek-v4-pro`、temperature 0 和独立版本化 Prompt/Schema。它只评价：

- 建议是否清晰且与证据一致。
- 归因是否完整、不过度推断。
- 记忆候选是否可读且没有添加规则未允许的事实。

Judge 不能修改 rule verdict，只能返回 `PASS | WARN | FAIL | UNAVAILABLE` 和结构化证据。Judge 调用失败时语义维度为 `UNAVAILABLE`，不能用默认高分代替。Release 对保留 Agent 要求语义证据完整；纯确定性 subject 可以在 manifest 明确不适用时跳过。

## 7. 评估证据存储

- 数据集、Schema、manifest、价格表和 Release 摘要进入 Git。
- 完整 case 输出、模型 usage、Judge 结果和成本进入 PostgreSQL。
- 原始 CI 日志、JSON 报告和覆盖率进入 artifact。
- 不持久化 chain-of-thought；Prompt 只保存版本化模板和哈希，不保存运行时敏感值。

Artifact 保留期：

- PR：14 天。
- Nightly：30 天。
- Release：180 天。
- Release 摘要和全部哈希：永久进入 Git。

## 8. PR 门禁

GitHub Actions 使用 Python 3.12 和 PostgreSQL 16，执行：

- 依赖安装和全部数据库迁移 dry-run/应用。
- 默认 pytest（排除 `external`）。
- Phase 11-14 ScriptedModel 和 Fake Adapter 回归。
- Golden Schema、manifest、生成器幂等和敏感信息检查。
- 文档 UTF-8、BOM、换行和尾随空白检查。
- 核心 Runtime branch coverage 至少 90%。

覆盖率只统计 `src/skill_runtime`、`src/plan_engine`、`src/agent_runtime` 和新 Evaluation Runtime，不为历史 UI/演示脚本制造无意义补测。PR 不启动 Kafka，不调用付费模型。

## 9. Nightly 门禁

Nightly 在 PR 门禁上增加真实 Kafka 和传输集成。真实模型默认关闭；只有以下条件同时满足才启用：

- 配置模型 secret。
- Repository variable `ENABLE_PAID_NIGHTLY=true`。
- 提供可解析人民币价格表。
- 单次 `NIGHTLY_MODEL_BUDGET_CNY` 不高于默认 0.10 元，调高必须显式修改受审配置。

Nightly 只运行版本化抽样 manifest，不消费 release holdout 的正式判定资格。

## 10. Release 门禁

Release 使用受保护 GitHub Environment 手动触发，输入明确的 dataset manifest、subject manifest 和代码 commit。固定执行：

- PR 全部门禁。
- 真实 Kafka/PostgreSQL/PostgresSaver。
- 完整 release holdout。
- 保留 Agent 的 flash 模型执行与 pro Judge。
- 费用、延迟、Token 和严重违规门禁。
- 版本回归对比和最终 ReleaseDecision。

输入哈希与运行结果不一致、case 缺失、Judge 不完整或费用无法核算时 fail-closed。

当前连续实施期间，Phase 13 Agent 调用与 Phase 14 首次 Release 的 Agent/Judge 调用共享预算作用域 `agent-runtime-completion-v1`，人民币硬上限仍为 3.00 元。Phase 13 已消费金额必须从 Release 可用余额中扣除；余额不足时 ReleaseDecision 为 `BLOCKED` 并暂停，不能自动增加预算或减少正式 case。付费 Nightly 在本次连续实施中保持关闭；项目完成后的未来 Nightly/Release 由受保护环境提供各自显式预算，不沿用本次无人监控授权。

## 11. ReleaseDecision

状态固定为 `PASS | FAIL | BLOCKED`。`BLOCKED` 只用于外部证据不可获得，例如受保护 secret 缺失或模型服务不可用；它不能被当作 PASS 发布。

Release 摘要至少包含：

- Git commit 和依赖锁定信息。
- Dataset、Schema、规则、Prompt、模型和价格哈希。
- 测试、覆盖率和迁移结果。
- 每个保留 Agent 的正式指标。
- 严重违规列表。
- 与上一 Release 的差异。

## 12. 默认路由与兼容退役

默认路由采用两次 Release 证据：先在已提交代码上通过 SubjectManifest 显式指定全部新 Runtime 路由并取得 PASS；随后才修改默认值、提交并推送，再对新提交运行同一完整 Release。第二次失败时必须用新的回滚提交恢复 Legacy 默认值，不能保留未经验证的新默认。

两次 Release 均通过后：

- Skill Runtime 三批默认 `SKILL_RUNTIME`。
- 手卡执行默认 `PLAN_ENGINE`。
- 可信售罄执行默认 `PLAN_ENGINE`。
- `LEGACY` 作为启动期显式回滚保留一个兼容周期，任何同次调用仍禁止 fallback。
- 删除 ToolRegistry 公共 Facade、注册写入口和生产依赖。
- 所有治理查询使用 Catalog、SkillPolicyView 或 SkillExecutor。

如果固定确定性基线通过而所有 Agent 均未保留，仍可 PASS；默认路由不依赖多 Agent 存在。

## 13. CI 安全

- Fork PR 不获得模型 secret。
- 工作流不打印 API key、Authorization header 或完整敏感 payload。
- 外部输出只写入不被自动执行的 artifact，不把外部文本作为 CI 命令。
- Release environment 需要受保护权限，不能由普通 PR 自动触发。
- 价格表和预算在派发前校验，并发 Worker 共享数据库预算账本。

## 14. 最终验收

Phase 14 Acceptance 通过后生成 `agent-runtime-final-acceptance.md`，汇总：

- 三场景业务闭环与技术分层。
- Skill、Plan、Event、Agent、Evaluation 的最终版本。
- 保留 Agent 数量及数据依据。
- 默认路由和回滚方式。
- CI 与 Golden Dataset 复现命令。
- 未进入本轮的真实淘宝 API、插件、UI 等边界。

只有 Final Acceptance、全部文档状态和远端提交闭合后，整条路线才完成。
