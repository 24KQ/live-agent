# Phase 15：Golden Dataset 与发布门禁讨论基线

文档状态：`SUPERSEDED_BY_PHASE_15_DESIGN`

本文件承接原 `phase-14-golden-release-gates-design.md` 的讨论成果，现已由
[Phase 15 Golden Release Gates Design](./phase-15-golden-release-gates-design.md)
和 [Phase 15 Implementation Plan](../plans/2026-07-18-phase-15-golden-release-gates-plan.md)
取代。本文件保留历史讨论输入，不是实施依据，也不授权代码修改。

Phase 15 Stage A 已完成 Design/Plan、决策日志、路线图、worklog 和恢复协议持久化；当前状态为
`PHASE_15_DESIGN_REVIEWED_AWAITING_IMPLEMENTATION_AUTHORIZATION`。只有用户单独授权
Stage B 后，才可按新 Design/Plan 修改业务代码、数据库、CI、真人采集或调用真实模型。

## 保留方向

- Golden Dataset、不可变 manifest、规则优先 Evaluation Interface、PR/Nightly/Release 三级 CI。
- PostgreSQL/Kafka Release 证据、敏感信息扫描、覆盖率、模型身份/价格/usage 与哈希门禁。
- ToolRegistry Facade 退役、默认路由双 Release 验证和最终 Agent Runtime Acceptance。

## 必须重审的输入

- Phase 14 是否满足零严重违规、90% 正确率、30% 关键漏报降低和 20% 耗时降低。
- `DECISION_SUPPORT` 是否仍保持默认关闭，是否存在可证明的默认路由晋升条件。
- 新增 Workspace、Proposal、OperatorDecision、MemoryConfirmation 和人工对照数据应如何进入 Golden/Release Dataset。
- 真实模型预算：Phase 15 固定保留 0.60 元，不得借用 Phase 13 的 2.40 元或 Phase 14 的 1.00 元额度。
- Phase 13 未保留 Specialist 与新 Copilot 的生产/评估身份隔离是否仍完整。

## 新 Design 已冻结的结果

- 技术发布与 Copilot 晋升使用双轨结论：`PASS | FAIL | BLOCKED` 与 `PROMOTE | KEEP_DISABLED | BLOCKED`。
- 活跃 Golden Dataset 固定为 48 例，Phase 13 的 240 例只做历史 Manifest 完整性检查。
- 真人交叉对照必须来自 3-5 名真实参与者、24-40 条记录；缺失时不得伪造 Promotion 证据。
- Technical PASS 必须绑定覆盖率、确定性规则、迁移、敏感扫描和真实 GitHub Actions PR/Release 证据。
- Phase 15 Acceptance 后停止，不自动进入新阶段；生产默认只有在独立 Promotion 通过后才可开启 `DECISION_SUPPORT`。

## 非目标

- 不改变 Phase 13 的正式去留结论。
- 不以 Golden/CI 替代 Phase 14 的运营工作台和人工协同验收。
- 不在本历史讨论基线中修改已经冻结的默认路由、真实模型规模、CI 门槛或 ToolRegistry 删除顺序；这些内容以新 Design/Plan 和 D-123 至 D-132 为准。
