# Phase 12B Preemption and Incremental Replan Acceptance

- 状态：技术验收通过；等待 Phase 13 Just-in-Time Gate
- 验收日期：2026-07-15
- Design：[Phase 12B Design](../specs/phase-12b-preemption-replan-design.md)
- Plan：[Phase 12B Implementation Plan](../plans/2026-07-14-phase-12b-preemption-replan-plan.md)
- 验收代码基线：Task 1-10 为 `e6f3414`；Task 11 与本报告在同一提交冻结

## 1. 验收结论

Phase 12B 已形成可重复的售罄业务闭环。可信 Kafka 事件先进入 Event Inbox，确定性
Coordinator 完成影响分析、局部冻结、高优先级紧急 child DAG、版本化 CAS、未知副作用
只读对账和 root 增量 Replan；播中 Harness 只消费校验后的 EvidenceRef，不再拥有第二条
售罄写入口。同次失败不回退 Legacy。

本阶段通过 Acceptance 后必须停止自动实施。Phase 13 的既有 Design/Plan 只是讨论基线，
需要读取本报告、预算和真实风险后完成 Just-in-Time 审核，并由用户单独授权。

## 2. 实际交付

1. 建立 SkillPolicyView，并将全部生产治理消费者迁离 ToolRegistry Facade。
2. 建立 Event Inbox、Occurrence、Application 的内存与 PostgreSQL 权威 Store。
3. 建立手动 Kafka offset 入站顺序：校验来源、持久化事件、事务提交、提交 offset。
4. 建立 PRODUCT/ROOM/PLATFORM 影响分析和协作式冻结；晚到结果保留并标记 superseded。
5. 将 `handle_sold_out_event` 升级为 `2.0.0`，使用可信事件授权和 expected_version CAS。
6. 建立 SIDE_EFFECT_UNKNOWN 严格只读对账，禁止创建第二个 Operation 或 Attempt。
7. 建立 priority 100 的固定紧急 child DAG，以及跨 PlanRun 的权威优先级 claim。
8. 建立不可变 PlanVersion Replan、来源事件 lineage、输入指纹和旧成功结果引用复用。
9. 建立 PreemptionCoordinator、启动冻结路由、EvidenceRef 和 Harness/API 正向消费边界。
10. 建立八场景 CLI 和固定业务 Trace/Markdown 报告。

## 3. 数据与授权边界

- PostgreSQL 新增 Event Inbox、Occurrence 和 Application 表，并扩展 PlanRun/PlanVersion
  lineage、priority、ready_at、planning_input、failure_signature 和 input_fingerprint。
- payload 自报 `trusted` 或 `approved` 不产生权限。只有启动冻结 Trust Profile 校验后
  持久化的 provenance 才能构造 EventAuthorizationContext。
- 同 event_id 同摘要追加 DUPLICATE；同 ID 不同摘要保留首次事实并标记 CONFLICT。
- Store 提交失败不得提交 Kafka offset；可靠落库的重复与冲突允许推进 offset。
- 售罄写只经过 SkillExecutor、Attempt Store、可信授权和 CAS；控制节点不直接写平台。

## 4. 调度、冻结与恢复

跨 PlanRun claim 使用 `priority DESC, ready_at ASC, node_id ASC`，并保留
`FOR UPDATE SKIP LOCKED`、资源 advisory lock、lease、heartbeat 和 fencing。紧急 child
固定为验证事件、售罄写、备选推荐、主播提示和汇总五个节点，LLM 不能改写结构、版本、
资源键或并发限制。

PRODUCT 事件只冻结受影响分支；ROOM/PLATFORM 事件冻结整计划。在途 NodeRun 可以闭合，
但受影响结果标记 superseded，不能重试、回收或成为 Replan 复用来源。PlanVersion 提交与
EventApplication 更新使用显式补偿协议，不伪装成跨 Store 原子事务。

## 5. 严格对账与 Replan

当售罄写发送后结果未知时，原 NodeRun 进入 WAITING_RECONCILIATION。只读查询同时闭合
商品身份、库存 0、inactive 和版本递增后，才确认原 Attempt；证据不足继续等待人工，
不得重发写。Coordinator 覆盖 NodeRun、Application、Inbox 三个崩溃窗口的幂等恢复。

