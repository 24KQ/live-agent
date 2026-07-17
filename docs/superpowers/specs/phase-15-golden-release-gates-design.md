# Phase 15 Golden Release Gates Design

文档状态：`REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`

本文件是 Phase 15 Just-in-Time Gate 后重新冻结的 Design。Stage A 只持久化设计和实施计划；业务代码、数据库迁移、真实模型、真人采集和 GitHub Actions 实施必须在用户单独授权 Stage B 后开始。

## 1. 阶段定位

Phase 15 不新增 Specialist Agent，也不改变 Phase 13 的 `REJECTED | INCONCLUSIVE` 历史结论。它把现有三场景人机协同 Runtime 收敛为可重复的技术发布门禁，并把“技术发布成功”和“Copilot 是否值得默认开启”拆成两个独立结论。

项目最终定位保持为：

> 面向直播电商播前、播中、播后三场景的人机协同决策支持与受控执行 Runtime。

Phase 14 的 `INCONCLUSIVE` 保留为不可改写的历史事实。Phase 15 可以在 Copilot 证据不足时完成确定性 Runtime 技术发布，但不得因此把 `DECISION_SUPPORT` 切成默认路由。

## 2. 双轨发布结论

技术发布使用 `TechnicalReleaseDecision`：

- `PASS`：确定性 Golden、迁移、测试、覆盖率、编码、敏感信息和托管 CI 均通过。
- `FAIL`：代码或确定性安全门禁失败。
- `BLOCKED`：强制 PostgreSQL/Kafka/GitHub Actions 等外部证据无法获得。

决策支持晋升使用 `DecisionSupportPromotionDecision`：

- `PROMOTE`：真实模型 smoke 和真人交叉对照同时满足全部严格门槛。
- `KEEP_DISABLED`：证据完整但质量/安全/效率门槛未满足，继续保持默认关闭。
- `BLOCKED`：真实模型、usage、价格或真人样本缺失，不能判断。

最终状态为：

- `RELEASED_DECISION_SUPPORT_ENABLED`：Technical `PASS` 且 Promotion `PROMOTE`，完成两次默认路由 Release。
- `RELEASED_DECISION_SUPPORT_DISABLED`：Technical `PASS` 且 Promotion 为 `KEEP_DISABLED` 或 `BLOCKED`。
- `NOT_RELEASED`：Technical 为 `FAIL` 或 `BLOCKED`。

## 3. 活跃 Golden Dataset

Phase 15 活跃清单固定为 48 个 case，按 development 12、validation 24、release holdout 12 拆分：

- 24 个 Runtime 安全 case：Skill Runtime、Plan/Checkpoint、Event/Replan 各 8 个。
- 16 个 Phase 14 播中复合事故 case：复用现有售罄/备品冲突、弹幕噪声、主播节奏和证据冲突数据。
- 8 个生命周期闭环 case：PREPARE 4 个、REVIEW 4 个，覆盖冻结、记忆读取、回放、资格和人工确认。

Phase 13 的 240 个 Specialist case 不进入当前 Release 执行；它们作为归档 Manifest 校验资产，PR 只验证来源 Manifest、Schema、case ID 和摘要未被篡改。

所有 Golden 内容必须脱敏、字节稳定、版本化、不可原地覆盖，并记录数据、Schema、生成器、规则、源码和来源 Manifest 摘要。

## 4. Evaluation Interface 与规则优先

新增 `src/release_gates/` 作为 Phase 15 评估内核，定义：

- `GoldenCase`、`GoldenManifest`、`SubjectManifest`。
- `EvaluationCaseResult`、`ReleaseRun` 和不可变 artifact digest。
- 确定性 Subject Runner 与统一错误归一化。
- `TechnicalReleaseDecision`、`DecisionSupportPromotionDecision` 和 `FinalReleaseStatus`。

规则优先检查 Skill 精确版本、生命周期、授权、参数/输出 Schema、EvidenceRef、Plan/Event 状态、CAS、幂等、fencing、敏感信息、费用和 no-fallback。严重违规直接使对应 case 失败，任何平均分、模型文本或未来 Judge 都不能覆盖规则结果。

