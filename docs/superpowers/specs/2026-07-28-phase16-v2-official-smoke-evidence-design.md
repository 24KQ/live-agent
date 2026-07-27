# Phase 16 V2 正式真实模型证据设计

## 状态

`EXECUTED_FAILED`。本设计是已合并 Phase 16 的有限证据整改，不开启 Phase 17，
不改变生产默认路由，也不改写已经发送的 V1 证据。V2 已真实发送首个 case 的
Analyst/Planner；Analyst 通过，Planner 以 `MODEL_OUTCOME_UNAVAILABLE` 失败，严格
`10/10` 条件未满足且同一 run 不得重试。

## 目标

通过独立的 `phase16-official-smoke-v2` run，验证受控双 Agent 可以在
DeepSeek V4 Pro 上完成十个冻结高冲突 case 的结构化调用。V2 只在严格
`10/10 case`、`20/20 call`、完整 receipt/usage/validation 和总费用不超过
`1.000000 CNY` 时得出 `PASS`。

## 不变量

- V1 run、Manifest、账本、源码闭包、报告及 `FAILED / ANALYST_VALIDATION_FAILED`
  结论不可修改。
- V2 使用独立 Profile、Manifest、账本表、CLI、receipt、报告和执行提交身份。
- Analyst 与 Planner 均只可输出可见证据的非空无重复 `evidence_ids` 子集；系统
  解析并回填完整六条权威 `EvidenceRef`，并从冻结 trigger codes 注入
  `finding_codes`。
- Smoke Profile 固定为 `deepseek-v4-pro`、温度 0、零 Skill、单次调用、60 秒、
  6000 总 token、2800 最大输出 token；它们不能进入 LIVE Registry、Coordinator、
  Store、HTTP 或经营命令路径。
- V2 预算导入既有 `0.073220 + 0.006306 = 0.079526 CNY` 支出，十个 slot 每例
  `.092000 CNY`，最大暴露为 `.999526 CNY`；不得关闭预算门禁。
- 预检未发送为 `BLOCKED + INCONCLUSIVE`；任一已发送异常、非 `stop` finish
  reason、缺 receipt/usage、结构验证失败或费用异常均为 `FAILED` 且立即停止。
- 无论结论如何，生产路由保持 `DETERMINISTIC_ONLY`，不得接入真实淘宝 API、
  自由 A2A、动态 handoff、共享 scratchpad、插件或热加载。

## 实现边界

共享 `BoundedSpecialistRunner` 仍负责 AgentAction、Schema、Resolver、token 和
预算验证。仅新增被 Profile digest 绑定的 V2 system-managed evidence mode；旧
Profile 的默认完整 EvidenceRef 行为和 digest 保持不变。V2 账本只保存脱敏
provider ID 摘要、模型、usage、成本、延迟、验证摘要和 HMAC tag，禁止保存 API key、
Prompt、模型正文、思维链或经营建议。
