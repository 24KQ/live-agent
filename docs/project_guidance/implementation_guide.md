# LiveAgent 实现指南

> **版本**: v1.0  
> **更新日期**: 2026-07-07  
> **用途**: 给后续开发者或 AI 编码 Agent 使用，说明 LiveAgent（淘宝主播 Agent 开源复刻版）从空项目开始应该按什么顺序实现。  
> **阅读顺序**: 先读 [PRD](./taobao_anchor_agent_prd.md)，再读 [Design Spec](./taobao_anchor_agent_design_spec.md)，最后按本文档执行。

---

## 1. 开发前准备

### 1.1 启动本地中间件

根据 `D:\Others\DockerData\PORTS.md`，本项目至少需要启动：

| 服务 | 路径 | 端口 |
| :--- | :--- | :--- |
| PostgreSQL | `D:\Others\DockerData\postgres\` | 5432 |
| Redis | `D:\Others\DockerData\redis\` | 6379 |
| Kafka | `D:\Others\DockerData\kafka\` | 9092 |

可选启动：

| 服务 | 路径 | 端口 | 用途 |
| :--- | :--- | :--- | :--- |
| pgAdmin | PostgreSQL compose 内 | 5050 | 查看数据库 |
| Kafka UI | Kafka compose 内 | 9093 | 查看 topic |
| MinIO | `D:\Others\DockerData\minio\` | 8900/8901 | 大文件卸载 |

### 1.2 本地连接信息

| 服务 | 地址 | 账号 | 密码 |
| :--- | :--- | :--- | :--- |
| PostgreSQL | `localhost:5432/postgres` | `${POSTGRES_USER}` | `${POSTGRES_PASSWORD}` |
| Redis | `localhost:6379` | 无 | 无 |
| Kafka | `localhost:9092` | 无 | 无 |
| MinIO API | `http://localhost:8900` | `${MINIO_ACCESS_KEY}` | `${MINIO_SECRET_KEY}` |

注意：公开仓库不保存真实凭据。请把本机真实值写入 `.env`，并确保 `.env` 已被 `.gitignore` 忽略。

---

## 2. 推荐实现顺序

### Step 1: 建项目骨架

创建目录：

```text
src/config
src/core
src/state
src/gateway
src/memory
src/skills
src/audit
tests/unit
tests/integration
scripts
docker
```

先创建 `.env.example`，不要提交真实 `.env`。

### Step 2: 做配置层

优先实现：

- `src/config/settings.py`
- `src/config/tool_registry.py`

目标：

- 能从环境变量读取 PostgreSQL、Redis、Kafka 配置。
- 能注册 Tool 的生命周期、风险等级、Schema、是否需要幂等键。

### Step 3: 做状态模型

优先实现：

- `src/state/models.py`

核心模型：

- `Lifecycle`
- `Product`
- `LiveRoomState`
- `AgentAction`
- `ToolCallRequest`
- `ToolCallResult`

原则：

- 所有模型都用 Pydantic 校验。
- 价格必须非负。
- 库存必须非负。
- trust_score 必须在 0.0 到 1.0 之间。

### Step 4: 做生命周期和 Reducer

优先实现：

- `src/core/lifecycle.py`
- `src/state/reducer.py`

先不要接 LLM。用单元测试直接喂 Action：

```text
SET_LIFECYCLE -> SWITCH_PRODUCT -> SET_PRICE -> MARK_SOLD_OUT
```

目标：

- 非法状态转换被拒绝。
- POST_LIVE 不能改价。
- 售罄后商品下架。

### Step 5: 做 Security Hook

优先实现：

- `src/core/security_hooks.py`
- `src/audit/tool_call_audit.py`
- `src/audit/approval_log.py`

先实现四种分支：

| 分支 | 行为 |
| :--- | :--- |
| auto | 直接放行 |
| soft-gate | 记录提示后放行 |
| hard-gate | 等待确认，拒绝则不执行 |
| block | 直接拒绝 |

### Step 6: 做基础 Skills

优先实现：

- `src/skills/pre_live_skills.py`
- `src/skills/on_live_skills.py`
- `src/skills/post_live_skills.py`

第一批工具：

- `query_products`
- `generate_plan`
- `generate_card`
- `switch_product`
- `change_price`
- `mark_sold_out`
- `summarize_data`

### Step 7: 接入数据库

优先实现：

- `docker/init_postgres.sql`
- `src/state/repositories.py`
- `scripts/seed_demo_data.py`

目标：

- 能创建表。
- 能写入样例商品。
- 能查询商品和状态。
- 能写审计日志。

### Step 8: 接入 LLM / LangGraph

在状态、工具、安全都稳定后，再接 LLM。

优先实现：

- `src/core/context_manager.py`
- `src/core/plan_engine.py`
- `src/app.py`

原则：

- LLM 输入必须是当前 State 快照，不是完整历史工具日志。
- LLM 输出必须是结构化 Action，不允许直接写数据库。
- LLM 幻觉调用未知工具时必须被 block。

### Step 9: 接入 Kafka 事件

优先实现：

- `src/gateway/event_models.py`
- `src/gateway/event_aggregator.py`
- `src/gateway/kafka_consumer.py`
- `scripts/produce_danmaku_events.py`
- `scripts/produce_inventory_events.py`

目标：

- 弹幕按 5 秒窗口聚合。
- 售罄事件优先级最高。
- Kafka 不可用时，CLI 模拟事件仍可跑 demo。

### Step 10: 做记忆与信任

优先实现：

- `src/memory/memory_store.py`
- `src/memory/trust_manager.py`
- `src/memory/belief_revision.py`
- `src/audit/decision_trace.py`

目标：

- 记忆按 L1/L2/L3 分层。
- 播后基于 Decision Trace 更新 trust_score。
- trust_score 影响 Tool Masking。

---

## 3. 每次新增功能的固定流程

1. 先更新 PRD 或在 issue/task 中明确需求。
2. 更新 Tool Registry 或数据模型。
3. 写单元测试，先让测试失败。
4. 实现最小代码。
5. 跑单元测试。
6. 跑相关集成测试。
7. 更新合规检查表。
8. 更新文档。

---

## 4. AI 编码 Agent 执行守则

如果后续让 AI 编码 Agent 继续开发，必须遵守：

- 不要跳过测试。
- 不要直接改数据库状态，必须走 Reducer。
- 不要把工具返回的大 JSON 直接塞进 LLM 历史。
- 不要新增未注册 Tool。
- 不要在 POST_LIVE 阶段开放改价、切品、发券。
- 不要把 `.env` 或真实密钥提交。
- 新增或修改代码时，必须按用户要求添加详细中文注释，并使用 UTF-8。

---

## 5. 推荐第一条开发任务

第一条任务不要做 LLM，不要做 UI。建议从最小可验证 Harness 开始：

```text
实现 Lifecycle + Product + LiveRoomState + Reducer + Tool Registry + Security Hook。
```

验收脚本：

```text
1. 创建 demo room。
2. 写入 3 个商品。
3. 切换到 ON_LIVE。
4. 查询商品。
5. 请求 change_price。
6. hard-gate 等待确认。
7. 确认后 Reducer 更新价格。
8. 审计表出现 tool_call 记录。
9. 切换到 POST_LIVE。
10. 再次请求 change_price，被 block。
```