Phase 15 不新增付费 LLM Judge。语义质量由结构化 Copilot 输出、真人交叉对照和既有规则证据共同判断；规则门禁不依赖模型判断。

## 5. 人工交叉对照

实现本地受控 study collector，而不是伪造人工结果：

- 3-5 名真实参与者，每人 8 次试验，总数 24-40 次。
- 四组场景使用固定 seed 做 Latin-square 顺序和条件平衡。
- 同一参与者不重复看到同一 case；指标按参与者与场景组配对。
- 输入只允许封闭动作、冲突判断、工作负担 1-7 和系统计算耗时。
- 参与者只保存加盐摘要，不保存姓名、自由文本、原始弹幕或 PII。
- ScriptedModel 采集仅用于 UI/流程诊断，不能触发 Promotion `PROMOTE`。
- Promotion-eligible study 必须引用真实 Copilot smoke 输出的 immutable artifact digest。

Promotion 严格要求：严重违规 0、安全正确率至少 90%、关键冲突漏报下降至少 30%、决策中位耗时下降至少 20%，并且所有记录、Manifest、模型 usage 和费用完整。

## 6. 模型和预算

Phase 15 新增预算身份 `PHASE15_COPILOT_SMOKE`，固定额度 0.60 元；Phase 13 的 2.40 元和 Phase 14 的 1.00 元不可借用。

真实模型只允许在受保护 Release 环境、模型身份/endpoint/价格/Prompt/Schema/数据集/代码摘要全部匹配后运行，最多 10 个 `deepseek-v4-flash` case，temperature 为 0，每例单次调用，不自动重试。

Promotion 需要 10/10 case 完成、fallback 0、严重违规 0、usage 可核算、安全正确率至少 90%，总费用不超过 0.60 元。usage 缺失按预留额结算并将 Promotion 标记为 `BLOCKED`。

## 7. CI 与 Release

GitHub Actions 分为三个工作流：

- PR Gate：Python 3.12、PostgreSQL 15、36 个非 holdout case、默认 unit/integration、迁移、编码、敏感信息和覆盖率；不启动付费模型。
- Nightly：完整 PostgreSQL/Kafka/PostgresSaver 和 36 个非 holdout case；真实模型默认关闭。
- Release：`phase15-release-*` tag 或手动触发，运行 48 个 case、完整基础设施和 ReleaseDecision。

最终技术 PASS 必须绑定精确 commit 上真实绿色 PR Gate 和 Release Actions run evidence。PR artifact 保留 14 天、Nightly 30 天、Release 180 天；Release 摘要和所有哈希永久进入 Git。

两次默认路由 Release 固定为：

1. 第一次使用显式 `SKILL_RUNTIME`/`PLAN_ENGINE` SubjectManifest；`DECISION_SUPPORT` 按 Promotion 结论显式开启或关闭。
2. 第一次 PASS 后，三批 Skill 默认切换为 `SKILL_RUNTIME`，手卡和售罄默认切换为 `PLAN_ENGINE`；只有 `PROMOTE` 才切换 `DECISION_SUPPORT`。
3. 第二次 Release 验证新默认。失败时使用新的 revert commit 恢复默认值，不改写历史。

## 8. 兼容与范围边界

- `ToolRegistry` 公共 Facade 在生产消费者已迁移到 `Catalog/SkillPolicyView` 后退役；旧兼容测试改为治理投影测试。
- Legacy 路由保留一个显式回滚周期；任何同次调用禁止 Runtime 失败后 fallback Legacy。
- 不接入真实淘宝 API、自由 A2A、动态 handoff、共享 scratchpad、插件或热加载。
- Phase 15 不重新保留 Phase 13 Specialist，不改变 Phase 14 历史 Acceptance，不把模拟人工数据当真人证据。

## 9. 验收边界

Phase 15 Acceptance 必须同时交付：48 case Golden Manifest、规则优先报告、双轨 ReleaseDecision、迁移和基础设施证据、覆盖率报告、GitHub Actions run evidence、三场景 Demo、ToolRegistry 退役报告和 Agent Runtime Final Acceptance。

没有真人或真实模型证据时，技术发布仍可为 `PASS`，但最终状态必须是 `RELEASED_DECISION_SUPPORT_DISABLED`，默认路由保持 `DETERMINISTIC_ONLY`。
