# Phase 7B：生产级 Agent 运行硬化设计

## 目标

在 Phase 7A Agent 回放与评估的基础上，补齐五类接近生产运行的能力边界，使 Harness Agent 和 Evaluation 系统可称为
一个完整的安全可审计的 Agent 基础设施。

## 设计决策

### 1. 操作员鉴权
- 轻量本地鉴权，基于请求头 X-Operator-Id / X-Operator-Token
- 三种角色层级：operator < reviewer < admin
- 默认关闭（兼容本地测试），通过 OPERATOR_AUTH_ENABLED 开启
- 不引入 OAuth/JWT，维持简单可靠的 token 配对

### 2. Harness 人审锁
- 行级乐观锁：lock_until + locked_by 字段
- 10 分钟默认审批过期（approval_expires_at）
- lock 默认 60 秒，同一 operator 可续租
- 过期后可配置过期原因（expired_reason）
- 幂等 key 防止同一审批重复执行工具

### 3. Evaluation Worker 恢复
- 租约过期（lease_until < now()）但 retry_count < 3 的 -> 重新 queued
- retry_count >= 3 -> 标记 failed
- 新增运维告警表记录审批过期、重试耗尽、审计写入失败等事件

### 4. 数据库迁移
- 统一 run_db_migrations.py，按依赖顺序执行
- AUTO_INITIALIZE_SCHEMA 控制是否自动 DDL
- 生产模式关闭自动初始，必须显式运行迁移

### 5. 敏感信息巡检
- check_sensitive_payloads.py 扫描 .env/API Key/密码/Token/用户目录
- 白名单机制排除已知测试占位符

## 未做但已记录
- 公网鉴权（OAuth/JWT）
- 租户隔离
- 在线监控系统对接和告警通道
- 部署流水线和 CI/CD
