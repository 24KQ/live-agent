# Phase 12A DAG PlanEngine Acceptance

- 状态：技术验收通过；按连续实施授权自动进入 Phase 12B
- 验收日期：2026-07-15
- Design：[Phase 12A DAG PlanEngine Design](../specs/phase-12a-dag-plan-engine-design.md)
- Plan：[Phase 12A DAG PlanEngine Implementation Plan](../plans/2026-07-14-phase-12a-dag-plan-engine-plan.md)
- 验收代码基线：Task 1-8 为 `9a8e5a6`，Task 9 与本报告在同一提交冻结

## 1. 验收结论

Phase 12A 已取得可重复的技术验收证据。固定手卡 DAG、受限输入绑定、不可变计划版本、关系型 PlanStore、FailurePolicy、Worker lease/fencing、Command Ledger、checkpoint 对账、播前 Graph 可选路由和五场景 Demo 均已落地。真实 PostgreSQL 与官方 PostgresSaver 集成测试证明了并发 claim、资源锁、过期 Worker 拒绝、PlanStore 领先复用和 checkpoint 领先 fail-closed。

用户已授权 Phase 12A-14 在技术门禁通过后连续实施，因此本报告通过后不再等待单独人工批准，下一步自动进入已冻结的 Phase 12B Implementation Plan。该授权不改变 Phase 12B 的范围和安全边界，也不把售罄抢占、紧急 DAG 或增量 Replan 追溯为 Phase 12A 已交付能力。

## 2. 实际交付

1. 建立版本化固定 `PlanProposalProvider` 和受限类型化 DAG，首期结构固定为 `PREPARE_CARD_BATCH -> 3 x generate_product_card -> COLLECT_CARD_RESULTS`。
2. 建立白名单校验、环检测、显式上游输出绑定、冻结输入快照、输入指纹和确定性 Capability Profile；Proposal 不得控制 Skill 版本、deadline、重试、资源锁或并发上限。
3. 建立 `plan_runs`、`plan_versions`、`plan_nodes`、`plan_node_dependencies`、`node_runs` 和 `plan_commands` 六张关系表及调度索引，DAG、输入输出和失败证据使用 JSONB。
4. 建立内存与 PostgreSQL PlanStore，支持不可变 PlanVersion、稳定 `logical_key`、NodeRun 历史、资源锁、最大并发 4、lease、心跳和 fencing token。
5. 建立集中式 FailurePolicy。Skill 只报告 `FailureFact`，Worker 负责持久化 `RETRY_WAIT`、执行恢复动作并在不可恢复失败后让整批协作式收敛为 `FAILED`。
6. 建立 `APPROVE`、`REJECT`、`RECONCILE`、`RESUME` 四类 Command Ledger；重复 `command_id` 返回首次结果，旧计划版本、错误节点状态和超时命令均 fail-closed。
7. 建立 PlanStore/checkpoint 三类对账入口。PlanStore 领先时复用已成功 NodeRun；checkpoint 领先时持久化 `INTERNAL_INVARIANT` 事故并阻止普通命令。
8. 建立启动冻结的 `LEGACY | PLAN_ENGINE` 手卡路由，默认 `LEGACY`。PlanEngine 只接管播前 Graph 的手卡批次，同次失败不回退 Legacy。
9. 删除 `TRUSTED_COMPAT`。Runtime 建播批准只接受 Graph 人审链产生的 `HUMAN_INTERRUPT`，旧 `confirmed_setup` 只保留给显式 Legacy 路径。
10. 增加五场景无外部依赖 Demo 与 `run_all.py phase12a-demo` 统一入口。

## 3. DAG、节点身份与输入绑定

准备和汇总节点是可审计的确定性控制节点，不新增 Skill、不创建 Phase 11B Attempt。三个手卡节点钉住 Catalog 中 `generate_product_card` 的精确版本，每个版本内使用新的 `node_id`，跨版本身份使用稳定 `logical_key`。

