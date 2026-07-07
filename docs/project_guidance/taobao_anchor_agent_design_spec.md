# LiveAgent 技术设计规范 (Design Spec)

> **版本**: v3.0  
> **更新日期**: 2026-07-07  
> **文档性质**: 正式架构设计与研发交付指导，描述“怎么做、按什么顺序做、怎么验证”  
> **适用范围**: 本地开发、MVP 实现、AI 编码 Agent 执行参考、技术评审与合规自查  
> **配套文档**: [PRD](./taobao_anchor_agent_prd.md)、[合规检查表](./compliance_checklist.md)、[研发排期交付计划](./delivery_plan.md)、[实现指南](./implementation_guide.md)

---

## 1. 总体设计目标

### 1.1 设计目标

LiveAgent（淘宝主播 Agent 开源复刻版）的目标不是做一个“会聊天的主播助手”，而是做一个被 Harness 工程约束住的直播运营副驾驶。大模型负责推理和生成建议，外层工程负责状态、权限、审计、恢复和安全边界。

核心设计目标如下：

| 目标 | 说明 | 验收方式 |
| :--- | :--- | :--- |
| 可控 | LLM 不直接执行业务写操作，只输出结构化 Action | 所有状态变更都能追溯到 Reducer |
| 可恢复 | 长任务中断后可从 checkpoint 恢复 | 抢占/崩溃恢复测试通过 |
| 可审计 | 建议、审批、工具调用、状态变更均可回放 | 任一业务结果能查到 trace_id |
| 可降级 | LLM 出错、超时或信任分低时自动收缩能力 | Tool Masking 与 fallback 测试通过 |
| 可迭代 | 模块边界清晰，后续可替换模拟网关为真实平台适配器 | Phase 1-4 可分阶段交付 |

### 1.2 架构原则

1. **LLM 只做决策，不做执行**：LLM 输出 Action JSON，由 Reducer 和 Tool Executor 执行确定性操作。
2. **上下文是预算，不是垃圾桶**：每轮推理只注入当前 State、必要记忆和当前任务，不把历史工具 JSON 全量塞进 messages。
3. **安全靠代码，不靠祈祷**：Prompt 只做软约束，真正的风险控制由 Hook、Schema、审批、幂等和审计实现。
4. **默认拒绝，显式放行**：未知工具、未知生命周期、未知风险等级、缺少幂等键的写操作全部 fail-closed。
5. **MVP 先本地模拟，再预留适配层**：第一版只连接本地中间件和模拟直播事件，不接真实淘宝生产 API。

---

## 2. 技术栈与版本基线

### 2.1 核心技术栈

| 层级 | 选型 | 版本建议 | 用途 |
| :--- | :--- | :--- | :--- |
| 编程语言 | Python | 3.11.x | 主语言，原生支持 asyncio |
| Agent 编排 | LangGraph | 0.2.x 系列 | 有状态图编排、checkpoint、conditional edge |
| LLM 抽象 | LangChain Core | 0.3.x 系列 | 模型调用、tool binding、message schema |
| 数据模型 | Pydantic | 2.x | 配置、State、Action、Tool Schema 校验 |
| 关系型数据库 | PostgreSQL | 16.x | 主状态、审计、checkpoint、记忆元数据 |
| 向量检索 | pgvector | 0.7.x+ | L1/L2/L3 记忆语义检索 |
| 缓存 | Redis | 7.x | 幂等键、事件去重、临时状态、分布式锁 |
| 消息队列 | Kafka KRaft | 3.7.x+ | 弹幕、库存、流量和系统事件流 |
| 对象存储 | MinIO | RELEASE 2024+ | 可选，大文件与报告附件卸载 |
| 容器化 | Docker Compose | v2 | 本地中间件一键启动 |
| 测试框架 | pytest | 8.x | 单元测试、集成测试 |

> 版本原则：研发实现时应将版本写入 `requirements.txt` 或 `pyproject.toml`，避免使用 `latest`。确需升级时，先跑完测试矩阵再更新文档。

### 2.2 本地 Docker 中间件连接信息

以下信息用于说明本项目需要哪些本地中间件。公开仓库不记录真实本地密码；开发者应从自己的 Docker Compose 配置或本机 `D:\Others\DockerData\PORTS.md` 查询真实值，并写入本地 `.env`。

