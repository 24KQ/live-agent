# LiveAgent 阶段执行记录

> 用途：记录各阶段任务、验收命令、测试反馈、问题修复和下一阶段建议，便于后续迭代优化。本文档只记录脱敏结论，不记录真实密码、Token 或本机私密配置。

## Phase 0: 项目脚手架与本地中间件验证

### 结果

- Python 项目结构已建立。
- `.env.example` 保留公开模板，真实 `.env` 保留在本机并被 Git 忽略。
- PostgreSQL、pgvector、Redis、Kafka 均已在本机验证通过。
- Phase 0 全量测试通过。

### 验收命令

```powershell
pytest -v
python scripts/check_infra.py
```

### 反馈

- worktree 环境需要单独复制本机 `.env`，否则中间件检查会使用公开默认密码并失败。
- PostgreSQL 首次使用 pgvector 前必须执行 `docker/init_postgres.sql`。

## Phase 1: 播前地基层

### 目标

用播前场景验证最小可控闭环：

```text
查询货盘 -> 生成建议 -> 改价 hard-gate -> 人工确认 -> Reducer 更新状态 -> PostgreSQL 审计记录
```

### 计划任务

- 状态模型：生命周期、商品、直播间状态、Action、DecisionTrace。
- 生命周期：合法流转与非法跳转拒绝。
- 工具注册表：播前工具白名单、生命周期、风险等级、Schema、门禁策略。
- 安全 Hook：auto、soft-gate、hard-gate、block。
- Reducer：SET_PRICE、MARK_SOLD_OUT、SWITCH_PRODUCT。
- 审计：工具调用和状态变更写入 PostgreSQL。
- 演示：CLI 跑通播前改价审批闭环。

### 验收命令

```powershell
pytest tests/unit/test_state_models.py -v
pytest tests/unit/test_lifecycle.py -v
pytest tests/unit/test_tool_registry.py -v
pytest tests/unit/test_security_hooks.py -v
pytest tests/unit/test_reducer.py -v
pytest tests/integration/test_tool_call_audit.py -v
pytest tests/integration/test_pre_live_flow.py -v
pytest -v
python scripts/check_infra.py
python scripts/run_phase1_pre_live_demo.py
```

### 执行反馈

- 单元测试覆盖状态模型、生命周期、工具注册、安全 Hook 和 Reducer。
- 集成测试覆盖 PostgreSQL 审计写入和播前改价审批闭环。
- 全量测试结果：`34 passed`。
- 中间件检查结果：PostgreSQL、pgvector、Redis、Kafka 全部通过。
- CLI 演示结果：未确认改价进入 hard-gate 且价格不变；确认后价格从 `99.00` 更新为 `89.90`，并生成审计 `audit_id`。
- 修复记录：并行执行集成测试时，审计表初始化 SQL 可能并发创建同名内部类型；已通过 PostgreSQL advisory lock 串行化 DDL 初始化。

### 最终结论

Phase 1 播前地基层验收通过。系统已经具备播前最小可控闭环，可作为 Phase 2 播前业务能力的地基。

### 下一阶段建议

- Phase 2 再引入样例商品数据持久化、真实货盘查询、排品草案和商品手卡生成。