Replan 在 root 锁内合并可应用事件，每个新版本使用新 node_id。输入指纹相同、成功且未
superseded 的节点只写 reused_from_node_id，不复制 NodeRun。相同 failure signature 与
input fingerprint 阻止等价循环；每个 root 最多两个新版本，预算耗尽后冻结转人工。

## 6. Harness 与 ToolRegistry

PlanEngine 路由下，Harness 在 reasoning 前读取 APPLIED EvidenceRef，校验建议摘要后直接
形成可审计建议，不调用 Planner，也不执行 `handle_sold_out_event`。Dashboard 和 HTTP
请求层使用同一启动冻结路由。Legacy 只可通过下一次进程装配显式回滚。

`src/` 中除兼容 Facade 自身外，ToolRegistry 生产 import 扫描为 0。Facade 和只针对它的
兼容测试按既定边界保留到 Phase 14，不在本阶段提前删除。

## 7. 业务闭环 Demo

固定场景 `live-session-p001-sold-out-v1` 输出：

- `business-loop-trace.json`：事件、冻结、child、原 Attempt 对账、Replan、复用和
  EvidenceRef 的规范化业务事实。
- `business-loop-report.md`：业务结果、控制链、失败恢复、证据索引和能力边界。

同一 Fixture 两次运行的 Trace 字节一致；内部随机 UUID 不进入规范产物。主场景实际只
执行一次售罄写，结果未知后通过只读对账闭合，p002/p003 在版本 2 中引用复用。默认 CLI
按固定顺序输出八类验收摘要；报告明确不声称真实 GMV、库存收益或转化率。

## 8. 测试与静态证据

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests/unit/test_phase12b_demo.py -q` | `3 passed` |
| PowerShell 展开 `tests/unit/test_phase12b_*.py` | `104 passed` |
| PowerShell 展开 `tests/integration/test_phase12b_*.py` | `19 passed` |
| `python -m pytest -q` | `1057 passed, 3 deselected, 9 warnings in 110.01s` |
| `python scripts/run_phase12b_preemption_demo.py` | 八行固定 JSON，退出码 0 |
| `python scripts/run_all.py phase12b-demo` | 固定 Trace/报告生成成功 |
| `python scripts/run_db_migrations.py --dry-run` | 12 个迁移步骤，Phase 12B required |

3 个 deselected 是默认配置排除项。9 条 warning 为既有 FastAPI/Starlette TestClient 与
Kafka serializer/deserializer 弃用告警，没有 Phase 12B 断言失败。

## 9. 设计偏差与收紧

1. D-097 将 priority 从审计字段提升为跨 PlanRun Store claim 规则，避免调用方排序失效。
2. D-098 将 planning input 与循环签名保存到不可变 PlanVersion，避免新版本静默读取旧输入。
3. Coordinator 增加 room-scoped claim、claim 后 root 唯一性复核和三个崩溃补偿窗口。
4. EvidenceRef 强制 APPLIED、applied_plan_version 和摘要一致，等待/失败不得生成成功证据。
5. Task 11 规范化 Trace 排除随机内部 UUID，但保留业务身份、版本、lineage 和摘要校验结论。

这些变化均是既有安全与恢复语义的收紧，没有引入真实淘宝 API、UI、LLM 规划或 Agent。

## 10. 有效提交

- `d794ff3`：事件契约与 SkillPolicyView。
- `8b1600b`：内存 Event Inbox。
- `25793f2`：PostgreSQL Event Store 与 lineage。
- `0762c2c`：Kafka 可信入站。
- `375b671`：影响分析与协作式冻结。
- `9d4bf97`：售罄 CAS 与严格对账。
- `703f072`：紧急 child DAG 与全局优先级。
- `e98df2a`：增量 Replan 与结果复用。
- `f6a7d1d`：生产消费者迁移到 SkillPolicyView。
- `e6f3414`：PreemptionCoordinator 与 Harness Evidence。
- Task 11：Demo、本报告与阶段 Gate，同本报告提交。

## 11. Phase 13 Gate

当前状态固定为 `AWAITING_PHASE_13_GATE`。下一轮只能重新审核 Phase 13 的业务价值、
确定性基线、数据集、模型预算和三个 Specialist Agent 候选去留门槛；未获得用户授权前，
不得运行真实模型、修改 Phase 13 业务代码或沿用旧计划自动实施。