输入只允许冻结计划输入、显式上游输出和 Schema 校验后的 JSON literal。派发前生成不可变输入快照与输入指纹；通用 JSONPath、表达式执行、任意函数调用和 Proposal 自带资源键均被拒绝。Phase 12A 不创建 Replan 版本，未来来源关系保留给 Phase 12B。

## 4. PlanStore、Worker 与失败收敛

- READY 节点通过 `FOR UPDATE SKIP LOCKED` claim；一次最多派发 4 个无冲突节点。
- 资源键由 Capability Profile 确定，同资源写、高风险操作和写操作保持串行。
- 每次 claim 创建独立 NodeRun；终态写入必须匹配当前 fencing token，过期 Worker 的晚到结果被拒绝。
- 可恢复失败持久化为 `RETRY_WAIT` 和绝对 `next_retry_at`，Worker 释放 claim 后等待下一次调度。
- 不可恢复失败停止派发新的普通节点，保留同批已成功手卡和全部 NodeRun，PlanRun 最终收敛为 `FAILED`。
- Skill Attempt 只作为可选关联，不能替代 PlanStore 的调度和计划事实。

## 5. Checkpoint 与对账

PlanStore 是执行事实权威源，Graph checkpoint 只保存 `plan_run_id`、`plan_version` 和手卡批次控制位置。成功 NodeRun 和结果必须先提交 PlanStore，Graph 节点随后返回并由官方 Saver 写 checkpoint。

- PlanStore 领先：从旧 checkpoint 重放时复用已成功 NodeRun，Demo 和 PostgreSQL 集成测试均证明不会再次调用 Skill。
- checkpoint 领先：不从 checkpoint 补造 NodeRun 或业务证据；持久化 reconciliation 原因、结构化失败事实、signature、次数和时间，冻结活动计划并等待人工对账。
- 对账入口：服务启动、后台周期扫描和人工命令执行前复用同一幂等逻辑。
- 事故清除只清除对账门禁，不自动解冻业务计划；普通命令在事故未清除时继续被权威 Store 拒绝。

实现只通过官方 checkpointer 的 `get_tuple()` 读取公开状态，没有读取或修改 PostgresSaver 内部表，也没有尝试跨 PlanStore 与 Saver 连接制造伪原子事务。

## 6. Command Ledger 证据

四类命令均要求唯一 `command_id`、`expected_plan_version` 和 `expected_node_status`。审批 TTL 为 10 分钟，对账 TTL 为 30 分钟，到期后 fail-closed。验收测试覆盖：

- 相同命令重复提交返回首次持久化结果。
- 旧 PlanVersion 和错误节点状态拒绝执行。
- reconciliation 事故存在时普通命令不得绕过冻结。
- 一个手卡节点不可恢复失败后，整批不会被误报为成功，已成功结果仍保留。

这些证据满足 D-071 的命令幂等、旧版本拒绝和批次失败收敛要求。

## 7. 五场景 Demo

Demo 固定输出以下五个隔离场景：

1. `three_cards_parallel`
2. `rate_limited_retry`
3. `unrecoverable_failure`
4. `planstore_ahead_recovery`
5. `duplicate_command`

每个场景重新装配 `InMemoryPlanStore`、固定 Proposal、真实 `PlanWorker`、FailurePolicy、脚本化单次 SkillExecutor 和隔离时钟。直接脚本恰好输出五行 JSON；统一入口保留既有 `[INFO]` 包装。Demo 不连接 PostgreSQL、Kafka、LLM 或真实淘宝 API。

## 8. 测试与静态检查证据

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| Task 9 Demo 专项 | `4 passed in 1.19s` | `0` |
| Phase 12A 单元聚合 11 文件 | `259 passed in 1.73s` | `0` |
| 指定 PostgreSQL/PostgresSaver 集成聚合 5 文件 | `14 passed in 11.73s` | `0` |
| `python -m pytest -q` | `906 passed, 3 deselected, 9 warnings in 71.56s` | `0` |
| `python scripts/run_phase12a_dag_plan_engine_demo.py` | 五场景按固定顺序输出 | `0` |
| `python scripts/run_all.py phase12a-demo` | 统一入口复现五场景 | `0` |
| `python scripts/run_db_migrations.py --dry-run` | 11 个迁移步骤均可发现，Phase 12A 标记 required | `0` |
| `git diff --check` | 无空白错误 | `0` |
| `python scripts/check_doc_encoding.py` | `4 errors/58 warnings`，未通过 | `1` |

