# Agent Runtime 业务闭环回放轨道

文档状态：`ACTIVE_DESIGN_BASELINE`

最后更新：2026-07-15

## 1. 目的与边界

本轨道把既有 Runtime、PlanEngine、事件和评估证据串成一个可重复运行的业务故事，
用于技术验收与面试展示。它不替代各 Phase 的 Design、Implementation Plan 或
Acceptance，也不新增真实淘宝 API、运营 UI 或 GMV 主张。

可证明的结论是：系统能够在受控 Fixture 中可靠执行、恢复、复用并留下审计证据。
不可证明的结论包括真实平台收入、库存收益、转化率或线上运营效果。

## 2. 固定场景

场景 ID 固定为 `live-session-p001-sold-out-v1`：

```text
冻结 LivePlanDraft 与 p001/p002/p003 商品快照
-> 三张单商品手卡并行生成
-> p001 可信售罄事件进入 Event Inbox
-> 持久化、去重、授权后提交 Kafka offset
-> PRODUCT 范围局部冻结 p001 依赖分支
-> handle_sold_out_event@2.0.0 执行一次 CAS 写
-> SIDE_EFFECT_UNKNOWN 仅通过严格只读对账确认
-> 高优先级紧急 child DAG
-> root 计划创建不可变 Replan 版本
-> p002/p003 已成功结果按 lineage 复用
-> 播后 Replay/Evaluation 与条件化 Agent 结论
-> Golden Dataset 与 Release Gate 验证
```

## 3. 证据与产物

Phase 12B Task 11 的无外部依赖 CLI 使用该场景，接收
`--scenario live-session-p001-sold-out-v1 --output-dir <dir>`，输出：

- `business-loop-trace.json`：冻结输入摘要、事件摘要、Inbox/offset 事实、影响范围、
  NodeRun 冻结与 superseded 事实、CAS Attempt、对账结论、child/root lineage、
  Replan 版本和复用节点。
- `business-loop-report.md`：业务结果、控制链路、失败与恢复、证据索引、能力边界。

同一 Fixture 重复运行的规范化 Trace 必须字节一致。报告与 Trace 都只能读取已有
事实，生成产物不得触发第二次写 Operation、自动重试或 Legacy fallback。

## 4. 阶段映射

- **Phase 12A**：提供冻结手卡批次、PlanStore/checkpoint 一致性和可重放节点结果。
- **Phase 12B**：交付本场景的事件、抢占、CAS、严格对账和增量 Replan 主 Trace。
- **Phase 13**：从持久化评估结果生成 `agent-decision-appendix.json` 与 Markdown 摘要；
  三个候选都必须记录 `RETAINED`、`REJECTED` 或 `INCONCLUSIVE`，主 Trace 不依赖
  任一 Agent 被保留。
- **Phase 14**：在既有 24 个 runtime core Golden case 中固定一个该场景变体，总数
  保持 264；Release 报告聚合 Trace、Agent 附录、Manifest、规则门禁和 ReleaseDecision。

## 5. 验收规则

- p001 的售罄分支必须显示可信事件、CAS 与对账证据；p002/p003 必须显示复用或未受影响
  的确定性理由。
- 事件授权、版本冲突、发送后未知和人工等待不能由自然语言摘要掩盖。
- Release 为 `FAIL` 或 `BLOCKED` 时，报告必须如实显示失败原因，不能声明业务闭环成功。
- Agent 附录只陈述真实样本数、严重违规、收益、延迟、Token、费用和证据哈希；
  `INCONCLUSIVE` 不是保留结论。