| 服务 | 地址 | 端口 | 账号 | 密码 | 本项目用途 | 必需级别 |
| :--- | :--- | :--- | :--- | :--- | :--- | :---: |
| PostgreSQL + pgvector | localhost | 5432 | `${POSTGRES_USER}` | `${POSTGRES_PASSWORD}` | 主库、记忆库、审计库、checkpoint | P0 |
| pgAdmin | localhost | 5050 | `${PGADMIN_EMAIL}` | `${PGADMIN_PASSWORD}` | PostgreSQL 可视化 | 可选 |
| Redis | localhost | 6379 | 无 | 无 | 缓存、幂等键、分布式锁 | P0 |
| RedisInsight | localhost | 5540 | 无 | 无 | Redis 可视化 | 可选 |
| Kafka | localhost | 9092 | 无 | 无 | 事件流、弹幕流、库存事件 | P0 |
| Kafka UI | localhost | 9093 | 无 | 无 | Kafka 可视化 | 可选 |
| MinIO API | localhost | 8900 | `${MINIO_ACCESS_KEY}` | `${MINIO_SECRET_KEY}` | 大文件、报告、长上下文卸载 | P2 |
| MinIO 控制台 | localhost | 8901 | `${MINIO_ACCESS_KEY}` | `${MINIO_SECRET_KEY}` | 对象存储可视化 | P2 |
| Milvus gRPC | localhost | 19530 | 无 | 无 | 可选向量库实验 | P2 |
| Milvus HTTP | localhost | 19091 | 无 | 无 | Milvus 健康检查 | P2 |
| Attu | localhost | 8000 | 无 | 无 | Milvus 可视化 | P2 |
| MySQL | localhost | 3306 | `${MYSQL_USER}` | `${MYSQL_PASSWORD}` | 可选对比实验，不作为主库 | 可选 |

### 2.3 本地环境变量规范

项目应提供 `.env.example`，真实 `.env` 不提交版本控制。建议变量如下：

```env
# PostgreSQL 主库配置：用于状态、审计、记忆和 checkpoint。
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=change_me

# Redis 配置：用于幂等键、短期缓存和分布式锁。
REDIS_HOST=localhost
REDIS_PORT=6379

# Kafka 配置：用于模拟弹幕、库存、流量和内部系统事件。
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_TOPIC_DANMAKU=anchor.danmaku
KAFKA_TOPIC_INVENTORY=anchor.inventory
KAFKA_TOPIC_TRAFFIC=anchor.traffic
KAFKA_TOPIC_COMMAND=anchor.command

# MinIO 可选配置：用于大文件、长报告和上下文卸载。
MINIO_ENDPOINT=http://localhost:8900
MINIO_ACCESS_KEY=change_me
MINIO_SECRET_KEY=change_me
MINIO_BUCKET=live-agent
```

### 2.4 中间件启动与注意事项

| 服务 | 路径 | 启动说明 | 注意事项 |
| :--- | :--- | :--- | :--- |
| PostgreSQL | `D:\Others\DockerData\postgres\` | 执行 `docker compose up -d` | 首次使用 pgvector 前执行 `CREATE EXTENSION IF NOT EXISTS vector;` |
| Redis | `D:\Others\DockerData\redis\` | 执行 `docker compose up -d` | Redis 无密码，仅用于本地开发 |
| Kafka | `D:\Others\DockerData\kafka\` | 执行 `docker compose up -d` | KRaft 模式首次消费者组可能需创建 `__consumer_offsets` |
| MinIO | `D:\Others\DockerData\minio\` | 执行 `docker compose up -d` | API 端口为 `8900`，不要误用 `9000` |
| Milvus | `D:\Others\DockerData\milvus\` | 依赖 MinIO + etcd 后启动 | MVP 不强依赖 |

---

## 3. 总体架构

### 3.1 架构分层

```text
[用户/模拟事件源]
    │
    ▼
[Gateway 事件网关]
    - Kafka consumer
    - 5 秒滑窗聚合
    - 事件优先级分级
    │
    ▼
[Harness Core]
    - LifecycleManager
    - PlanEngine
    - PreemptionController
    - SecurityHooks
    - ContextManager
    │
    ▼
[LLM / LangGraph]
    - 规划 DAG
    - 生成 Action JSON
    - 生成手卡/话术/复盘摘要
    │
    ▼
[Tool Executor + Reducer]
    - Schema 校验
    - 幂等检查
    - 审批检查
    - 确定性状态写入
    │
    ▼
[State / Memory / Audit Stores]
    - PostgreSQL + pgvector
    - Redis
    - Kafka
    - MinIO 可选