全量测试的 9 条 warning 为既有 FastAPI/Starlette TestClient 与 Kafka Serializer/Deserializer 弃用告警；3 个 deselected 为默认配置排除项。指定集成聚合全部实际执行并通过，没有用内存替身替代 PostgreSQL 或官方 Saver 证据。

## 9. 编码扫描与历史问题

全仓编码扫描不能声明通过。4 个 error 均来自 `scripts/check_doc_encoding.py` 自身用于检测 U+FFFD 的样例；58 个 warning 为本阶段目标外的既有 BOM 或混合换行。Task 9 目标代码、测试、Demo、本报告、路线图和 worklog 通过严格 UTF-8 解码、字节往返、无 BOM、无 replacement character、统一 LF 和无尾随空白检查，目标命中为 0。

本阶段没有为了追求全仓扫描绿色而修改历史编码文件。

## 10. Design 与 Plan 偏差

1. Task 6 发现 checkpoint 领先事故若只存在于内存，重启后无法解释冻结原因或阻止普通命令。按 D-076 扩展 `plan_runs` 持久化当前事故聚合，没有新增第七张事故表。
2. 规格自审发现非法 checkpoint 引用最初只抛校验异常，无法留下 fail-closed 事实。新增红灯测试后改为持久化 `INTERNAL_INVARIANT` 并冻结活动计划。
3. Task 7 自审发现候选绑定可引用冻结输入之外的商品。新增红灯测试后在创建 PlanRun 前拒绝，避免持久化不可执行计划。
4. Task 8 按 D-075 删除 `TRUSTED_COMPAT`，没有让普通 `confirmed_setup`、PlanEngine 状态或 arguments 变成审批证据。
5. Task 9 的 `run_all.py` 初次补丁形成混合换行，严格扫描准确命中；提交前机械统一为 UTF-8 无 BOM/LF，并恢复到全仓既有 `4 errors/58 warnings` 基线。

以上偏差均为冻结设计范围内的安全收紧或事实持久化补全，没有实现 Phase 12B 功能，也没有改变公开 Skill Schema、Agent 门槛或模型预算。

## 11. 有效提交

- `c877bbd`：Plan 模型与固定 Proposal。
- `43cba9f`：绑定、Capability Profile 与状态机。
- `ade64b6`：内存 PlanStore 与 Command Ledger。
- `09f403e`：FailurePolicy、Worker、lease 与 fencing。
- `37d6f8a`：PostgreSQL PlanStore、迁移和并发集成证据。
- `6029ad3`：checkpoint 对账与持久化事故事实。
- `7cbf026`：播前 Graph 可选 PlanEngine 路由。
- `9a8e5a6`：删除 `TRUSTED_COMPAT`。
- Task 9：五场景 Demo、本报告和阶段状态，与本报告同提交。

## 12. Phase 12B 进入条件

1. Phase 12A Acceptance 技术门禁已通过。
2. 连续实施授权已经覆盖 Phase 12B，不需要再次等待阶段批准。
3. Phase 12B 必须重新读取其 Design、Implementation Plan、D-077 至 D-085、本报告和实时执行状态。
4. Event Inbox、Kafka offset、可信事件授权、协作式冻结、紧急 child DAG、CAS、严格对账和不可变 Replan 必须按既定 Task 顺序实施。
5. 不得扩大到真实淘宝 API、UI、插件、热加载或 Phase 13 Agent 生产接入。

当前结论：Phase 12A 正式完成，下一主线为 Phase 12B Task 1。
