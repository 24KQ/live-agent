# Phase 16 V5 受控 E2E 真实模型证据设计

## 状态

`IMPLEMENTATION_AUTHORIZED`。本设计是 Phase 16 的独立 V5 真实模型证据收口，不修改 V1 至 V4
已经发送的事实、账本、Manifest、源码闭包或结论。

## 目标

在 DeepSeek V4 Pro 上验证受控 `EvidenceAnalystAgent -> DecisionPlannerAgent` 的真实调用路径。
正式 `PASS` 仅在独立 V5 campaign 的校准成功后，十个冻结高冲突 case 都完成双阶段调用、完整
回执、结构校验和冻结语义校验时产生。

## 冻结边界

- 模型固定为 `deepseek-v4-pro`，endpoint 固定为 `api.deepseek.com`，温度为 `0`，不允许 fallback。
- V5 专用 Adapter 在不改动共享 Adapter 的前提下，保留 `response_format=json_object`，并明确发送
  `thinking={"type":"disabled"}`。它不保存或回传 `reasoning_content`。
- V5 Profile 为零 Skill、单模型调用、60 秒 deadline、6000 总 token、2800 最大输出 token。Prompt 使用
  V2 的 system-managed Evidence ID 模式，并加入不含真实 ID、标签、案例或经营建议的 FINAL 信封骨架。
- 模型只输出受控 `evidence_ids`、约束、风险、解释和 Planner 候选；系统注入 finding、完整
  EvidenceRef 和其他确定性谱系事实。所有模型输出仍经共享 `BoundedSpecialistRunner` 的 AgentAction、
  JSON Schema、Resolver、预算和领域验证。
- V5 不进入 LIVE Registry、Coordinator、Store、HTTP、WebSocket、OperatorDecision 或经营命令路径。
  默认路由始终为 `DETERMINISTIC_ONLY`。

## 预算与证据

V5 建立一个新的 append-only campaign，总预算为 `1.000000 CNY`，覆盖一个独立合成校准 case 与十个
正式 case。每个阶段最大预约为 `0.030000 CNY`；发送前按冻结价格和实际 request 估算，超额则在网络前
阻断。V1 至 V4 的历史费用永久保留并在报告中披露，但不从 V5 campaign 的独立预算中扣除。

账本只保存受限的 request/receipt/outcome 摘要、HMAC、模型、finish reason、usage、量化延迟、成本和
验证结论；禁止保存 API Key、Prompt、模型正文、思维链、原始 provider ID 或经营建议。

## 执行状态机

1. 离线预检、Manifest 和 PostgreSQL 契约全部通过后，才允许校准 case 发送 Analyst 和 Planner。
2. 校准必须 `2/2 PASS`，正式 run 才可创建十个 case slot。
3. 正式 run 中每例 Analyst 通过后才可发送 Planner；每个阶段仅一次调用。
4. 未发送阻断为 `BLOCKED + INCONCLUSIVE`；任一已发送失败、非 `stop`、缺回执或 usage、结构/证据/
   语义/预算失败均为 `FAILED`，立即停止且不重试、不修补文本。
5. 只有 `10/10` case、`20/20` 调用和全部安全语义通过时，V5 结论为
   `PASS: CONTROLLED_E2E_QUALIFIED`。该结论不等同生产上线，仍等待独立的生产化阶段。