```

### 3.2 核心数据流

1. Gateway 从 Kafka 或本地模拟脚本接收事件。
2. EventAggregator 聚合 5 秒窗口内的相似弹幕和重复事件。
3. PriorityQueue 根据事件等级排序。
4. PlanEngine 读取当前 State 快照，生成或调整 DAG。
5. LangGraph 节点调用 LLM 生成结构化 Action。
6. PreToolCall Hook 校验工具、生命周期、风险等级、幂等键和审批状态。
7. Tool Executor 调用 Skill 或模拟业务函数。
8. Reducer 写入 PostgreSQL，更新直播间状态。
9. Decision Trace、Tool Call Audit 和 Approval Log 写入审计表。
10. PostReasoning Hook 校验输出，副屏/终端展示建议。

### 3.3 运行模式

| 模式 | 用途 | 特点 |
| :--- | :--- | :--- |
| CLI Demo 模式 | Phase 1 快速验证 | 主播确认通过终端输入完成 |
| Kafka 模拟模式 | Phase 2/3 集成验证 | 弹幕、库存、流量事件来自 Kafka |
| Web 副屏模式 | Phase 4 演示 | 浏览器显示建议、审批和状态 |
| 平台适配模式 | 后续扩展 | 将模拟 Skill 替换为真实平台 API 适配器 |

---

## 4. 项目目录结构

```text
live_agent/
├── docs/
│   ├── project_guidance/
│   │   ├── taobao_anchor_agent_prd.md
│   │   ├── taobao_anchor_agent_design_spec.md
│   │   ├── compliance_checklist.md
│   │   ├── delivery_plan.md
│   │   └── implementation_guide.md
│   ├── worklog/
│   │   ├── task_plan.md
│   │   ├── findings.md
│   │   └── progress.md
│   └── study/
│       └── ...
├── docker/
│   ├── init_postgres.sql
│   └── README.md
├── scripts/
│   ├── seed_demo_data.py
│   ├── produce_danmaku_events.py
│   ├── produce_inventory_events.py
│   └── run_demo_live.py
├── src/
│   ├── config/
│   │   ├── settings.py
│   │   └── tool_registry.py
│   ├── gateway/
│   │   ├── kafka_consumer.py
│   │   ├── event_aggregator.py
│   │   └── event_models.py
│   ├── core/
│   │   ├── lifecycle.py
│   │   ├── context_manager.py
│   │   ├── plan_engine.py
│   │   ├── preemption.py
│   │   └── security_hooks.py
│   ├── state/
│   │   ├── models.py
│   │   ├── reducer.py
│   │   ├── repositories.py
│   │   └── checkpointer.py
│   ├── memory/
│   │   ├── memory_store.py
│   │   ├── belief_revision.py
│   │   └── trust_manager.py
│   ├── skills/
│   │   ├── pre_live_skills.py
│   │   ├── on_live_skills.py
│   │   └── post_live_skills.py
│   ├── audit/
│   │   ├── decision_trace.py
│   │   ├── tool_call_audit.py
│   │   └── approval_log.py
│   └── app.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── .env.example
├── requirements.txt
└── README.md
```

---

## 5. 数据库设计

### 5.1 设计原则

- PostgreSQL 是 MVP 主库，负责状态、记忆、审计和 checkpoint。
- pgvector 用于记忆语义检索；若后续使用 Milvus，只替换 MemoryStore 实现，不改变上层接口。
- 所有业务写操作必须可追溯到 `trace_id`、`tool_call_id` 或 `approval_id`。
- 所有有副作用操作必须携带 `idempotency_key`。

### 5.2 初始化扩展

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

### 5.3 直播间状态表 `live_room_state`

```sql
CREATE TABLE live_room_state (
    room_id VARCHAR(50) PRIMARY KEY,
    session_id VARCHAR(50) NOT NULL,
    lifecycle VARCHAR(20) NOT NULL DEFAULT 'PRE_LIVE'
        CHECK (lifecycle IN ('PRE_LIVE', 'ON_LIVE', 'POST_LIVE')),
    current_product_id VARCHAR(50),
    viewer_count INT NOT NULL DEFAULT 0 CHECK (viewer_count >= 0),
    trust_score NUMERIC(4,3) NOT NULL DEFAULT 1.000 CHECK (trust_score >= 0 AND trust_score <= 1),
    plan_version INT NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'PAUSED', 'RECOVERING', 'MANUAL_TAKEOVER', 'CLOSED')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 5.4 商品表 `products`

