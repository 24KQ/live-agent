# Phase 15：Golden Dataset 与发布门禁讨论基线

文档状态：`DISCUSSION_BASELINE`

本文件承接原 `phase-14-golden-release-gates-design.md` 的讨论成果，但不是实施计划，也不授权代码修改。Phase 14 Human-Centered Decision Support Acceptance 完成后，必须重新执行 Just-in-Time Gate，基于实际 Copilot 结论、人工对照样本、预算余额、Workspace 接口和真实模型证据生成新的 Phase 15 Design/Plan。

## 保留方向

- Golden Dataset、不可变 manifest、规则优先 Evaluation Interface、PR/Nightly/Release 三级 CI。
- PostgreSQL/Kafka Release 证据、敏感信息扫描、覆盖率、模型身份/价格/usage 与哈希门禁。
- ToolRegistry Facade 退役、默认路由双 Release 验证和最终 Agent Runtime Acceptance。

## 必须重审的输入

- Phase 14 是否满足零严重违规、90% 正确率、30% 关键漏报降低和 20% 耗时降低。
- `DECISION_SUPPORT` 是否仍保持默认关闭，是否存在可证明的默认路由晋升条件。
- 新增 Workspace、Proposal、OperatorDecision、MemoryConfirmation 和人工对照数据应如何进入 Golden/Release Dataset。
- 真实模型预算：Phase 15 保留 0.60 元，不得借用 Phase 14 的 1.00 元额度。
- Phase 13 未保留 Specialist 与新 Copilot 的生产/评估身份隔离是否仍完整。

## 非目标

- 不改变 Phase 13 的正式去留结论。
- 不以 Golden/CI 替代 Phase 14 的运营工作台和人工协同验收。
- 不在本讨论基线中决定默认路由、真实模型规模、CI 门槛或 ToolRegistry 删除时间。
