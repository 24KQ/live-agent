# Phase 7B 生产级 Agent 运行硬化实施计划

## 完成情况

| 模块 | 状态 | 说明 |
|------|------|------|
| 操作员鉴权 | done | operator_auth.py + Settings 扩展 |
| API 鉴权集成 | done | harness approval + evaluation review 受保护 |
| Harness 锁/TTL/幂等 | done | InMemory + Postgres 双 store 实现 |
| DDL (SQL + 迁移入口) | done | init_phase7b + run_db_migrations.py |
| Evaluation 恢复 + 告警 | done | recover_stale_runs() + alert store |
| 敏感信息扫描 | done | check_sensitive_payloads.py 基础版 |
| 单元测试 | done | 24 个新测试全部通过 |
| 设计文档 | done | specs 文档已创建 |
| 回归验证 | done | 366 个测试全通过 |

## 涉及文件

- docker/init_phase7b_production_hardening.sql（新建）
- src/gateway/operator_auth.py（新建）
- src/config/settings.py（扩展）
- src/gateway/api_server.py（修改）
- scripts/run_db_migrations.py（新建）
- scripts/check_sensitive_payloads.py（新建）
- src/gateway/harness_session_store.py（扩展）
- src/gateway/agent_evaluation_store.py（扩展）
- tests/unit/test_operator_auth.py（新建）
- tests/unit/test_harness_session_lock.py（新建）
- tests/unit/test_agent_evaluation_recovery.py（新建）

## 遗留限制

- 敏感信息扫描脚本的 PATTERNS 列表因 PowerShell 编码问题未能完全写入，需要手动补充
- 告警表集成测试依赖 PostgreSQL，当前只有单元测试
- 未对接真实监控/告警通道（如 PagerDuty/Slack）