```sql
CREATE TABLE products (
    product_id VARCHAR(50) PRIMARY KEY,
    room_id VARCHAR(50) NOT NULL REFERENCES live_room_state(room_id),
    name VARCHAR(200) NOT NULL,
    price NUMERIC(10,2) NOT NULL CHECK (price >= 0),
    original_price NUMERIC(10,2) NOT NULL CHECK (original_price >= 0),
    stock INT NOT NULL DEFAULT 0 CHECK (stock >= 0),
    is_on_shelf BOOLEAN NOT NULL DEFAULT TRUE,
    category VARCHAR(20) CHECK (category IN ('traffic', 'profit', 'atmosphere')),
    sort_order INT NOT NULL DEFAULT 0,
    conversion_rate NUMERIC(6,4) NOT NULL DEFAULT 0 CHECK (conversion_rate >= 0 AND conversion_rate <= 1),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_products_room_sort ON products(room_id, sort_order);
CREATE INDEX idx_products_room_shelf ON products(room_id, is_on_shelf);
```

### 5.5 记忆表 `memory_entries`

```sql
CREATE TABLE memory_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id VARCHAR(50) NOT NULL,
    anchor_id VARCHAR(50) NOT NULL,
    layer VARCHAR(5) NOT NULL CHECK (layer IN ('L1', 'L2', 'L3')),
    content TEXT NOT NULL,
    embedding vector(1536),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence NUMERIC(4,3) NOT NULL DEFAULT 0.500 CHECK (confidence >= 0 AND confidence <= 1),
    evidence_weight NUMERIC(8,3) NOT NULL DEFAULT 0,
    source VARCHAR(50) NOT NULL CHECK (source IN ('user_stated', 'system_observed', 'offline_summary', 'manual_import')),
    valid_from TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_memory_room_layer ON memory_entries(room_id, layer);
CREATE INDEX idx_memory_metadata ON memory_entries USING gin(metadata);
CREATE INDEX idx_memory_embedding ON memory_entries USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 5.6 决策轨迹表 `decision_trace_log`

```sql
CREATE TABLE decision_trace_log (
    trace_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id VARCHAR(50) NOT NULL,
    session_id VARCHAR(50) NOT NULL,
    lifecycle VARCHAR(20) NOT NULL CHECK (lifecycle IN ('PRE_LIVE', 'ON_LIVE', 'POST_LIVE')),
    decision_point VARCHAR(80) NOT NULL,
    question TEXT,
    agent_output JSONB NOT NULL,
    recommended_action JSONB,
    trust_at_moment NUMERIC(4,3) NOT NULL CHECK (trust_at_moment >= 0 AND trust_at_moment <= 1),
    anchor_action VARCHAR(20) CHECK (anchor_action IN ('ACCEPT', 'REJECT', 'MODIFY', 'NO_RESPONSE')),
    anchor_alternative JSONB,
    lift NUMERIC(10,4),
    trust_delta NUMERIC(6,4),
    result_status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
        CHECK (result_status IN ('PENDING', 'EVALUATED', 'SKIPPED', 'ERROR')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_decision_session ON decision_trace_log(session_id, created_at);
```

### 5.7 工具调用审计表 `tool_call_audit`

```sql
CREATE TABLE tool_call_audit (
    tool_call_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id UUID REFERENCES decision_trace_log(trace_id),
    room_id VARCHAR(50) NOT NULL,
    session_id VARCHAR(50) NOT NULL,
    tool_name VARCHAR(80) NOT NULL,
    lifecycle VARCHAR(20) NOT NULL,
    risk_level VARCHAR(20) NOT NULL CHECK (risk_level IN ('auto', 'soft-gate', 'hard-gate', 'block')),
    idempotency_key VARCHAR(100),
    request_payload JSONB NOT NULL,
    response_payload JSONB,
    status VARCHAR(20) NOT NULL CHECK (status IN ('ALLOW', 'BLOCKED', 'WAITING_APPROVAL', 'EXECUTED', 'FAILED')),
    blocked_reason TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE UNIQUE INDEX uq_tool_idempotency_key ON tool_call_audit(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_tool_call_session ON tool_call_audit(session_id, created_at);
```

### 5.8 审批记录表 `approval_log`

```sql
CREATE TABLE approval_log (
    approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tool_call_id UUID NOT NULL REFERENCES tool_call_audit(tool_call_id),
    room_id VARCHAR(50) NOT NULL,
    session_id VARCHAR(50) NOT NULL,
    approval_type VARCHAR(20) NOT NULL CHECK (approval_type IN ('soft-gate', 'hard-gate')),
    approval_status VARCHAR(20) NOT NULL CHECK (approval_status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')),
    requested_payload JSONB NOT NULL,
    reviewer VARCHAR(80) NOT NULL DEFAULT 'anchor',
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 5.9 Checkpoint 表 `langgraph_checkpoints`

```sql
CREATE TABLE langgraph_checkpoints (
    thread_id VARCHAR(100) NOT NULL,
    checkpoint_id VARCHAR(100) NOT NULL,
    parent_checkpoint_id VARCHAR(100),
    room_id VARCHAR(50) NOT NULL,
    session_id VARCHAR(50) NOT NULL,
    state_payload JSONB NOT NULL,
    plan_payload JSONB,
    is_frozen BOOLEAN NOT NULL DEFAULT FALSE,
    frozen_by_event VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (thread_id, checkpoint_id)
);

CREATE INDEX idx_checkpoints_session ON langgraph_checkpoints(session_id, created_at);
CREATE INDEX idx_checkpoints_frozen ON langgraph_checkpoints(session_id, is_frozen);
```

---

## 6. 生命周期状态机

### 6.1 状态定义

| 状态 | 含义 | 允许业务写操作 | 典型工具 |
| :--- | :--- | :---: | :--- |
| PRE_LIVE | 播前准备 | 仅允许模拟建播类写操作 | 查询货盘、排品、手卡、偏好对账、模拟建播 |
| ON_LIVE | 播中执行 | 允许受控业务写操作 | 切品、改价、发券、弹幕回复、售罄处理 |
| POST_LIVE | 播后复盘 | 禁止业务写操作，允许审计/记忆写入 | 数据归因、决策复盘、信任更新、记忆回写 |

### 6.2 合法转换

```text
PRE_LIVE --start_live--> ON_LIVE --end_live--> POST_LIVE --reset--> PRE_LIVE
```

非法转换必须拒绝，例如 `PRE_LIVE -> POST_LIVE` 需要先经过 `ON_LIVE`，除非系统处于测试模式并显式传入 `force=true`。

### 6.3 工具白名单

```python
TOOL_WHITELIST = {
    "PRE_LIVE": [
        "query_products",
        "generate_plan",
        "generate_card",
        "setup_live",
        "confirm_belief",
    ],
    "ON_LIVE": [
        "query_products",
        "switch_product",
        "change_price",
        "issue_coupon",
        "reply_danmaku",
        "mark_sold_out",
    ],
    "POST_LIVE": [
        "summarize_data",
        "review_decisions",
        "recalculate_trust",
        "write_memory",
    ],
}
```

说明：`POST_LIVE` 禁止改价、切品、发券等业务写操作，但允许审计、信任分和记忆类写入。

---

## 7. Tool Registry 与安全等级

### 7.1 工具注册字段

每个工具必须注册以下字段：

| 字段 | 含义 | 示例 |
| :--- | :--- | :--- |
| name | 工具唯一名 | `change_price` |
| description | 给 LLM 的能力说明 | “修改模拟商品售价，仅播中可用” |
| lifecycle | 允许阶段 | `["ON_LIVE"]` |
| risk_level | 风险等级 | `hard-gate` |
| input_schema | 参数 Schema | Pydantic model / JSON Schema |
| output_schema | 返回 Schema | Pydantic model / JSON Schema |
| idempotent | 是否需要幂等键 | `true` |
| timeout_seconds | 超时时间 | `10` |
| owner | 负责人/模块 | `skills.on_live` |

### 7.2 风险等级

| 等级 | 策略 | 示例 |
| :--- | :--- | :--- |
| auto | 无副作用或只读，自动放行 | 查询商品、生成参考回复 |
| soft-gate | 有副作用但低风险，提示并记录 | 模拟建播、常规切品 |
| hard-gate | 高风险，必须等待主播确认 | 大幅改价、发券、批量操作 |
| block | 红线操作，直接拒绝 | 负数价格、未知工具、越生命周期调用 |

### 7.3 工具清单

| 工具名 | 生命周期 | 风险等级 | 是否需要幂等键 | 功能 |
| :--- | :--- | :--- | :---: | :--- |
| `query_products` | PRE / ON | auto | 否 | 查询货盘商品 |
| `generate_plan` | PRE | auto | 否 | 生成排品方案 |
| `generate_card` | PRE | auto | 否 | 生成商品手卡 |
| `setup_live` | PRE | soft-gate | 是 | 模拟建播 |
| `confirm_belief` | PRE | soft-gate | 是 | 偏好对账确认 |
| `switch_product` | ON | soft-gate | 是 | 切换当前商品 |
| `change_price` | ON | hard-gate | 是 | 修改模拟价格 |
| `issue_coupon` | ON | hard-gate | 是 | 发放模拟优惠券 |
| `reply_danmaku` | ON | auto | 否 | 生成弹幕参考回复 |
| `mark_sold_out` | ON | soft-gate | 是 | 标记售罄并下架 |
| `summarize_data` | POST | auto | 否 | 播后数据归因 |
| `review_decisions` | POST | auto | 否 | 决策复盘 |
| `recalculate_trust` | POST | auto | 是 | 更新信任分 |
| `write_memory` | POST | soft-gate | 是 | 写入长期记忆 |

---

## 8. Reducer 状态管理

### 8.1 Action 格式

```json
{
  "type": "CHANGE_PRICE",
  "room_id": "room_demo_001",
  "session_id": "session_demo_001",
  "trace_id": "uuid",
  "idempotency_key": "uuid",
  "payload": {
    "product_id": "p001",
    "new_price": 99.00,
    "reason": "转化率低于阈值，主播确认降价"
  }
}
```

### 8.2 Reducer 规则

| Action | 允许阶段 | 状态变更 | 安全要求 |
| :--- | :--- | :--- | :--- |
| `SET_PRICE` | ON_LIVE | 更新商品价格 | hard-gate、幂等键、价格非负 |
| `MARK_SOLD_OUT` | ON_LIVE | 库存置 0，下架商品 | soft-gate 或系统售罄事件 |
| `SWITCH_PRODUCT` | ON_LIVE | 更新 current_product_id | 商品必须上架且库存 > 0 |
| `SET_LIFECYCLE` | 任意合法转换 | 更新 lifecycle | 必须符合状态机规则 |
| `WRITE_MEMORY` | POST_LIVE | 写入记忆 | 主观偏好必须确认 |
| `UPDATE_TRUST` | POST_LIVE | 更新 trust_score | 分数钳制 0.0-1.0 |

### 8.3 上下文注入策略

每轮 LLM 推理前只注入以下信息：

```json
{
  "lifecycle": "ON_LIVE",
  "current_product": {
    "product_id": "p003",
    "name": "舒缓面霜",
    "price": 129.00,
    "stock": 23
  },
  "viewer_count": 1280,
  "trust_score": 0.72,
  "recent_events_summary": "过去 5 秒内 18 条弹幕询问敏感肌，p003 库存下降较快",
  "allowed_tools": ["query_products", "switch_product", "change_price", "issue_coupon", "reply_danmaku", "mark_sold_out"]
}
```

---

## 9. Security Hooks

### 9.1 Hook 时机

| Hook | 时机 | 职责 |
| :--- | :--- | :--- |
| PreReasoning | LLM 推理前 | 注入 State、记忆、工具掩码 |
| PreToolCall | 工具执行前 | 生命周期、风险、Schema、幂等、审批校验 |
| PostToolCall | 工具执行后 | 校验返回、写审计、触发 Reducer |
| PostReasoning | LLM 输出后 | 幻觉检测、Schema 校验、敏感内容检查 |
| OnSessionEnd | 会话结束 | 保存 checkpoint、回写摘要 |
| OnLiveEnd | 下播 | 触发播后复盘和写操作锁定 |

### 9.2 PreToolCall 决策流程

```text
tool_name 是否注册？
  否 -> block
  是 -> 是否在当前 lifecycle 白名单？
        否 -> block
        是 -> 参数 Schema 是否通过？
              否 -> 参数异常，允许框架修复一次
              是 -> 是否副作用工具且缺少 idempotency_key？
                    是 -> block
                    否 -> 根据 risk_level 执行 auto / soft-gate / hard-gate / block
```

### 9.3 Dynamic Tool Masking

| trust_score | 工具可见范围 | 输出形态 |
| :--- | :--- | :--- |
| >= 0.7 | 当前生命周期内所有非 block 工具 | 直接给建议、反例和替代方案 |
| 0.4-0.7 | auto + soft-gate 工具 | 给弱建议，强调供参考 |
| < 0.4 | auto 工具 | 只给数据和证据，不给方向性决策 |

若 LLM 幻觉调用已被 Mask 的工具，系统必须：

1. 记录 `tool_call_audit.status = 'BLOCKED'`。
2. 向 LLM 返回“该工具当前不可用，请改用只读分析或等待主播确认”。
3. 不执行任何业务写操作。

---

## 10. PlanEngine 与抢占调度

### 10.1 Plan DAG 节点结构

```json
{
  "node_id": "generate_card_p001",
  "type": "GENERATE_CARD",
  "depends_on": ["generate_plan"],
  "input": {
    "product_id": "p001"
  },
  "status": "PENDING",
  "retry_count": 0,
  "max_retry": 2
}
```

### 10.2 DAG 执行规则

- 无依赖节点可以并行执行。
- 节点成功后写入 checkpoint。
- 节点失败后按错误类型决定重试、跳过或 Replan。
- 已完成节点不得重复执行，除非人工要求重新生成。
- 计划变更必须提升 `plan_version`。

### 10.3 事件优先级

| Level | 事件 | 行为 |
| :---: | :--- | :--- |
| 5 | 商品售罄、平台风控告警 | 立即抢占，冻结低优任务 |
| 4 | 库存低于阈值、价格越界 | 高优先处理 |
| 3 | 流量激增/骤降、转化异常 | 排队优先处理 |
| 2 | 主播临时改计划 | 更新 DAG |
| 1 | 普通弹幕、手卡生成 | 可被中断 |

### 10.4 抢占恢复流程

1. Level 5 事件到达。
2. PreemptionController 取消当前低优任务。
3. 当前节点捕获取消信号，写入 frozen checkpoint。
4. 应急 DAG 处理售罄或风控事件。
5. Reducer 更新商品状态。
6. 系统加载 frozen checkpoint。
7. PlanEngine 判断原 DAG 是否仍有效。
8. 有效则恢复；无效则对受影响节点增量 Replan。

---

## 11. 记忆与信任系统

### 11.1 三层记忆

| 层级 | 来源 | 内容 | 写入规则 |
| :--- | :--- | :--- | :--- |
| L1 | 主播声明 | 偏好、约束、反馈 | 需要主播确认 |
| L2 | 客观事实 | 商品、库存、价格、转化数据 | 来自系统数据 |
| L3 | 行为归纳 | 讲解时长、切品习惯、粉丝画像 | 播后离线归纳 |

### 11.2 记忆对账

当 L1 与 L3 矛盾时，不直接覆盖 L1，而是累计证据：

```text
new_weight = old_weight * decay_factor(delta_time) + anomaly_score
```

当 `evidence_weight >= 5.0` 时，在下一次播前触发偏好对账：

> “你之前说开场用引流款，但最近 3 场实际都用了氛围款且效果更好，是否更新偏好？”

### 11.3 信任分更新

| 事件 | trust_delta |
| :--- | :--- |
| 主播采纳且效果好 | +0.05 |
| 主播采纳但效果差 | -0.10 |
| 主播拒绝且事后证明 Agent 对 | +0.03 |
| 主播拒绝且事后证明主播对 | -0.05 |

最终分数必须钳制在 `[0.0, 1.0]`。

---

## 12. Kafka Topic 设计

| Topic | 方向 | 消息类型 | 示例 |
| :--- | :--- | :--- | :--- |
| `anchor.danmaku` | 输入 | 弹幕消息 | “敏感肌能用吗？” |
| `anchor.inventory` | 输入 | 库存变化 | `{"product_id":"p001","stock":0}` |
| `anchor.traffic` | 输入 | 流量变化 | `{"viewer_count":2000}` |
| `anchor.command` | 输入 | 主播/中控命令 | “跳过后两个商品” |
| `anchor.agent_output` | 输出 | Agent 建议 | 副屏提示 |
| `anchor.audit` | 输出 | 审计事件 | 工具调用、审批结果 |

Kafka 注意事项：

- 首次使用消费者组前，KRaft 模式可能需要创建 `__consumer_offsets`。
- 本地开发可通过 Kafka UI `localhost:9093` 查看 topic 与消息。
- 测试脚本应固定消息 key，例如 `session_id`，便于同一直播会话有序处理。

---

## 13. 错误处理与降级

| 异常 | 检测方式 | 处理策略 |
| :--- | :--- | :--- |
| LLM 超时 | 调用超过 timeout | 返回兜底建议，后台继续处理或重试 |
| 幻觉商品 | 输出商品 ID 不存在 | 拦截输出，要求重新基于 State 生成 |
| 工具参数错误 | Schema 校验失败 | 框架修复一次，仍失败则 block |
| 工具重复调用 | idempotency_key 已存在 | 返回上次结果，不重复执行 |
| 死循环 | 相同工具相似参数连续 3 次 | 中断当前任务，记录审计 |
| DB 不可用 | 连接异常 | 进入只读/人工接管模式 |
| Kafka 不可用 | consumer 断开 | CLI 模拟事件降级 |

---

## 14. 测试策略

### 14.1 测试矩阵

| 类型 | 覆盖内容 | 示例 |
| :--- | :--- | :--- |
| 单元测试 | 纯函数、状态机、Schema、Reducer | `test_reducer_set_price_requires_on_live` |
| 安全测试 | 工具白名单、风险等级、hard-gate、block | `test_post_live_change_price_blocked` |
| 集成测试 | PostgreSQL、Redis、Kafka | `test_inventory_event_marks_sold_out` |
| 恢复测试 | checkpoint、抢占、恢复 | `test_preemption_freeze_and_resume` |
| 记忆测试 | L1/L3 矛盾、trust_score | `test_belief_revision_threshold` |
| 端到端测试 | 播前 -> 播中 -> 播后完整流程 | `test_demo_live_full_cycle` |

### 14.2 最低完成标准

Phase 1 完成前必须通过：

- 生命周期状态机单测。
- Reducer 单测。
- Security Hook 单测。
- Tool Registry 单测。
- PostgreSQL 连接测试。
- Redis 幂等键测试。

Phase 3 完成前必须通过：

- Kafka 事件消费测试。
- 抢占恢复测试。
- PlanEngine DAG 执行测试。

Phase 4 完成前必须通过：

- 端到端演示脚本。
- 合规检查表自检。
- 播后报告生成。

---

## 15. 开发分期计划

### Phase 1: 地基层

目标：建立最小 Harness 骨架。

交付：

- `settings.py`
- `models.py`
- `tool_registry.py`
- `lifecycle.py`
- `reducer.py`
- `security_hooks.py`
- `query_products`、`change_price`、`switch_product`
- 基础 SQL 初始化脚本

验收：跑通“查询商品 -> 建议改价 -> hard-gate -> Reducer -> 审计”。

### Phase 2: 记忆与信任

目标：让 Agent 跨会话有记忆，并能根据反馈调整能力。

交付：

- `memory_store.py`
- `trust_manager.py`
- `belief_revision.py`
- `decision_trace.py`
- `write_memory`

验收：播后更新 trust_score，下一场根据分数调整工具掩码。

### Phase 3: 事件与调度

目标：实现 Kafka 事件驱动、DAG 规划、抢占和恢复。

交付：

- `kafka_consumer.py`
- `event_aggregator.py`
- `plan_engine.py`
- `preemption.py`
- 模拟事件脚本

验收：生成手卡过程中被售罄事件抢占，处理完后恢复。

### Phase 4: 端到端演示

目标：形成可展示的完整直播闭环。

交付：

- CLI 或 Web 副屏
- 端到端 demo 脚本
- 播后报告
- 合规检查结果

验收：完整演示播前、播中、播后流程。

---

## 16. 合规与安全落地要求

开发时必须遵守：

- 不接入真实淘宝生产 API。
- 不写入真实资金、订单、用户个人信息。
- 示例数据必须脱敏。
- 本地账号密码只存在于开发者自己的 `.env` 或 Docker 配置中，不进入公开仓库。
- 新增 Tool 前必须更新 Tool Registry 和 [合规检查表](./compliance_checklist.md)。
- 高危工具必须具备 Schema 校验、幂等键、审批记录和审计记录。
- 对外输出建议必须经过事实校验，不能编造商品信息。

---

## 17. 后续扩展方向

| 扩展 | 前提 |
| :--- | :--- |
| 真实平台适配器 | 完成模拟 Skill 与审计链路，获得 API 授权 |
| Web 副屏 | Phase 1-3 稳定后开发 |
| Milvus 替代 pgvector | MemoryStore 接口稳定后实验 |
| MinIO 上下文卸载 | 长手卡、报告附件和大数据文件出现后启用 |
| Planner 微调 | 积累足够标注 DAG 与执行数据后再考虑 |
